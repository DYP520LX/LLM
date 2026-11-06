"""Configuration loading for DAgent.

The core layer only knows about :class:`Settings`; each UI can decide how to
pass a configuration path or otherwise construct settings.

Every behavioral parameter has a documented default here, so the project
``config.ini`` only needs to override what it wants to change.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

CONFIG_FILENAME = "config.ini"

DEFAULT_BASE_URL = "http://127.0.0.1:9999/v1"
DEFAULT_API_KEY = "mock"
DEFAULT_MODEL = "mock"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOOL_ROUNDS = 10
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_CONTEXT_WARNING_RATIO = 0.60
DEFAULT_COMPACTION_TARGET_TOKENS = 5_000
DEFAULT_AGENTS_FILENAME = "AGENTS.md"
DEFAULT_CONTEXT_ARCHIVE_DIR = ".agents/context"
DEFAULT_SESSIONS_DIR = ".agents/sessions"
DEFAULT_MCP_CONFIG_FILENAME = ".mcp.json"
DEFAULT_MCP_TIMEOUT = 30.0
DEFAULT_MCP_CONNECT_GRACE_SECONDS = 5.0
DEFAULT_MCP_CALL_GRACE_SECONDS = 5.0
DEFAULT_MCP_SHUTDOWN_GRACE_SECONDS = 8.0
DEFAULT_MCP_MAX_RESULT_CHARS = 30_000
DEFAULT_TUI_SIDEBAR_WIDTH = 36
DEFAULT_TUI_SESSION_LIST_LIMIT = 50
DEFAULT_TUI_MESSAGE_PREVIEW_CHARS = 1_200

DEFAULT_CONFIG_TEXT = f"""\
# DAgent 项目配置文件
# 本文件由 DAgent 在首次启动时自动创建。请修改下面的 mock 配置后重新启动。

[llm]
# OpenAI 兼容接口地址（通常以 /v1 结尾）
base_url = {DEFAULT_BASE_URL}
# API Key；本地服务可以填写任意占位值
api_key = {DEFAULT_API_KEY}
# 模型名称
model = {DEFAULT_MODEL}
# 是否使用流式输出（推荐 true，可同时获取 Provider Token Usage）
stream = true
# 单次 LLM 请求超时（秒）
timeout = {DEFAULT_TIMEOUT:g}
# 单轮对话中允许的最大工具调用轮数
max_tool_rounds = {DEFAULT_MAX_TOOL_ROUNDS}
# 可选：模型上下文窗口大小，不填时默认 128000 并显示「默认」
# context_window = {DEFAULT_CONTEXT_WINDOW}

[agent]
# 可选：覆盖 DAgent 默认 System Prompt；留空使用内置提示词
system_prompt =
# 项目级说明文件；首次加载会话时注入一次，后续复用
agents_file = {DEFAULT_AGENTS_FILENAME}

[context]
# 上下文达到该比例后暂停普通提示词并要求 /compact
warning_ratio = {DEFAULT_CONTEXT_WARNING_RATIO:g}
# 压缩摘要目标 Token 数
compaction_target_tokens = {DEFAULT_COMPACTION_TARGET_TOKENS}
# 原始上下文归档目录（相对项目根目录）
archive_dir = {DEFAULT_CONTEXT_ARCHIVE_DIR}

[sessions]
# 会话历史目录（相对项目根目录）
dir = {DEFAULT_SESSIONS_DIR}

[mcp]
# MCP Server 配置文件（相对项目根目录）
config_file = {DEFAULT_MCP_CONFIG_FILENAME}
# .mcp.json 中未写 timeout 时的默认值（秒）
default_timeout = {DEFAULT_MCP_TIMEOUT:g}
connect_grace_seconds = {DEFAULT_MCP_CONNECT_GRACE_SECONDS:g}
call_grace_seconds = {DEFAULT_MCP_CALL_GRACE_SECONDS:g}
shutdown_grace_seconds = {DEFAULT_MCP_SHUTDOWN_GRACE_SECONDS:g}
# MCP 工具结果进入上下文前的最大字符数
max_result_chars = {DEFAULT_MCP_MAX_RESULT_CHARS}

[tui]
# 右侧 MCP 状态栏宽度（列）
sidebar_width = {DEFAULT_TUI_SIDEBAR_WIDTH}
# /resume 列表最多显示的历史会话数量
session_list_limit = {DEFAULT_TUI_SESSION_LIST_LIMIT}
# TUI 中恢复历史消息时的预览字符数
message_preview_chars = {DEFAULT_TUI_MESSAGE_PREVIEW_CHARS}

