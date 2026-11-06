"""Configuration loading for DAgent.

The core layer only knows about :class:`Settings`; each UI can decide how to
pass a configuration path or otherwise construct settings.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

CONFIG_FILENAME = "config.ini"


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
    timeout: float = 120.0
    max_tool_rounds: int = 10
    search_api_key: str = ""


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
        timeout = parser.getfloat("llm", "timeout", fallback=120.0)
        max_tool_rounds = parser.getint("llm", "max_tool_rounds", fallback=10)
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

    return Settings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        stream=stream,
        timeout=timeout,
        max_tool_rounds=max_tool_rounds,
        search_api_key=search_api_key,
    )
