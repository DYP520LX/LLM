from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.bootstrap import build_system_prompt
from core.skills import SkillCatalog, SkillError, normalize_skill_name
from core.tools import build_registry
from core.tools.skill_tools import createskill, loadskill
from core.types import ToolCall


SKILL_MD = """---
name: demo-skill
description: >-
  Demonstrates the skill loader
  with a folded description.
---

# Demo Skill

Step one.
Step two.
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="dagent_skills_")
        self.root = Path(self._temp.name)
        self.skills_root = self.root / ".agents" / "skills"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _make_skill(self, directory: str, text: str = SKILL_MD) -> Path:
        skill_dir = self.skills_root / directory
        _write(skill_dir / "SKILL.md", text)
        return skill_dir

    def test_discovers_only_metadata(self) -> None:
        self._make_skill("demo")
        _write(self.skills_root / "broken" / "README.md", "no skill file")

        catalog = SkillCatalog.discover(self.root)

        self.assertEqual(list(catalog.skills), ["demo-skill"])
        skill = catalog.get("demo-skill")
        self.assertEqual(
            skill.description,
            "Demonstrates the skill loader with a folded description.",
        )
        self.assertEqual(skill.directory, (self.skills_root / "demo").resolve())
        self.assertTrue(any("broken" in warning for warning in catalog.warnings))

    def test_requires_name_and_description(self) -> None:
        _write(
            self.skills_root / "no-description" / "SKILL.md",
            "---\nname: only-name\n---\n\nbody\n",
        )
        _write(
            self.skills_root / "no-name" / "SKILL.md",
            "---\ndescription: 描述\n---\n\nbody\n",
        )

        catalog = SkillCatalog.discover(self.root)

        self.assertEqual(catalog.skills, {})
        self.assertEqual(len(catalog.warnings), 2)

    def test_prompt_section_lists_skills_and_usage(self) -> None:
        self._make_skill("demo")

        catalog = SkillCatalog.discover(self.root)
        section = catalog.prompt_section()

        self.assertIn("You have access to the following skills:", section)
        self.assertIn("demo-skill: Demonstrates", section)
        self.assertIn("loadskill", section)

        prompt = build_system_prompt(catalog)
        self.assertIn("你是 DAgent", prompt)
        self.assertIn("demo-skill", prompt)

    def test_empty_catalog_leaves_prompt_unchanged(self) -> None:
        catalog = SkillCatalog.discover(self.root)

        self.assertEqual(catalog.prompt_section(), "")
        self.assertNotIn("skills", build_system_prompt(catalog))

    def test_duplicate_names_are_skipped(self) -> None:
        self._make_skill("first")
        self._make_skill("second")

        catalog = SkillCatalog.discover(self.root)

        self.assertEqual(list(catalog.skills), ["demo-skill"])
        self.assertTrue(any("重复" in warning for warning in catalog.warnings))

    def test_utf8_bom_frontmatter_is_accepted(self) -> None:
        skill_dir = self.skills_root / "bom"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(
            b"\xef\xbb\xbf" + SKILL_MD.encode("utf-8")
        )

        catalog = SkillCatalog.discover(self.root)

        self.assertEqual(list(catalog.skills), ["demo-skill"])


class SkillLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="dagent_skills_")
        self.root = Path(self._temp.name)
        self.skills_root = self.root / ".agents" / "skills"
        self.skill_dir = self.skills_root / "demo"
        _write(self.skill_dir / "SKILL.md", SKILL_MD)
        _write(self.skill_dir / "references" / "guide.md", "guide content\n")
        _write(self.skill_dir / "scripts" / "run.py", "print('hi')\n")
        _write(self.skill_dir / "a" / "notes.md", "a notes\n")
        _write(self.skill_dir / "b" / "notes.md", "b notes\n")
        _write(self.root / "outside.md", "secret\n")

        self.catalog = SkillCatalog.discover(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_loads_skill_body(self) -> None:
        content = self.catalog.load("demo-skill")
        self.assertIn("# Demo Skill", content)
        self.assertIn("Step two.", content)

    def test_unknown_skill_reports_available_names(self) -> None:
        with self.assertRaises(SkillError) as caught:
            self.catalog.load("missing")
        self.assertIn("demo-skill", str(caught.exception))

    def test_attachment_relative_to_skill_directory(self) -> None:
        self.assertEqual(
            self.catalog.load("demo-skill", "references/guide.md"),
            "guide content\n",
        )

    def test_attachment_project_relative_and_absolute(self) -> None:
        project_relative = ".agents/skills/demo/scripts/run.py"
        self.assertIn("print", self.catalog.load("demo-skill", project_relative))

        absolute = str((self.skill_dir / "references" / "guide.md").resolve())
        self.assertEqual(self.catalog.load("demo-skill", absolute), "guide content\n")

    def test_bare_filename_is_searched_recursively(self) -> None:
        self.assertIn("print", self.catalog.load("demo-skill", "run.py"))

    def test_ambiguous_filename_returns_candidates(self) -> None:
        with self.assertRaises(SkillError) as caught:
            self.catalog.load("demo-skill", "notes.md")

        message = str(caught.exception)
        self.assertIn("a/notes.md", message)
        self.assertIn("b/notes.md", message)

    def test_paths_outside_skill_are_rejected(self) -> None:
        with self.assertRaises(SkillError) as traversal:
            self.catalog.load("demo-skill", "../outside.md")
        self.assertIn("技能目录之外", str(traversal.exception))

        with self.assertRaises(SkillError) as absolute:
            self.catalog.load("demo-skill", str((self.root / "outside.md").resolve()))
        self.assertIn("技能目录之外", str(absolute.exception))

    def test_missing_attachment(self) -> None:
        with self.assertRaises(SkillError) as caught:
            self.catalog.load("demo-skill", "references/missing.md")
        self.assertIn("找不到文件", str(caught.exception))

    def test_tool_returns_error_string(self) -> None:
        self.assertIn("错误：", loadskill(self.catalog, "demo-skill", "missing.md"))
        self.assertIn("# Demo Skill", loadskill(self.catalog, "demo-skill"))


class SkillNameTests(unittest.TestCase):
    def test_normalizes_to_lowercase_and_hyphens(self) -> None:
        self.assertEqual(normalize_skill_name("Weekly Report"), "weekly-report")
        self.assertEqual(normalize_skill_name("  My__Skill!!  "), "my-skill")
        self.assertEqual(normalize_skill_name("weekly-report"), "weekly-report")

    def test_rejects_empty_path_and_non_english_names(self) -> None:
        for name in ("", "   ", "../escape", "a/b", "C:\\skill", "周报"):
            with self.subTest(name=name):
                with self.assertRaises(SkillError):
                    normalize_skill_name(name)


class SkillCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="dagent_skills_")
        self.root = Path(self._temp.name)
        self.skills_root = self.root / ".agents" / "skills"
        self.catalog = SkillCatalog.discover(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_creates_frontmatter_and_body_from_plain_markdown(self) -> None:
        result = createskill(
            self.catalog,
            "weekly-report",
            "# 周报技能\n\n根据周报模板整理本周工作记录。\n\n## 步骤\n\n1. 汇总记录\n",
        )

        skill_file = self.skills_root / "weekly-report" / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        content = skill_file.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        self.assertIn("name: weekly-report", content)
        self.assertIn("description: 根据周报模板整理本周工作记录。", content)
        self.assertIn("# 周报技能", content)
        self.assertIn("## 步骤", content)
        self.assertIn("已创建技能：weekly-report", result)

        discovered = SkillCatalog.discover(self.root)
        self.assertEqual(
            discovered.get("weekly-report").description,
            "根据周报模板整理本周工作记录。",
        )

    def test_preserves_complete_skill_document(self) -> None:
        document = (
            "---\n"
            "name: weekly-report\n"
            "description: 根据周报模板整理本周工作记录。\n"
            "license: MIT\n"
            "---\n"
            "\n"
            "# 周报技能\n"
            "\n"
            "详细步骤。\n"
        )

        createskill(self.catalog, "weekly-report", document)

        skill_file = self.skills_root / "weekly-report" / "SKILL.md"
        self.assertEqual(skill_file.read_text(encoding="utf-8"), document)

    def test_repairs_name_and_keeps_other_frontmatter_fields(self) -> None:
        document = (
            "---\n"
            "name: Old Name\n"
            "description: 使用模板生成周报。\n"
            "allowed-tools: Runcli\n"
            "---\n"
            "\n"
            "# 周报技能\n"
        )

        createskill(self.catalog, "weekly-report", document)

        content = (self.skills_root / "weekly-report" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: weekly-report", content)
        self.assertNotIn("Old Name", content)
        self.assertIn("allowed-tools: Runcli", content)
        self.assertIn("# 周报技能", content)

    def test_normalizes_name_and_reports_the_result(self) -> None:
        result = createskill(self.catalog, "Weekly Report", "整理本周工作记录。")

        self.assertTrue((self.skills_root / "weekly-report" / "SKILL.md").is_file())
        self.assertIn("规范化为 weekly-report", result)

    def test_existing_skill_is_not_overwritten(self) -> None:
        original = "---\nname: existing\ndescription: 原内容\n---\n\n旧正文\n"
        _write(self.skills_root / "existing" / "SKILL.md", original)
        catalog = SkillCatalog.discover(self.root)

        result = createskill(catalog, "existing", "新的正文")

        self.assertTrue(result.startswith("错误："), result)
        self.assertIn("已存在", result)
        self.assertEqual(
            (self.skills_root / "existing" / "SKILL.md").read_text(encoding="utf-8"),
            original,
        )

    def test_existing_directory_without_skill_file_is_not_reused(self) -> None:
        (self.skills_root / "plain-dir").mkdir(parents=True)

        result = createskill(self.catalog, "plain-dir", "正文")

        self.assertTrue(result.startswith("错误："), result)
        self.assertIn("已存在", result)
        self.assertFalse((self.skills_root / "plain-dir" / "SKILL.md").exists())

    def test_duplicate_catalog_name_in_another_directory_is_rejected(self) -> None:
        _write(
            self.skills_root / "first" / "SKILL.md",
            "---\nname: shared\ndescription: 已存在的技能\n---\n\n旧正文\n",
        )
        catalog = SkillCatalog.discover(self.root)

        result = createskill(catalog, "shared", "新的正文")

        self.assertTrue(result.startswith("错误："), result)
        self.assertIn("已被", result)
        self.assertFalse((self.skills_root / "shared").exists())

    def test_invalid_skill_context_is_rejected_without_creating_directories(self) -> None:
        empty = createskill(self.catalog, "empty-skill", "   ")
        self.assertIn("skillContext 不能为空", empty)

        frontmatter_only = createskill(
            self.catalog,
            "frontmatter-only",
            "---\nname: frontmatter-only\ndescription: 只有元数据\n---\n",
        )
        self.assertIn("缺少正文内容", frontmatter_only)

        unclosed = createskill(
            self.catalog, "unclosed", "---\nname: unclosed\ndescription: 未闭合\n"
        )
        self.assertIn("没有闭合", unclosed)

        self.assertFalse((self.skills_root / "empty-skill").exists())
        self.assertFalse((self.skills_root / "frontmatter-only").exists())
        self.assertFalse((self.skills_root / "unclosed").exists())

    def test_created_skill_can_be_loaded_immediately(self) -> None:
        createskill(self.catalog, "demo-new", "# 新技能\n\n加载测试。\n")

        content = self.catalog.load("demo-new")
        self.assertIn("name: demo-new", content)
        self.assertIn("# 新技能", content)

    def test_yaml_sensitive_name_is_quoted(self) -> None:
        createskill(self.catalog, "2024-01-01", "日期技能正文。")

        content = self.catalog.load("2024-01-01")
        self.assertIn('name: "2024-01-01"', content)


class SkillToolRegistrationTests(unittest.TestCase):
    def test_skill_tools_are_registered_with_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dagent_skills_") as temp:
            root = Path(temp)
            _write(root / ".agents" / "skills" / "demo" / "SKILL.md", SKILL_MD)
            catalog = SkillCatalog.discover(root)

            registry = build_registry(skill_catalog=catalog)

            self.assertIn("loadskill", registry.names())
            self.assertIn("createskill", registry.names())
            self.assertEqual(len(registry), 17)

            result = registry.execute(
                ToolCall(
                    "call_1",
                    "loadskill",
                    '{"name": "demo-skill", "filename": "SKILL.md"}',
                )
            )
            self.assertTrue(result.ok)
            self.assertIn("# Demo Skill", result.content)

            missing = registry.execute(
                ToolCall("call_2", "loadskill", '{"name": "nope"}')
            )
            self.assertTrue(missing.ok)
            self.assertTrue(missing.content.startswith("错误："))

            created = registry.execute(
                ToolCall(
                    "call_3",
                    "createskill",
                    '{"name": "new-skill", "skillContext": "# 新技能\\n\\n正文。\\n"}',
                )
            )
            self.assertTrue(created.ok, created.content)
            self.assertIn("已创建技能：new-skill", created.content)
            self.assertTrue(
                (root / ".agents" / "skills" / "new-skill" / "SKILL.md").is_file()
            )

            bad_arguments = registry.execute(
                ToolCall("call_4", "createskill", '{"name": "missing-context"}')
            )
            self.assertFalse(bad_arguments.ok)
            self.assertIn("参数不匹配", bad_arguments.content)

    def test_no_skill_tools_without_catalog(self) -> None:
        names = build_registry().names()
        self.assertNotIn("loadskill", names)
        self.assertNotIn("createskill", names)


if __name__ == "__main__":
    unittest.main()
