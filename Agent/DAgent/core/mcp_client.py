"""MCP (Model Context Protocol) client integration.

DAgent is synchronous: the agent loop calls tools as plain Python functions.
The official MCP Python SDK is asynchronous, so this module keeps one event
loop alive on a background thread.  Each configured server gets a long-lived
``ClientSession`` (and its transport) inside that loop, while the agent thread
uses ``run_coroutine_threadsafe`` to call ``list_tools`` / ``call_tool``.

Discovered MCP tools are registered as ordinary :class:`core.tools.registry.Tool`
objects.  A binding table maps the name exposed to the LLM back to the server
and the original MCP tool name, which also lets DAgent prefix names only when
that is necessary to avoid collisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from .mcp_config import MCPServerConfig, STDIO
from .tools.registry import Tool, ToolRegistry
from .types import ToolResult

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when mcp is not installed
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    streamablehttp_client = None  # type: ignore[assignment]
    MCP_AVAILABLE = False


MAX_TOOL_NAME = 64
MAX_RESULT_CHARS = 30000
CONNECT_GRACE_SECONDS = 5.0
CALL_GRACE_SECONDS = 5.0
SHUTDOWN_GRACE_SECONDS = 8.0

_TOOL_NAME_INVALID = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class MCPToolInfo:
    """A tool advertised by an MCP Server, before name registration."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class MCPToolBinding:
    """The mapping between the LLM-visible name and the MCP tool."""

    exposed_name: str
    server: str
    tool: str


