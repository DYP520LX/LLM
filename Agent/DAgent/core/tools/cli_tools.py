"""Local CLI tool: run an installed program and return its output.

The command is assembled as an argument list and executed with
``shell=False`` so model-supplied arguments are never interpreted by a shell.
This is a teaching tool: there is intentionally no allow-list, deny-list or
approval step, and commands run with the same OS user as the agent.
"""

from __future__ import annotations

import subprocess

from .registry import Tool

TIMEOUT_SECONDS = 30.0
MAX_STREAM_CHARS = 6000


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_output(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return _normalize_newlines(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    return _normalize_newlines(data.decode("utf-8", errors="replace"))


def _normalize_args(args: object) -> list[str]:
    if args is None:
        return []
    if isinstance(args, str):
        raise ValueError(
            "args 必须是字符串数组，例如 [\"--version\"]；"
            "不要传单个字符串或整条 Shell 命令"
        )
    if not isinstance(args, (list, tuple)):
        raise ValueError("args 必须是字符串数组")
    if any(isinstance(item, (list, tuple, dict)) for item in args):
        raise ValueError("args 中的每一项都必须是字符串")
    return [str(item) for item in args]


def _truncate(
    text: str,
    max_chars: int = MAX_STREAM_CHARS,
) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _format_stream(
    name: str,
    text: str,
    lines: list[str],
    max_chars: int = MAX_STREAM_CHARS,
) -> None:
    shown, truncated = _truncate(text, max_chars)
    if truncated:
        lines.append(
            f"--- {name}（已截断：原始 {len(text)} 字符，"
            f"仅显示前 {max_chars} 字符） ---"
        )
    else:
        lines.append(f"--- {name} ---")
    lines.append(shown if shown else "（空）")


def _format_result(
    command_text: str,
    exit_code: str,
    stdout: str,
    stderr: str,
    status: str | None = None,
    max_stream_chars: int = MAX_STREAM_CHARS,
) -> str:
    lines = [f"命令：{command_text}", f"退出码：{exit_code}"]
    if status:
        lines.append(f"状态：{status}")
    lines.append("")
    _format_stream("stdout", stdout, lines, max_stream_chars)
    _format_stream("stderr", stderr, lines, max_stream_chars)
    return "\n".join(lines)


def _failure_hint(exit_code: int, stderr: str) -> str | None:
    """Explain a failed command whose stderr is empty.

    Silent flags such as ``curl -s`` hide the real error, which leaves the model
    with an exit code but no way to choose a better next step.
    """

    if exit_code == 0 or stderr.strip():
        return None
    return (
        "命令以非 0 退出码结束，但 stderr 为空，可能是程序使用了静默参数"
        "（例如 curl 的 -s）隐藏了错误。请改用会显示错误的参数（如 curl -sS "
        "或去掉 -s）后重试；如果是网络请求失败，也可以换一个接口或稍后重试。"
    )


def run_cli(
    command: str,
    args: list[str] | None = None,
    *,
    timeout: float = TIMEOUT_SECONDS,
    max_stream_chars: int = MAX_STREAM_CHARS,
) -> str:
    """Execute ``command`` with ``args`` and return stdout, stderr and exit code."""

    program = str(command or "").strip()
    if not program:
        return "错误：command 不能为空"

    try:
        arguments = _normalize_args(args)
    except ValueError as error:
        return f"错误：{error}"

    argv = [program, *arguments]
    command_text = subprocess.list2cmdline(argv)

    try:
        completed = subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = _decode_output(error.stdout)
        stderr = _decode_output(error.stderr)
        return _format_result(
            command_text,
            "无（进程被终止）",
            stdout,
            stderr,
            status=f"执行超时（超过 {timeout:g} 秒），进程已被终止",
            max_stream_chars=max_stream_chars,
        )
    except FileNotFoundError:
        return (
            f"错误：找不到程序或命令：{program}\n"
            "请确认该 CLI 已安装并在 PATH 中，或传入可执行文件的完整路径。"
            "该工具不会经过 Shell，因此不支持内置命令（如 dir、cd）和管道。"
        )
    except PermissionError:
        return f"错误：没有权限执行：{program}"
    except OSError as error:
        return f"错误：启动命令失败：{error}"

    stdout = _decode_output(completed.stdout)
    stderr = _decode_output(completed.stderr)
    output = _format_result(
        command_text,
        str(completed.returncode),
        stdout,
        stderr,
        max_stream_chars=max_stream_chars,
    )
    hint = _failure_hint(completed.returncode, stderr)
    if hint:
        output = f"{output}\n\n提示：{hint}"
    return output


def tools(
    timeout: float = TIMEOUT_SECONDS,
    max_stream_chars: int = MAX_STREAM_CHARS,
) -> list[Tool]:
    def handler(command: str, args: list[str] | None = None) -> str:
        return run_cli(
            command,
            args,
            timeout=timeout,
            max_stream_chars=max_stream_chars,
        )

    return [
        Tool(
            name="Runcli",
            description=(
                "运行本地已安装的命令行程序（CLI）。command 是程序名或可执行文件"
                "路径，args 是参数数组。命令以参数列表方式执行，不经过 Shell，"
                "因此不支持管道、重定向、通配符和 Shell 内置命令。"
                "返回标准输出、标准错误和退出码。"
                "如果退出码非 0，请根据 stderr 和提示判断原因；"
                "需要时更换参数或接口后重试。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的程序名称或可执行文件路径，例如 python、git",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "传给程序的参数列表，例如 [\"--version\"]；"
                            "省略时表示不带参数"
                        ),
                    },
                },
                "required": ["command"],
            },
            handler=handler,
        )
    ]
