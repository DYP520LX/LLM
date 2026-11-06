"""Textual TUI for DAgent.

This module is intentionally a presentation layer only: it renders
``core.events.AgentEvent`` values and never talks to the LLM or tools directly.
"""

from __future__ import annotations

from typing import Any, Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Static

from core.agent import Agent
from core.config import Settings
from core.events import (
    AssistantText,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
)
from core.types import ToolResult


class DAgentApp(App[None]):
    """A chat interface with streaming output and tool activity."""

    CSS_PATH = "styles.tcss"
    TITLE = "DAgent"
    SUB_TITLE = "本地工具型 AI 助手"
    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+l", "clear", "清空会话"),
    ]

    def __init__(self, agent: Agent, settings: Settings) -> None:
        super().__init__()
        self.agent = agent
        self.settings = settings
        self._busy = False
        self._assistant: Static | None = None
        self._assistant_parts: list[str] = []

        self._transcript: VerticalScroll
        self._status: Static
        self._prompt: Input
        self._send: Button

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="transcript")
        yield Static("正在启动…", id="status")
        with Horizontal(id="composer"):
            yield Input(placeholder="输入问题，Enter 发送", id="prompt")
            yield Button("发送", id="send", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._transcript = self.query_one("#transcript", VerticalScroll)
        self._status = self.query_one("#status", Static)
        self._prompt = self.query_one("#prompt", Input)
        self._send = self.query_one("#send", Button)

        stream_state = "开" if self.settings.stream else "关"
        self._add_system(
            "DAgent 已启动\n"
            f"模型：{self.settings.model}    Stream：{stream_state}    "
            f"工具：{len(self.agent.tool_names)} 个\n"
            "输入问题后按 Enter 发送；Ctrl+L 清空会话，Ctrl+Q 退出。"
        )
        if getattr(self.agent, "startup_notes", ()):
            notes = "\n".join(
                f"- {note}" for note in self.agent.startup_notes
            )
            self._add_message(f"启动警告：\n{notes}", "error")
        self._set_status("就绪")
        self._prompt.focus()

    def on_unmount(self) -> None:
        close = getattr(self.agent, "close", None)
        if close is not None:
            close()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self._submit()

    def _submit(self) -> None:
        if self._busy:
            return
        text = self._prompt.value.strip()
        if not text:
            return

        self._prompt.value = ""
        self._assistant = None
        self._assistant_parts = []
        self._add_message(f"你：{text}", "user")
        self._set_busy(True)
        self._set_status("正在思考…")
        self._run_agent(text)

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

        if not self.is_running:
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
        preview = arguments if len(arguments) <= 300 else arguments[:300] + "…"
        self._add_message(f"调用工具：{name}\n参数：{preview}", "tool")
        self._set_status(f"正在执行工具：{name}…")

    def _on_tool_finished(self, result: ToolResult) -> None:
        preview = result.content
        if len(preview) > 1200:
            preview = preview[:1200] + "\n…（已截断）"
        label = "结果" if result.ok else "工具错误"
        css_class = "tool-result" if result.ok else "error"
        self._add_message(f"{label}：\n{preview}", css_class)
        self._set_status("正在整理结果…")

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
        self._set_status("就绪")

    def _on_failure(self, message: str) -> None:
        self._add_message(f"请求失败：{message}", "error")
        self._assistant = None
        self._assistant_parts = []
        self._set_status("请求失败")

    def _add_message(self, text: str, css_class: str) -> Static:
        widget = Static(Text(text), classes=f"message {css_class}")
        self._transcript.mount(widget)
        self._scroll_to_end()
        return widget

    def _add_system(self, text: str) -> None:
        self._add_message(text, "system")

    def _set_status(self, text: str) -> None:
        self._status.update(Text(text))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._prompt.disabled = busy
        self._send.disabled = busy
        if not busy:
            self._prompt.focus()

    def _scroll_to_end(self) -> None:
        self.call_after_refresh(self._transcript.scroll_end, animate=False)

    async def action_clear(self) -> None:
        if self._busy:
            self._set_status("请等待当前回答完成后再清空")
            return
        await self._transcript.remove_children()
        self.agent.reset()
        self._assistant = None
        self._assistant_parts = []
        self._add_system("会话已清空。")
        self._set_status("就绪")
        self._prompt.focus()
