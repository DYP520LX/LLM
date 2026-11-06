"""UI-agnostic events emitted by :class:`core.agent.Agent`.

A TUI, web UI, or desktop UI can consume these events without knowing how the
LLM client or the tools are implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .types import ToolResult


@dataclass(frozen=True)
class AssistantText:
    """A chunk of assistant text, suitable for incremental rendering."""

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """The agent is about to execute a tool."""

    name: str
    arguments: str


@dataclass(frozen=True)
class ToolCallFinished:
    """A tool finished executing."""

    result: ToolResult


@dataclass(frozen=True)
class TurnCompleted:
    """The current user turn finished successfully."""

    content: str


@dataclass(frozen=True)
class TurnFailed:
    """The current user turn failed with a user-displayable message."""

    message: str


AgentEvent = Union[
    AssistantText,
    ToolCallStarted,
    ToolCallFinished,
    TurnCompleted,
    TurnFailed,
]
