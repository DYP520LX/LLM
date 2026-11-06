"""The UI-independent agent loop.

The agent owns conversation history, drives the LLM, executes requested tools,
and yields events that any front end can render.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

from .errors import LLMError
from .events import (
    AgentEvent,
    AssistantText,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
)
from .llm import LLMClient, LLMTextDelta, LLMTurnComplete
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
    ) -> None:
        self._client = client
        self._tools = tools
        self._max_tool_rounds = max(1, max_tool_rounds)
        self._system_prompt = system_prompt.strip()
        self._messages: list[ChatMessage] = self._initial_messages()
        self._startup_notes = tuple(note for note in startup_notes if note)
        self._closers = tuple(closers)

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
    def startup_notes(self) -> tuple[str, ...]:
        """Non-fatal problems found while assembling the agent."""

        return self._startup_notes

    def close(self) -> None:
        """Release tools/resources such as MCP sessions and subprocesses."""

        closers, self._closers = self._closers, ()
        for closer in reversed(closers):
            try:
                closer()
            except Exception:
                pass
        self._tools.close()

    def reset(self) -> None:
        """Clear the conversation history, keeping the system prompt."""

        self._messages[:] = self._initial_messages()

    def run_turn(self, user_input: str) -> Iterator[AgentEvent]:
        """Run one user turn and yield UI-agnostic events."""

        self._messages.append({"role": "user", "content": user_input})
        nudged = False

        for _ in range(self._max_tool_rounds):
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

        yield TurnFailed(
            f"工具调用超过 {self._max_tool_rounds} 轮，已停止本轮。"
        )
