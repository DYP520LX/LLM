"""Read the ``.mcp.json`` file from the current project directory.

The file follows the widely used MCP client format::

    {
      "mcpServers": {
        "my-server": {
          "type": "stdio",
          "command": "python",
          "args": ["server.py"],
          "env": {"TOKEN": "${MY_TOKEN}"}
        },
        "remote-server": {
          "type": "streamable_http",
          "url": "https://example.com/mcp",
          "headers": {"Authorization": "Bearer ${MY_TOKEN}"}
        }
      }
    }

Only the two transports required by DAgent are supported: ``stdio`` and
``streamable_http``.  ``http`` / ``streamable-http`` are accepted as aliases of
the latter.  When ``type`` is omitted it is inferred from ``command`` or
``url``.

Configuration problems never abort startup: every invalid server entry is
skipped and reported through :attr:`MCPConfig.warnings`, so the remaining MCP
servers and all local tools keep working.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

MCP_CONFIG_FILENAME = ".mcp.json"
DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 3600.0

STDIO = "stdio"
STREAMABLE_HTTP = "streamable_http"

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class MCPServerConfig:
    """Connection settings for one MCP Server."""

    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class MCPConfig:
    """The parsed ``.mcp.json`` and any non-fatal problems."""

    servers: tuple[MCPServerConfig, ...] = ()
    warnings: tuple[str, ...] = ()
    path: Path | None = None


def load_mcp_config(
    project_root: str | Path | None = None,
    filename: str = MCP_CONFIG_FILENAME,
    default_timeout: float = DEFAULT_TIMEOUT,
) -> MCPConfig:
    """Load ``<project_root>/.mcp.json``.

    ``project_root`` defaults to the process working directory, which is the
    directory DAgent was started in.  A missing file is not an error: MCP is
    optional and DAgent keeps working with its built-in tools only.
    """

    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path.cwd()
    )
    path = root / filename
    if not path.is_file():
        return MCPConfig(path=path)

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        return MCPConfig(
            warnings=(f"无法读取 MCP 配置文件 {path}：{error}",),
            path=path,
        )

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as error:
        return MCPConfig(
            warnings=(
                f"MCP 配置文件 {path} 不是合法 JSON："
                f"第 {error.lineno} 行第 {error.colno} 列：{error.msg}",
            ),
            path=path,
        )

    if not isinstance(data, dict):
        return MCPConfig(
            warnings=(f"MCP 配置文件 {path} 的顶层必须是 JSON 对象",),
            path=path,
        )

    raw_servers = data.get("mcpServers")
    if raw_servers is None:
        raw_servers = data.get("mcp_servers")
    if raw_servers is None:
        return MCPConfig(
            warnings=(f"MCP 配置文件 {path} 缺少 mcpServers 字段",),
            path=path,
        )
    if not isinstance(raw_servers, dict):
        return MCPConfig(
            warnings=(f"MCP 配置文件 {path} 的 mcpServers 必须是 JSON 对象",),
            path=path,
        )

    servers: list[MCPServerConfig] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for raw_name, raw_server in raw_servers.items():
        name = str(raw_name).strip()
        if not name:
            warnings.append("mcpServers 中存在空名称的服务器，已跳过")
            continue
        if name in seen:
            warnings.append(f"MCP Server 名称重复：{name}，已跳过后一个")
            continue
        try:
            server, server_warnings = _parse_server(
                name, raw_server, root, default_timeout
            )
        except ValueError as error:
            warnings.append(f"MCP Server {name!r} 配置无效，已跳过：{error}")
            continue
        if server is None:
            continue
        seen.add(name)
        servers.append(server)
        warnings.extend(f"MCP Server {name!r}：{item}" for item in server_warnings)

    return MCPConfig(servers=tuple(servers), warnings=tuple(warnings), path=path)


def _parse_server(
    name: str,
    raw: Any,
    root: Path,
    default_timeout: float = DEFAULT_TIMEOUT,
) -> tuple[MCPServerConfig | None, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError("服务器配置必须是 JSON 对象")

    if raw.get("disabled") is True or raw.get("enabled") is False:
        return None, []

    warnings: list[str] = []
    transport_value = raw.get("type", raw.get("transport", ""))
    transport = _normalize_transport(transport_value, raw)

    timeout = _parse_timeout(raw.get("timeout"), warnings, default_timeout)

    if transport == STDIO:
        command = _expand(str(raw.get("command", "")).strip(), warnings)
        if not command:
            raise ValueError("stdio 服务器缺少 command")

        raw_args = raw.get("args", [])
        if raw_args is None:
            raw_args = []
        if not isinstance(raw_args, (list, tuple)):
            raise ValueError("args 必须是字符串数组")
        args = tuple(_expand(str(item), warnings) for item in raw_args)

        raw_env = raw.get("env", {})
        if raw_env is None:
            raw_env = {}
        if not isinstance(raw_env, dict):
            raise ValueError("env 必须是字符串键值对象")
        env = {
            str(key): _expand(str(value), warnings)
            for key, value in raw_env.items()
        }

        cwd_value = raw.get("cwd", "")
        cwd = _expand(str(cwd_value).strip(), warnings) if cwd_value else ""
        if cwd:
            cwd_path = Path(cwd).expanduser()
            if not cwd_path.is_absolute():
                cwd_path = root / cwd_path
            cwd = str(cwd_path.resolve())

        return (
            MCPServerConfig(
                name=name,
                transport=STDIO,
                command=command,
                args=args,
                env=env,
                cwd=cwd,
                timeout=timeout,
            ),
            warnings,
        )

    url = _expand(str(raw.get("url", "")).strip(), warnings)
    if not url:
        raise ValueError("streamable_http 服务器缺少 url")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"url 必须是有效的 http/https 地址：{url}")

    raw_headers = raw.get("headers", {})
    if raw_headers is None:
        raw_headers = {}
    if not isinstance(raw_headers, dict):
        raise ValueError("headers 必须是字符串键值对象")
    headers = {
        str(key): _expand(str(value), warnings)
        for key, value in raw_headers.items()
    }

    return (
        MCPServerConfig(
            name=name,
            transport=STREAMABLE_HTTP,
            url=url,
            headers=headers,
            timeout=timeout,
        ),
        warnings,
    )


def _normalize_transport(value: Any, raw: Mapping[str, Any]) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        if raw.get("url"):
            text = STREAMABLE_HTTP
        elif raw.get("command"):
            text = STDIO
        else:
            raise ValueError("缺少 type，且无法从 command/url 推断传输方式")

    if text in {"stdio", "command"}:
        return STDIO
    if text in {"streamable_http", "streamablehttp", "http", "http_stream"}:
        return STREAMABLE_HTTP
    raise ValueError(
        f"不支持的传输类型 {value!r}；只支持 stdio 和 streamable_http"
    )


def _parse_timeout(
    value: Any,
    warnings: list[str],
    default_timeout: float = DEFAULT_TIMEOUT,
) -> float:
    default = float(default_timeout) if default_timeout > 0 else DEFAULT_TIMEOUT
    if value is None or value == "":
        return default
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        warnings.append(f"timeout 不是数字，已使用默认值 {default:g}")
        return default
    if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        warnings.append(
            f"timeout 超出范围 {MIN_TIMEOUT:g}-{MAX_TIMEOUT:g}，"
            f"已使用默认值 {default:g}"
        )
        return default
    return timeout


def _expand(value: str, warnings: list[str]) -> str:
    """Expand ``${VAR}`` references using the process environment."""

    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        missing.add(name)
        return match.group(0)

    expanded = _ENV_REFERENCE.sub(replace, value)
    for name in sorted(missing):
        warnings.append(f"环境变量 {name} 未设置，保留原样")
    return expanded
