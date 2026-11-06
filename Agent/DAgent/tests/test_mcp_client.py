from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.mcp_client import (
    MCP_AVAILABLE,
    MCPClientManager,
    MCPToolInfo,
    register_mcp_tools,
)
from core.mcp_config import STDIO, MCPServerConfig
from core.bootstrap import create_agent
from core.config import Settings
from core.events import ToolCallFinished, TurnCompleted
from core.llm import LLMTextDelta, LLMTurnComplete
from core.tools import build_registry
from core.tools.registry import Tool, ToolRegistry
from core.types import ToolCall, ToolResult


class FakeManager:
    def __init__(self, infos: list[MCPToolInfo]) -> None:
        self._infos = tuple(infos)
        self._bindings = {}
        self.calls: list[tuple[str, dict]] = []

    @property
    def tool_infos(self) -> tuple[MCPToolInfo, ...]:
        return self._infos

    def bind(self, binding) -> None:
        self._bindings[binding.exposed_name] = binding

    def call_exposed_tool(self, exposed_name: str, arguments: dict) -> ToolResult:
        self.calls.append((exposed_name, arguments))
        return ToolResult(
            name=exposed_name,
            content=f"called {exposed_name}: {json.dumps(arguments)}",
            ok=True,
        )

    @property
    def bindings(self):
        return tuple(self._bindings.values())


def _info(server: str, name: str, description: str = "") -> MCPToolInfo:
    return MCPToolInfo(
        server=server,
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
    )


