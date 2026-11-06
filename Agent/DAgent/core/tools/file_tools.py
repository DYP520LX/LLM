"""Local file and directory tools.

All tools return human-readable strings. The registry converts unexpected
exceptions into tool results, so a bad path or permission error is reported to
the model rather than crashing the agent.
"""

from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path

from .registry import Tool


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise ValueError("文件包含二进制数据")
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法按 UTF-8 或 GBK 解码，可能不是纯文本文件")


def _is_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            _decode_text(file.read(4096))
        return True
    except (OSError, ValueError):
        return False


def get_current_directory() -> str:
    return str(Path.cwd())


def list_directory(path: str = ".") -> str:
    target = _resolve(path)
    if not target.is_dir():
        return f"错误：目录不存在：{target}"

    entries = sorted(target.iterdir(), key=lambda item: item.name.lower())
    if not entries:
        return f"目录：{target}\n（空目录）"

    lines = [f"目录：{target}"]
    for entry in entries:
        if entry.is_dir():
            kind = "目录"
        elif entry.is_file():
            kind = "文件"
        else:
            kind = "其他"
        lines.append(f"[{kind}] {entry.name}")
    return "\n".join(lines)


def get_file_type(path: str) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"错误：路径不存在：{target}"
    if target.is_dir():
        return f"路径：{target}\n类型：目录"
    if not target.is_file():
        return f"路径：{target}\n类型：其他（不是普通文件或目录）"

    extension = target.suffix or "无"
    mime_type, _ = mimetypes.guess_type(str(target))
    text_flag = "是" if _is_text_file(target) else "否"
    return (
        f"路径：{target}\n"
        f"类型：文件\n"
        f"扩展名：{extension}\n"
        f"MIME 类型：{mime_type or '未知'}\n"
        f"纯文本文件：{text_flag}"
    )


def read_text_file(path: str) -> str:
    target = _resolve(path)
    if not target.is_file():
        return f"错误：不是文件：{target}"
    try:
        return _decode_text(target.read_bytes())
    except ValueError as error:
        return f"错误：{error}"


def modify_text_file(path: str, content: str) -> str:
    target = _resolve(path)
    if not target.is_file():
        return f"错误：文件不存在：{target}"
    if not _is_text_file(target):
        return f"错误：不是纯文本文件：{target}"
    target.write_text(content, encoding="utf-8", newline="")
    return f"已修改文件：{target}（{len(content)} 字符）"


def create_file(directory: str, filename: str, content: str = "") -> str:
    target_dir = _resolve(directory)
    if not target_dir.is_dir():
        return f"错误：目录不存在：{target_dir}"
    if (
        not filename
        or filename in {".", ".."}
        or Path(filename).name != filename
    ):
        return "错误：filename 只能是文件名，不能包含路径分隔符"
    target = target_dir / filename
    if target.exists():
        return f"错误：文件已存在：{target}"
    with target.open("x", encoding="utf-8", newline="") as file:
        file.write(content)
    return f"已创建文件：{target}（{len(content)} 字符）"


def create_directory(path: str) -> str:
    target = _resolve(path)
    if target.is_dir():
        return f"目录已存在：{target}"
    if target.exists():
        return f"错误：路径已存在且不是目录：{target}"
    target.mkdir(parents=True)
    return f"已创建目录：{target}"


def rename_path(old_path: str, new_path: str) -> str:
    old_target = _resolve(old_path)
    new_target = _resolve(new_path)
    if not old_target.exists():
        return f"错误：源路径不存在：{old_target}"
    if new_target.exists():
        return f"错误：目标路径已存在：{new_target}"
    old_target.rename(new_target)
    kind = "目录" if new_target.is_dir() else "文件"
    return f"已将{kind}重命名：{old_target} -> {new_target}"


def delete_directory(path: str) -> str:
    target = _resolve(path)
    if not target.is_dir():
        return f"错误：目录不存在：{target}"
    if target == target.parent:
        return "错误：拒绝删除根目录"
    cwd = Path.cwd().resolve()
    if cwd == target or target in cwd.parents:
        return "错误：拒绝删除当前工作目录或其上级目录"
    shutil.rmtree(target)
    return f"已删除目录（含全部内容）：{target}"


def delete_file(path: str) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"错误：文件不存在：{target}"
    if target.is_dir():
        return f"错误：这是目录，请使用 delete_directory：{target}"
    target.unlink()
    return f"已删除文件：{target}"


def tools() -> list[Tool]:
    return [
        Tool(
            name="get_current_directory",
            description="获取当前工作目录的绝对路径。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=get_current_directory,
        ),
        Tool(
            name="list_directory",
            description="列出指定目录下的文件和子目录；不传 path 时列出当前目录。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录路径，省略时使用当前目录",
                    }
                },
                "required": [],
            },
            handler=list_directory,
        ),
        Tool(
            name="get_file_type",
            description=(
                "获取指定路径的类型信息，包括是文件还是目录、扩展名、"
                "MIME 类型以及是否为纯文本文件。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录路径"}
                },
                "required": ["path"],
            },
            handler=get_file_type,
        ),
        Tool(
            name="read_text_file",
            description="读取指定纯文本文件的全部内容。二进制文件会被拒绝。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读取的纯文本文件路径"}
                },
                "required": ["path"],
            },
            handler=read_text_file,
        ),
        Tool(
            name="modify_text_file",
            description="用新内容覆盖指定纯文本文件的全部内容。文件必须已经存在。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要修改的纯文本文件路径"},
                    "content": {"type": "string", "description": "新的完整文件内容"},
                },
                "required": ["path", "content"],
            },
            handler=modify_text_file,
        ),
        Tool(
            name="create_file",
            description="在指定目录中创建一个新文件。如果同名文件已存在则拒绝创建。",
            parameters={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "新文件所在的目录路径",
                    },
                    "filename": {
                        "type": "string",
                        "description": "新文件名，不能包含路径分隔符",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容，省略时创建空文件",
                    },
                },
                "required": ["directory", "filename"],
            },
            handler=create_file,
        ),
        Tool(
            name="create_directory",
            description="创建目录，会自动创建不存在的上级目录。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的目录路径"}
                },
                "required": ["path"],
            },
            handler=create_directory,
        ),
        Tool(
            name="rename_path",
            description="重命名文件或目录。目标路径已存在时会拒绝操作。",
            parameters={
                "type": "object",
                "properties": {
                    "old_path": {"type": "string", "description": "原路径"},
                    "new_path": {"type": "string", "description": "新路径"},
                },
                "required": ["old_path", "new_path"],
            },
            handler=rename_path,
        ),
        Tool(
            name="delete_directory",
            description="删除指定目录及其全部内容，支持非空目录，操作不可恢复。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的目录路径"}
                },
                "required": ["path"],
            },
            handler=delete_directory,
        ),
        Tool(
            name="delete_file",
            description="删除指定文件，操作不可恢复。目录请使用 delete_directory。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件路径"}
                },
                "required": ["path"],
            },
            handler=delete_file,
        ),
    ]
