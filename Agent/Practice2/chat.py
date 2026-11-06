import configparser
import json
import mimetypes
import os
import shutil
import subprocess
import urllib.request

cfg = configparser.ConfigParser()
cfg.read("config.ini", encoding="utf-8")
base_url = cfg.get("llm", "base_url").rstrip("/")
api_key = cfg.get("llm", "api_key")
model = cfg.get("llm", "model")
stream = cfg.getboolean("llm", "stream")


def _decode_command_output(data):
    for encoding in ("utf-8", "gbk"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def get_local_time():
    """用 subprocess 调用系统 date 命令获取本地时间。"""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "date /T & time /T"], capture_output=True
        )
    else:
        result = subprocess.run(["date"], capture_output=True)
    if result.returncode != 0:
        error = _decode_command_output(result.stderr).strip()
        return "错误：" + (error or f"date 命令退出码 {result.returncode}")
    return _decode_command_output(result.stdout).strip() or "错误：date 命令没有输出"


def is_leap_year(year):
    """在 Agent 内部计算指定年份是否为闰年。"""
    year = int(year)
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return f"{year} 年是闰年。" if leap else f"{year} 年不是闰年。"


def get_current_directory():
    return os.getcwd()


def list_directory(path="."):
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(target):
        return f"错误：目录不存在：{target}"
    entries = sorted(os.listdir(target), key=str.lower)
    if not entries:
        return f"目录：{target}\n（空目录）"
    lines = [f"目录：{target}"]
    for entry in entries:
        entry_path = os.path.join(target, entry)
        if os.path.isdir(entry_path):
            kind = "目录"
        elif os.path.isfile(entry_path):
            kind = "文件"
        else:
            kind = "其他"
        lines.append(f"[{kind}] {entry}")
    return "\n".join(lines)


def _decode_text(data):
    if b"\x00" in data:
        raise ValueError("文件包含二进制数据")
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法按 UTF-8 或 GBK 解码，可能不是纯文本文件")


def _is_text_file(path):
    try:
        with open(path, "rb") as file:
            _decode_text(file.read(4096))
        return True
    except (OSError, ValueError):
        return False


def get_file_type(path):
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(target):
        return f"错误：路径不存在：{target}"
    if os.path.isdir(target):
        return f"路径：{target}\n类型：目录"
    if not os.path.isfile(target):
        return f"路径：{target}\n类型：其他（不是普通文件或目录）"
    extension = os.path.splitext(target)[1] or "无"
    mime_type, _ = mimetypes.guess_type(target)
    text_flag = "是" if _is_text_file(target) else "否"
    return (
        f"路径：{target}\n"
        f"类型：文件\n"
        f"扩展名：{extension}\n"
        f"MIME 类型：{mime_type or '未知'}\n"
        f"纯文本文件：{text_flag}"
    )


def read_text_file(path):
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(target):
        return f"错误：不是文件：{target}"
    with open(target, "rb") as file:
        data = file.read()
    try:
        return _decode_text(data)
    except ValueError as error:
        return f"错误：{error}"


def modify_text_file(path, content):
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(target):
        return f"错误：文件不存在：{target}"
    if not _is_text_file(target):
        return f"错误：不是纯文本文件：{target}"
    with open(target, "w", encoding="utf-8", newline="") as file:
        file.write(content)
    return f"已修改文件：{target}（{len(content)} 字符）"


def create_file(directory, filename, content=""):
    target_dir = os.path.abspath(os.path.expanduser(directory))
    if not os.path.isdir(target_dir):
        return f"错误：目录不存在：{target_dir}"
    if not filename or os.path.basename(filename) != filename:
        return "错误：filename 只能是文件名，不能包含路径分隔符"
    target = os.path.join(target_dir, filename)
    if os.path.exists(target):
        return f"错误：文件已存在：{target}"
    with open(target, "x", encoding="utf-8", newline="") as file:
        file.write(content)
    return f"已创建文件：{target}（{len(content)} 字符）"


def create_directory(path):
    target = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(target):
        return f"目录已存在：{target}"
    os.makedirs(target)
    return f"已创建目录：{target}"


def rename_path(old_path, new_path):
    old_target = os.path.abspath(os.path.expanduser(old_path))
    new_target = os.path.abspath(os.path.expanduser(new_path))
    if not os.path.exists(old_target):
        return f"错误：源路径不存在：{old_target}"
    if os.path.exists(new_target):
        return f"错误：目标路径已存在：{new_target}"
    os.rename(old_target, new_target)
    if os.path.isdir(new_target):
        kind = "目录"
    else:
        kind = "文件"
    return f"已将{kind}重命名：{old_target} -> {new_target}"


