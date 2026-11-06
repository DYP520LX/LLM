"""Composition helpers shared by all UI front ends."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .agent import Agent
from .config import Settings
from .llm import OpenAICompatibleClient
from .mcp_client import MCPClientManager, register_mcp_tools
from .mcp_config import load_mcp_config
from .skills import SkillCatalog
from .tools import build_registry

SYSTEM_PROMPT = """你是 DAgent，一个可以直接调用工具的助手。

- 需要实时信息、本地环境信息或精确计算时，优先调用合适的工具，不要凭记忆猜测，也不要把操作步骤直接推给用户让用户自己执行。
- 工具列表中可能包含由外部 MCP Server 提供的工具（描述末尾会标注来源）。它们与本地工具用法相同，请照常根据 Schema 传入参数。
- 工具返回结果后，直接基于结果用中文回答用户的问题。
- 如果工具报告失败，先根据错误信息更换参数、命令或等价接口后重试；避免重复完全相同的失败调用。确实无法完成时，再简要说明失败原因。
- 当用户要求创建新的 Agent Skill 时，调用 createskill 创建 SKILL.md；如果还需要 references/、scripts/ 等附件，创建完成后继续调用文件工具。
- 不要编造工具没有返回的数据。"""


def build_system_prompt(catalog: SkillCatalog) -> str:
    """Append the discovered skill metadata (name + description) to the prompt."""

    section = catalog.prompt_section()
    if not section:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{section}"


def create_agent(
    settings: Settings, project_root: str | Path | None = None
) -> Agent:
    """Build an agent with the default provider and tool registry.

    A web or desktop UI can call this function directly, or inject its own
    ``LLMClient``/``ToolRegistry`` into :class:`Agent` for testing.

    ``project_root`` defaults to the current working directory, which is where
    ``.agents/skills`` and ``.mcp.json`` are looked up. Skill bodies are never
    loaded here: only ``name`` and ``description`` reach the system prompt, and
    the full content is fetched lazily by the ``loadskill`` tool.

    MCP is optional.  Configuration or connection problems are collected in
    ``Agent.startup_notes`` instead of preventing the local tools from working.
    """

    catalog = SkillCatalog.discover(project_root)

    registry = build_registry(settings.search_api_key, catalog)

    notes: list[str] = []
    closers: list[Callable[[], None]] = []

    mcp_config = load_mcp_config(project_root)
    notes.extend(mcp_config.warnings)
    if mcp_config.servers:
        manager = MCPClientManager(mcp_config.servers)
        manager.start()
        register_mcp_tools(registry, manager)
        notes.extend(manager.startup_warnings)
        closers.append(manager.close)

    return Agent(
        client=OpenAICompatibleClient(settings),
        tools=registry,
        max_tool_rounds=settings.max_tool_rounds,
        system_prompt=build_system_prompt(catalog),
        startup_notes=notes,
        closers=closers,
    )
