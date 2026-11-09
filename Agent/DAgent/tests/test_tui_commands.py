from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input, Static

from core.agent import Agent
from core.bootstrap import MCPLoadResult
from core.config import Settings
from core.mcp_client import MCPServerStatus
from core.sessions import SessionStore
from core.tools import build_registry
from tui.app import DAgentApp
from tui.screens import ConfigCreatedApp, SessionListScreen


class TuiCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = SessionStore(self.root / "sessions")
        self.settings = Settings(
            base_url="http://localhost",
            api_key="test",
            model="fake",
            stream=True,
        )
        self.agent = Agent(
            object(),  # No LLM call is made by these tests.
            build_registry(),
            system_prompt="基础系统提示",
            context_archive_dir=self.root / ".agents" / "context",
        )

    def tearDown(self) -> None:
        try:
            self.agent.close()
        finally:
            self._temp.cleanup()

    async def _wait_for(self, pilot, predicate, timeout: float = 5.0) -> None:
        for _ in range(int(timeout / 0.1)):
            await pilot.pause(0.1)
            if predicate():
                return
        self.fail("等待 TUI 状态超时")

    async def test_startup_injects_agents_md_and_persists_event(self) -> None:
        (self.root / "AGENTS.md").write_text(
            "必须遵守项目约束。",
            encoding="utf-8",
        )
        app = DAgentApp(
            self.agent,
            self.settings,
            project_root=self.root,
            session_store=self.store,
        )
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.2)

            self.assertTrue(self.agent.has_project_context())
            self.assertIn("必须遵守项目约束。", self.agent.messages[1]["content"])
            sessions = self.store.list_sessions()
            self.assertEqual(len(sessions), 1)
            events = (
                self.root
                / "sessions"
                / sessions[0].id
                / "events.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn('"type": "agents_md"', events)
            self.assertIn("必须遵守项目约束。", events)

    async def test_slash_palette_filters_and_enter_executes_selected(self) -> None:
        app = DAgentApp(
            self.agent,
            self.settings,
            project_root=self.root,
            session_store=self.store,
        )
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/s"
            await pilot.pause(0.1)

            self.assertTrue(app._slash_visible)  # type: ignore[attr-defined]
            self.assertEqual(
                [command.name for command in app._slash_items],  # type: ignore[attr-defined]
                ["resume", "save", "status"],
            )
            self.assertNotIn(
                "hidden",
                app.query_one("#slash-palette").classes,
            )

            await pilot.press("down")
            await pilot.pause(0.1)
            self.assertEqual(app._slash_index, 1)  # type: ignore[attr-defined]
            await pilot.press("enter")
            await pilot.pause(0.2)

            messages = " ".join(
                str(widget.content) for widget in app.query(".message")
            )
            self.assertIn("会话已保存", messages)
            self.assertFalse(app._slash_visible)  # type: ignore[attr-defined]

    async def test_new_saves_previous_session_and_resets_history(self) -> None:
        app = DAgentApp(
            self.agent,
            self.settings,
            project_root=self.root,
            session_store=self.store,
        )
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.2)
            old_id = app._session_info.id  # type: ignore[attr-defined]
            self.agent.replace_messages(
                [
                    {"role": "system", "content": "基础系统提示"},
                    {"role": "user", "content": "旧会话问题"},
                    {"role": "assistant", "content": "旧会话回答"},
                ]
            )

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/new"
            await pilot.press("enter")
            await pilot.pause(0.2)

            new_id = app._session_info.id  # type: ignore[attr-defined]
            self.assertNotEqual(old_id, new_id)
            saved = self.store.load(old_id)
            self.assertTrue(
                any(
                    message.get("content") == "旧会话问题"
                    for message in saved
                )
            )
            self.assertFalse(
                any(
                    message.get("content") == "旧会话问题"
                    for message in self.agent.messages
                )
            )

    async def test_resume_screen_selects_with_arrow_and_restores_history(self) -> None:
        first = self.store.create()
        self.store.save(
            first.id,
            [
                {"role": "system", "content": "基础系统提示"},
                {"role": "user", "content": "需要恢复的问题"},
                {"role": "assistant", "content": "需要恢复的回答"},
            ],
        )
        meta_path = self.root / "sessions" / first.id / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["updated_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        app = DAgentApp(
            self.agent,
            self.settings,
            project_root=self.root,
            session_store=self.store,
        )
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/resume"
            await pilot.press("enter")
            await pilot.pause(0.2)

            self.assertIsInstance(app.screen, SessionListScreen)
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, SessionListScreen)

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/resume"
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, SessionListScreen)
            # The newly created startup session is first; move to the saved one.
            await pilot.press("down")
            await pilot.press("enter")
            await self._wait_for(
                pilot,
                lambda: any(
                    message.get("content") == "需要恢复的问题"
                    for message in self.agent.messages
                ),
            )

            self.assertTrue(
                any(
                    message.get("content") == "需要恢复的回答"
                    for message in self.agent.messages
                )
            )
            messages = " ".join(
                str(widget.content) for widget in app.query(".message")
            )
            self.assertIn("需要恢复的问题", messages)

    async def test_mcp_sidebar_refreshes_with_connected_tools(self) -> None:
        app = DAgentApp(self.agent, self.settings)
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.2)
            app._on_mcp_loaded(  # type: ignore[attr-defined]
                MCPLoadResult(
                    manager=None,
                    statuses=(
                        MCPServerStatus(
                            name="PetHospitalMCP",
                            transport="streamable_http",
                            connected=True,
                            tools=("list_pets", "add_pet"),
                        ),
                        MCPServerStatus(
                            name="AnythingLLMMCP",
                            transport="streamable_http",
                            connected=False,
                            error="连接超时",
                        ),
                    ),
                    added_tools=2,
                )
            )
            await pilot.pause(0.2)

            panel = app.query_one("#mcp-panel", Static)
            text = str(panel.content)
            self.assertIn("PetHospitalMCP", text)
            self.assertIn("list_pets", text)
            self.assertIn("AnythingLLMMCP", text)
            self.assertIn("连接超时", text)
            self.assertIn("1/2", text)

    async def test_mcp_loading_worker_refreshes_sidebar_after_startup(self) -> None:
        (self.root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "FakeMCP": {
                            "type": "streamable_http",
                            "url": "http://127.0.0.1:1/mcp",
                            "timeout": 1,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = MCPLoadResult(
            manager=None,
            statuses=(
                MCPServerStatus(
                    name="FakeMCP",
                    transport="streamable_http",
                    connected=True,
                    tools=("fake_tool",),
                ),
            ),
            added_tools=1,
        )
        app = DAgentApp(
            self.agent,
            self.settings,
            project_root=self.root,
            session_store=self.store,
            load_mcp=True,
        )
        with patch("tui.app.load_mcp_into_agent", return_value=result):
            async with app.run_test(size=(110, 34)) as pilot:
                await self._wait_for(
                    pilot,
                    lambda: bool(app._mcp_statuses),  # type: ignore[attr-defined]
                )
                panel = app.query_one("#mcp-panel", Static)
                text = str(panel.content)
                self.assertIn("FakeMCP", text)
                self.assertIn("fake_tool", text)
                self.assertFalse(app._mcp_loading)  # type: ignore[attr-defined]

    async def test_config_created_dialog_shows_path_and_exits(self) -> None:
        config_path = self.root / "config.ini"
        app = ConfigCreatedApp(config_path)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause(0.1)
            text = str(app.query_one("#setup-text", Static).content)
            self.assertIn(str(config_path), text)
            self.assertIn("重新启动", text)
            await pilot.click("#setup-exit")


if __name__ == "__main__":
    unittest.main()
