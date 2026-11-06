"""Time-related tools implemented by calling an external system command."""

from __future__ import annotations

import os
import subprocess

from .registry import Tool


def _decode_command_output(data: bytes) -> str:
    # Windows consoles may emit UTF-8 or the active ANSI code page (GBK here).
    for encoding in ("utf-8", "gbk"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def get_local_time() -> str:
    """Return local date/time from the system ``date`` command."""

    if os.name == "nt":
        # ``date`` and ``time`` are cmd built-ins, so run them through cmd.
        # ``/T`` prints the value without prompting to change it.
        result = subprocess.run(
            ["cmd", "/c", "date /T & time /T"],
            capture_output=True,
        )
    else:
        result = subprocess.run(["date"], capture_output=True)

    if result.returncode != 0:
        error = _decode_command_output(result.stderr).strip()
        return "错误：" + (error or f"date 命令退出码 {result.returncode}")
    return _decode_command_output(result.stdout).strip() or "错误：date 命令没有输出"


def tools() -> list[Tool]:
    return [
        Tool(
            name="get_local_time",
            description="获取本机当前的本地日期和时间，通过运行系统 date 命令实现。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=get_local_time,
        )
    ]
