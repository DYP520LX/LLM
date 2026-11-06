"""Modal screens and the first-run configuration dialog."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, ListItem, ListView, Static

from core.sessions import SessionInfo


class SessionListScreen(ModalScreen[SessionInfo | None]):
    """Resume panel: Up/Down highlights, Enter restores, Esc cancels."""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, sessions: list[SessionInfo]) -> None:
        super().__init__()
        self._sessions = sessions
        self._list: ListView

    def compose(self) -> ComposeResult:
        with Container(id="session-dialog"):
            yield Static("历史会话", id="session-title")
            yield Static(
                "↑/↓ 选择 · Enter 恢复 · Esc 取消",
                id="session-hint",
            )
            yield ListView(
                *[
                    ListItem(Static(self._label(session)))
                    for session in self._sessions
                ],
                id="session-list",
            )
            with Horizontal(id="session-actions"):
                yield Button("取消", id="session-cancel")
        return

    def on_mount(self) -> None:
        self._list = self.query_one("#session-list", ListView)
        if self._sessions:
            self._list.index = 0
        self._list.focus()

    @staticmethod
    def _label(session: SessionInfo) -> Text:
        label = Text()
        label.append(session.title or "新会话", style="bold")
        label.append(
            f"\n{session.updated_at} · {session.message_count} 条消息"
            f" · {session.id}",
            style="dim",
        )
        if session.preview:
            label.append(f"\n{session.preview}", style="italic")
        return label

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if 0 <= event.index < len(self._sessions):
            self.dismiss(self._sessions[event.index])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "session-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigCreatedApp(App[None]):
    """Small dialog shown after creating a mock ``config.ini`` on first run."""

    CSS_PATH = "styles.tcss"
    TITLE = "DAgent 首次配置"
    BINDINGS = [("ctrl+q", "quit", "退出")]

    def __init__(self, config_path: str | Path) -> None:
        super().__init__()
        self.config_path = Path(config_path)

    def compose(self) -> ComposeResult:
        with Container(id="setup-host"):
            with Container(id="setup-dialog"):
                yield Static("已创建配置文件", id="setup-title")
                yield Static(
                    Text(
                        "DAgent 在当前目录没有找到 config.ini，"
                        "已生成一份完整的 mock 配置：\n\n"
                        f"{self.config_path}\n\n"
                        "请修改 [llm] 段中的 base_url、api_key、model "
                        "等字段，然后重新启动 DAgent。\n\n"
                        "在配置完成前，当前 mock 配置不会连接真实的模型服务。"
                    ),
                    id="setup-text",
                )
                yield Button("退出", id="setup-exit", variant="primary")
        return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "setup-exit":
            self.exit()

    def action_quit(self) -> None:
        self.exit()


__all__ = ["ConfigCreatedApp", "SessionListScreen"]
