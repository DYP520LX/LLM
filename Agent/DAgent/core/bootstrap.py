"""Composition helpers shared by all UI front ends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from .config import Settings
from .llm import OpenAICompatibleClient
from .mcp_client import (
    MCPClientManager,
    MCPServerStatus,
    register_mcp_tools,
)
from .mcp_config import MCPConfig, load_mcp_config
from .skills import SkillCatalog
from .tools import build_registry

SYSTEM_PROMPT = """你是 DAgent，一个可以直接调用工具的助手。

- 需要实时信息、本地环境信息或精确计算时，优先调用合适的工具，不要凭记忆猜测，也不要把操作步骤直接推给用户让用户自己执行。
- 工具列表中可能包含由外部 MCP Server 提供的工具（描述末尾会标注来源）。它们与本地工具用法相同，请照常根据 Schema 传入参数。
- 工具返回结果后，直接基于结果用中文回答用户的问题。
- 如果工具报告失败，先根据错误信息更换参数、命令或等价接口后重试；避免重复完全相同的失败调用。确实无法完成时，再简要说明失败原因。
- 当用户要求创建新的 Agent Skill 时，调用 createskill 创建 SKILL.md；如果还需要 references/、scripts/ 等附件，创建完成后继续调用文件工具。
- 如果当前上下文是压缩摘要且缺少某些历史细节，可以调用 recallContext 在归档中检索原始记录。
- 不要编造工具没有返回的数据。"""


@dataclass(frozen=True)
class MCPLoadResult:
    """Outcome of an asynchronous MCP connection attempt."""

    manager: MCPClientManager | None
    statuses: tuple[MCPServerStatus, ...] = ()
    warnings: tuple[str, ...] = ()
    config_path: Path | None = None
    added_tools: int = 0

    @property
    def connected_count(self) -> int:
        return sum(1 for status in self.statuses if status.connected)


def build_system_prompt(
    catalog: SkillCatalog,
    base_prompt: str = "",
) -> str:
    """Append the discovered skill metadata (name + description) to the prompt."""

    prompt = (base_prompt or SYSTEM_PROMPT).strip()
    section = catalog.prompt_section()
    if not section:
        return prompt
    return f"{prompt}\n\n{section}"


def create_agent(
    settings: Settings,
    project_root: str | Path | None = None,
    *,
    load_mcp: bool = True,
) -> Agent:
    """Build an agent with the default provider and tool registry.

    A web or desktop UI can call this function directly, or inject its own
    ``LLMClient``/``ToolRegistry`` into :class:`Agent` for testing.

    ``project_root`` defaults to the current working directory, which is where
    ``.agents/skills`` and ``.mcp.json`` are looked up. Skill bodies are never
    loaded here: only ``name`` and ``description`` reach the system prompt, and
    the full content is fetched lazily by the ``loadskill`` tool.

    ``load_mcp=False`` returns immediately with only local tools.  The TUI uses
    this to paint its interface first and then calls :func:`load_mcp_into_agent`
    from a worker thread.  The default remains synchronous for API users and
    tests that expect MCP tools to be present when ``create_agent`` returns.

    MCP is optional.  Configuration or connection problems are collected in
    ``Agent.startup_notes`` instead of preventing local tools from working.
    """

    catalog = SkillCatalog.discover(project_root)
    root = catalog.project_root
    context_dir = _resolve_project_path(root, settings.context_archive_dir)
    registry = build_registry(
        settings.search_api_key,
        catalog,
        context_dir,
        settings,
    )

    prompt = build_system_prompt(catalog, settings.system_prompt)
    agent = Agent(
        client=OpenAICompatibleClient(settings),
        tools=registry,
        max_tool_rounds=settings.max_tool_rounds,
        system_prompt=prompt,
        context_window=settings.context_window,
        context_archive_dir=context_dir,
        context_warning_ratio=settings.context_warning_ratio,
        compaction_target_tokens=settings.compaction_target_tokens,
    )

    if load_mcp:
        result = load_mcp_into_agent(agent, settings, root)
        agent.add_startup_notes(result.warnings)
        agent.refresh_context_usage()
    return agent


def load_mcp_into_agent(
    agent: Agent,
    settings: Settings,
    project_root: str | Path | None = None,
    mcp_config: MCPConfig | None = None,
) -> MCPLoadResult:
    """Load MCP tools into an already-created agent.

    This function is synchronous and may block for the configured connection
    timeout, so a UI must call it from a worker thread.  It is intentionally
    separate from :func:`create_agent` to keep TUI startup instant.
    """

    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path.cwd()
    )
    config = mcp_config or load_mcp_config(
        root,
        filename=settings.mcp_config_filename,
        default_timeout=settings.mcp_default_timeout,
    )
    warnings = list(config.warnings)
    if not config.servers:
        return MCPLoadResult(
            manager=None,
            warnings=tuple(warnings),
            config_path=config.path,
        )

    manager = MCPClientManager(
        config.servers,
        connect_grace_seconds=settings.mcp_connect_grace_seconds,
        call_grace_seconds=settings.mcp_call_grace_seconds,
        shutdown_grace_seconds=settings.mcp_shutdown_grace_seconds,
        max_result_chars=settings.mcp_max_result_chars,
        default_timeout=settings.mcp_default_timeout,
    )
    try:
        manager.start()
    except BaseException:
        manager.close()
        raise
    warnings.extend(manager.startup_warnings)
    try:
        bindings = register_mcp_tools(agent.registry, manager)
        statuses = manager.server_statuses
    except BaseException:
        # Never leak the background event loop or stdio children when tool
        # registration fails halfway through.
        manager.close()
        raise
    if bindings or any(status.connected for status in statuses):
        agent.add_closer(manager.close)
    else:
        # No server connected and no tool was registered; release the idle loop.
        manager.close()
        manager = None
    return MCPLoadResult(
        manager=manager,
        statuses=statuses,
        warnings=tuple(warnings),
        config_path=config.path,
        added_tools=len(bindings),
    )


def _resolve_project_path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


__all__ = [
    "MCPLoadResult",
    "SYSTEM_PROMPT",
    "build_system_prompt",
    "create_agent",
    "load_mcp_into_agent",
]
