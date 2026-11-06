from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.mcp_config import (
    DEFAULT_TIMEOUT,
    STDIO,
    STREAMABLE_HTTP,
    load_mcp_config,
)


class MCPConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, data) -> Path:
        path = self.root / ".mcp.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_missing_file_is_not_an_error(self) -> None:
        config = load_mcp_config(self.root)

        self.assertEqual(config.servers, ())
        self.assertEqual(config.warnings, ())

    def test_reads_stdio_and_streamable_http_servers(self) -> None:
        self._write(
            {
                "mcpServers": {
                    "local": {
                        "type": "stdio",
                        "command": "python",
                        "args": ["server.py", "--flag"],
                        "cwd": "tools",
                        "env": {"TOKEN": "abc"},
                        "timeout": 12,
                    },
                    "remote": {
                        "type": "streamable_http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer token"},
                    },
                }
            }
        )

        config = load_mcp_config(self.root)

        self.assertEqual(config.warnings, ())
        self.assertEqual([server.name for server in config.servers], ["local", "remote"])
        local, remote = config.servers
        self.assertEqual(local.transport, STDIO)
        self.assertEqual(local.command, "python")
        self.assertEqual(local.args, ("server.py", "--flag"))
        self.assertEqual(local.env, {"TOKEN": "abc"})
        self.assertEqual(local.cwd, str((self.root / "tools").resolve()))
        self.assertEqual(local.timeout, 12.0)

        self.assertEqual(remote.transport, STREAMABLE_HTTP)
        self.assertEqual(remote.url, "https://example.com/mcp")
        self.assertEqual(remote.headers, {"Authorization": "Bearer token"})
        self.assertEqual(remote.timeout, DEFAULT_TIMEOUT)

    def test_infers_transport_and_accepts_aliases(self) -> None:
        self._write(
            {
                "mcpServers": {
                    "by-command": {"command": "python", "args": ["server.py"]},
                    "by-url": {"url": "https://example.com/mcp"},
                    "http-alias": {
                        "type": "http",
                        "url": "https://example.com/mcp2",
                    },
                }
            }
        )

        servers = load_mcp_config(self.root).servers

        self.assertEqual(
            [(server.name, server.transport) for server in servers],
            [
                ("by-command", STDIO),
                ("by-url", STREAMABLE_HTTP),
                ("http-alias", STREAMABLE_HTTP),
            ],
        )

    def test_environment_references_are_expanded(self) -> None:
        self._write(
            {
                "mcpServers": {
                    "remote": {
                        "type": "streamable_http",
                        "url": "https://${MCP_HOST}/mcp",
                        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                    }
                }
            }
        )

        with mock.patch.dict(
            os.environ,
            {"MCP_HOST": "example.com", "MCP_TOKEN": "secret"},
            clear=False,
        ):
            config = load_mcp_config(self.root)

        server = config.servers[0]
        self.assertEqual(server.url, "https://example.com/mcp")
        self.assertEqual(
            server.headers, {"Authorization": "Bearer secret"}
        )

    def test_missing_environment_variable_is_reported(self) -> None:
        self._write(
            {
                "mcpServers": {
                    "remote": {
                        "type": "streamable_http",
                        "url": "https://example.com/${MISSING_VAR}",
                    }
                }
            }
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            config = load_mcp_config(self.root)

        self.assertTrue(
            any("MISSING_VAR" in warning for warning in config.warnings)
        )

    def test_invalid_servers_are_skipped_independently(self) -> None:
        self._write(
            {
                "mcpServers": {
                    "good": {
                        "type": "streamable_http",
                        "url": "https://example.com/mcp",
                    },
                    "bad-transport": {"type": "websocket", "url": "ws://x"},
                    "bad-stdio": {"type": "stdio"},
                    "bad-url": {"type": "streamable_http", "url": "ftp://x"},
                    "disabled": {
                        "type": "stdio",
                        "command": "python",
                        "disabled": True,
                    },
                }
            }
        )

        config = load_mcp_config(self.root)

        self.assertEqual([server.name for server in config.servers], ["good"])
        self.assertEqual(len(config.warnings), 3)
        joined = "\n".join(config.warnings)
        self.assertIn("websocket", joined)
        self.assertIn("command", joined)
        self.assertIn("http/https", joined)

    def test_invalid_json_reports_line_and_column(self) -> None:
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {', encoding="utf-8"
        )

        config = load_mcp_config(self.root)

        self.assertEqual(config.servers, ())
        self.assertIn("不是合法 JSON", config.warnings[0])
        self.assertIn("第 1 行", config.warnings[0])

    def test_invalid_utf8_reports_warning_instead_of_raising(self) -> None:
        (self.root / ".mcp.json").write_bytes(b"\xff\xfe\x00\x00")

        config = load_mcp_config(self.root)

        self.assertEqual(config.servers, ())
        self.assertEqual(len(config.warnings), 1)
        self.assertIn("无法读取 MCP 配置文件", config.warnings[0])

    def test_top_level_servers_must_be_an_object(self) -> None:
        self._write({"mcpServers": []})

        config = load_mcp_config(self.root)

        self.assertEqual(config.servers, ())
        self.assertIn("mcpServers 必须是 JSON 对象", config.warnings[0])

    def test_timeout_out_of_range_falls_back_to_default(self) -> None:
        self._write(
            {
                "mcpServers": {
                    "local": {
                        "type": "stdio",
                        "command": "python",
                        "timeout": 0,
                    }
                }
            }
        )

        config = load_mcp_config(self.root)

        self.assertEqual(config.servers[0].timeout, DEFAULT_TIMEOUT)
        self.assertTrue(
            any("timeout" in warning for warning in config.warnings)
        )


if __name__ == "__main__":
    unittest.main()
