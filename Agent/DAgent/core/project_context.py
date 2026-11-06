"""One-time project instructions loaded from ``AGENTS.md``.

The content is appended to the conversation as a system message during the
first load of a session.  The same message is persisted with the session and is
reused on every later turn, so the provider cache key does not change between
requests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .types import ChatMessage

AGENTS_FILENAME = "AGENTS.md"
AGENTS_MARKER = "【项目说明 AGENTS.md】"


def agents_message(
    project_root: str | Path,
    filename: str = AGENTS_FILENAME,
) -> tuple[ChatMessage, Path] | None:
    """Read ``AGENTS.md`` and turn it into a stable system message.

    Returns ``None`` when the file is missing or contains only whitespace.
    """

    root = Path(project_root).expanduser().resolve()
    path = root / filename
    if not path.is_file():
        return None

    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None

    text = ""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text.strip():
        return None

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    content = (
        f"{AGENTS_MARKER}\n"
        f"来源：{path.as_posix()}\n\n"
        f"{text}"
    )
    return {"role": "system", "content": content}, path


def has_agents_message(messages: Sequence[ChatMessage]) -> bool:
    """Return whether a saved conversation already contains AGENTS.md."""

    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith(AGENTS_MARKER):
            return True
    return False


def insert_agents_message(
    messages: Sequence[ChatMessage],
    message: ChatMessage,
) -> list[ChatMessage]:
    """Insert project instructions after leading system messages."""

    result = [dict(item) for item in messages]
    index = 0
    while index < len(result) and result[index].get("role") == "system":
        index += 1
    result.insert(index, dict(message))
    return result


__all__ = [
    "AGENTS_FILENAME",
    "AGENTS_MARKER",
    "agents_message",
    "has_agents_message",
    "insert_agents_message",
]
