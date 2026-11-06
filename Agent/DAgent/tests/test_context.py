from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.agent import Agent
from core.context import (
    COMPACTION_PROMPT,
    ContextUsage,
    archive_note,
    build_compaction_messages,
    estimate_tokens,
    recall_context,
    save_context_archive,
)
from core.errors import ContextCompactionError, LLMError
from core.events import ContextUsageUpdated, TurnFailed
from core.llm import LLMTextDelta, LLMTurnComplete, TokenUsage
from core.tools import build_registry
from core.types import ToolCall


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def stream(self, messages, tools):
        self.calls.append((copy.deepcopy(list(messages)), list(tools)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if len(response) == 2:
            text, tool_calls = response
            usage = None
        else:
            text, tool_calls, usage = response
        if text:
            yield LLMTextDelta(text)
        yield LLMTurnComplete(text, tool_calls, usage)


def _sample_messages():
    return [
        {"role": "system", "content": "你是测试助手。"},
        {"role": "user", "content": "请查询 2024 年是不是闰年。"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "is_leap_year",
                        "arguments": '{"year": 2024}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "2024 年是闰年。",
        },
    ]


class ContextUtilityTests(unittest.TestCase):
    def test_context_usage_threshold(self) -> None:
        usage = ContextUsage(used_tokens=60, window_tokens=100)
        self.assertAlmostEqual(usage.percent, 60.0)
        self.assertTrue(usage.needs_compaction)

        usage = ContextUsage(used_tokens=59, window_tokens=100)
        self.assertFalse(usage.needs_compaction)

    def test_token_estimate_treats_chinese_as_denser(self) -> None:
        self.assertGreater(estimate_tokens("你好世界"), estimate_tokens("abcd"))

    def test_compaction_prompt_contains_archive_note_hint(self) -> None:
        request = build_compaction_messages(_sample_messages())
        self.assertEqual(request[0]["content"], COMPACTION_PROMPT)
        self.assertIn("2024 年是不是闰年", request[1]["content"])
        self.assertIn("is_leap_year", request[1]["content"])

    def test_archive_note_matches_required_format(self) -> None:
        note = archive_note("20261022-153000", datetime(2026, 10, 22, 15, 30, 0))
        self.assertEqual(
            note,
            "> 此前上下文已于 2026-10-22 15:30:00 完成压缩，"
            "原始上下文保存在编号为 20261022-153000 的归档文件中。"
            "如需查找历史信息，请使用 recallContext 工具。",
        )


class ArchiveTests(unittest.TestCase):
    def test_archive_preserves_messages_and_generates_unique_ids(self) -> None:
        messages = _sample_messages()
        now = datetime(2026, 10, 22, 15, 30, 0)
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            directory = Path(temp)
            archive_id, path, completed_at = save_context_archive(
                directory, messages, now=now
            )
            second_id, second_path, _ = save_context_archive(
                directory, messages, now=now
            )

            self.assertEqual(archive_id, "20261022-153000")
            self.assertEqual(second_id, "20261022-153000-01")
            self.assertTrue(path.is_file())
            self.assertTrue(second_path.is_file())
            self.assertEqual(completed_at, now)
            self.assertEqual(path.name, "20261022-153000.jsonl")

            archived = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(archived, messages)

    def test_recall_searches_all_archives_and_returns_location(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            directory = Path(temp)
            save_context_archive(
                directory,
                _sample_messages(),
                now=datetime(2026, 10, 22, 15, 30, 0),
            )
            save_context_archive(
                directory,
                [{"role": "user", "content": "另一段包含关键词：苹果"}],
                now=datetime(2026, 10, 23, 9, 0, 0),
            )

            result = recall_context(directory, "闰年")

            self.assertIn("20261022-153000", result)
            self.assertIn("第 2 行", result)
            self.assertIn("role", result)
            self.assertIn("闰年", result)

    def test_recall_can_filter_by_archive_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            directory = Path(temp)
            save_context_archive(
                directory,
                [{"role": "user", "content": "第一份归档：猕猴桃"}],
                now=datetime(2026, 10, 22, 15, 30, 0),
            )
            save_context_archive(
                directory,
                [{"role": "user", "content": "第二份归档：猕猴桃"}],
                now=datetime(2026, 10, 23, 9, 0, 0),
            )

            result = recall_context(
                directory, "猕猴桃", archive_id="20261022-153000"
            )

            self.assertIn("20261022-153000", result)
            self.assertNotIn("20261023-090000", result)

    def test_recall_searches_tool_arguments_and_results(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            directory = Path(temp)
            save_context_archive(
                directory,
                _sample_messages(),
                now=datetime(2026, 10, 22, 15, 30, 0),
            )

            tool_result = recall_context(directory, "is_leap_year")
            self.assertIn("is_leap_year", tool_result)
            self.assertIn("call_1", tool_result)

    def test_recall_limits_output_size(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            directory = Path(temp)
            messages = [
                {"role": "user", "content": "关键词 " + "很长内容" * 500}
                for _ in range(20)
            ]
            save_context_archive(directory, messages)

            result = recall_context(directory, "关键词", max_chars=500)

            self.assertLessEqual(len(result), 500)

    def test_recall_rejects_path_traversal_and_reports_missing_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            directory = Path(temp)
            self.assertIn("只能包含", recall_context(directory, "x", archive_id=".."))
            self.assertIn("还没有", recall_context(directory, "x"))
            self.assertIn(
                "找不到", recall_context(directory, "x", archive_id="missing")
            )


class RecallToolTests(unittest.TestCase):
    def test_recall_context_is_a_registered_function_tool(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            directory = Path(temp)
            save_context_archive(
                directory,
                [{"role": "user", "content": "工具检索目标：火龙果"}],
                now=datetime(2026, 10, 22, 15, 30, 0),
            )
            registry = build_registry(context_dir=directory)

            result = registry.execute(
                ToolCall(
                    "call_1",
                    "recallContext",
                    json.dumps({"text": "火龙果"}, ensure_ascii=False),
                )
            )

            self.assertTrue(result.ok, result.content)
            self.assertIn("20261022-153000", result.content)
            self.assertIn("火龙果", result.content)


class AgentCompactionTests(unittest.TestCase):
    def test_compact_archives_then_replaces_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            llm = ScriptedLLM(
                [
                    ("第一轮回答。", []),
                    ("## 用户目标\n继续完成任务。\n", []),
                ]
            )
            agent = Agent(
                llm,
                build_registry(),
                system_prompt="你是测试助手。",
                context_window=2_000,
                context_archive_dir=temp,
            )
            list(agent.run_turn("开始任务"))
            before_messages = copy.deepcopy(agent.messages)

            result = agent.compact()

            self.assertEqual(llm.calls[-1][1], [])
            self.assertEqual(llm.calls[-1][0][0]["content"], COMPACTION_PROMPT)
            self.assertIn("开始任务", llm.calls[-1][0][1]["content"])

            archived = [
                json.loads(line)
                for line in result.archive_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(archived, before_messages)

            self.assertEqual(
                agent.messages[0], {"role": "system", "content": "你是测试助手。"}
            )
            self.assertEqual(len(agent.messages), 2)
            self.assertEqual(agent.messages[1]["role"], "assistant")
            after_content = agent.messages[1]["content"]
            self.assertIn("【历史上下文压缩摘要】", after_content)
            self.assertIn("继续完成任务", after_content)
            self.assertIn(result.archive_id, after_content)
            self.assertIn("recallContext", after_content)
            self.assertFalse(agent.needs_compaction)
            self.assertIn("开始任务", result.before_text)
            self.assertEqual(result.after_text, after_content)

    def test_compact_failure_keeps_original_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            llm = ScriptedLLM([("第一轮回答。", []), LLMError("摘要服务失败")])
            agent = Agent(
                llm,
                build_registry(),
                system_prompt="你是测试助手。",
                context_archive_dir=temp,
            )
            list(agent.run_turn("开始任务"))
            before_messages = copy.deepcopy(agent.messages)

            with self.assertRaises(ContextCompactionError) as raised:
                agent.compact()

            self.assertIn("摘要服务失败", str(raised.exception))
            self.assertEqual(agent.messages, before_messages)
            self.assertEqual(len(list(Path(temp).glob("*.jsonl"))), 1)

    def test_archive_failure_stops_before_summary_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_context_") as temp:
            bad_directory = Path(temp) / "not-a-directory"
            bad_directory.write_text("x", encoding="utf-8")
            llm = ScriptedLLM([("第一轮回答。", []), ("不应被调用", [])])
            agent = Agent(
                llm,
                build_registry(),
                system_prompt="你是测试助手。",
                context_archive_dir=bad_directory,
            )
            list(agent.run_turn("开始任务"))
            before_messages = copy.deepcopy(agent.messages)

            with self.assertRaises(ContextCompactionError):
                agent.compact()

            self.assertEqual(agent.messages, before_messages)
            self.assertEqual(len(llm.calls), 1)

    def test_threshold_blocks_new_prompts_until_compaction(self) -> None:
        llm = ScriptedLLM(
            [
                (
                    "回答。",
                    [],
                    TokenUsage(
                        prompt_tokens=40,
                        completion_tokens=20,
                        total_tokens=60,
                    ),
                )
            ]
        )
        agent = Agent(
            llm,
            build_registry(),
            context_window=100,
            context_archive_dir=tempfile.gettempdir(),
        )

        events = list(agent.run_turn("你好"))
        usage_events = [
            event for event in events if isinstance(event, ContextUsageUpdated)
        ]

        self.assertEqual(len(usage_events), 1)
        self.assertTrue(usage_events[0].needs_compaction)
        self.assertTrue(agent.needs_compaction)
        before_messages = copy.deepcopy(agent.messages)

        blocked = list(agent.run_turn("继续聊天"))

        self.assertIsInstance(blocked[-1], TurnFailed)
        self.assertIn("/compact", blocked[-1].message)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(agent.messages, before_messages)

    def test_compact_rejects_empty_conversation(self) -> None:
        llm = ScriptedLLM([("不应被调用", [])])
        agent = Agent(
            llm,
            build_registry(),
            system_prompt="你是测试助手。",
            context_archive_dir=tempfile.gettempdir(),
        )

        with self.assertRaises(ContextCompactionError):
            agent.compact()
        self.assertEqual(len(llm.calls), 0)


if __name__ == "__main__":
    unittest.main()
