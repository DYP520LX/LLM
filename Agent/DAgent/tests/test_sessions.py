from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.agent import Agent
from core.project_context import (
    AGENTS_MARKER,
    agents_message,
    has_agents_message,
    insert_agents_message,
)
from core.sessions import SessionError, SessionStore
from core.tools import build_registry


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name) / "sessions"
        self.store = SessionStore(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_create_save_load_and_list(self) -> None:
        info = self.store.create()
        self.assertTrue((self.root / info.id / "meta.json").is_file())

        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "帮我检查项目"},
            {"role": "assistant", "content": "好的"},
        ]
        saved = self.store.save(info.id, messages)

        self.assertEqual(saved.title, "帮我检查项目")
        self.assertEqual(saved.message_count, 3)
        self.assertEqual(self.store.load(info.id), messages)
        self.assertEqual([item.id for item in self.store.list_sessions()], [info.id])
        self.assertEqual(self.store.get(info.id).message_count, 3)

    def test_event_log_contains_snapshot_and_agents_content(self) -> None:
        info = self.store.create()
        self.store.append_event(
            info.id,
            "agents_md",
            {
                "path": "AGENTS.md",
                "message": {
                    "role": "system",
                    "content": f"{AGENTS_MARKER}\n\n项目约束",
                },
            },
        )
        self.store.save(
            info.id,
            [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "你好"},
            ],
        )

        events_path = self.root / info.id / "events.jsonl"
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(any(event["type"] == "session_created" for event in events))
        agents_event = next(
            event for event in events if event["type"] == "agents_md"
        )
        self.assertIn("项目约束", agents_event["message"]["content"])
        snapshot = next(event for event in events if event["type"] == "snapshot")
        self.assertEqual(snapshot["message_count"], 2)

    def test_load_falls_back_to_last_snapshot(self) -> None:
        info = self.store.create()
        messages = [{"role": "user", "content": "从事件日志恢复"}]
        self.store.save(info.id, messages)
        (self.root / info.id / "messages.json").unlink()

        self.assertEqual(self.store.load(info.id), messages)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(SessionError):
            self.store.load("../outside")


class ProjectContextTests(unittest.TestCase):
    def test_agents_message_and_marker_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text(
                "# 项目说明\n\n- 使用中文\n",
                encoding="utf-8",
            )
            loaded = agents_message(root)
        self.assertIsNotNone(loaded)
        message, path = loaded
        self.assertEqual(message["role"], "system")
        self.assertTrue(message["content"].startswith(AGENTS_MARKER))
        self.assertIn("使用中文", message["content"])
        self.assertEqual(path.name, "AGENTS.md")
        self.assertTrue(has_agents_message([message]))

    def test_agent_injects_project_context_once_after_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("必须遵守项目约束。", encoding="utf-8")
            loaded = agents_message(root)
        message, _ = loaded
        agent = Agent(
            object(),  # The LLM is not called by this test.
            build_registry(),
            system_prompt="基础系统提示",
        )
        try:
            self.assertTrue(agent.inject_project_context(message))
            self.assertFalse(agent.inject_project_context(message))
            self.assertTrue(agent.has_project_context())
            self.assertEqual(agent.messages[0]["role"], "system")
            self.assertEqual(agent.messages[0]["content"], "基础系统提示")
            self.assertIn(AGENTS_MARKER, agent.messages[1]["content"])
        finally:
            agent.close()

    def test_insert_agents_message_keeps_leading_system_messages(self) -> None:
        messages = [
            {"role": "system", "content": "base"},
            {"role": "system", "content": "另一个系统消息"},
            {"role": "user", "content": "问题"},
        ]
        result = insert_agents_message(
            messages,
            {"role": "system", "content": f"{AGENTS_MARKER}\n项目"},
        )
        self.assertEqual(
            [message["role"] for message in result],
            ["system", "system", "system", "user"],
        )
        self.assertIn(AGENTS_MARKER, result[2]["content"])


if __name__ == "__main__":
    unittest.main()
