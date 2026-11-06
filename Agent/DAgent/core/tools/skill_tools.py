"""Agent Skills tools: on-demand loading and creation."""

from __future__ import annotations

from ..skills import SkillCatalog, SkillError
from .registry import Tool


def loadskill(
    catalog: SkillCatalog, name: str, filename: str | None = None
) -> str:
    """Load a skill's ``SKILL.md`` or one of its attachment files."""

    try:
        return catalog.load(name, filename)
    except SkillError as error:
        return f"错误：{error}"


def createskill(
    catalog: SkillCatalog,
    name: str,
    skill_context: str | None = None,
    *,
    skillContext: str | None = None,
) -> str:
    """Create a new skill below the catalog's ``.agents/skills`` directory.

    ``skillContext`` is the JSON-schema parameter name exposed to the model;
    ``skill_context`` is accepted as the Python-style equivalent.
    """

    context = skill_context if skill_context is not None else skillContext
    if context is None:
        return "错误：缺少 skillContext 参数"

    try:
        skill = catalog.create_skill(name, context)
    except SkillError as error:
        return f"错误：{error}"

    lines = [
        f"已创建技能：{skill.name}",
        f"目录：{skill.directory}",
        f"文件：{skill.skill_file}",
    ]
    requested = str(name or "").strip()
    if requested and requested != skill.name:
        lines.append(
            f"说明：name 已规范化为 {skill.name}"
            "（只使用小写英文字母、数字和连字符）。"
        )
    lines.append(
        "SKILL.md 已包含 YAML Frontmatter（name、description）和正文。"
        "现在可以通过 loadskill 加载该技能；如需 references/、scripts/ 等"
        "附加目录或文件，请继续调用 create_directory、create_file 等工具。"
    )
    return "\n".join(lines)


def tools(catalog: SkillCatalog) -> list[Tool]:
    def load_handler(name: str, filename: str | None = None) -> str:
        return loadskill(catalog, name, filename)

    def create_handler(name: str, skillContext: str) -> str:
        return createskill(catalog, name, skillContext=skillContext)

    return [
        Tool(
            name="loadskill",
            description=(
                "加载指定技能的完整说明，或该技能目录中的附件文件。"
                "只传 name 时返回该技能 SKILL.md 的全文；"
                "同时传 filename 时只读取该附件文件，不会重复加载 SKILL.md。"
                "filename 支持相对于技能目录的路径、相对于项目根目录的路径和绝对路径；"
                "只给文件名时会递归搜索该技能目录，若有多个同名文件则返回候选路径。"
                "只能读取该技能目录内部的文件。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名称"},
                    "filename": {
                        "type": "string",
                        "description": (
                            "可选。技能附件的路径或文件名。省略时加载 SKILL.md 全文。"
                        ),
                    },
                },
                "required": ["name"],
            },
            handler=load_handler,
        ),
        Tool(
            name="createskill",
            description=(
                "根据用户要求创建一个新的 Agent Skill。"
                "当用户要求创建、新增或生成技能时调用本工具。"
                "工具会在当前项目的 .agents/skills/<name>/ 目录下创建 SKILL.md，"
                "并确保文件包含符合 Agent Skills 规范的 YAML Frontmatter"
                "（name、description）和正文。"
                "name 是技能名，建议使用小写英文字母、数字和连字符；"
                "大写字母、空格和下划线会被规范化。"
                "skillContext 是要写入 SKILL.md 的完整内容，可以包含 YAML Frontmatter；"
                "如果省略 Frontmatter，工具会根据 name 和正文自动生成 name 与 description。"
                "如果名称对应的技能或目录已经存在，工具会返回明确提示，不会覆盖原有内容。"
                "如需添加 references/、scripts/ 等目录或辅助文件，创建完 SKILL.md 后"
                "继续使用 create_directory、create_file 等文件工具。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "技能名称，例如 weekly-report。"
                            "应使用小写英文字母、数字和连字符。"
                        ),
                    },
                    "skillContext": {
                        "type": "string",
                        "description": (
                            "要写入 SKILL.md 的完整内容。可以包含 YAML Frontmatter；"
                            "缺少时会自动生成 name 和 description，其余内容作为正文写入。"
                        ),
                    },
                },
                "required": ["name", "skillContext"],
            },
            handler=create_handler,
        ),
    ]
