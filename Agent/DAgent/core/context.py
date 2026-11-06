"""Context accounting, compression archives and full-text recall.

The agent keeps the live conversation in memory.  Before a compression the
complete message list is written to ``<project>/.agents/context/<id>.jsonl``;
the JSONL file is the source of truth for :func:`recall_context`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .types import ChatMessage

DEFAULT_CONTEXT_WINDOW = 128_000
CONTEXT_WARNING_RATIO = 0.60
CONTEXT_ARCHIVE_SUBDIR = Path(".agents") / "context"
COMPACTION_SUMMARY_TARGET_TOKENS = 5_000

_ARCHIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def build_compaction_prompt(
    target_tokens: int = COMPACTION_SUMMARY_TARGET_TOKENS,
) -> str:
    """Render the compression prompt with the configured target size."""

    return f"""你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制，需要将历史对话压缩为一份高质量的工作摘要，以便 Agent 在新的上下文中继续执行任务。

请认真阅读提供的历史上下文，提取对后续工作仍然有价值的信息，并生成一份约 {target_tokens:,} Tokens 的 Markdown 摘要。

摘要应重点保留以下内容：

1. **用户目标与要求**：用户希望完成什么任务？有哪些必须遵守的约束、偏好和验收条件？
2. **已经完成的工作**：哪些任务已经完成？实现了哪些功能？获得了哪些已经验证的结果？
3. **当前工作状态**：Agent 正在处理什么任务？当前进展到哪一步？是否存在尚未解决的问题？
4. **关键技术信息**：保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。
5. **关键决策与原因**：记录用户和 Agent 已经确定的技术方案，以及重要的设计取舍。
6. **下一步任务**：明确接下来应该执行的操作，以及需要继续验证的事项。

请遵守以下要求：

- 优先保留对后续任务有直接影响的信息，删除寒暄、重复讨论、过时的尝试和无关内容。
- 对已经验证的事实与尚未验证的推测作出明确区分。
- 不要将计划完成的任务描述为已经完成。
- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。
- 对失败的尝试，只保留有助于避免重复错误的信息。
- 如果信息存在冲突，应优先保留用户最新确认的要求，并简要说明仍然存在的冲突。
- 如果某些历史细节无法在摘要中完整保留，可简要说明它们已保存到原始上下文归档中，后续可以通过 `recallContext` 工具重新检索。
- 使用清晰的 Markdown 结构输出摘要，不要附加解释或无关内容。