[search]
# 百度千帆百度搜索 API Key，用于 searchweb 工具
baidu_qianfan_api_key =
"""


def default_config_path() -> Path:
    """Return the configuration path for the current project directory.

    DAgent is started from the project it operates on; ``config.ini`` lives
    next to ``.agents`` and ``.mcp.json`` instead of next to this source file.
    The path is resolved on every call so a process that changes its working
    directory (for example an embedded UI) still uses the current project.
    """

    return Path.cwd() / CONFIG_FILENAME


@dataclass(frozen=True)
class Settings:
    """Runtime settings shared by every UI."""

    base_url: str
    api_key: str
    model: str
    stream: bool = True
    timeout: float = DEFAULT_TIMEOUT
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    search_api_key: str = ""
    context_window: int | None = None
    context_warning_ratio: float = DEFAULT_CONTEXT_WARNING_RATIO
    compaction_target_tokens: int = DEFAULT_COMPACTION_TARGET_TOKENS
    system_prompt: str = ""
    agents_filename: str = DEFAULT_AGENTS_FILENAME
    context_archive_dir: str = DEFAULT_CONTEXT_ARCHIVE_DIR
    sessions_dir: str = DEFAULT_SESSIONS_DIR
    mcp_config_filename: str = DEFAULT_MCP_CONFIG_FILENAME
    mcp_default_timeout: float = DEFAULT_MCP_TIMEOUT
    mcp_connect_grace_seconds: float = DEFAULT_MCP_CONNECT_GRACE_SECONDS
    mcp_call_grace_seconds: float = DEFAULT_MCP_CALL_GRACE_SECONDS
    mcp_shutdown_grace_seconds: float = DEFAULT_MCP_SHUTDOWN_GRACE_SECONDS
    mcp_max_result_chars: int = DEFAULT_MCP_MAX_RESULT_CHARS
    tui_sidebar_width: int = DEFAULT_TUI_SIDEBAR_WIDTH
    tui_session_list_limit: int = DEFAULT_TUI_SESSION_LIST_LIMIT
    tui_message_preview_chars: int = DEFAULT_TUI_MESSAGE_PREVIEW_CHARS


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from an INI file.

    ``path`` defaults to ``<current working directory>/config.ini``, the same
    project directory that holds ``.agents`` and ``.mcp.json``. Passing a path
    explicitly keeps the core reusable from a web or desktop composition root.
    """

    config_path = Path(path).expanduser() if path is not None else default_config_path()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")

    parser = configparser.ConfigParser()
    try:
        read_files = parser.read(config_path, encoding="utf-8")
    except (configparser.Error, OSError) as error:
        raise ConfigError(f"读取配置失败：{error}") from error
    if not read_files:
        raise ConfigError(f"无法读取配置文件：{config_path}")
    if not parser.has_section("llm"):
        raise ConfigError("配置文件中缺少 [llm] 段")

    try:
        base_url = parser.get("llm", "base_url").strip().rstrip("/")
        api_key = parser.get("llm", "api_key", fallback="").strip()
        model = parser.get("llm", "model").strip()
        stream = parser.getboolean("llm", "stream", fallback=True)
        timeout = parser.getfloat("llm", "timeout", fallback=DEFAULT_TIMEOUT)
        max_tool_rounds = parser.getint(
            "llm",
            "max_tool_rounds",
            fallback=_get_int(
                parser, "agent", "max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS
            ),
        )
        context_window = _parse_context_window(
            parser.get(
                "llm",
                "context_window",
                fallback=_get_str(parser, "context", "context_window", ""),
            )
        )
        context_warning_ratio = _get_float(
            parser,
            "context",
            "warning_ratio",
            DEFAULT_CONTEXT_WARNING_RATIO,
        )
        compaction_target_tokens = _get_int(
            parser,
            "context",
            "compaction_target_tokens",
            DEFAULT_COMPACTION_TARGET_TOKENS,
        )
        system_prompt = _get_str(parser, "agent", "system_prompt", "")
        agents_filename = _get_str(
            parser, "agent", "agents_file", DEFAULT_AGENTS_FILENAME
        )
        context_archive_dir = _get_str(
            parser,
            "context",
            "archive_dir",
            DEFAULT_CONTEXT_ARCHIVE_DIR,
        )
        sessions_dir = _get_str(
            parser, "sessions", "dir", DEFAULT_SESSIONS_DIR
        )
        mcp_config_filename = _get_str(
            parser, "mcp", "config_file", DEFAULT_MCP_CONFIG_FILENAME
        )
        mcp_default_timeout = _get_float(
            parser, "mcp", "default_timeout", DEFAULT_MCP_TIMEOUT
        )
        mcp_connect_grace_seconds = _get_float(
            parser,
            "mcp",
            "connect_grace_seconds",
            DEFAULT_MCP_CONNECT_GRACE_SECONDS,
        )
        mcp_call_grace_seconds = _get_float(
            parser,
            "mcp",
            "call_grace_seconds",
            DEFAULT_MCP_CALL_GRACE_SECONDS,
        )
        mcp_shutdown_grace_seconds = _get_float(
            parser,
            "mcp",
            "shutdown_grace_seconds",
            DEFAULT_MCP_SHUTDOWN_GRACE_SECONDS,
        )
        mcp_max_result_chars = _get_int(
            parser,
            "mcp",
            "max_result_chars",
            DEFAULT_MCP_MAX_RESULT_CHARS,
        )
        tui_sidebar_width = _get_int(
            parser, "tui", "sidebar_width", DEFAULT_TUI_SIDEBAR_WIDTH
        )
        tui_session_list_limit = _get_int(
            parser,
            "tui",
            "session_list_limit",
            DEFAULT_TUI_SESSION_LIST_LIMIT,
        )
        tui_message_preview_chars = _get_int(
            parser,
            "tui",
            "message_preview_chars",
            DEFAULT_TUI_MESSAGE_PREVIEW_CHARS,
        )

        search_api_key = ""
        if parser.has_section("search"):
            search_api_key = parser.get(
                "search", "baidu_qianfan_api_key", fallback="", raw=True
            ).strip()
    except (configparser.Error, ValueError) as error:
        raise ConfigError(f"配置项格式错误：{error}") from error

    if not base_url:
        raise ConfigError("[llm] base_url 不能为空")
    if not model:
        raise ConfigError("[llm] model 不能为空")
    if timeout <= 0:
        raise ConfigError("[llm] timeout 必须大于 0")
    if max_tool_rounds <= 0:
        raise ConfigError("[llm] max_tool_rounds 必须大于 0")
    if context_window is not None and context_window <= 0:
        raise ConfigError("[llm] context_window 必须是正整数")
    if not 0 < context_warning_ratio <= 1:
        raise ConfigError("[context] warning_ratio 必须在 (0, 1] 范围内")
    if compaction_target_tokens <= 0:
        raise ConfigError("[context] compaction_target_tokens 必须大于 0")
    if not agents_filename:
        raise ConfigError("[agent] agents_file 不能为空")
    if not context_archive_dir:
        raise ConfigError("[context] archive_dir 不能为空")
    if not sessions_dir:
        raise ConfigError("[sessions] dir 不能为空")
    if not mcp_config_filename:
        raise ConfigError("[mcp] config_file 不能为空")
    if mcp_default_timeout <= 0:
        raise ConfigError("[mcp] default_timeout 必须大于 0")
    if mcp_connect_grace_seconds < 0:
        raise ConfigError("[mcp] connect_grace_seconds 不能为负数")
    if mcp_call_grace_seconds < 0:
        raise ConfigError("[mcp] call_grace_seconds 不能为负数")
    if mcp_shutdown_grace_seconds < 0:
        raise ConfigError("[mcp] shutdown_grace_seconds 不能为负数")
    if mcp_max_result_chars <= 0:
        raise ConfigError("[mcp] max_result_chars 必须大于 0")
    if tui_sidebar_width < 20:
        raise ConfigError("[tui] sidebar_width 必须至少为 20")
    if tui_session_list_limit <= 0:
        raise ConfigError("[tui] session_list_limit 必须大于 0")
    if tui_message_preview_chars <= 0:
        raise ConfigError("[tui] message_preview_chars 必须大于 0")

    return Settings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        stream=stream,
        timeout=timeout,
        max_tool_rounds=max_tool_rounds,
        search_api_key=search_api_key,
        context_window=context_window,
        context_warning_ratio=context_warning_ratio,
        compaction_target_tokens=compaction_target_tokens,
        system_prompt=system_prompt,
        agents_filename=agents_filename,
        context_archive_dir=context_archive_dir,
        sessions_dir=sessions_dir,
        mcp_config_filename=mcp_config_filename,
        mcp_default_timeout=mcp_default_timeout,
        mcp_connect_grace_seconds=mcp_connect_grace_seconds,
        mcp_call_grace_seconds=mcp_call_grace_seconds,
        mcp_shutdown_grace_seconds=mcp_shutdown_grace_seconds,
        mcp_max_result_chars=mcp_max_result_chars,
        tui_sidebar_width=tui_sidebar_width,
        tui_session_list_limit=tui_session_list_limit,
        tui_message_preview_chars=tui_message_preview_chars,
    )


