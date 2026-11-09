"""Default tool catalog assembly."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..skills import SkillCatalog
from . import (
    calendar_tools,
    cli_tools,
    context_tools,
    file_tools,
    search_tools,
    skill_tools,
    time_tools,
    web_tools,
)
from .registry import Tool, ToolRegistry


def build_registry(
    search_api_key: str = "",
    skill_catalog: SkillCatalog | None = None,
    context_dir: str | Path | None = None,
    settings: Settings | None = None,
) -> ToolRegistry:
    """Create a registry containing all built-in tools.

    ``search_api_key`` comes from :class:`core.config.Settings`; when it is
    empty the ``searchweb`` tool still exists but reports a clear configuration
    error to the model. ``skill_catalog`` enables the ``loadskill`` and
    ``createskill`` tools; when it is ``None`` (as in tests that only exercise
    built-in tools) no skill tool is registered. ``context_dir`` points at the
    archive directory used by ``recallContext``. ``settings`` supplies optional
    per-tool limits and timeouts; omitted values fall back to each tool's
    documented defaults.
    """

    if settings is not None and not search_api_key:
        search_api_key = settings.search_api_key

    registry = ToolRegistry()
    for module in (time_tools, calendar_tools, file_tools):
        for tool in module.tools():
            registry.register(tool)

    if settings is None:
        web_tools_list = web_tools.tools()
        cli_tools_list = cli_tools.tools()
        search_tools_list = search_tools.tools(search_api_key)
    else:
        web_tools_list = web_tools.tools(
            timeout=settings.web_timeout,
            default_max_chars=settings.web_default_max_chars,
            max_chars=settings.web_max_chars,
        )
        cli_tools_list = cli_tools.tools(
            timeout=settings.cli_timeout,
            max_stream_chars=settings.cli_max_stream_chars,
        )
        search_tools_list = search_tools.tools(
            search_api_key,
            timeout=settings.search_timeout,
            default_top_k=settings.search_default_top_k,
            max_top_k=settings.search_max_top_k,
            max_snippet_chars=settings.search_max_snippet_chars,
        )
    for tool in (*web_tools_list, *cli_tools_list, *search_tools_list):
        registry.register(tool)
    if context_dir is None:
        project_root = (
            skill_catalog.project_root if skill_catalog is not None else Path.cwd()
        )
        context_dir = project_root / ".agents" / "context"
    for tool in context_tools.tools(context_dir):
        registry.register(tool)
    if skill_catalog is not None:
        for tool in skill_tools.tools(skill_catalog):
            registry.register(tool)
    return registry


__all__ = ["Tool", "ToolRegistry", "build_registry"]