**最终目标：使一个从未阅读过原始对话的 Agent，仅凭这份摘要，就能够理解当前任务、继承关键决策，并继续开展工作。**"""


COMPACTION_PROMPT = build_compaction_prompt()


class ContextArchiveError(Exception):
    """Raised when the original context cannot be persisted safely."""


@dataclass(frozen=True)
class ContextUsage:
    """Token usage for the context that will be sent on the next request."""

    used_tokens: int
    window_tokens: int = DEFAULT_CONTEXT_WINDOW
    is_default_window: bool = False
    estimated: bool = False
    warning_ratio: float = CONTEXT_WARNING_RATIO

    @property
    def percent(self) -> float:
        if self.window_tokens <= 0:
            return 0.0
        return self.used_tokens / self.window_tokens * 100.0

    @property
    def needs_compaction(self) -> bool:
        return self.percent >= self.warning_ratio * 100.0


@dataclass(frozen=True)
class CompactResult:
    """Information returned after a successful manual compression."""

    archive_id: str
    archive_path: Path
    summary: str
    before_text: str
    after_text: str
    usage: ContextUsage


def estimate_tokens(text: str) -> int:
    """Estimate tokens without a provider tokenizer.

    Chinese characters are treated as roughly one token each, while ASCII
    text is treated as roughly four characters per token.  The estimate is
    intentionally conservative and only used when the API omits usage.
    """

    if not text:
        return 0
    ascii_count = sum(1 for character in text if ord(character) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, non_ascii_count + (ascii_count + 3) // 4)


def estimate_context_tokens(
    messages: Sequence[ChatMessage],
    tool_schemas: Iterable[dict[str, Any]] = (),
) -> int:
    """Estimate the complete request context, including tool definitions."""

    message_json = json.dumps(
        list(messages), ensure_ascii=False, separators=(",", ":"), default=str
    )
    tool_json = json.dumps(
        list(tool_schemas), ensure_ascii=False, separators=(",", ":"), default=str
    )
    # A small per-message allowance covers role markers and protocol overhead.
    return (
        estimate_tokens(message_json)
        + estimate_tokens(tool_json)
        + len(messages) * 4
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def message_text(message: ChatMessage) -> str:
    """Return all searchable/displayable parts of one message."""

    parts = [f"role: {message.get('role', 'unknown')}"]
    if message.get("tool_call_id"):
        parts.append(f"tool_call_id: {message['tool_call_id']}")
    if message.get("content") is not None:
        parts.append("content:\n" + _text(message.get("content")))
    if message.get("tool_calls"):
        parts.append(
            "tool_calls:\n"
            + json.dumps(
                message["tool_calls"], ensure_ascii=False, indent=2, default=str
            )
        )
    return "\n".join(parts)


def format_messages(messages: Sequence[ChatMessage]) -> str:
    """Format messages for the compression prompt and the preview panel."""

    blocks: list[str] = []
    for index, message in enumerate(messages, 1):
        role = str(message.get("role", "unknown"))
        header = f"### 消息 {index} · {role}"
        body = message_text(message)
        blocks.append(f"{header}\n\n{body}")
    return "\n\n".join(blocks)


def build_compaction_messages(
    messages: Sequence[ChatMessage],
    target_tokens: int = COMPACTION_SUMMARY_TARGET_TOKENS,
) -> list[ChatMessage]:
    """Build the only LLM request allowed during compression."""

    history = format_messages(messages)
    return [
        {"role": "system", "content": build_compaction_prompt(target_tokens)},
        {
            "role": "user",
            "content": (
                "以下是需要压缩的历史上下文。请严格按照系统要求，"
                "输出一份可直接用于后续工作的 Markdown 摘要。\n\n"
                + history
            ),
        },
    ]


def archive_note(archive_id: str, completed_at: datetime) -> str:
    return (
        f"> 此前上下文已于 {completed_at:%Y-%m-%d %H:%M:%S} 完成压缩，"
        f"原始上下文保存在编号为 {archive_id} 的归档文件中。"
        "如需查找历史信息，请使用 recallContext 工具。"
    )


def _unique_archive_id(context_dir: Path, now: datetime) -> str:
    base = now.strftime("%Y%m%d-%H%M%S")
    archive_id = base
    counter = 1
    while (context_dir / f"{archive_id}.jsonl").exists():
        archive_id = f"{base}-{counter:02d}"
        counter += 1
    return archive_id


def save_context_archive(
    context_dir: str | Path,
    messages: Sequence[ChatMessage],
    *,
    now: datetime | None = None,
) -> tuple[str, Path, datetime]:
    """Persist the complete message list before compression.

    The file is written atomically: a temporary JSONL is flushed and synced,
    then renamed to the final archive name.  The caller only proceeds with
    compression after this function returns successfully.
    """

    if not messages:
        raise ContextArchiveError("当前没有可归档的上下文消息。")

    directory = Path(context_dir).expanduser()
    completed_at = now or datetime.now()
    temporary: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        archive_id = _unique_archive_id(directory, completed_at)
        path = directory / f"{archive_id}.jsonl"
        temporary = directory / f".{archive_id}.{os.getpid()}.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for message in messages:
                handle.write(
                    json.dumps(
                        dict(message),
                        ensure_ascii=False,
                        default=str,
                    )
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if not path.is_file() or path.stat().st_size == 0:
            raise OSError("归档文件写入后为空")
    except OSError as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ContextArchiveError(f"无法保存上下文归档：{error}") from error

    return archive_id, path, completed_at


def _normalise_archive_id(value: str) -> str:
    archive_id = str(value or "").strip()
    if archive_id.endswith(".jsonl"):
        archive_id = archive_id[: -len(".jsonl")]
    if (
        not archive_id
        or archive_id in {".", ".."}
        or not _ARCHIVE_ID_PATTERN.fullmatch(archive_id)
    ):
        raise ValueError("归档编号只能包含字母、数字、点、下划线和连字符")
    return archive_id


def _iter_archive(path: Path) -> Iterable[tuple[int, ChatMessage]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                yield line_number, message  # type: ignore[misc]


def _compact_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def recall_context(
    context_dir: str | Path,
    text: str,
    archive_id: str | None = None,
    *,
    max_chars: int = 6_000,
    max_matches: int = 10,
) -> str:
    """Search archived JSONL messages using local case-insensitive keywords."""

    query = str(text or "").strip()
    if not query:
        return "错误：text 不能为空，请提供需要检索的关键词或文本。"

    directory = Path(context_dir).expanduser()
    if archive_id:
        try:
            selected = _normalise_archive_id(archive_id)
        except ValueError as error:
            return f"错误：{error}"
        path = directory / f"{selected}.jsonl"
        if not path.is_file():
            return f"错误：找不到上下文归档 {selected}（{path}）。"
        paths = [path]
    else:
        if not directory.is_dir():
            return f"当前项目还没有上下文归档：{directory}"
        paths = sorted(
            directory.glob("*.jsonl"), key=lambda item: item.name, reverse=True
        )
        if not paths:
            return f"当前项目还没有上下文归档：{directory}"

    terms = [term for term in re.split(r"\s+", query) if term]
    matches: list[tuple[int, str, str, int, str, str]] = []

    for path in paths:
        try:
            messages = list(_iter_archive(path))
        except OSError as error:
            continue
        for line_number, message in messages:
            haystack = message_text(message)
            lowered = haystack.lower()
            score = 0
            positions: list[int] = []
            for term in terms:
                needle = term.lower()
                position = lowered.find(needle)
                if position >= 0:
                    score += lowered.count(needle)
                    positions.append(position)
            if not positions:
                continue
            first = min(positions)
            start = max(0, first - 120)
            end = min(len(haystack), first + 280)
            snippet = _compact_whitespace(haystack[start:end])
            if start:
                snippet = "…" + snippet
            if end < len(haystack):
                snippet += "…"
            matches.append(
                (
                    score,
                    path.stem,
                    path.name,
                    line_number,
                    str(message.get("role", "unknown")),
                    snippet,
                )
            )

    if not matches:
        return f"未找到包含关键词 {query!r} 的上下文记录。"

    matches.sort(key=lambda item: (-item[0], item[1], item[3]))
    shown = matches[:max_matches]
    header = (
        f"找到 {len(matches)} 条匹配记录"
        f"{'，仅显示前 ' + str(len(shown)) + ' 条' if len(matches) > len(shown) else ''}："
    )
    lines = [header]
    used = len(header)
    for index, (_, archive, filename, line_number, role, snippet) in enumerate(
        shown, 1
    ):
        block = (
            f"{index}. 归档 `{archive}`（{filename}）"
            f" · 第 {line_number} 行 · 角色 `{role}`\n   {snippet}"
        )
        if used + len(block) > max_chars:
            lines.append("…（结果已截断，请缩小关键词范围或指定 id）")
            break
        lines.append(block)
        used += len(block)
    result = "\n\n".join(lines)
    if len(result) > max_chars:
        result = result[: max(0, max_chars - 1)].rstrip() + "…"
    return result
