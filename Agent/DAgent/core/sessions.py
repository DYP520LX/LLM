"""Persistent conversation sessions and append-only event logs.

A session is one directory below the configured ``sessions_dir``::

    .agents/sessions/20261106-153000-ab12/
        meta.json       # title, timestamps and message count
        messages.json   # authoritative conversation snapshot
        events.jsonl    # append-only audit log (messages, AGENTS.md, ...)

The agent still owns the live history.  The TUI calls :meth:`SessionStore.save`
when the user runs ``/save``, switches sessions, or the current turn finishes.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .types import ChatMessage

MESSAGES_FILENAME = "messages.json"
META_FILENAME = "meta.json"
EVENTS_FILENAME = "events.jsonl"
DEFAULT_TITLE = "新会话"

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SessionError(Exception):
    """Raised when a session cannot be created, read or written."""


@dataclass(frozen=True)
class SessionInfo:
    """Lightweight session metadata used by the resume list."""

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    preview: str = ""
    path: Path | None = None

    @property
    def directory(self) -> Path | None:
        return self.path


class SessionStore:
    """Read and write DAgent sessions under a project directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._lock = threading.RLock()

    # -- public API --------------------------------------------------------

    def create(self, title: str = "") -> SessionInfo:
        """Create a new, empty session directory and return its metadata."""

        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            session_id = self._new_session_id()
            directory = self._directory(session_id)
            directory.mkdir(parents=True, exist_ok=False)
            now = _now()
            info = SessionInfo(
                id=session_id,
                title=title.strip() or DEFAULT_TITLE,
                created_at=now,
                updated_at=now,
                message_count=0,
                preview="",
                path=directory,
            )
            self._write_json(
                directory / MESSAGES_FILENAME,
                [],
            )
            self._write_meta(info)
            self.append_event(session_id, "session_created", {"title": info.title})
            return info

    def save(
        self,
        session_id: str,
        messages: Sequence[ChatMessage],
        title: str | None = None,
    ) -> SessionInfo:
        """Write a complete conversation snapshot and append an event."""

        with self._lock:
            directory = self._directory(session_id)
            directory.mkdir(parents=True, exist_ok=True)
            previous = self._read_meta(session_id)
            now = _now()
            snapshot = _json_safe_messages(messages)
            resolved_title = (
                title.strip()
                if title is not None and title.strip()
                else _derive_title(snapshot)
            )
            info = SessionInfo(
                id=session_id,
                title=resolved_title,
                created_at=previous.created_at if previous else now,
                updated_at=now,
                message_count=len(snapshot),
                preview=_preview(snapshot),
                path=directory,
            )
            self._write_json(directory / MESSAGES_FILENAME, snapshot)
            self._write_meta(info)
            self.append_event(
                session_id,
                "snapshot",
                {
                    "title": info.title,
                    "message_count": info.message_count,
                    "messages": snapshot,
                },
            )
            return info

    def load(self, session_id: str) -> list[ChatMessage]:
        """Load the authoritative message snapshot for a session."""

        with self._lock:
            directory = self._directory(session_id)
            path = directory / MESSAGES_FILENAME
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = self._last_snapshot(session_id)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise SessionError(f"读取会话 {session_id} 失败：{error}") from error

            if not isinstance(data, list):
                raise SessionError(f"会话 {session_id} 的消息格式无效")
            return [
                dict(message)
                for message in data
                if isinstance(message, dict) and message.get("role")
            ]

    def get(self, session_id: str) -> SessionInfo | None:
        with self._lock:
            try:
                return self._read_meta(session_id)
            except SessionError:
                return None

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        """Return sessions ordered by most recently updated first."""

        if not self.root.is_dir():
            return []
        with self._lock:
            sessions: list[SessionInfo] = []
            for directory in self.root.iterdir():
                if not directory.is_dir():
                    continue
                if not _SESSION_ID_RE.fullmatch(directory.name):
                    continue
                info = self._read_meta(directory.name)
                if info is not None:
                    sessions.append(info)
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        if limit is not None:
            return sessions[: max(0, limit)]
        return sessions

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one JSON line to ``events.jsonl``."""

        event = {
            "timestamp": _now(),
            "type": str(event_type),
        }
        if payload:
            event.update(_json_safe_value(payload))
        directory = self._directory(session_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / EVENTS_FILENAME).open(
                "a", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(
                    json.dumps(event, ensure_ascii=False, default=str) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise SessionError(f"写入会话 {session_id} 事件日志失败：{error}") from error

    def session_dir(self, session_id: str) -> Path:
        return self._directory(session_id)

    # -- internals ---------------------------------------------------------

    def _new_session_id(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for index in range(1000):
            suffix = "" if index == 0 else f"-{index:02d}"
            candidate = f"{stamp}{suffix}"
            if not (self.root / candidate).exists():
                return candidate
        raise SessionError("无法生成唯一的会话编号")

    def _directory(self, session_id: str) -> Path:
        value = str(session_id or "")
        if not _SESSION_ID_RE.fullmatch(value) or ".." in value:
            raise SessionError(f"无效的会话编号：{session_id!r}")
        return self.root / value

    def _read_meta(self, session_id: str) -> SessionInfo | None:
        directory = self._directory(session_id)
        path = directory / META_FILENAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SessionError(f"读取会话 {session_id} 元数据失败：{error}") from error
        if not isinstance(data, dict):
            raise SessionError(f"会话 {session_id} 元数据格式无效")
        return SessionInfo(
            id=session_id,
            title=str(data.get("title") or DEFAULT_TITLE),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            message_count=int(data.get("message_count") or 0),
            preview=str(data.get("preview") or ""),
            path=directory,
        )

    def _write_meta(self, info: SessionInfo) -> None:
        directory = self._directory(info.id)
        self._write_json(
            directory / META_FILENAME,
            {
                "id": info.id,
                "title": info.title,
                "created_at": info.created_at,
                "updated_at": info.updated_at,
                "message_count": info.message_count,
                "preview": info.preview,
            },
        )

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SessionError(f"写入会话文件 {path} 失败：{error}") from error

    def _last_snapshot(self, session_id: str) -> list[ChatMessage]:
        path = self._directory(session_id) / EVENTS_FILENAME
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as error:
            raise SessionError(f"读取会话 {session_id} 事件日志失败：{error}") from error

        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "snapshot":
                continue
            messages = event.get("messages")
            if isinstance(messages, list):
                return [
                    dict(message)
                    for message in messages
                    if isinstance(message, dict) and message.get("role")
                ]
        return []


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _derive_title(messages: Iterable[ChatMessage]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        text = " ".join(content.split())
        if text:
            return text[:48]
    return DEFAULT_TITLE


def _preview(messages: Sequence[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return " ".join(content.split())[:80]
    return ""


def _json_safe_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    return [
        _json_safe_value(dict(message))
        for message in messages
        if isinstance(message, dict) and message.get("role")
    ]


def _json_safe_value(value: Any) -> Any:
    """Round-trip through JSON so sessions never contain live SDK objects."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "DEFAULT_TITLE",
    "EVENTS_FILENAME",
    "MESSAGES_FILENAME",
    "META_FILENAME",
    "SessionError",
    "SessionInfo",
    "SessionStore",
]
