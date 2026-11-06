"""Default tool catalog assembly."""

from __future__ import annotations

from ..skills import SkillCatalog
from . import (
    calendar_tools,
    cli_tools,
    file_tools,
    search_tools,
    skill_tools,
    time_tools,
    web_tools,
)
from .registry import Tool, ToolRegistry


def build_registry(
    search_api_key: str = "", skill_catalog: SkillCatalog | None = None
) -> ToolRegistry:
    """Create a registry containing all built-in tools.

    ``search_api_key`` comes from :class:`core.config.Settings`; when it is
    empty the ``searchweb`` tool still exists but reports a clear configuration
    error to the model. ``skill_catalog`` enables the ``loadskill`` and
    ``createskill`` tools; when it is ``None`` (as in tests that only exercise
    built-in tools) no skill tool is registered.
    """

    registry = ToolRegistry()
    for module in (time_tools, calendar_tools, file_tools, web_tools, cli_tools):
        for tool in module.tools():
            registry.register(tool)
    for tool in search_tools.tools(search_api_key):
        registry.register(tool)
    if skill_catalog is not None:
        for tool in skill_tools.tools(skill_catalog):
            registry.register(tool)
    return registry


__all__ = ["Tool", "ToolRegistry", "build_registry"]
