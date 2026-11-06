from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.tools import build_registry
from core.tools import calendar_tools, file_tools, time_tools
from core.types import ToolCall


class ToolTests(unittest.TestCase):
    def test_registry_contains_all_builtin_tools(self) -> None:
        registry = build_registry()
        self.assertEqual(len(registry), 15)
        self.assertEqual(len(registry.schemas()), 15)
        self.assertIn("get_local_time", registry.names())
        self.assertIn("delete_file", registry.names())
        self.assertIn("fetch_web", registry.names())
        self.assertIn("searchweb", registry.names())
        self.assertIn("Runcli", registry.names())

    def test_time_tool(self) -> None:
        result = time_tools.get_local_time()
        self.assertTrue(result)
        self.assertFalse(result.startswith("错误："), result)

    def test_leap_year(self) -> None:
        self.assertEqual(calendar_tools.is_leap_year(2024), "2024 年是闰年。")
        self.assertEqual(calendar_tools.is_leap_year(1900), "1900 年不是闰年。")
        self.assertEqual(calendar_tools.is_leap_year(2000), "2000 年是闰年。")

    def test_registry_executes_tool(self) -> None:
        registry = build_registry()
        result = registry.execute(
            ToolCall(id="call_1", name="is_leap_year", arguments='{"year": 2024}')
        )
        self.assertTrue(result.ok)
        self.assertIn("2024 年是闰年", result.content)

    def test_registry_handles_unknown_tool_and_bad_arguments(self) -> None:
        registry = build_registry()
        unknown = registry.execute(ToolCall("call_1", "missing", "{}"))
        self.assertFalse(unknown.ok)
        self.assertIn("未知工具", unknown.content)

        bad_json = registry.execute(ToolCall("call_2", "is_leap_year", "{"))
        self.assertFalse(bad_json.ok)
        self.assertIn("JSON", bad_json.content)

    def test_file_operations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_tools_") as temp:
            root = Path(temp)
            sub = root / "sub"

            self.assertIn("已创建目录", file_tools.create_directory(str(sub)))
            created = file_tools.create_file(str(sub), "a.txt", "hello\n第二行\n")
            self.assertIn("已创建文件", created)

            listing = file_tools.list_directory(str(sub))
            self.assertIn("[文件] a.txt", listing)

            file_type = file_tools.get_file_type(str(sub / "a.txt"))
            self.assertIn("text/plain", file_type)
            self.assertIn("纯文本文件：是", file_type)

            self.assertEqual(
                file_tools.read_text_file(str(sub / "a.txt")),
                "hello\n第二行\n",
            )

            self.assertIn(
                "已修改文件",
                file_tools.modify_text_file(str(sub / "a.txt"), "新内容\n"),
            )
            self.assertEqual(
                file_tools.read_text_file(str(sub / "a.txt")), "新内容\n"
            )

            self.assertIn(
                "重命名",
                file_tools.rename_path(str(sub / "a.txt"), str(sub / "b.txt")),
            )
            self.assertTrue((sub / "b.txt").is_file())

            self.assertIn("已删除文件", file_tools.delete_file(str(sub / "b.txt")))
            self.assertFalse((sub / "b.txt").exists())

            self.assertIn(
                "已创建文件",
                file_tools.create_file(str(sub), "keep.txt", "keep"),
            )
            self.assertIn("已删除目录", file_tools.delete_directory(str(sub)))
            self.assertFalse(sub.exists())

    def test_binary_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_binary_") as temp:
            path = Path(temp) / "data.bin"
            path.write_bytes(b"\x00\x01\x02binary")

            file_type = file_tools.get_file_type(str(path))
            self.assertIn("纯文本文件：否", file_type)

            content = file_tools.read_text_file(str(path))
            self.assertIn("二进制", content)

    def test_create_file_rejects_path_separator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_file_") as temp:
            result = file_tools.create_file(temp, "sub/a.txt", "x")
            self.assertIn("filename 只能是文件名", result)

    def test_delete_directory_protects_working_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_guard_") as temp:
            root = Path(temp)
            with mock.patch.object(Path, "cwd", return_value=root):
                result = file_tools.delete_directory(str(root))
            self.assertIn("拒绝删除当前工作目录", result)
            self.assertTrue(root.exists())


if __name__ == "__main__":
    unittest.main()
