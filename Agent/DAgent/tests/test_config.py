from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.config import ensure_config, load_settings
from core.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_loads_settings_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[llm]\n"
                "base_url = http://127.0.0.1:9999\n"
                "api_key = test\n"
                "model = mock\n",
                encoding="utf-8",
            )
            settings = load_settings(path)

        self.assertEqual(settings.base_url, "http://127.0.0.1:9999")
        self.assertEqual(settings.api_key, "test")
        self.assertEqual(settings.model, "mock")
        self.assertTrue(settings.stream)
        self.assertEqual(settings.timeout, 120.0)
        self.assertEqual(settings.max_tool_rounds, 10)
        self.assertEqual(settings.search_api_key, "")
        self.assertIsNone(settings.context_window)

    def test_parses_explicit_options(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[llm]\n"
                "base_url = http://localhost:1234/\n"
                "model = mock\n"
                "stream = false\n"
                "timeout = 5\n"
                "max_tool_rounds = 3\n",
                encoding="utf-8",
            )
            settings = load_settings(path)

        self.assertEqual(settings.base_url, "http://localhost:1234")
        self.assertFalse(settings.stream)
        self.assertEqual(settings.timeout, 5.0)
        self.assertEqual(settings.max_tool_rounds, 3)

    def test_reads_search_api_key_from_search_section(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[llm]\n"
                "base_url = http://127.0.0.1:9999\n"
                "model = mock\n"
                "\n"
                "[search]\n"
                "baidu_qianfan_api_key = bce-v3/ALTAK-test\n",
                encoding="utf-8",
            )
            settings = load_settings(path)

        self.assertEqual(settings.search_api_key, "bce-v3/ALTAK-test")

    def test_empty_search_api_key_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[llm]\n"
                "base_url = http://127.0.0.1:9999\n"
                "model = mock\n"
                "\n"
                "[search]\n"
                "baidu_qianfan_api_key =\n",
                encoding="utf-8",
            )
            settings = load_settings(path)

        self.assertEqual(settings.search_api_key, "")

    def test_default_config_is_in_current_project_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[llm]\n"
                "base_url = http://127.0.0.1:9999\n"
                "model = mock\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(temp)
                settings = load_settings()
            finally:
                os.chdir(previous)

        self.assertEqual(settings.model, "mock")

    def test_parses_optional_context_window(self) -> None:
        for raw, expected in (
            ("128000", 128000),
            ("128k", 128000),
            ("1m", 1000000),
        ):
            with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
                path = Path(temp) / "config.ini"
                path.write_text(
                    "[llm]\n"
                    "base_url = http://127.0.0.1:9999\n"
                    "model = mock\n"
                    f"context_window = {raw}\n",
                    encoding="utf-8",
                )
                settings = load_settings(path)
            self.assertEqual(settings.context_window, expected)

    def test_invalid_context_window_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[llm]\n"
                "base_url = http://127.0.0.1:9999\n"
                "model = mock\n"
                "context_window = zero\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_settings(path)

    def test_missing_file_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings("missing-config.ini")

    def test_ensure_config_creates_complete_mock_file_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "nested" / "config.ini"
            created_path, created = ensure_config(path)

            self.assertTrue(created)
            self.assertEqual(created_path, path)
            self.assertTrue(path.is_file())
            text = path.read_text(encoding="utf-8")
            self.assertIn("http://127.0.0.1:9999/v1", text)
            self.assertIn("api_key = mock", text)
            self.assertIn("model = mock", text)
            self.assertIn("[sessions]", text)
            self.assertIn("[mcp]", text)

            settings = load_settings(path)
            self.assertEqual(settings.base_url, "http://127.0.0.1:9999/v1")
            self.assertEqual(settings.api_key, "mock")
            self.assertEqual(settings.model, "mock")

            path.write_text("user edited", encoding="utf-8")
            _, created_again = ensure_config(path)
            self.assertFalse(created_again)
            self.assertEqual(path.read_text(encoding="utf-8"), "user edited")

    def test_parses_behavior_and_mcp_options(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[llm]\n"
                "base_url = http://127.0.0.1:9999\n"
                "model = mock\n"
                "\n"
                "[agent]\n"
                "system_prompt = 自定义提示\n"
                "agents_file = PROJECT.md\n"
                "\n"
                "[context]\n"
                "warning_ratio = 0.7\n"
                "compaction_target_tokens = 7000\n"
                "archive_dir = archives\n"
                "\n"
                "[sessions]\n"
                "dir = history\n"
                "\n"
                "[mcp]\n"
                "config_file = servers.json\n"
                "default_timeout = 12\n"
                "connect_grace_seconds = 1\n"
                "call_grace_seconds = 2\n"
                "shutdown_grace_seconds = 3\n"
                "max_result_chars = 1234\n"
                "\n"
                "[tui]\n"
                "sidebar_width = 40\n"
                "session_list_limit = 12\n"
                "message_preview_chars = 500\n"
                "\n"
                "[tools]\n"
                "cli_timeout = 11\n"
                "cli_max_stream_chars = 2222\n"
                "web_timeout = 12\n"
                "web_default_max_chars = 3333\n"
                "web_max_chars = 4444\n"
                "search_timeout = 13\n"
                "search_default_top_k = 6\n"
                "search_max_top_k = 9\n"
                "search_max_snippet_chars = 555\n",
                encoding="utf-8",
            )
            settings = load_settings(path)

        self.assertEqual(settings.system_prompt, "自定义提示")
        self.assertEqual(settings.agents_filename, "PROJECT.md")
        self.assertEqual(settings.context_warning_ratio, 0.7)
        self.assertEqual(settings.compaction_target_tokens, 7000)
        self.assertEqual(settings.context_archive_dir, "archives")
        self.assertEqual(settings.sessions_dir, "history")
        self.assertEqual(settings.mcp_config_filename, "servers.json")
        self.assertEqual(settings.mcp_default_timeout, 12.0)
        self.assertEqual(settings.mcp_connect_grace_seconds, 1.0)
        self.assertEqual(settings.mcp_call_grace_seconds, 2.0)
        self.assertEqual(settings.mcp_shutdown_grace_seconds, 3.0)
        self.assertEqual(settings.mcp_max_result_chars, 1234)
        self.assertEqual(settings.tui_sidebar_width, 40)
        self.assertEqual(settings.tui_session_list_limit, 12)
        self.assertEqual(settings.tui_message_preview_chars, 500)
        self.assertEqual(settings.cli_timeout, 11.0)
        self.assertEqual(settings.cli_max_stream_chars, 2222)
        self.assertEqual(settings.web_timeout, 12.0)
        self.assertEqual(settings.web_default_max_chars, 3333)
        self.assertEqual(settings.web_max_chars, 4444)
        self.assertEqual(settings.search_timeout, 13.0)
        self.assertEqual(settings.search_default_top_k, 6)
        self.assertEqual(settings.search_max_top_k, 9)
        self.assertEqual(settings.search_max_snippet_chars, 555)

    def test_missing_llm_section_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text("[other]\nvalue = 1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