class MCPClientManager:
    """Owns the background event loop and all MCP client sessions."""

    def __init__(self, servers: tuple[MCPServerConfig, ...] | list[MCPServerConfig]):
        self._servers: dict[str, MCPServerConfig] = {
            server.name: server for server in servers
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._serve_futures: dict[str, Any] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._sessions: dict[str, Any] = {}
        self._tool_infos: list[MCPToolInfo] = []
        self._bindings: dict[str, MCPToolBinding] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.RLock()
        self._started = False
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Connect to every configured server, without raising on failure.

        Connections are opened concurrently.  A broken server is recorded in
        :attr:`startup_warnings` and does not prevent the other servers (or the
        local tools) from being used.
        """

        with self._lock:
            if self._started or self._closed:
                return
            self._started = True

        if not self._servers:
            return

        if not MCP_AVAILABLE:
            message = (
                "缺少 mcp Python SDK，MCP 工具不可用。"
                '请运行：python -m pip install "mcp>=1.10,<2"'
            )
            with self._lock:
                for name in self._servers:
                    self._errors[name] = message
            return

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="dagent-mcp",
            daemon=True,
        )
        self._thread.start()

        ready_events: dict[str, threading.Event] = {}
        for name, server in self._servers.items():
            event = threading.Event()
            ready_events[name] = event
            self._serve_futures[name] = asyncio.run_coroutine_threadsafe(
                self._serve(server, event),
                self._loop,
            )

        # All connections were started in parallel, so the overall wait is the
        # slowest configured timeout plus a small handshake grace period.
        deadline = time.monotonic() + max(
            server.timeout for server in self._servers.values()
        ) + CONNECT_GRACE_SECONDS

        for name, server in self._servers.items():
            event = ready_events[name]
            if not event.is_set():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    event.wait(remaining)

            if event.is_set():
                continue

            timed_out = False
            with self._lock:
                if name not in self._sessions:
                    self._errors.setdefault(
                        name,
                        f"连接超时（超过 {server.timeout:g} 秒）",
                    )
                    timed_out = True
            if timed_out:
                self._cancel_future(self._serve_futures.get(name))

    def close(self) -> None:
        """Close sessions and stop the background thread.  Safe to call twice."""

        with self._lock:
            if self._closed:
                return
            self._closed = True

        loop = self._loop
        if loop is None or loop.is_closed():
            return

        for future in list(self._serve_futures.values()):
            self._cancel_future(future)

        try:
            loop.call_soon_threadsafe(self._begin_shutdown)
        except RuntimeError:
            return

        if self._thread is not None:
            self._thread.join(timeout=SHUTDOWN_GRACE_SECONDS + 2.0)
            if self._thread.is_alive():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
                self._thread.join(timeout=2.0)

    @staticmethod
    def _cancel_future(future: Any) -> None:
        if future is not None and not future.done():
            future.cancel()

    def _run_event_loop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = [
                    task for task in asyncio.all_tasks(loop) if not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()

    def _begin_shutdown(self) -> None:
        loop = self._loop
        if loop is None:
            return
        for event in list(self._stop_events.values()):
            event.set()

        async def wait_then_stop() -> None:
            current = asyncio.current_task()
            tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not current and not task.done()
            ]
            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=SHUTDOWN_GRACE_SECONDS,
                    )
                except Exception:
                    pass
            loop.stop()

        loop.create_task(wait_then_stop())

    async def _serve(
        self, config: MCPServerConfig, ready: threading.Event
    ) -> None:
        """Open one connection, publish its tools, then keep it alive."""

        stop_event = asyncio.Event()
        with self._lock:
            self._stop_events[config.name] = stop_event

        try:
            async with AsyncExitStack() as stack:
                if config.transport == STDIO:
                    parameters = StdioServerParameters(  # type: ignore[misc]
                        command=config.command,
                        args=list(config.args),
                        env={**os.environ, **dict(config.env)},
                        cwd=config.cwd or None,
                    )
                    transport = stdio_client(parameters)  # type: ignore[misc]
                else:
                    transport = streamablehttp_client(  # type: ignore[misc]
                        config.url,
                        headers=dict(config.headers) or None,
                        timeout=timedelta(seconds=config.timeout),
                        sse_read_timeout=timedelta(
                            seconds=max(config.timeout, 300.0)
                        ),
                    )

                streams = await stack.enter_async_context(transport)
                session = await stack.enter_async_context(
                    ClientSession(  # type: ignore[misc]
                        streams[0],
                        streams[1],
                        read_timeout_seconds=timedelta(
                            seconds=config.timeout
                        ),
                    )
                )
                await session.initialize()
                infos = await self._list_all_tools(session, config)

                with self._lock:
                    self._sessions[config.name] = session
                    self._tool_infos.extend(infos)
                    self._errors.pop(config.name, None)
                ready.set()

                await stop_event.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # ExceptionGroup, OSError, anything.
            with self._lock:
                self._errors[config.name] = _format_exception(error)
        finally:
            with self._lock:
                self._sessions.pop(config.name, None)
                self._tool_infos = [
                    info
                    for info in self._tool_infos
                    if info.server != config.name
                ]
                self._stop_events.pop(config.name, None)
            ready.set()

    async def _list_all_tools(
        self, session: Any, config: MCPServerConfig
    ) -> list[MCPToolInfo]:
        infos: list[MCPToolInfo] = []
        cursor: str | None = None
        for _ in range(100):  # Defensive bound for misbehaving servers.
            if cursor:
                result = await session.list_tools(cursor=cursor)
            else:
                result = await session.list_tools()
            for tool in result.tools:
                infos.append(
                    MCPToolInfo(
                        server=config.name,
                        name=str(tool.name),
                        description=str(tool.description or ""),
                        input_schema=_normalize_schema(tool.inputSchema),
                    )
                )
            cursor = getattr(result, "nextCursor", None)
            if not cursor:
                break
        return infos

    # -- tool registry integration ----------------------------------------

    @property
    def tool_infos(self) -> tuple[MCPToolInfo, ...]:
        with self._lock:
            return tuple(self._tool_infos)

    @property
    def bindings(self) -> tuple[MCPToolBinding, ...]:
        with self._lock:
            return tuple(self._bindings.values())

    @property
    def startup_warnings(self) -> tuple[str, ...]:
        with self._lock:
            errors = dict(self._errors)
        return tuple(
            f"MCP Server {name} 连接失败：{message}"
            for name, message in errors.items()
        )

    def bind(self, binding: MCPToolBinding) -> None:
        with self._lock:
            self._bindings[binding.exposed_name] = binding

    def call_exposed_tool(
        self, exposed_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """Execute an LLM tool call using the exposed-name binding table."""

        binding = None
        with self._lock:
            binding = self._bindings.get(exposed_name)
        if binding is None:
            return ToolResult(
                name=exposed_name,
                content=f"错误：未知的 MCP 工具：{exposed_name}",
                ok=False,
            )
        return self.call_tool(
            binding.server,
            binding.tool,
            arguments,
            exposed_name=exposed_name,
        )

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        exposed_name: str | None = None,
    ) -> ToolResult:
        """Forward a tool call to its MCP Server and normalize the result."""

        result_name = exposed_name or tool_name
        if not isinstance(arguments, dict):
            return ToolResult(
                name=result_name,
                content="错误：MCP 工具参数必须是 JSON 对象",
                ok=False,
            )

        with self._lock:
            session = self._sessions.get(server_name)
            server = self._servers.get(server_name)
            error = self._errors.get(server_name)

        if session is None:
            detail = error or "未建立连接"
            return ToolResult(
                name=result_name,
                content=(
                    f"错误：MCP Server {server_name} 当前不可用：{detail}"
                ),
                ok=False,
            )
        if self._loop is None or self._loop.is_closed():
            return ToolResult(
                name=result_name,
                content=f"错误：MCP Server {server_name} 的连接已关闭",
                ok=False,
            )

        try:
            future = asyncio.run_coroutine_threadsafe(
                session.call_tool(tool_name, arguments),
                self._loop,
            )
        except RuntimeError as error:
            return ToolResult(
                name=result_name,
                content=(
                    f"错误：无法提交 MCP 工具调用到 {server_name}：{error}"
                ),
                ok=False,
            )

        timeout = (server.timeout if server else 30.0) + CALL_GRACE_SECONDS
        try:
            call_result = future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            return ToolResult(
                name=result_name,
                content=(
                    f"错误：调用 MCP 工具 {server_name}.{tool_name} "
                    f"超时（超过 {timeout:g} 秒）"
                ),
                ok=False,
            )
        except Exception as error:
            return ToolResult(
                name=result_name,
                content=(
                    f"错误：调用 MCP 工具 {server_name}.{tool_name} 失败："
                    f"{_format_exception(error)}"
                ),
                ok=False,
            )

        return _format_call_result(result_name, server_name, tool_name, call_result)


# -- registration ----------------------------------------------------------


def register_mcp_tools(
    registry: ToolRegistry, manager: MCPClientManager
) -> tuple[MCPToolBinding, ...]:
    """Register every discovered MCP tool in ``registry``.

    A tool keeps its original name when that name is already a valid LLM tool
    name and does not collide with a local tool or another MCP server.  In all
    other cases the exposed name is ``<server>__<tool>``.  The returned bindings
    (also stored on the manager) preserve the original names for forwarding.
    """

    infos = manager.tool_infos
    if not infos:
        return ()

    bare_counts = Counter(info.name for info in infos)
    used = set(registry.names())
    bindings: list[MCPToolBinding] = []

    for info in infos:
        base = _sanitize_name(info.name, "tool")
        needs_prefix = (
            base != info.name
            or info.name in used
            or bare_counts[info.name] > 1
        )
        candidate = (
            _prefixed_name(info.server, info.name) if needs_prefix else base
        )
        candidate = _unique_name(candidate, used)
        used.add(candidate)

        binding = MCPToolBinding(
            exposed_name=candidate,
            server=info.server,
            tool=info.name,
        )
        manager.bind(binding)
        bindings.append(binding)

        description = info.description.strip() or f"MCP 工具 {info.name}"
        source = f"MCP Server {info.server}"
        if source not in description:
            description = f"{description}\n\n（由 {source} 提供）"

        registry.register(
            Tool(
                name=candidate,
                description=description,
                parameters=_normalize_schema(info.input_schema),
                handler=_make_handler(manager, candidate),
            )
        )

    return tuple(bindings)


def _make_handler(
    manager: MCPClientManager, exposed_name: str
) -> Callable[..., ToolResult]:
    def handler(**arguments: Any) -> ToolResult:
        return manager.call_exposed_tool(exposed_name, arguments)

    return handler


# -- naming ----------------------------------------------------------------


def _sanitize_name(value: str, fallback: str) -> str:
    cleaned = _TOOL_NAME_INVALID.sub("_", str(value or "")).strip("_-")
    cleaned = re.sub(r"_{2,}", "__", cleaned)
    return cleaned or fallback


def _prefixed_name(server: str, tool: str) -> str:
    name = f"{_sanitize_name(server, 'server')}__{_sanitize_name(tool, 'tool')}"
    if len(name) <= MAX_TOOL_NAME:
        return name
    digest = hashlib.sha1(
        f"{server}\x00{tool}".encode("utf-8")
    ).hexdigest()[:8]
    stem = name[: MAX_TOOL_NAME - len(digest) - 2].rstrip("_-") or "mcp"
    return f"{stem}__{digest}"


def _unique_name(name: str, used: set[str]) -> str:
    if name not in used:
        return name
    for index in range(2, 1000):
        suffix = f"_{index}"
        candidate = name[: MAX_TOOL_NAME - len(suffix)] + suffix
        if candidate not in used:
            return candidate
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return _prefixed_name("mcp", digest)


# -- result and schema normalization --------------------------------------


def _normalize_schema(schema: Any) -> dict[str, Any]:
    if hasattr(schema, "model_dump"):
        try:
            schema = schema.model_dump()
        except Exception:
            schema = None
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = json.loads(json.dumps(schema, ensure_ascii=False, default=str))
    if not isinstance(normalized.get("type"), str) or not normalized["type"]:
        normalized["type"] = "object"
    if normalized.get("type") == "object":
        properties = normalized.get("properties")
        if not isinstance(properties, dict):
            normalized["properties"] = {}
    return normalized


def _format_call_result(
    exposed_name: str,
    server_name: str,
    tool_name: str,
    result: Any,
) -> ToolResult:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        rendered = _render_content_block(block)
        if rendered:
            parts.append(rendered)

    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            rendered_structured = json.dumps(
                structured, ensure_ascii=False, indent=2, default=str
            )
        except (TypeError, ValueError):
            rendered_structured = str(structured)
        parts.append(f"结构化数据：\n{rendered_structured}")

    content = "\n\n".join(part for part in parts if part).strip()
    is_error = bool(
        getattr(result, "isError", False)
        or getattr(result, "is_error", False)
    )
    if not content:
        content = "（MCP 工具没有返回内容）"
    content = _truncate(content)
    if is_error:
        content = (
            f"MCP 工具 {server_name}.{tool_name} 返回错误：\n{content}"
        )

    return ToolResult(name=exposed_name, content=content, ok=not is_error)


def _render_content_block(block: Any) -> str:
    block_type = str(getattr(block, "type", "") or "")

    if block_type == "text":
        return str(getattr(block, "text", "") or "")

    if block_type in {"image", "audio"}:
        mime = str(getattr(block, "mimeType", "") or "未知类型")
        data = str(getattr(block, "data", "") or "")
        kind = "图像" if block_type == "image" else "音频"
        return (
            f"[{kind}内容：{mime}，base64 长度 {len(data)} 字符；"
            "当前对话不展开二进制内容]"
        )

    if block_type == "resource":
        resource = getattr(block, "resource", None)
        return _render_resource(resource)

    if block_type == "resource_link":
        name = str(getattr(block, "name", "") or "")
        uri = str(getattr(block, "uri", "") or "")
        description = str(getattr(block, "description", "") or "")
        label = name or uri or "未命名资源"
        line = f"[资源链接：{label}"
        if uri and uri != label:
            line += f"（{uri}）"
        if description:
            line += f" - {description}"
        return line + "]"

    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text

    if hasattr(block, "model_dump"):
        try:
            return json.dumps(
                block.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception:
            pass
    return str(block)


def _render_resource(resource: Any) -> str:
    uri = str(getattr(resource, "uri", "") or "")
    mime = str(getattr(resource, "mimeType", "") or "")
    text = getattr(resource, "text", None)
    if isinstance(text, str):
        header = f"资源 {uri}" if uri else "资源"
        if mime:
            header += f"（{mime}）"
        return f"{header}：\n{text}"

    blob = getattr(resource, "blob", None)
    if blob is not None:
        label = f"资源 {uri}" if uri else "二进制资源"
        if mime:
            label += f"（{mime}）"
        try:
            size = len(str(blob))
        except Exception:
            size = 0
        return f"[{label}，base64 长度 {size} 字符；当前对话不展开二进制内容]"

    return str(resource)


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (
        text[:MAX_RESULT_CHARS].rstrip()
        + f"\n\n[结果已截断：原始 {len(text)} 字符，"
        f"仅保留前 {MAX_RESULT_CHARS} 字符]"
    )


def _format_exception(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        messages: list[str] = []
        for inner in error.exceptions:
            message = _format_exception(inner)
            if message and message not in messages:
                messages.append(message)
        return "；".join(messages) or type(error).__name__
    message = str(error).strip()
    if not message:
        return type(error).__name__
    return f"{type(error).__name__}: {message}"


__all__ = [
    "MCPToolBinding",
    "MCPToolInfo",
    "MCPClientManager",
    "register_mcp_tools",
]