class MCPRegistrationTests(unittest.TestCase):
    def test_unique_valid_tool_names_are_not_prefixed(self) -> None:
        manager = FakeManager([_info("server-a", "calculate")])
        registry = build_registry()

        bindings = register_mcp_tools(registry, manager)

        self.assertEqual(bindings[0].exposed_name, "calculate")
        self.assertIn("calculate", registry.names())
        # The description records the source server for the model.
        schema = next(
            item
            for item in registry.schemas()
            if item["function"]["name"] == "calculate"
        )
        self.assertIn("MCP Server server-a", schema["function"]["description"])

    def test_local_tool_collision_gets_server_prefix(self) -> None:
        manager = FakeManager([_info("server-a", "fetch_web")])
        registry = build_registry()

        bindings = register_mcp_tools(registry, manager)

        self.assertEqual(bindings[0].exposed_name, "server-a__fetch_web")
        self.assertEqual(bindings[0].tool, "fetch_web")
        self.assertIn("fetch_web", registry.names())
        self.assertIn("server-a__fetch_web", registry.names())

    def test_duplicate_tool_names_across_servers_both_get_prefixes(self) -> None:
        manager = FakeManager(
            [
                _info("server-a", "search"),
                _info("server-b", "search"),
            ]
        )
        registry = build_registry()

        bindings = register_mcp_tools(registry, manager)

        self.assertEqual(
            [binding.exposed_name for binding in bindings],
            ["server-a__search", "server-b__search"],
        )

    def test_invalid_characters_are_sanitized_and_made_unique(self) -> None:
        manager = FakeManager(
            [
                _info("server a", "tool.one"),
                _info("server-a", "tool one"),
            ]
        )
        registry = build_registry()

        bindings = register_mcp_tools(registry, manager)

        names = [binding.exposed_name for binding in bindings]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(len(name) <= 64 for name in names))
        self.assertTrue(all("." not in name for name in names))
        self.assertTrue(all(" " not in name for name in names))
        self.assertEqual(registry.names().count(names[0]), 1)

    def test_long_names_are_truncated_to_64_characters(self) -> None:
        manager = FakeManager(
            [_info("s" * 60, "t" * 60), _info("s" * 60, "t" * 59 + "u")]
        )
        registry = build_registry()

        bindings = register_mcp_tools(registry, manager)

        self.assertEqual(len(bindings), 2)
        for binding in bindings:
            self.assertLessEqual(len(binding.exposed_name), 64)
        self.assertNotEqual(
            bindings[0].exposed_name, bindings[1].exposed_name
        )

    def test_handler_dispatches_through_binding_table(self) -> None:
        manager = FakeManager([_info("server-a", "calculate")])
        registry = build_registry()
        bindings = register_mcp_tools(registry, manager)

        result = registry.execute(
            ToolCall(
                "call_1",
                bindings[0].exposed_name,
                '{"year": 2024}',
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(manager.calls[0][0], "calculate")
        self.assertEqual(manager.calls[0][1], {"year": 2024})
        self.assertIn("called calculate", result.content)

    def test_registry_preserves_tool_result_errors(self) -> None:
        registry = ToolRegistry(
            [
                Tool(
                    name="failing",
                    description="",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: ToolResult(
                        name="internal", content="失败详情", ok=False
                    ),
                )
            ]
        )

        result = registry.execute(ToolCall("call_1", "failing", "{}"))

        self.assertFalse(result.ok)
        self.assertEqual(result.name, "failing")
        self.assertEqual(result.content, "失败详情")


SERVER_SCRIPT = textwrap.dedent(
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("DAgentTestServer")

    @server.tool()
    def add(a: int, b: int) -> int:
        \"\"\"Add two numbers.\"\"\"
        return a + b

    @server.tool()
    def echo(text: str) -> str:
        \"\"\"Echo text back.\"\"\"
        return text

    if __name__ == "__main__":
        server.run(transport="stdio")
    """
)


@unittest.skipUnless(MCP_AVAILABLE, "mcp Python SDK is not installed")
class MCPStdioIntegrationTests(unittest.TestCase):
    def test_connect_list_and_call_over_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "server.py"
            script.write_text(SERVER_SCRIPT, encoding="utf-8")
            config = MCPServerConfig(
                name="local-test",
                transport=STDIO,
                command=sys.executable,
                args=(str(script),),
                timeout=30,
            )
            manager = MCPClientManager((config,))
            try:
                manager.start()
                self.assertEqual(manager.startup_warnings, ())
                self.assertEqual(
                    [info.name for info in manager.tool_infos],
                    ["add", "echo"],
                )

                registry = build_registry()
                bindings = register_mcp_tools(registry, manager)
                add = next(
                    binding for binding in bindings if binding.tool == "add"
                )

                result = registry.execute(
                    ToolCall(
                        "call_1",
                        add.exposed_name,
                        '{"a": 2, "b": 5}',
                    )
                )
                self.assertTrue(result.ok)
                self.assertIn("7", result.content)

                failure = registry.execute(
                    ToolCall(
                        "call_2",
                        add.exposed_name,
                        '{"a": "not a number", "b": 1}',
                    )
                )
                self.assertFalse(failure.ok)
                self.assertIn("返回错误", failure.content)
            finally:
                manager.close()

    def test_failed_server_does_not_prevent_other_tools(self) -> None:
        config = MCPServerConfig(
            name="broken",
            transport=STDIO,
            command=sys.executable,
            args=("-c", "raise SystemExit(3)"),
            timeout=5,
        )
        manager = MCPClientManager((config,))
        try:
            manager.start()
            self.assertTrue(manager.startup_warnings)
            self.assertIn("broken", manager.startup_warnings[0])
            self.assertEqual(manager.tool_infos, ())

            registry = build_registry()
            self.assertEqual(register_mcp_tools(registry, manager), ())
            self.assertIn("is_leap_year", registry.names())
        finally:
            manager.close()


class FakeLLM:
    def __init__(self, responses) -> None:
        self.responses = list(responses)

    def stream(self, messages, tools):
        text, tool_calls = self.responses.pop(0)
        if text:
            yield LLMTextDelta(text)
        yield LLMTurnComplete(text, tool_calls)


@unittest.skipUnless(MCP_AVAILABLE, "mcp Python SDK is not installed")
class MCPBootstrapIntegrationTests(unittest.TestCase):
    def test_create_agent_loads_project_config_and_calls_mcp_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "server.py"
            script.write_text(SERVER_SCRIPT, encoding="utf-8")
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "local-test": {
                                "type": "stdio",
                                "command": sys.executable,
                                "args": [str(script)],
                                "timeout": 30,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            settings = Settings(
                base_url="http://example.invalid",
                api_key="test",
                model="test",
            )
            agent = create_agent(settings, project_root=root)
            try:
                self.assertEqual(agent.startup_notes, ())
                self.assertIn("add", agent.tool_names)
                self.assertIn("echo", agent.tool_names)

                agent._client = FakeLLM(
                    [
                        (
                            "",
                            [
                                ToolCall(
                                    "call_1",
                                    "echo",
                                    '{"text": "hello from mcp"}',
                                )
                            ],
                        ),
                        ("完成", []),
                    ]
                )
                events = list(agent.run_turn("调用 MCP 工具"))

                finished = [
                    event
                    for event in events
                    if isinstance(event, ToolCallFinished)
                ]
                self.assertEqual(len(finished), 1)
                self.assertTrue(finished[0].result.ok)
                self.assertIn("hello from mcp", finished[0].result.content)
                self.assertIsInstance(events[-1], TurnCompleted)
            finally:
                agent.close()


if __name__ == "__main__":
    unittest.main()
