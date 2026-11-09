"""The UI-independent agent loop.

The agent owns conversation history, drives the LLM, executes requested tools,
and yields events that any front end can render.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from .context import (
    COMPACTION_SUMMARY_TARGET_TOKENS,
    CONTEXT_ARCHIVE_SUBDIR,
    CONTEXT_WARNING_RATIO,
    DEFAULT_CONTEXT_WINDOW,
    CompactResult,
    ContextArchiveError,
    ContextUsage,
    archive_note,
    build_compaction_messages,
    estimate_context_tokens,
    format_messages,
    save_context_archive,
)
from .errors import ContextCompactionError, LLMError
from .events import (
    AgentEvent,
    AssistantText,
    ContextUsageUpdated,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
)
from .llm import LLMClient, LLMTextDelta, LLMTurnComplete, TokenUsage
from .project_context import has_agents_message, insert_agents_message
from .tools.registry import ToolRegistry
from .types import ChatMessage, ToolCall


class Agent:
    """Stateful conversation agent with tool calling support."""

    def __init__(
        self,
        client: LLMClient,
        tools: ToolRegistry,
        max_tool_rounds: int = 10,
        system_prompt: str = "",
        *,
        startup_notes: Sequence[str] = (),
        closers: Sequence[Callable[[], None]] = (),
        context_window: int | None = None,
        context_archive_dir: str | Path | None = None,
        context_warning_ratio: float = CONTEXT_WARNING_RATIO,
        compaction_target_tokens: int = COMPACTION_SUMMARY_TARGET_TOKENS,
    ) -> None:
        self._client = client
        self._tools = tools
        self._max_tool_rounds = max(1, max_tool_rounds)
        self._system_prompt = system_prompt.strip()
        self._messages: list[ChatMessage] = self._initial_messages()
        self._startup_notes = tuple(note for note in startup_notes if note)
        self._closers = list(closers)

        valid_window = context_window if context_window and context_window > 0 else None
        self._context_window = valid_window or DEFAULT_CONTEXT_WINDOW
        self._context_window_is_default = valid_window is None
        self._context_warning_ratio = (
            context_warning_ratio
            if 0 < context_warning_ratio <= 1
            else CONTEXT_WARNING_RATIO
        )
        self._compaction_target_tokens = max(1, compaction_target_tokens)
        self._context_archive_dir = (
            Path(context_archive_dir).expanduser()
            if context_archive_dir is not None
            else Path.cwd() / CONTEXT_ARCHIVE_SUBDIR
        )
        self._context_usage = self._estimate_context_usage()
        self._needs_compaction = False
        self._compacting = False

    def _initial_messages(self) -> list[ChatMessage]:
        if not self._system_prompt:
            return []
        return [{"role": "system", "content": self._system_prompt}]

    @property
    def messages(self) -> list[ChatMessage]:
        """Return a shallow copy of the conversation history."""

        return list(self._messages)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self._tools.names()

    @property
    def registry(self) -> ToolRegistry:
        """The mutable registry used for late-bound MCP tools."""

        return self._tools

    @property
    def startup_notes(self) -> tuple[str, ...]:
        """Non-fatal problems found while assembling the agent."""

        return self._startup_notes

    @property
    def context_usage(self) -> ContextUsage:
        """Latest context-window usage, including messages and tool schemas."""

        return self._context_usage

    @property
    def context_window(self) -> int:
        return self._context_window

    @property
    def needs_compaction(self) -> bool:
        """Whether normal user prompts are paused until ``compact()`` succeeds."""

        return self._needs_compaction

    @property
    def is_compacting(self) -> bool:
        return self._compacting

    @property
    def context_archive_dir(self) -> Path:
        return self._context_archive_dir

    @property
    def context_warning_ratio(self) -> float:
        return self._context_warning_ratio

    @property
    def compaction_target_tokens(self) -> int:
        return self._compaction_target_tokens

    def _estimate_context_usage(self) -> ContextUsage:
        return ContextUsage(
            used_tokens=estimate_context_tokens(
                self._messages, self._tools.schemas()
            ),
            window_tokens=self._context_window,
            is_default_window=self._context_window_is_default,
            estimated=True,
            warning_ratio=self._context_warning_ratio,
        )

    def _usage_from_api(self, usage: TokenUsage | None) -> ContextUsage:
        if usage is not None and usage.total_tokens > 0:
            return ContextUsage(
                used_tokens=usage.total_tokens,
                window_tokens=self._context_window,
                is_default_window=self._context_window_is_default,
                estimated=False,
                warning_ratio=self._context_warning_ratio,
            )
        return self._estimate_context_usage()

    def _set_context_usage(self, usage: ContextUsage) -> ContextUsageUpdated:
        self._context_usage = usage
        self._needs_compaction = usage.needs_compaction
        return ContextUsageUpdated(
            used_tokens=usage.used_tokens,
            window_tokens=usage.window_tokens,
            percent=usage.percent,
            needs_compaction=usage.needs_compaction,
            is_default_window=usage.is_default_window,
            estimated=usage.estimated,
            warning_ratio=usage.warning_ratio,
        )

    def refresh_context_usage(self) -> ContextUsage:
        """Recompute usage after tools or project context are added."""

        self._context_usage = self._estimate_context_usage()
        self._needs_compaction = self._context_usage.needs_compaction
        return self._context_usage

    def replace_messages(self, messages: Sequence[ChatMessage]) -> None:
        """Replace the live history, for example when resuming a session."""

        self._messages[:] = [
            copy.deepcopy(message)
            for message in messages
            if isinstance(message, dict) and message.get("role")
        ]
        self._needs_compaction = False
        self.refresh_context_usage()

    def inject_project_context(self, message: ChatMessage) -> bool:
        """Add AGENTS.md once, after the leading system prompts."""

        if has_agents_message(self._messages):
            return False
        self._messages[:] = insert_agents_message(self._messages, message)
        self.refresh_context_usage()
        return True

    def has_project_context(self) -> bool:
        return has_agents_message(self._messages)

    def add_closer(self, closer: Callable[[], None]) -> None:
        self._closers.append(closer)

    def add_startup_notes(self, notes: Sequence[str]) -> None:
        if not notes:
            return
        self._startup_notes = (
            *self._startup_notes,
            *(note for note in notes if note),
        )

    def close(self) -> None:
        """Release tools/resources such as MCP sessions and subprocesses."""

        closers, self._closers = self._closers, []
        for closer in reversed(closers):
            try:
                closer()
            except Exception:
                pass
        self._tools.close()

    def compact(self) -> CompactResult:
        """Archive the conversation and replace it with an LLM summary.

        Only one LLM request is made and no Function Calling tools are passed.
        The archive is fully written before the summary request starts, and the
        live history is replaced only after a non-empty summary is returned.
        """

        if self._compacting:
            raise ContextCompactionError("上下文压缩已经在进行中。")

        self._compacting = True
        try:
            original = copy.deepcopy(self._messages)
            if not any(
                message.get("role") in {"user", "assistant", "tool"}
                for message in original
            ):
                raise ContextCompactionError("当前没有可压缩的对话内容。")

            before_text = format_messages(original)
            try:
                archive_id, archive_path, completed_at = save_context_archive(
                    self._context_archive_dir, original
                )
            except ContextArchiveError as error:
                raise ContextCompactionError(
                    f"保存原始上下文失败：{error}"
                ) from error

            request_messages = build_compaction_messages(
                original, self._compaction_target_tokens
            )
            content_parts: list[str] = []
            final_content = ""
            try:
                for event in self._client.stream(request_messages, []):
                    if isinstance(event, LLMTextDelta):
                        content_parts.append(event.text)
                    elif isinstance(event, LLMTurnComplete):
                        final_content = event.content
                        # Deliberately ignore any tool_calls returned by the
                        # model: compression is not allowed to execute tools.
            except Exception as error:
                raise ContextCompactionError(
                    f"生成上下文摘要失败：{error}"
                ) from error

            summary = "".join(content_parts).strip() or final_content.strip()
            if not summary:
                raise ContextCompactionError("LLM 没有返回有效的上下文摘要。")

            note = archive_note(archive_id, completed_at)
            after_content = (
                "【历史上下文压缩摘要】\n\n"
                f"{summary}\n\n"
                f"{note}"
            )
            system_messages: list[ChatMessage] = []
            for message in original:
                if message.get("role") != "system":
                    break
                system_messages.append(copy.deepcopy(message))
            self._messages[:] = system_messages + [
                {"role": "assistant", "content": after_content}
            ]
            self._needs_compaction = False
            self._context_usage = self._estimate_context_usage()

            return CompactResult(
                archive_id=archive_id,
                archive_path=archive_path,
                summary=summary,
                before_text=before_text,
                after_text=after_content,
                usage=self._context_usage,
            )
        finally:
            self._compacting = False

    def reset(self) -> None:
        """Clear the conversation history, keeping the system prompt."""

        self._messages[:] = self._initial_messages()
        self._needs_compaction = False
        self._context_usage = self._estimate_context_usage()

    def run_turn(self, user_input: str) -> Iterator[AgentEvent]:
        """Run one user turn and yield UI-agnostic events."""

        if self._compacting:
            yield TurnFailed("正在压缩上下文，暂时不能执行其他任务。")
            return
        if self._needs_compaction:
            ratio = self._context_warning_ratio * 100
            yield TurnFailed(
                f"上下文使用量已达到模型窗口的 {ratio:.0f}%，"
                "普通提示词已暂停。"
                "请先执行 /compact 压缩上下文。"
            )
            return

        self._messages.append({"role": "user", "content": user_input})
        nudged = False

        for _ in range(self._max_tool_rounds):
            turn_usage: TokenUsage | None = None
            try:
                content_parts: list[str] = []
                final_content = ""
                tool_calls: list[ToolCall] = []
                for event in self._client.stream(
                    self._messages, self._tools.schemas()
                ):
                    if isinstance(event, LLMTextDelta):
                        content_parts.append(event.text)
                        yield AssistantText(event.text)
                    elif isinstance(event, LLMTurnComplete):
                        final_content = event.content
                        tool_calls = event.tool_calls
                        turn_usage = event.usage
                content = "".join(content_parts) or final_content
            except LLMError as error:
                yield TurnFailed(str(error))
                return

            assistant_message: ChatMessage = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_message["content"] = content or None
                assistant_message["tool_calls"] = [
                    tool_call.to_payload() for tool_call in tool_calls
                ]
            self._messages.append(assistant_message)
            yield self._set_context_usage(self._usage_from_api(turn_usage))

            if not tool_calls and not content.strip():
                if not nudged:
                    nudged = True
                    self._messages.append(
                        {
                            "role": "system",
                            "content": (
                                "你还没有回答用户。请基于已有的工具结果，"
                                "用中文直接给出最终回答，不要只输出推理或空内容。"
                            ),
                        }
                    )
                    continue
                yield TurnFailed("模型返回了空回复，请重试或检查模型配置。")
                return

            if not tool_calls:
                yield TurnCompleted(content)
                return

            for tool_call in tool_calls:
                yield ToolCallStarted(tool_call.name, tool_call.arguments)
                result = self._tools.execute(tool_call)
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result.content,
                    }
                )
                yield ToolCallFinished(result)

            # Tool results are appended after the provider's usage report, so
            # refresh with an estimate before the next model request.
            yield self._set_context_usage(self._estimate_context_usage())

        yield TurnFailed(
            f"工具调用超过 {self._max_tool_rounds} 轮，已停止本轮。"
        )
