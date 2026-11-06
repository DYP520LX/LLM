"""Shared data structures used by the core layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class FunctionCallPayload(TypedDict):
    """Serialized function call content returned by an OpenAI-compatible API."""

    name: str
    arguments: str


class ToolCallPayload(TypedDict):
    """Serialized tool call stored in the assistant message history."""

    id: str
    type: str
    function: FunctionCallPayload


class ChatMessage(TypedDict, total=False):
    """A single message accepted by the chat completions API."""

    role: str
    content: str | None
    tool_calls: list[ToolCallPayload]
    tool_call_id: str


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the LLM, independent of any provider format."""

    id: str
    name: str
    arguments: str

    def to_payload(self) -> ToolCallPayload:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class ToolResult:
    """The outcome of executing a tool."""

    name: str
    content: str
    ok: bool = True
