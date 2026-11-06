from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from core.tools import build_registry, cli_tools
from core.types import ToolCall


def _completed(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class RunCliTests(unittest.TestCase):
    def test_registry_contains_runcli(self) -> None:
        registry = build_registry()
        self.assertIn("Runcli", registry.names())
        self.assertEqual(len(registry), 16)

    def test_runs_program_with_argument_list_and_shell_false(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess, "run", return_value=_completed(b"ok\n")
        ) as run:
            output = cli_tools.run_cli("git", ["status", "--short"])

        argv = run.call_args.args[0]
        self.assertEqual(argv, ["git", "status", "--short"])
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(kwargs["check"])
        self.assertIn("退出码：0", output)
        self.assertIn("ok", output)

    def test_returns_stdout_stderr_and_exit_code(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess,
            "run",
            return_value=_completed(b"result\n", b"warning\n", returncode=3),
        ):
            output = cli_tools.run_cli("some-tool", [])

        self.assertIn("退出码：3", output)
        self.assertIn("--- stdout ---\nresult", output)
        self.assertIn("--- stderr ---\nwarning", output)

    def test_nonzero_exit_without_stderr_gets_retry_hint(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess,
            "run",
            return_value=_completed(b"", b"", returncode=35),
        ):
            output = cli_tools.run_cli("curl", ["-s", "https://example.com"])

        self.assertIn("退出码：35", output)
        self.assertIn("提示：", output)
        self.assertIn("curl -sS", output)
        self.assertIn("换一个接口", output)

    def test_nonzero_exit_with_stderr_has_no_retry_hint(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess,
            "run",
            return_value=_completed(b"", b"connection refused\n", returncode=7),
        ):
            output = cli_tools.run_cli("curl", ["-sS", "https://example.com"])

        self.assertIn("connection refused", output)
        self.assertNotIn("提示：", output)

    def test_real_process_round_trip(self) -> None:
        output = cli_tools.run_cli(
            sys.executable,
            ["-c", "import sys; print('hello'); sys.stderr.write('oops\\n')"],
        )

        self.assertIn("退出码：0", output)
        self.assertIn("hello", output)
        self.assertIn("oops", output)

    def test_real_nonzero_exit_code(self) -> None:
        output = cli_tools.run_cli(
            sys.executable, ["-c", "import sys; sys.exit(7)"]
        )

        self.assertIn("退出码：7", output)

    def test_missing_program_is_reported(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess, "run", side_effect=FileNotFoundError
        ):
            output = cli_tools.run_cli("definitely-not-installed", [])

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("找不到程序", output)
        self.assertIn("不会经过 Shell", output)

    def test_permission_error_is_reported(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess, "run", side_effect=PermissionError
        ):
            output = cli_tools.run_cli("./script", [])

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("没有权限执行", output)

    def test_timeout_is_reported_with_partial_output(self) -> None:
        error = subprocess.TimeoutExpired(
            ["slow"], 30, output=b"partial\n", stderr=b"still running\n"
        )
        with mock.patch.object(cli_tools.subprocess, "run", side_effect=error):
            output = cli_tools.run_cli("slow", [])

        self.assertIn("执行超时", output)
        self.assertIn("进程已被终止", output)
        self.assertIn("partial", output)
        self.assertIn("still running", output)

    def test_long_output_is_truncated_and_marked(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess, "run", return_value=_completed(b"a" * 20000)
        ):
            output = cli_tools.run_cli("chatty", [])

        self.assertIn("已截断", output)
        self.assertIn("原始 20000 字符", output)
        self.assertNotIn("a" * (cli_tools.MAX_STREAM_CHARS + 1), output)

    def test_empty_output_is_explicit(self) -> None:
        with mock.patch.object(
            cli_tools.subprocess, "run", return_value=_completed()
        ):
            output = cli_tools.run_cli("silent", [])

        self.assertEqual(output.count("（空）"), 2)

    def test_string_args_are_rejected(self) -> None:
        with mock.patch.object(cli_tools.subprocess, "run") as run:
            output = cli_tools.run_cli("git", "--version")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("字符串数组", output)
        run.assert_not_called()

    def test_empty_command_is_rejected(self) -> None:
        output = cli_tools.run_cli("   ", [])
        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("command 不能为空", output)

    def test_decode_output_falls_back_to_gbk(self) -> None:
        self.assertEqual(cli_tools._decode_output("中文".encode("gbk")), "中文")

    def test_registry_executes_runcli(self) -> None:
        registry = build_registry()
        with mock.patch.object(
            cli_tools.subprocess, "run", return_value=_completed(b"v1.0\n")
        ):
            result = registry.execute(
                ToolCall("call_1", "Runcli", '{"command": "tool", "args": ["-v"]}')
            )

        self.assertTrue(result.ok)
        self.assertIn("退出码：0", result.content)
        self.assertIn("v1.0", result.content)


if __name__ == "__main__":
    unittest.main()