def delete_directory(path):
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(target):
        return f"错误：目录不存在：{target}"
    if target == os.path.dirname(target):
        return "错误：拒绝删除根目录"
    cwd = os.path.abspath(os.getcwd())
    if cwd == target or cwd.startswith(target + os.sep):
        return "错误：拒绝删除当前工作目录或其上级目录"
    shutil.rmtree(target)
    return f"已删除目录（含全部内容）：{target}"


def delete_file(path):
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(target):
        return f"错误：文件不存在：{target}"
    if os.path.isdir(target):
        return f"错误：这是目录，请使用 delete_directory：{target}"
    os.remove(target)
    return f"已删除文件：{target}"


TOOL_FUNCTIONS = {
    "get_local_time": get_local_time,
    "is_leap_year": is_leap_year,
    "get_current_directory": get_current_directory,
    "list_directory": list_directory,
    "get_file_type": get_file_type,
    "read_text_file": read_text_file,
    "modify_text_file": modify_text_file,
    "create_file": create_file,
    "create_directory": create_directory,
    "rename_path": rename_path,
    "delete_directory": delete_directory,
    "delete_file": delete_file,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_local_time",
            "description": "获取本机当前的本地日期和时间，通过运行系统 date 命令实现。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "is_leap_year",
            "description": "判断一个年份是否为闰年，由 Agent 内部代码计算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "要判断的年份，例如 2024",
                    }
                },
                "required": ["year"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_directory",
            "description": "获取当前工作目录的绝对路径。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出指定目录下的文件和子目录；不传 path 时列出当前目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录路径，省略时使用当前目录",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_type",
            "description": "获取指定路径的类型信息，包括是文件还是目录、扩展名、MIME 类型以及是否为纯文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "读取指定纯文本文件的全部内容。二进制文件会被拒绝。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读取的纯文本文件路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_text_file",
            "description": "用新内容覆盖指定纯文本文件的全部内容。文件必须已经存在。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要修改的纯文本文件路径"},
                    "content": {"type": "string", "description": "新的完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "在指定目录中创建一个新文件。如果同名文件已存在则拒绝创建。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "创建目录，会自动创建不存在的上级目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的目录路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_path",
            "description": "重命名文件或目录。目标路径已存在时会拒绝操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {"type": "string", "description": "原路径"},
                    "new_path": {"type": "string", "description": "新路径"},
                },
                "required": ["old_path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_directory",
            "description": "删除指定目录及其全部内容，支持非空目录，操作不可恢复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的目录路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除指定文件，操作不可恢复。目录请使用 delete_directory。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件路径"}
                },
                "required": ["path"],
            },
        },
    },
]


def build_request(messages):
    body = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "tools": TOOLS,
    }
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("Content-Type", "application/json")
    return req


def chat_once(messages):
    req = build_request(messages)
    with urllib.request.urlopen(req) as resp:
        if stream:
            content = ""
            tool_calls = {}
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content += piece
                    print(piece, end="", flush=True)
                for tool_call in delta.get("tool_calls") or []:
                    index = tool_call.get("index", 0)
                    call = tool_calls.setdefault(
                        index,
                        {
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if tool_call.get("id"):
                        call["id"] = tool_call["id"]
                    function = tool_call.get("function") or {}
                    call["function"]["name"] += function.get("name") or ""
                    call["function"]["arguments"] += function.get("arguments") or ""
            if content:
                print()
            return content, [tool_calls[index] for index in sorted(tool_calls)]
        data = json.loads(resp.read().decode("utf-8"))
        message = data["choices"][0]["message"]
        content = message.get("content") or ""
        if content:
            print(content)
        return content, message.get("tool_calls") or []


def execute_tool(tool_call):
    function = tool_call.get("function") or {}
    name = function.get("name") or ""
    raw_arguments = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            return "错误：工具参数必须是 JSON 对象"
    except json.JSONDecodeError as error:
        return f"错误：工具参数不是合法 JSON：{error}"
    if name not in TOOL_FUNCTIONS:
        return f"错误：未知工具：{name}"
    try:
        return str(TOOL_FUNCTIONS[name](**arguments))
    except TypeError as error:
        return f"错误：工具参数不匹配：{error}"
    except Exception as error:
        return f"错误：{type(error).__name__}: {error}"


MAX_TOOL_ROUNDS = 10


def main():
    messages = []
    while True:
        question = input("你：")
        messages.append({"role": "user", "content": question})
        for _ in range(MAX_TOOL_ROUNDS):
            content, tool_calls = chat_once(messages)
            assistant_message = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_message["content"] = content or None
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)
            if not tool_calls:
                break
            for tool_call in tool_calls:
                name = tool_call["function"]["name"]
                arguments = tool_call["function"]["arguments"]
                print(f"[工具] {name} {arguments}")
                result = execute_tool(tool_call)
                print(f"[结果] {result}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", ""),
                        "content": result,
                    }
                )
        else:
            print("[提示] 工具调用轮数过多，已停止本轮。")


if __name__ == "__main__":
    main()

