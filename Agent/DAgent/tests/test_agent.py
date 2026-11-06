from __future__ import annotations

import tempfile
import unittest

from core.agent import Agent
from core.bootstrap import create_agent
from core.config import Settings
from core.errors import LLMError
from core.events import (
    AssistantText,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
)
from core.llm import LLMTextDelta, LLMTurnComplete
from core.tools import build_registry
from core.types import ToolCall


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def stream(self, messages, tools):
        self.calls.append((list(messages), list(tools)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        text, tool_calls = response
        if text:
            yield LLMTextDelta(text)
        yield LLMTurnComplete(text, tool_calls)


class AgentTests(unittest.TestCase):
    def test_tool_call_loop_and_history(self) -> None:
        llm = FakeLLM(
            [
                ("", [ToolCall("call_1", "is_leap_year", '{"year": 2024}')]),
                ("2024 年是闰年。", []),
            ]
        )
        agent = Agent(llm, build_registry())

        events = list(agent.run_turn("2024 年是闰年吗？"))

        self.assertTrue(any(isinstance(event, ToolCallStarted) for event in events))
        self.assertTrue(any(isinstance(event, ToolCallFinished) for event in events))
        self.assertIsInstance(events[-1], TurnCompleted)
        self.assertEqual(events[-1].content, "2024 年是闰年。")
        self.assertEqual(len(llm.calls), 2)

        first_messages, first_tools = llm.calls[0]
        self.assertEqual(len(first_tools), 16)
        self.assertEqual(first_messages[-1]["role"], "user")

        second_messages, _ = llm.calls[1]
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertIn("2024 年是闰年", second_messages[-1]["content"])

        self.assertEqual(
            [message["role"] for message in agent.messages],
            ["user", "assistant", "tool", "assistant"],
        )

    def test_streaming_text_events(self) -> None:
        llm = FakeLLM([("你好，我是 DAgent。", [])])
        agent = Agent(llm, build_registry())

        events = list(agent.run_turn("你好"))

        texts = [event.text for event in events if isinstance(event, AssistantText)]
        self.assertEqual(texts, ["你好，我是 DAgent。"])
        self.assertEqual(events[-1].content, "你好，我是 DAgent。")

    def test_llm_error_becomes_turn_failed(self) -> None:
        llm = FakeLLM([LLMError("网络错误")])
        agent = Agent(llm, build_registry())

        events = list(agent.run_turn("你好"))

        self.assertIsInstance(events[-1], TurnFailed)
        self.assertIn("网络错误", events[-1].message)

    def test_tool_round_limit(self) -> None:
        llm = FakeLLM(
            [
                ("", [ToolCall("call_1", "is_leap_year", '{"year": 2024}')]),
                ("", [ToolCall("call_2", "is_leap_year", '{"year": 2024}')]),
            ]
        )
        agent = Agent(llm, build_registry(), max_tool_rounds=2)

        events = list(agent.run_turn("一直调用工具"))

        self.assertIsInstance(events[-1], TurnFailed)
        self.assertIn("超过 2 轮", events[-1].message)
        self.assertEqual(len(llm.calls), 2)

    def test_system_prompt_is_sent_first_and_survives_reset(self) -> None:
        llm = FakeLLM([("好的", [])])
        agent = Agent(llm, build_registry(), system_prompt="你是测试助手。")

        list(agent.run_turn("你好"))

        first_messages, _ = llm.calls[0]
        self.assertEqual(first_messages[0], {"role": "system", "content": "你是测试助手。"})
        self.assertEqual(agent.messages[0]["role"], "system")

        agent.reset()

        self.assertEqual(
            agent.messages, [{"role": "system", "content": "你是测试助手。"}]
        )

    def test_bootstrap_installs_default_system_prompt(self) -> None:
        settings = Settings(
            base_url="http://example.invalid", api_key="test", model="test"
        )

        with tempfile.TemporaryDirectory() as project_root:
            agent = create_agent(settings, project_root=project_root)
            try:
                self.assertEqual(agent.messages[0]["role"], "system")
                self.assertIn("工具", agent.messages[0]["content"])
                self.assertEqual(agent.startup_notes, ())
            finally:
                agent.close()

    def test_empty_reply_is_retried_once(self) -> None:
        llm = FakeLLM([("", []), ("这是最终回答。", [])])
        agent = Agent(llm, build_registry())

        events = list(agent.run_turn("你好"))

        self.assertIsInstance(events[-1], TurnCompleted)
        self.assertEqual(events[-1].content, "这是最终回答。")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[1][0][-1]["role"], "system")

    def test_repeated_empty_reply_becomes_failure(self) -> None:
        llm = FakeLLM([("", []), ("", [])])
        agent = Agent(llm, build_registry())

        events = list(agent.run_turn("你好"))

        self.assertIsInstance(events[-1], TurnFailed)
        self.assertIn("空回复", events[-1].message)
        self.assertEqual(len(llm.calls), 2)

    def test_reset_clears_history(self) -> None:
        llm = FakeLLM([("好的", [])])
        agent = Agent(llm, build_registry())
        list(agent.run_turn("你好"))
        self.assertEqual(len(agent.messages), 2)

        agent.reset()

        self.assertEqual(agent.messages, [])


if __name__ == "__main__":
    unittest.main()
