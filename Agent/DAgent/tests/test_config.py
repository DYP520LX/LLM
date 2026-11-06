from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.config import load_settings
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

    def test_missing_file_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings("missing-config.ini")

    def test_missing_llm_section_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_config_") as temp:
            path = Path(temp) / "config.ini"
            path.write_text("[other]\nvalue = 1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
