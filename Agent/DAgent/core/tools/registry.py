"""Tool registry: turns local Python functions into LLM-callable tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..types import ToolCall, ToolResult


@dataclass(frozen=True)
class Tool:
    """A tool definition plus its local implementation."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str | ToolResult]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Holds the available tools and executes calls safely."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._closers: list[Callable[[], None]] = []
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def add_closer(self, closer: Callable[[], None]) -> None:
        """Register a cleanup callback (for example an MCP client manager)."""

        self._closers.append(closer)

    def close(self) -> None:
        """Run cleanup callbacks once, newest first, without raising."""

        closers, self._closers = self._closers, []
        for closer in reversed(closers):
            try:
                closer()
            except Exception:
                # Cleanup must not mask the original shutdown path.
                pass

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                name=call.name,
                content=f"错误：未知工具：{call.name}",
                ok=False,
            )

        try:
            arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as error:
            return ToolResult(
                name=call.name,
                content=f"错误：工具参数不是合法 JSON：{error}",
                ok=False,
            )
        if not isinstance(arguments, dict):
            return ToolResult(
                name=call.name,
                content="错误：工具参数必须是 JSON 对象",
                ok=False,
            )

        try:
            result = tool.handler(**arguments)
        except TypeError as error:
            return ToolResult(
                name=call.name,
                content=f"错误：工具参数不匹配：{error}",
                ok=False,
            )
        except Exception as error:  # Tools must never crash the agent loop.
            return ToolResult(
                name=call.name,
                content=f"错误：{type(error).__name__}: {error}",
                ok=False,
            )
        if isinstance(result, ToolResult):
            if result.name == call.name:
                return result
            return ToolResult(
                name=call.name, content=result.content, ok=result.ok
            )
        return ToolResult(name=call.name, content=str(result), ok=True)
