"""OpenAI-compatible LLM client.

The client converts provider-specific streaming chunks into small, stable
events so that the agent and all UIs stay provider-agnostic.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.client import HTTPResponse
from typing import Any, Iterator, Protocol, Sequence

from .config import Settings
from .errors import LLMError
from .types import ChatMessage, ToolCall

def _clean_tool_name(name: Any) -> str:
    """Remove leaked chat-template channel tokens from a function name.

    Some gateways expose raw gpt-oss tokens such as
    ``fetch_web<|channel|>commentary`` instead of ``fetch_web``.
    """

    return str(name or "").split("<|", 1)[0].strip()


@dataclass(frozen=True)
class LLMTextDelta:
    """A piece of streamed assistant text."""

    text: str


@dataclass(frozen=True)
class LLMTurnComplete:
    """The final result of one LLM request."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


LLMEvent = LLMTextDelta | LLMTurnComplete


class LLMClient(Protocol):
    """Port implemented by any chat completion provider."""

    def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, Any]],
    ) -> Iterator[LLMEvent]:
        """Yield text deltas and one final turn result."""
        ...


class OpenAICompatibleClient:
    """Minimal client for the OpenAI ``/chat/completions`` protocol."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, Any]],
    ) -> Iterator[LLMEvent]:
        body: dict[str, Any] = {
            "model": self._settings.model,
            "messages": list(messages),
            "stream": self._settings.stream,
        }
        if tools:
            body["tools"] = list(tools)

        request = urllib.request.Request(
            self._settings.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        request.add_header("Authorization", "Bearer " + self._settings.api_key)
        request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(
                request, timeout=self._settings.timeout
            ) as response:
                if self._settings.stream:
                    yield from self._consume_stream(response)
                else:
                    yield from self._consume_full_response(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            raise LLMError(f"LLM 请求失败（HTTP {error.code}）：{detail}") from error
        except urllib.error.URLError as error:
            raise LLMError(f"无法连接 LLM 服务：{error.reason}") from error
        except TimeoutError as error:
            raise LLMError("LLM 请求超时") from error

    def _consume_full_response(self, response: HTTPResponse) -> Iterator[LLMEvent]:
        raw = response.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            message = payload["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise LLMError(f"无法解析 LLM 响应：{error}") from error
        if not isinstance(message, dict):
            raise LLMError("无法解析 LLM 响应：message 不是对象")

        content = message.get("content") or ""
        if content:
            yield LLMTextDelta(content)
        yield LLMTurnComplete(
            content=content,
            tool_calls=self._tool_calls_from_payloads(message.get("tool_calls") or []),
        )

    def _consume_stream(self, response: HTTPResponse) -> Iterator[LLMEvent]:
        content_parts: list[str] = []
        builders: dict[int, dict[str, str]] = {}

        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break

            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as error:
                raise LLMError(f"无法解析流式响应：{error}") from error
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise LLMError(f"LLM 返回错误：{chunk['error']}")

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            text = delta.get("content")
            if text:
                content_parts.append(text)
                yield LLMTextDelta(text)

            for tool_call in delta.get("tool_calls") or []:
                index = tool_call.get("index", 0)
                builder = builders.setdefault(
                    index,
                    {
                        "id": f"call_{index}",
                        "name": "",
                        "arguments": "",
                    },
                )
                if tool_call.get("id"):
                    builder["id"] = tool_call["id"]
                function = tool_call.get("function") or {}
                builder["name"] += function.get("name") or ""
                builder["arguments"] += function.get("arguments") or ""

        tool_calls = [
            ToolCall(
                id=builder["id"],
                name=_clean_tool_name(builder["name"]),
                arguments=builder["arguments"] or "{}",
            )
            for builder in self._merge_orphan_fragments(builders)
        ]
        yield LLMTurnComplete(content="".join(content_parts), tool_calls=tool_calls)

    @staticmethod
    def _merge_orphan_fragments(
        builders: dict[int, dict[str, str]],
    ) -> list[dict[str, str]]:
        """Attach nameless argument fragments to the previous tool call.

        Some OpenAI-compatible gateways occasionally emit the tail of a tool
        call's arguments under a new ``index`` with no function name. Executing
        that fragment as a separate call would produce invalid JSON and an
        unknown tool. A nameless fragment cannot be a valid call on its own, so
        appending it to the closest named call is the safest recovery.
        """

        merged: list[dict[str, str]] = []
        for _, builder in sorted(builders.items()):
            if not builder["name"] and merged:
                merged[-1]["arguments"] += builder["arguments"]
                continue
            merged.append(builder)
        return merged

    @staticmethod
    def _tool_calls_from_payloads(payloads: Sequence[Any]) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for index, payload in enumerate(payloads):
            if not isinstance(payload, dict):
                continue
            function = payload.get("function") or {}
            if not isinstance(function, dict):
                continue
            tool_calls.append(
                ToolCall(
                    id=payload.get("id") or f"call_{index}",
                    name=_clean_tool_name(function.get("name")),
                    arguments=self._normalize_arguments(
                        function.get("arguments")
                    ),
                )
            )
        return tool_calls

    @staticmethod
    def _normalize_arguments(arguments: Any) -> str:
        if not arguments:
            return "{}"
        if isinstance(arguments, str):
            return arguments
        return json.dumps(arguments, ensure_ascii=False)
