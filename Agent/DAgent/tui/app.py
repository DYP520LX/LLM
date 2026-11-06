"""Textual TUI for DAgent.

This module is intentionally a presentation layer only: it renders
``core.events.AgentEvent`` values and never talks to the LLM or tools directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Static

from core.agent import Agent
from core.bootstrap import MCPLoadResult, load_mcp_into_agent
from core.config import Settings
from core.context import CompactResult, ContextUsage
from core.events import (
    AssistantText,
    ContextUsageUpdated,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
)
from core.mcp_client import MCPServerStatus
from core.mcp_config import load_mcp_config
from core.project_context import AGENTS_MARKER, agents_message
from core.sessions import SessionError, SessionInfo, SessionStore
from core.types import ToolResult
from tui.screens import SessionListScreen


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    aliases: tuple[str, ...] = ()


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("new", "新建会话并自动保存当前会话"),
    SlashCommand("resume", "选择并恢复历史会话", ("sessions",)),
    SlashCommand("save", "保存当前会话"),
    SlashCommand("quit", "保存并退出 DAgent", ("exit",)),
    SlashCommand("compact", "压缩当前上下文并归档原文"),
    SlashCommand("clear", "清空当前会话（保留当前会话编号）"),
    SlashCommand("help", "显示全部斜杠命令", ("?",)),
    SlashCommand("status", "显示模型、会话、上下文与 MCP 状态"),
    SlashCommand("mcp", "刷新 MCP 服务器和工具状态"),
    SlashCommand("agents", "查看 AGENTS.md 项目说明注入状态"),
)


class CompactPreviewScreen(ModalScreen[None]):
    """Floating two-column preview of the context before/after compression."""

    BINDINGS = [("escape", "close", "关闭")]

    def __init__(self, before_text: str, after_text: str) -> None:
        super().__init__()
        self._before_text = before_text
        self._after_text = after_text

    def compose(self) -> ComposeResult:
        with Container(id="compact-dialog"):
            yield Static("上下文压缩预览", id="compact-title")
            with Horizontal(id="compact-columns"):
                with Vertical(classes="compact-column"):
                    yield Static("压缩前原文", classes="compact-heading")
                    yield VerticalScroll(
                        Static(Text(self._before_text), id="compact-before"),
                        classes="compact-scroll",
                    )
                with Vertical(classes="compact-column"):
                    yield Static("压缩后摘要", classes="compact-heading")
                    yield VerticalScroll(
                        Static(Text(self._after_text), id="compact-after"),
                        classes="compact-scroll",
                    )
            yield Button("关闭", id="compact-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "compact-close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class DAgentApp(App[None]):
    """A chat interface with streaming output, sessions and MCP status."""

    CSS_PATH = "styles.tcss"
    TITLE = "DAgent"
    SUB_TITLE = "本地工具型 AI 助手"
    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+l", "clear", "清空会话"),
    ]

    def __init__(
        self,
        agent: Agent,
        settings: Settings,
        *,
        project_root: str | Path | None = None,
        session_store: SessionStore | None = None,
        load_mcp: bool = False,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.settings = settings
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else Path.cwd()
        )
        self._session_store = session_store
        if self._session_store is None and project_root is not None:
            self._session_store = SessionStore(
                self._resolve_project_path(settings.sessions_dir)
            )
        self._load_mcp = load_mcp
        self._session_info: SessionInfo | None = None
        self._busy = False
        self._assistant: Static | None = None
        self._assistant_parts: list[str] = []
        self._context_warning_shown = False
        self._closing = False

        self._slash_visible = False
        self._slash_items: list[SlashCommand] = []
        self._slash_index = 0

        self._mcp_config = None
        self._mcp_result: MCPLoadResult | None = None
        self._mcp_statuses: tuple[MCPServerStatus, ...] = ()
        self._pending_statuses: tuple[MCPServerStatus, ...] = ()
        self._mcp_loading = False
        self._mcp_started = False
        self._mcp_error = ""

        self._transcript: VerticalScroll
        self._context_meter: Static
        self._status: Static
        self._prompt: Input
        self._send: Button
        self._mcp_panel: Static
        self._sidebar: Vertical
        self._slash_palette: Vertical
        self._slash_items_view: Static

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("上下文：统计中…", id="context-meter")
        with Horizontal(id="main-area"):
            yield VerticalScroll(id="transcript")
            with Vertical(id="mcp-sidebar"):
                yield Static("MCP 服务器", id="sidebar-title")
                yield VerticalScroll(
                    Static("正在等待…", id="mcp-panel"),
                    id="sidebar-scroll",
                )
        yield Static("正在启动…", id="status")
        with Vertical(id="slash-palette", classes="hidden"):
            yield Static("斜杠命令", id="slash-title")
            yield Static("", id="slash-items")
        with Horizontal(id="composer"):
            yield Input(placeholder="输入问题，Enter 发送；/ 查看命令", id="prompt")
            yield Button("发送", id="send", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._transcript = self.query_one("#transcript", VerticalScroll)
        self._context_meter = self.query_one("#context-meter", Static)
        self._status = self.query_one("#status", Static)
        self._prompt = self.query_one("#prompt", Input)
        self._send = self.query_one("#send", Button)
        self._mcp_panel = self.query_one("#mcp-panel", Static)
        self._sidebar = self.query_one("#mcp-sidebar", Vertical)
        self._slash_palette = self.query_one("#slash-palette", Vertical)
        self._slash_items_view = self.query_one("#slash-items", Static)
        self._sidebar.styles.width = self.settings.tui_sidebar_width

        self._bootstrap_session()

        stream_state = "开" if self.settings.stream else "关"
        session_line = ""
        if self._session_info is not None:
            session_line = f"会话：{self._session_info.id}\n"
        self._add_system(
            "DAgent 已启动\n"
            f"{session_line}"
            f"模型：{self.settings.model}    Stream：{stream_state}    "
            f"工具：{len(self.agent.tool_names)} 个\n"
            "输入 / 查看斜杠命令补全；Enter 发送；"
            "Ctrl+L 清空会话，Ctrl+Q 退出。"
        )
        self._refresh_context_meter()
        if getattr(self.agent, "startup_notes", ()):
            notes = "\n".join(
                f"- {note}" for note in self.agent.startup_notes
            )
            self._add_message(f"启动警告：\n{notes}", "error")
        self._set_status("就绪")
        self._update_prompt_hint()
        self._prompt.focus()
        self._start_mcp_loading()

    def on_unmount(self) -> None:
        self._closing = True
        self._save_current_session(silent=True)
        close = getattr(self.agent, "close", None)
        if close is not None:
            close()

    # -- input and slash commands -----------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._submit()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            await self._submit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "prompt":
            self._update_slash_palette(event.value)

    def on_key(self, event: events.Key) -> None:
        if not self._slash_visible:
            return
        if event.key in {"up", "down"}:
            if self._slash_items:
                step = -1 if event.key == "up" else 1
                self._slash_index = (
                    self._slash_index + step
                ) % len(self._slash_items)
                self._render_slash_palette()
            event.prevent_default()
            event.stop()
            return
        if event.key == "tab" and self._slash_items:
            command = self._slash_items[self._slash_index]
            self._prompt.value = f"/{command.name}"
            self._prompt.cursor_position = len(self._prompt.value)
            self._update_slash_palette(self._prompt.value)
            event.prevent_default()
            event.stop()
            return
        if event.key == "escape":
            self._hide_slash_palette()
            event.prevent_default()
            event.stop()

    async def _submit(self) -> None:
        if self._busy:
            return
        text = self._prompt.value.strip()
        if not text:
            return

        if text.startswith("/"):
            await self._handle_slash_input(text)
            return

        if self._agent_needs_compaction():
            self._add_system(
                f"当前上下文已达到模型窗口的 {self._warning_percent():.0f}%，"
                "普通提示词已暂停。"
                "请输入 /compact 压缩上下文后再继续。"
            )
            self._set_status("请先执行 /compact")
            return

        self._hide_slash_palette()
        self._prompt.value = ""
        self._assistant = None
        self._assistant_parts = []
        self._add_message(f"你：{text}", "user")
        self._set_busy(True)
        self._set_status("正在思考…")
        self._run_agent(text)

    async def _handle_slash_input(self, text: str) -> None:
        command = self._lookup_command(text.split(maxsplit=1)[0].lower())
        if command is None and self._slash_visible and self._slash_items:
            command = self._slash_items[self._slash_index]
        self._hide_slash_palette()
        self._prompt.value = ""
        if command is None:
            self._add_system(f"未知的斜杠命令：{text}\n输入 /help 查看可用命令。")
            self._set_status("未知命令")
            self._prompt.focus()
            return
        await self._execute_command(command.name)

    def _lookup_command(self, token: str) -> SlashCommand | None:
        name = token.lstrip("/").lower()
        for command in SLASH_COMMANDS:
            if command.name == name or name in command.aliases:
                return command
        return None

    def _matching_commands(self, value: str) -> list[SlashCommand]:
        prefix = value.lstrip("/").lower()
        return [
            command
            for command in SLASH_COMMANDS
            if command.name.startswith(prefix)
            or any(alias.startswith(prefix) for alias in command.aliases)
        ]

    def _update_slash_palette(self, value: str) -> None:
        if (
            not value.startswith("/")
            or " " in value
            or "\n" in value
        ):
            self._hide_slash_palette()
            return
        items = self._matching_commands(value)
        if not items:
            self._hide_slash_palette()
            return
        if [item.name for item in items] != [item.name for item in self._slash_items]:
            self._slash_index = 0
        self._slash_items = items
        self._slash_index = min(self._slash_index, len(items) - 1)
        self._slash_visible = True
        self._slash_palette.set_class(False, "hidden")
        self._render_slash_palette()

    def _render_slash_palette(self) -> None:
        if not self._slash_items:
            self._hide_slash_palette()
            return
        lines: list[str] = []
        for index, command in enumerate(self._slash_items):
            marker = "▶" if index == self._slash_index else " "
            lines.append(f"{marker} /{command.name:<9} {command.description}")
        self._slash_items_view.update(Text("\n".join(lines)))

    def _hide_slash_palette(self) -> None:
        self._slash_visible = False
        self._slash_items = []
        self._slash_index = 0
        if hasattr(self, "_slash_palette"):
            self._slash_palette.set_class(True, "hidden")

    async def _execute_command(self, name: str) -> None:
        handler = getattr(self, f"_command_{name}", None)
        if handler is None:
            self._add_system(f"命令 /{name} 尚未实现。")
            return
        await handler()

    async def _command_help(self) -> None:
        lines = ["可用斜杠命令："]
        for command in SLASH_COMMANDS:
            aliases = ""
            if command.aliases:
                aliases = "（别名：" + "、".join(
                    f"/{alias}" for alias in command.aliases
                ) + "）"
            lines.append(f"/{command.name:<9} {command.description}{aliases}")
        lines.append("")
        lines.append("输入 / 后继续输入可过滤命令，↑/↓ 选择，Enter 执行。")
        self._add_system("\n".join(lines))
        self._set_status("就绪")
        self._prompt.focus()

    async def _command_new(self) -> None:
        if not await self._ensure_not_busy():
            return
        await self._save_current_session(silent=True)
        old_id = self._session_info.id if self._session_info else ""
        if self._session_store is not None:
            try:
                self._session_info = self._session_store.create()
            except SessionError as error:
                self._session_info = None
                self._add_message(f"创建新会话失败：{error}", "error")
                return
        else:
            self._session_info = None

        reset = getattr(self.agent, "reset", None)
        if callable(reset):
            reset()
        self._assistant = None
        self._assistant_parts = []
        self._context_warning_shown = False
        await self._transcript.remove_children()
        self._ensure_agents_context()
        self._save_current_session(silent=True)
        self._refresh_context_meter()
        self._refresh_mcp_panel()
        message = "已新建会话。"
        if old_id:
            message += f"\n上一会话已自动保存：{old_id}"
        if self._session_info is not None:
            message += f"\n当前会话：{self._session_info.id}"
        self._add_system(message)
        self._set_status("就绪")
        self._update_prompt_hint()
        self._prompt.focus()

    async def _command_resume(self) -> None:
        if not await self._ensure_not_busy():
            return
        if self._session_store is None:
            self._add_system("当前未启用会话持久化，无法恢复历史会话。")
            self._prompt.focus()
            return
        try:
            sessions = self._session_store.list_sessions(
                self.settings.tui_session_list_limit
            )
        except SessionError as error:
            self._add_message(f"读取历史会话失败：{error}", "error")
            return
        if not sessions:
            self._add_system("没有可恢复的历史会话。")
            self._prompt.focus()
            return
        self.push_screen(SessionListScreen(sessions), self._on_resume_selected)

    def _on_resume_selected(self, session: SessionInfo | None) -> None:
        if session is None:
            self._set_status("已取消恢复")
            self._prompt.focus()
            return
        self.run_worker(
            self._resume_session(session),
            group="session",
            exclusive=True,
        )

    async def _resume_session(self, session: SessionInfo) -> None:
        if self._session_store is None:
            return
        self._set_status(f"正在恢复会话 {session.id}…")
        await self._save_current_session(silent=True)
        try:
            messages = self._session_store.load(session.id)
        except SessionError as error:
            self._add_message(f"恢复会话失败：{error}", "error")
            self._set_status("恢复失败")
            return

        replace = getattr(self.agent, "replace_messages", None)
        if not callable(replace):
            self._add_message(
                "当前 Agent 不支持恢复会话历史。",
                "error",
            )
            self._set_status("恢复失败")
            return
        replace(messages)
        self._session_info = self._session_store.get(session.id) or session
        self._ensure_agents_context()
        await self._transcript.remove_children()
        self._render_history(self._agent_messages())
        self._save_current_session(silent=True)
        self._context_warning_shown = False
        self._refresh_context_meter()
        self._refresh_mcp_panel()
        self._set_status(f"已恢复会话 {session.id}")
        self._update_prompt_hint()
        self._prompt.focus()

    async def _command_save(self) -> None:
        info = self._save_current_session()
        if info is None:
            self._add_system("当前未启用会话持久化，无法保存。")
        else:
            self._add_system(
                f"会话已保存：{info.id}\n"
                f"目录：{info.path}\n"
                f"消息数：{info.message_count}"
            )
            self._set_status("会话已保存")
        self._prompt.focus()

    async def _command_quit(self) -> None:
        await self.action_quit()

    async def _command_compact(self) -> None:
        if not await self._ensure_not_busy():
            return
        self._start_compact()

    async def _command_clear(self) -> None:
        await self.action_clear()

    async def _command_status(self) -> None:
        usage = getattr(self.agent, "context_usage", None)
        lines = [
            "当前状态",
            f"模型：{self.settings.model}",
            f"Stream：{'开' if self.settings.stream else '关'}",
            f"工具：{len(self.agent.tool_names)} 个（本地 + 已加载 MCP）",
        ]
        if self._session_info is not None:
            lines.append(
                f"会话：{self._session_info.id} · "
                f"{self._session_info.title} · "
                f"{self._session_info.message_count} 条消息"
            )
            if self._session_info.path is not None:
                lines.append(f"会话目录：{self._session_info.path}")
        else:
            lines.append("会话：未启用持久化")
        if isinstance(usage, ContextUsage):
            lines.append(
                f"上下文：{usage.used_tokens / 1000:.1f}k / "
                f"{usage.window_tokens / 1000:g}k "
                f"({usage.percent:.1f}%)"
            )
        lines.append(
            "AGENTS.md："
            + (
                "已注入并复用"
                if self._agent_has_project_context()
                else "未注入"
            )
        )
        statuses = self._all_mcp_statuses()
        if not statuses:
            lines.append("MCP：未配置")
        else:
            connected = sum(1 for item in statuses if item.connected)
            lines.append(f"MCP：{connected}/{len(statuses)} 个服务器已连接")
            for status in statuses:
                tool_text = (
                    f"{len(status.tools)} 个工具"
                    if status.tools
                    else "无工具"
                )
                state = "已连接" if status.connected else (
                    status.error or "未连接"
                )
                lines.append(f"  - {status.name}：{state}，{tool_text}")
        self._add_system("\n".join(lines))
        self._set_status("就绪")
        self._prompt.focus()

    async def _command_mcp(self) -> None:
        if self._load_mcp and not self._mcp_started:
            self._start_mcp_loading()
            self._add_system("正在后台连接 MCP，完成后会自动刷新右侧状态栏。")
        else:
            self._refresh_mcp_panel()
            statuses = self._all_mcp_statuses()
            if not statuses:
                self._add_system("当前没有配置 MCP Server。")
            else:
                connected = sum(1 for item in statuses if item.connected)
                self._add_system(
                    f"MCP 状态已刷新：{connected}/{len(statuses)} 个服务器已连接。"
                    "详细工具列表见右侧边栏。"
                )
        self._set_status("就绪")
        self._prompt.focus()

    async def _command_agents(self) -> None:
        path = self._agents_path()
        injected = self._agent_has_project_context()
        lines = [
            "AGENTS.md 项目说明",
            f"文件：{path}",
            f"存在：{'是' if path.is_file() else '否'}",
            f"当前会话已注入：{'是' if injected else '否'}",
        ]
        if path.is_file() and not injected:
            lines.append(
                "该文件会在新建会话或恢复未包含项目说明的会话时注入一次。"
            )
        lines.append("策略：一次注入，后续轮次直接复用，避免破坏 Provider 缓存。")
        self._add_system("\n".join(lines))
        self._set_status("就绪")
        self._prompt.focus()

    async def _ensure_not_busy(self) -> bool:
        if not self._busy:
            return True
        self._set_status("请等待当前回答完成")
        return False

    # -- sessions and AGENTS.md -------------------------------------------

    def _bootstrap_session(self) -> None:
        if self._session_store is None:
            return
        try:
            self._session_info = self._session_store.create()
        except SessionError as error:
            self._add_message(f"创建会话失败：{error}", "error")
            self._session_info = None
            return
        self._ensure_agents_context()
        self._save_current_session(silent=True)

    def _ensure_agents_context(self) -> bool:
        loaded = agents_message(self.project_root, self.settings.agents_filename)
        if loaded is None:
            return False
        message, path = loaded
        inject = getattr(self.agent, "inject_project_context", None)
        if not callable(inject):
            return False
        try:
            injected = bool(inject(message))
        except Exception as error:
            self._add_message(f"注入 AGENTS.md 失败：{error}", "error")
            return False
        if not injected:
            return False

        if self._session_store is not None and self._session_info is not None:
            try:
                self._session_store.append_event(
                    self._session_info.id,
                    "agents_md",
                    {
                        "path": str(path),
                        "message": message,
                    },
                )
            except SessionError as error:
                self._add_message(f"持久化 AGENTS.md 事件失败：{error}", "error")
            self._save_current_session(silent=True)
        return True

    def _agent_has_project_context(self) -> bool:
        check = getattr(self.agent, "has_project_context", None)
        if callable(check):
            try:
                return bool(check())
            except Exception:
                return False
        return False

    def _agents_path(self) -> Path:
        path = Path(self.settings.agents_filename).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        return path

    def _agent_messages(self) -> list[dict[str, Any]]:
        messages = getattr(self.agent, "messages", ())
        if callable(messages):
            messages = messages()
        if not isinstance(messages, Sequence):
            return []
        return [
            dict(message)
            for message in messages
            if isinstance(message, dict) and message.get("role")
        ]

    def _save_current_session(self, silent: bool = False) -> SessionInfo | None:
        if self._session_store is None or self._session_info is None:
            if not silent:
                self._add_system("当前未启用会话持久化。")
            return None
        try:
            info = self._session_store.save(
                self._session_info.id,
                self._agent_messages(),
            )
        except SessionError as error:
            if not silent:
                self._add_message(f"保存会话失败：{error}", "error")
            return None
        self._session_info = info
        return info

    def _render_history(self, messages: Sequence[dict[str, Any]]) -> None:
        self._assistant = None
        self._assistant_parts = []
        if not messages:
            self._add_system("该会话没有可显示的消息。")
            return
        preview_limit = self.settings.tui_message_preview_chars
        for index, message in enumerate(messages):
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                text = content if isinstance(content, str) else ""
                if text.startswith(AGENTS_MARKER):
                    self._add_system("已恢复项目说明：AGENTS.md")
                elif index == 0:
                    self._add_system("系统提示已恢复。")
                elif text:
                    self._add_system(_preview_text(text, preview_limit))
            elif role == "user":
                text = content if isinstance(content, str) else ""
                if text:
                    self._add_message(f"你：{text}", "user")
            elif role == "assistant":
                text = content if isinstance(content, str) else ""
                if text:
                    self._add_message(text, "assistant")
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    name = function.get("name") if isinstance(function, dict) else ""
                    if name:
                        self._add_system(f"历史工具调用：{name}")
            elif role == "tool":
                text = content if isinstance(content, str) else ""
                if text:
                    self._add_message(
                        f"工具结果：\n{_preview_text(text, preview_limit)}",
                        "tool-result",
                    )
        self._scroll_to_end()

    def _resolve_project_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    # -- MCP loading and sidebar ------------------------------------------

    def _start_mcp_loading(self) -> None:
        if not self._load_mcp or self._mcp_started:
            self._refresh_mcp_panel()
            return
        self._mcp_started = True
        try:
            self._mcp_config = load_mcp_config(
                self.project_root,
                filename=self.settings.mcp_config_filename,
                default_timeout=self.settings.mcp_default_timeout,
            )
        except Exception as error:
            self._mcp_error = f"读取 MCP 配置失败：{error}"
            self._refresh_mcp_panel()
            self._add_message(self._mcp_error, "error")
            return
        self._pending_statuses = tuple(
            MCPServerStatus(
                name=server.name,
                transport=server.transport,
                connected=False,
                error="等待连接…",
            )
            for server in self._mcp_config.servers
        )
        self._mcp_loading = True
        self._refresh_mcp_panel()
        self._set_status("MCP 正在后台连接…")
        self._load_mcp_worker()

    @work(thread=True, exclusive=True, group="mcp")
    def _load_mcp_worker(self) -> None:
        try:
            result = load_mcp_into_agent(
                self.agent,
                self.settings,
                self.project_root,
                mcp_config=self._mcp_config,
            )
        except Exception as error:
            self._post(
                self._on_mcp_error,
                f"{type(error).__name__}: {error}",
            )
        else:
            self._post(self._on_mcp_loaded, result)

    def _on_mcp_loaded(self, result: MCPLoadResult) -> None:
        self._mcp_loading = False
        self._mcp_result = result
        self._mcp_statuses = result.statuses
        self._pending_statuses = ()
        self._mcp_error = ""
        refresh = getattr(self.agent, "refresh_context_usage", None)
        if callable(refresh):
            refresh()
        self._refresh_context_meter()
        self._refresh_mcp_panel()
        if result.warnings:
            self._add_message(
                "MCP 警告：\n"
                + "\n".join(f"- {warning}" for warning in result.warnings),
                "error",
            )
        if result.statuses:
            connected = result.connected_count
            self._add_system(
                f"MCP 后台加载完成：{connected}/{len(result.statuses)} "
                f"个服务器已连接，新增 {result.added_tools} 个工具。"
            )
        elif result.config_path is not None:
            self._add_system("MCP 后台加载完成：未配置 MCP Server。")
        if not self._busy:
            self._set_status("就绪")

    def _on_mcp_error(self, message: str) -> None:
        self._mcp_loading = False
        self._mcp_error = message
        self._pending_statuses = ()
        self._refresh_mcp_panel()
        self._add_message(f"MCP 加载失败：{message}", "error")
        if not self._busy:
            self._set_status("MCP 加载失败")

    def _all_mcp_statuses(self) -> tuple[MCPServerStatus, ...]:
        if self._mcp_statuses:
            return self._mcp_statuses
        return self._pending_statuses

    def _refresh_mcp_panel(self) -> None:
        text = Text()
        statuses = self._all_mcp_statuses()
        if self._mcp_loading:
            text.append("正在后台连接 MCP…\n\n", style="bold yellow")
        elif self._mcp_error:
            text.append("MCP 加载失败\n", style="bold red")
            text.append(self._mcp_error + "\n\n", style="red")
        elif not statuses:
            if not self._load_mcp:
                text.append("MCP 异步加载未启用\n\n", style="bold")
            else:
                text.append("未配置 MCP Server\n\n", style="bold")
            text.append(
                "在项目根目录创建 .mcp.json 后，"
                "连接状态与工具会显示在这里。",
                style="dim",
            )
            self._mcp_panel.update(text)
            return

        connected = sum(1 for item in statuses if item.connected)
        text.append(
            f"MCP 服务器 {connected}/{len(statuses)} 已连接\n\n",
            style="bold",
        )
        for status in statuses:
            if status.connected:
                text.append("● ", style="green")
                text.append(status.name, style="bold green")
            elif self._mcp_loading and status.error == "等待连接…":
                text.append("◌ ", style="yellow")
                text.append(status.name, style="bold yellow")
            else:
                text.append("○ ", style="red")
                text.append(status.name, style="bold red")
            text.append(f"\n   {status.transport}\n", style="dim")
            if status.connected:
                if status.tools:
                    for tool in status.tools:
                        text.append(f"   • {tool}\n")
                else:
                    text.append("   （无可用工具）\n", style="dim")
            elif status.error and status.error != "等待连接…":
                text.append(f"   {_preview_text(status.error, 180)}\n", style="red")
            text.append("\n")

        mcp_tool_count = sum(len(item.tools) for item in statuses)
        text.append(f"本地工具：{len(self.agent.tool_names)} 个\n", style="dim")
        text.append(f"MCP 工具：{mcp_tool_count} 个", style="dim")
        self._mcp_panel.update(text)

    # -- agent execution ---------------------------------------------------

    def _start_compact(self) -> None:
        self._prompt.value = ""
        self._hide_slash_palette()
        self._assistant = None
        self._assistant_parts = []
        self._add_message("你：/compact", "user")
        self._set_busy(True)
        self._set_status("正在压缩上下文…")
        self._run_compact()

    @work(thread=True, exclusive=True, group="agent")
    def _run_compact(self) -> None:
        """Run the single-LLM-call compression in a worker thread."""

        try:
            result = self.agent.compact()
        except Exception as error:
            self._post(self._on_compact_failure, f"{type(error).__name__}: {error}")
        else:
            self._post(self._on_compact_success, result)
        finally:
            self._post(self._finish_compaction_busy)

    @work(thread=True, exclusive=True, group="agent")
    def _run_agent(self, prompt: str) -> None:
        """Run the synchronous core agent in a worker thread."""

        try:
            for event in self.agent.run_turn(prompt):
                if isinstance(event, AssistantText):
                    self._post(self._on_text, event.text)
                elif isinstance(event, ToolCallStarted):
                    self._post(self._on_tool_started, event.name, event.arguments)
                elif isinstance(event, ToolCallFinished):
                    self._post(self._on_tool_finished, event.result)
                elif isinstance(event, ContextUsageUpdated):
                    self._post(self._on_context_usage, event)
                elif isinstance(event, TurnCompleted):
                    self._post(self._on_complete, event.content)
                elif isinstance(event, TurnFailed):
                    self._post(self._on_failure, event.message)
        except Exception as error:  # Last-resort UI safety net.
            self._post(self._on_failure, f"{type(error).__name__}: {error}")
        finally:
            self._post(self._set_busy, False)

    def _post(self, callback: Callable[..., Any], *args: Any) -> None:
        """Schedule a UI update from the worker thread."""

        if self._closing or not self.is_running:
            return
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            # The app can be shutting down while a worker is still finishing.
            pass

    def _on_text(self, text: str) -> None:
        if self._assistant is None:
            self._assistant = self._add_message("", "assistant")
            self._assistant_parts = []
        self._assistant_parts.append(text)
        self._assistant.update(Text("".join(self._assistant_parts)))
        self._scroll_to_end()
        self._set_status("正在回答…")

    def _on_tool_started(self, name: str, arguments: str) -> None:
        self._assistant = None
        self._assistant_parts = []
        preview = self._truncate_text(
            arguments,
            min(300, self.settings.tui_message_preview_chars),
        )
        self._add_message(f"调用工具：{name}\n参数：{preview}", "tool")
        self._set_status(f"正在执行工具：{name}…")

    def _on_tool_finished(self, result: ToolResult) -> None:
        preview = self._truncate_text(
            result.content,
            self.settings.tui_message_preview_chars,
        )
        label = "结果" if result.ok else "工具错误"
        css_class = "tool-result" if result.ok else "error"
        self._add_message(f"{label}：\n{preview}", css_class)
        self._set_status("正在整理结果…")

    def _on_context_usage(self, event: ContextUsageUpdated) -> None:
        usage = ContextUsage(
            used_tokens=event.used_tokens,
            window_tokens=event.window_tokens,
            is_default_window=event.is_default_window,
            estimated=event.estimated,
            warning_ratio=event.warning_ratio,
        )
        self._refresh_context_meter(usage)
        ratio_text = f"{event.warning_ratio * 100:.0f}%"
        if event.needs_compaction:
            if not self._context_warning_shown:
                self._add_message(
                    f"警告：当前上下文已使用 {event.percent:.1f}%，"
                    f"达到模型窗口的 {ratio_text}。普通提示词已暂停，"
                    "请输入 /compact 压缩上下文。",
                    "warning",
                )
                self._context_warning_shown = True
            self._set_status(f"上下文已达 {ratio_text}，请输入 /compact")
        else:
            self._context_warning_shown = False
        self._update_prompt_hint()

    def _on_compact_success(self, result: CompactResult) -> None:
        self._context_warning_shown = False
        self._refresh_context_meter(result.usage)
        self._add_system(
            "上下文压缩完成。\n"
            f"归档编号：{result.archive_id}\n"
            f"归档文件：{result.archive_path}\n"
            "当前会话已替换为压缩摘要，可以继续输入。"
        )
        self._save_current_session(silent=True)
        self._set_status("压缩完成")
        self._update_prompt_hint()
        self.push_screen(
            CompactPreviewScreen(result.before_text, result.after_text)
        )

    def _on_compact_failure(self, message: str) -> None:
        self._add_message(
            f"上下文压缩失败：{message}\n"
            "原有上下文保持不变，可以再次输入 /compact 重试。",
            "error",
        )
        self._assistant = None
        self._assistant_parts = []
        self._set_status("压缩失败")
        self._update_prompt_hint()

    def _finish_compaction_busy(self) -> None:
        self._busy = False
        self._prompt.disabled = False
        self._send.disabled = False
        self._update_prompt_hint()
        if len(self.screen_stack) <= 1:
            self._prompt.focus()

    def _on_complete(self, content: str) -> None:
        if self._assistant is None:
            if content:
                self._add_message(content, "assistant")
            else:
                self._add_system("（模型没有返回文本）")
        elif not self._assistant_parts:
            self._assistant.update(Text("（模型没有返回文本）"))
        self._assistant = None
        self._assistant_parts = []
        self._save_current_session(silent=True)
        ratio_text = f"{self._warning_percent():.0f}%"
        if self._agent_needs_compaction():
            self._set_status(f"上下文已达 {ratio_text}，请输入 /compact")
            self._update_prompt_hint()
        else:
            self._set_status("就绪")

    def _on_failure(self, message: str) -> None:
        self._add_message(f"请求失败：{message}", "error")
        self._assistant = None
        self._assistant_parts = []
        self._save_current_session(silent=True)
        self._set_status("请求失败")

    # -- widgets and status ------------------------------------------------

    def _add_message(self, text: str, css_class: str) -> Static:
        widget = Static(Text(text), classes=f"message {css_class}")
        self._transcript.mount(widget)
        self._scroll_to_end()
        return widget

    def _add_system(self, text: str) -> None:
        self._add_message(text, "system")

    def _set_status(self, text: str) -> None:
        self._status.update(Text(text))

    def _refresh_context_meter(self, usage: ContextUsage | None = None) -> None:
        if usage is None:
            usage = getattr(self.agent, "context_usage", None)
        if not isinstance(usage, ContextUsage):
            usage = ContextUsage(
                used_tokens=0,
                window_tokens=128_000,
                is_default_window=True,
                estimated=True,
                warning_ratio=self._warning_ratio(),
            )

        used_k = usage.used_tokens / 1000
        window_k = usage.window_tokens / 1000
        window_text = f"{window_k:g}k"
        if usage.is_default_window:
            window_text += "（默认）"
        text = (
            f"上下文：{used_k:.1f}k / {window_text}"
            f" · 已占用 {usage.percent:.1f}%"
        )
        if usage.estimated:
            text += " · Token 为估算值"
        if usage.needs_compaction:
            text += " · 请执行 /compact"
        self._context_meter.update(Text(text))
        self._context_meter.set_class(usage.needs_compaction, "warning")
        critical_ratio = min(1.0, usage.warning_ratio + 0.25)
        self._context_meter.set_class(
            usage.percent >= critical_ratio * 100.0,
            "critical",
        )

    def _warning_ratio(self) -> float:
        value = getattr(self.agent, "context_warning_ratio", 0.60)
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            return 0.60
        return ratio if 0 < ratio <= 1 else 0.60

    def _warning_percent(self) -> float:
        return self._warning_ratio() * 100.0

    def _agent_needs_compaction(self) -> bool:
        return bool(getattr(self.agent, "needs_compaction", False))

    def _update_prompt_hint(self) -> None:
        if self._busy:
            self._prompt.placeholder = "正在处理…"
        elif self._agent_needs_compaction():
            self._prompt.placeholder = (
                f"上下文已达 {self._warning_percent():.0f}%，"
                "请输入 /compact"
            )
        else:
            self._prompt.placeholder = "输入问题，Enter 发送；/ 查看命令"

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._prompt.disabled = busy
        self._send.disabled = busy
        self._update_prompt_hint()
        if not busy:
            self._prompt.focus()

    def _scroll_to_end(self) -> None:
        self.call_after_refresh(self._transcript.scroll_end, animate=False)

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n…（已截断）"

    async def action_clear(self) -> None:
        if self._busy:
            self._set_status("请等待当前回答完成后再清空")
            return
        await self._transcript.remove_children()
        reset = getattr(self.agent, "reset", None)
        if callable(reset):
            reset()
        self._ensure_agents_context()
        self._save_current_session(silent=True)
        self._assistant = None
        self._assistant_parts = []
        self._context_warning_shown = False
        self._refresh_context_meter()
        self._add_system("会话已清空。")
        self._set_status("就绪")
        self._update_prompt_hint()
        self._prompt.focus()

    async def action_quit(self) -> None:
        self._save_current_session(silent=True)
        self.exit()


def _preview_text(text: str, limit: int) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "\n…（已截断）"


__all__ = [
    "CompactPreviewScreen",
    "DAgentApp",
    "SLASH_COMMANDS",
    "SlashCommand",
]