def ensure_config(path: str | Path | None = None) -> tuple[Path, bool]:
    """Create a complete mock ``config.ini`` when it does not exist.

    Returns ``(path, created)``. Existing files are never overwritten.
    """

    config_path = Path(path).expanduser() if path is not None else default_config_path()
    if config_path.is_file():
        return config_path, False

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with config_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(DEFAULT_CONFIG_TEXT)
    except FileExistsError:
        return config_path, False
    except OSError as error:
        raise ConfigError(f"创建默认配置文件失败：{config_path}（{error}）") from error
    return config_path, True


def _get_str(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    default: str,
) -> str:
    if not parser.has_section(section):
        return default
    return parser.get(section, option, fallback=default).strip()


def _get_float(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    default: float,
) -> float:
    if not parser.has_section(section):
        return default
    return parser.getfloat(section, option, fallback=default)


def _get_int(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    default: int,
) -> int:
    if not parser.has_section(section):
        return default
    return parser.getint(section, option, fallback=default)


def _parse_context_window(value: str) -> int | None:
    """Accept an integer token count or a short ``128k`` / ``1m`` value."""

    raw = str(value or "").strip().lower()
    if not raw:
        return None
    multiplier = 1
    if raw.endswith("k"):
        multiplier = 1_000
        raw = raw[:-1].strip()
    elif raw.endswith("m"):
        multiplier = 1_000_000
        raw = raw[:-1].strip()
    tokens = int(float(raw) * multiplier)
    return tokens
