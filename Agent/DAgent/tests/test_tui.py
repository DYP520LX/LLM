from __future__ import annotations

import unittest

from textual.widgets import Input

from core.config import Settings
from core.events import (
    AssistantText,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
)
from core.types import ToolResult
from tui.app import DAgentApp


class FakeAgent:
    def __init__(self) -> None:
        self.tool_names = ("fake_tool",)
        self.prompts: list[str] = []

    def run_turn(self, user_input: str):
        self.prompts.append(user_input)
        yield AssistantText("你好")
        yield AssistantText("，世界")
        yield ToolCallStarted("fake_tool", "{}")
        yield ToolCallFinished(ToolResult("fake_tool", "工具结果", True))
        yield AssistantText("最终回答")
        yield TurnCompleted("最终回答")

    def reset(self) -> None:
        self.prompts.clear()


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
