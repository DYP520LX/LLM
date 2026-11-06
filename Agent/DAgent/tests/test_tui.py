from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textual.widgets import Input, Static

from core.config import Settings
from core.context import CompactResult, ContextUsage
from core.events import (
    AssistantText,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
)
from core.types import ToolResult
from tui.app import CompactPreviewScreen, DAgentApp


class FakeAgent:
    def __init__(self) -> None:
        self.tool_names = ("fake_tool",)
        self.startup_notes = ()
        self.prompts: list[str] = []
        self.context_usage = ContextUsage(
            used_tokens=0,
            window_tokens=128_000,
            is_default_window=True,
            estimated=True,
        )
        self.needs_compaction = False
        self.compact_calls = 0
        self.compact_result = CompactResult(
            archive_id="20261022-153000",
            archive_path=Path(tempfile.gettempdir()) / "20261022-153000.jsonl",
            summary="压缩后的工作摘要",
            before_text="压缩前的原始上下文",
            after_text="压缩后的工作摘要\n\n> 归档说明",
            usage=ContextUsage(
                used_tokens=5_000,
                window_tokens=128_000,
                is_default_window=True,
                estimated=True,
            ),
        )

    def run_turn(self, user_input: str):
        self.prompts.append(user_input)
        yield AssistantText("你好")
        yield AssistantText("，世界")
        yield ToolCallStarted("fake_tool", "{}")
        yield ToolCallFinished(ToolResult("fake_tool", "工具结果", True))
        yield AssistantText("最终回答")
        yield TurnCompleted("最终回答")

    def compact(self) -> CompactResult:
        self.compact_calls += 1
        return self.compact_result

    def reset(self) -> None:
        self.prompts.clear()
        self.needs_compaction = False
        self.context_usage = ContextUsage(
            used_tokens=0,
            window_tokens=128_000,
            is_default_window=True,
            estimated=True,
        )


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_and_streaming_turn(self) -> None:
        fake_agent = FakeAgent()
        app = DAgentApp(
            fake_agent,  # type: ignore[arg-type]
            Settings(
                base_url="http://localhost",
                api_key="test",
                model="fake",
                stream=True,
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)
            self.assertIsNotNone(prompt)

            prompt.value = "测试输入"
            await pilot.press("enter")

            for _ in range(50):
                await pilot.pause(0.1)
                if not app._busy:  # type: ignore[attr-defined]
                    break
            await pilot.pause(0.1)

            self.assertEqual(fake_agent.prompts, ["测试输入"])
            messages = " ".join(
                str(widget.content) for widget in app.query(".message")
            )
            self.assertIn("最终回答", messages)
            self.assertIn("fake_tool", messages)
            self.assertIn("工具结果", messages)

    async def test_context_meter_shows_default_window_and_percentage(self) -> None:
        fake_agent = FakeAgent()
        fake_agent.context_usage = ContextUsage(
            used_tokens=64_000,
            window_tokens=128_000,
            is_default_window=True,
            estimated=False,
        )
        app = DAgentApp(
            fake_agent,  # type: ignore[arg-type]
            Settings(
                base_url="http://localhost",
                api_key="test",
                model="fake",
                stream=True,
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            meter = app.query_one("#context-meter", Static)
            text = str(meter.content)
            self.assertIn("64.0k", text)
            self.assertIn("128k", text)
            self.assertIn("默认", text)
            self.assertIn("50.0%", text)

    async def test_compact_command_opens_preview_and_close_button(self) -> None:
        fake_agent = FakeAgent()
        app = DAgentApp(
            fake_agent,  # type: ignore[arg-type]
            Settings(
                base_url="http://localhost",
                api_key="test",
                model="fake",
                stream=True,
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/compact"
            await pilot.press("enter")

            for _ in range(50):
                await pilot.pause(0.1)
                if not app._busy and isinstance(app.screen, CompactPreviewScreen):
                    break
            await pilot.pause(0.1)

            self.assertEqual(fake_agent.compact_calls, 1)
            self.assertIsInstance(app.screen, CompactPreviewScreen)
            before = app.screen.query_one("#compact-before", Static)
            after = app.screen.query_one("#compact-after", Static)
            self.assertIn("压缩前的原始上下文", str(before.content))
            self.assertIn("压缩后的工作摘要", str(after.content))

            await pilot.click("#compact-close")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, CompactPreviewScreen)

    async def test_threshold_pauses_normal_prompts(self) -> None:
        fake_agent = FakeAgent()
        fake_agent.needs_compaction = True
        fake_agent.context_usage = ContextUsage(
            used_tokens=80_000,
            window_tokens=128_000,
            is_default_window=True,
            estimated=False,
        )
        app = DAgentApp(
            fake_agent,  # type: ignore[arg-type]
            Settings(
                base_url="http://localhost",
                api_key="test",
                model="fake",
                stream=True,
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)
            prompt.value = "普通问题"
            await pilot.press("enter")
            await pilot.pause(0.1)

            self.assertEqual(fake_agent.prompts, [])
            messages = " ".join(
                str(widget.content) for widget in app.query(".message")
            )
            self.assertIn("普通提示词已暂停", messages)
            self.assertIn("/compact", messages)

    async def test_clear_action(self) -> None:
        app = DAgentApp(
            FakeAgent(),  # type: ignore[arg-type]
            Settings(
                base_url="http://localhost",
                api_key="test",
                model="fake",
                stream=True,
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+l")
            await pilot.pause()
            messages = [str(widget.content) for widget in app.query(".message")]
            self.assertEqual(len(messages), 1)
            self.assertIn("会话已清空", messages[0])


if __name__ == "__main__":
    unittest.main()
