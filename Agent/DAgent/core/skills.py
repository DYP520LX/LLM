"""Agent Skills discovery, on-demand loading and creation.

A *skill* is a directory below ``<project>/.agents/skills`` that contains a
``SKILL.md`` file. The YAML frontmatter of that file declares the two required
fields ``name`` and ``description``. Only those two fields are read during
startup; the body of ``SKILL.md`` and any bundled attachments stay out of the
conversation until the model calls the ``loadskill`` tool.

New skills are written by the ``createskill`` tool through
:meth:`SkillCatalog.create_skill`, which never overwrites an existing skill.

The project root is the working directory the process was started in, not the
location of this package and not the Python installation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SKILL_FILE = "SKILL.md"
SKILLS_SUBDIR = Path(".agents") / "skills"

_FRONTMATTER_FENCE = "---"
_BLOCK_SCALAR_PATTERN = re.compile(r"^[|>][+-]?$")
_SKILL_NAME_SEPARATORS = re.compile(r"[^a-z0-9]+")
_REPEATED_HYPHENS = re.compile(r"-{2,}")
_PLAIN_SCALAR_UNSAFE_START = frozenset("-?:,[]{}#&*!|>'\"%@`")
_PLAIN_SCALAR_RESERVED = frozenset(
    {"true", "false", "null", "~", "yes", "no", "on", "off"}
)
_YAML_IMPLICIT_SCALAR = re.compile(
    r"^(?:[+-]?\d+(?:\.\d*)?|\d{4}-\d{2}-\d{2}(?:[Tt ].*)?)$"
)


class SkillError(Exception):
    """Raised when a skill cannot be loaded or created."""


def normalize_skill_name(name: str) -> str:
    """Return the canonical slug used for a skill directory and ``name`` field.

    Uppercase letters, spaces, underscores and punctuation are folded into
    lowercase letters, digits and single hyphens. Non-English names and path
    separators are rejected instead of being silently mangled.
    """

    raw = str(name or "").strip()
    if not raw:
        raise SkillError("技能名称不能为空")

    if any(separator in raw for separator in ("/", "\\")):
        raise SkillError(
            f"技能名称不能包含路径分隔符：{raw}。"
            "请使用小写英文字母、数字和连字符，例如 weekly-report"
        )

    for character in raw:
        if character.isascii():
            continue
        if character.isspace() or character in {"_", "-", "."}:
            continue
        raise SkillError(
            f"技能名称只能包含英文字母、数字、空格、下划线和连字符：{raw}。"
            "请改用英文名称，例如 weekly-report"
        )

    slug = _SKILL_NAME_SEPARATORS.sub("-", raw.casefold())
    slug = _REPEATED_HYPHENS.sub("-", slug).strip("-")
    if not slug:
        raise SkillError(
            f"无法从技能名称 {raw!r} 生成合法的英文目录名；"
            "请使用小写英文字母、数字和连字符，例如 weekly-report"
        )
    return slug


@dataclass(frozen=True)
class Skill:
    """A discovered skill; only metadata is kept in memory at startup."""

    name: str
    description: str
    directory: Path
    skill_file: Path

    @property
    def relative_directory(self) -> str:
        return self.directory.as_posix()


@dataclass
class SkillCatalog:
    """The set of skills available to the agent."""

    project_root: Path
    skills_root: Path
    skills: dict[str, Skill] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # -- discovery ---------------------------------------------------------

    @classmethod
    def discover(cls, project_root: str | Path | None = None) -> "SkillCatalog":
        """Scan ``<project_root>/.agents/skills`` for skill directories.

        ``project_root`` defaults to the current working directory, i.e. the
        directory the program was launched from.
        """

        root = Path(project_root).expanduser().resolve() if project_root else Path.cwd()
        return cls.discover_in(root / SKILLS_SUBDIR, project_root=root)

    @classmethod
    def discover_in(
        cls, skills_root: str | Path, project_root: str | Path | None = None
    ) -> "SkillCatalog":
        """Scan ``skills_root`` directly; mainly useful for tests and embedding."""

        root_path = Path(skills_root).expanduser().resolve()
        if project_root is None:
            base = root_path
            for _ in range(len(SKILLS_SUBDIR.parts)):
                base = base.parent
            project_root_path = base
        else:
            project_root_path = Path(project_root).expanduser().resolve()

        catalog = cls(project_root=project_root_path, skills_root=root_path)
        catalog._scan()
        return catalog

    def _scan(self) -> None:
        if not self.skills_root.is_dir():
            return

        for entry in sorted(self.skills_root.iterdir(), key=lambda item: item.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue

            skill_file = _find_skill_file(entry)
            if skill_file is None:
                self.warnings.append(f"跳过 {entry.name}：目录中没有 {SKILL_FILE}")
                continue

            try:
                text = _read_text(skill_file)
            except (OSError, SkillError) as error:
                self.warnings.append(f"跳过 {entry.name}：无法读取 {SKILL_FILE}（{error}）")
                continue

            metadata, _body = _split_frontmatter(text)
            name = metadata.get("name", "").strip()
            description = metadata.get("description", "").strip()
            if not name:
                self.warnings.append(f"跳过 {entry.name}：Frontmatter 缺少 name 字段")
                continue
            if not description:
                self.warnings.append(f"跳过 {entry.name}：Frontmatter 缺少 description 字段")
                continue
            if name in self.skills:
                self.warnings.append(
                    f"跳过 {entry.name}：技能名称 {name} 与 "
                    f"{self.skills[name].directory.name} 重复"
                )
                continue

            self.skills[name] = Skill(
                name=name,
                description=description,
                directory=entry.resolve(),
                skill_file=skill_file.resolve(),
            )

    # -- creation ---------------------------------------------------------

    def create_skill(self, name: str, skill_context: str) -> Skill:
        """Create ``<skills_root>/<slug>/SKILL.md`` without overwriting files.

        ``skill_context`` may be a complete ``SKILL.md`` including frontmatter,
        or only the Markdown body. A missing frontmatter is generated from the
        requested name and the first prose paragraph; a complete frontmatter is
        preserved as written. The new skill is registered in this catalog so
        ``loadskill`` works immediately in the same session.
        """

        slug = normalize_skill_name(name)
        existing = self._find_existing_skill_directory(slug)
        if existing is not None:
            raise SkillError(
                f"技能 {slug} 已存在：{existing}。"
                "为避免覆盖原有内容，未创建任何文件。"
                "如需修改，请先读取现有 SKILL.md，再使用 modify_text_file 等工具。"
            )

        duplicate = next(
            (
                skill
                for skill in self.skills.values()
                if skill.name.casefold() == slug.casefold()
            ),
            None,
        )
        if duplicate is not None:
            raise SkillError(
                f"技能名称 {slug} 已被 {duplicate.directory} 使用。"
                "为避免重名，未创建任何文件。"
            )

        document, description = _build_skill_document(slug, skill_context)
        skill_dir = self.skills_root / slug
        skill_file = skill_dir / SKILL_FILE

        try:
            self.skills_root.mkdir(parents=True, exist_ok=True)
            skill_dir.mkdir(exist_ok=False)
        except FileExistsError as error:
            raise SkillError(
                f"技能目录已存在：{skill_dir}。"
                "为避免覆盖原有内容，未创建任何文件。"
            ) from error
        except OSError as error:
            raise SkillError(f"创建技能目录失败：{skill_dir}（{error}）") from error

        try:
            with skill_file.open("x", encoding="utf-8", newline="\n") as file:
                file.write(document)
        except FileExistsError as error:
            _remove_empty_directory(skill_dir)
            raise SkillError(
                f"技能文件已存在：{skill_file}。"
                "为避免覆盖原有内容，未创建任何文件。"
            ) from error
        except OSError as error:
            _remove_empty_directory(skill_dir)
            raise SkillError(f"写入技能文件失败：{skill_file}（{error}）") from error

        skill = Skill(
            name=slug,
            description=description,
            directory=skill_dir.resolve(),
            skill_file=skill_file.resolve(),
        )
        self.skills[slug] = skill
        return skill

    def _find_existing_skill_directory(self, slug: str) -> Path | None:
        """Find an existing skill directory case-insensitively."""

        if not self.skills_root.is_dir():
            return None
        folded = slug.casefold()
        for entry in self.skills_root.iterdir():
            if entry.is_dir() and entry.name.casefold() == folded:
                return entry.resolve()
        return None

    # -- prompt integration ------------------------------------------------

    def prompt_section(self) -> str:
        """Render the skill metadata block appended to the system prompt."""

        if not self.skills:
            return ""

        lines = ["You have access to the following skills:"]
        for skill in self.skills.values():
            description = " ".join(skill.description.split())
            lines.append(f"{skill.name}: {description}")
        lines.append(
            "Use the 'loadskill' tool to load the full instructions of a skill "
            "when needed. You can also pass its optional 'filename' argument to "
            "load an additional file referenced by that skill."
        )
        return "\n".join(lines)

    # -- loading -----------------------------------------------------------

    def get(self, name: str) -> Skill:
        skill = self.skills.get(name)
        if skill is not None:
            return skill
        folded = name.strip().casefold()
        for candidate in self.skills.values():
            if candidate.name.casefold() == folded:
                return candidate
        available = "、".join(self.skills) or "（无）"
        raise SkillError(f"未知技能：{name}。可用技能：{available}")

    def load(self, name: str, filename: str | None = None) -> str:
        """Return the full ``SKILL.md`` text or one of the skill's attachments."""

        skill = self.get(name)

        if filename is None or not str(filename).strip():
            try:
                return _read_text(skill.skill_file)
            except OSError as error:
                raise SkillError(f"无法读取 {skill.skill_file}：{error}") from error

        target = self._resolve_attachment(skill, str(filename).strip())
        try:
            return _read_text(target)
        except OSError as error:
            raise SkillError(f"无法读取 {target}：{error}") from error

    def _resolve_attachment(self, skill: Skill, filename: str) -> Path:
        skill_dir = skill.directory.resolve()
        raw = Path(filename).expanduser()

        if raw.is_absolute():
            candidates = [raw]
        else:
            candidates = [
                skill_dir / raw,
                self.project_root / raw,
                Path.cwd() / raw,
            ]

        escaped = any(part == ".." for part in raw.parts)
        for candidate in candidates:
            resolved = _safe_resolve(candidate)
            if resolved is None:
                continue
            if not _is_within(resolved, skill_dir):
                if resolved.is_file():
                    escaped = True
                continue
            if resolved.is_file():
                return resolved

        if raw.is_absolute():
            resolved_absolute = _safe_resolve(raw)
            if resolved_absolute is not None and not _is_within(
                resolved_absolute, skill_dir
            ):
                escaped = True

        if escaped:
            raise SkillError(
                f"拒绝读取技能目录之外的文件：{filename}。"
                f"只能读取 {skill.name} 技能目录（{skill_dir}）内的附件。"
            )

        matches = _search_skill_directory(skill_dir, raw)
        if len(matches) == 1:
            return matches[0]
        if matches:
            relative = "\n".join(
                f"- {(match.relative_to(skill_dir)).as_posix()}" for match in matches
            )
            raise SkillError(
                f"技能 {skill.name} 中有多个名为 {filename} 的候选文件，"
                f"请使用 filename 指定其中之一：\n{relative}"
            )

        raise SkillError(
            f"在技能 {skill.name} 的目录中找不到文件：{filename}"
        )


# -- document creation -----------------------------------------------------


def _build_skill_document(slug: str, skill_context: str) -> tuple[str, str]:
    """Return a spec-compliant ``SKILL.md`` document and its description."""

    text = str(skill_context or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not text.strip():
        raise SkillError("skillContext 不能为空：请提供要写入 SKILL.md 的技能内容")

    if text.splitlines()[0].strip() == _FRONTMATTER_FENCE and _frontmatter_block(
        text
    ) is None:
        raise SkillError(
            "skillContext 的 YAML Frontmatter 没有闭合的 --- 分隔线；"
            "请补全 Frontmatter 后重试"
        )

    metadata, body = _split_frontmatter(text)
    if not body.strip():
        raise SkillError(
            "skillContext 缺少正文内容：YAML Frontmatter 之后必须包含技能说明"
        )

    description = metadata.get("description", "").strip()
    if not description:
        description = _derive_description(body, slug)

    frontmatter_name = metadata.get("name", "").strip()
    if frontmatter_name == slug and metadata.get("description", "").strip():
        # The model already supplied a valid document. Keep it unchanged so
        # optional frontmatter fields and formatting survive.
        document = text if text.endswith("\n") else f"{text}\n"
        return document, description

    extras = _frontmatter_extras(_frontmatter_block(text) or [])
    header = [
        f"name: {_yaml_scalar(slug)}",
        f"description: {_yaml_scalar(description)}",
        *extras,
    ]
    document = "---\n" + "\n".join(header) + "\n---\n\n" + body.strip("\n") + "\n"
    return document, description


def _frontmatter_block(text: str) -> list[str] | None:
    """Return the lines between the two frontmatter fences, if complete."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            return lines[1:index]
    return None


def _frontmatter_extras(lines: list[str]) -> list[str]:
    """Keep optional frontmatter entries while dropping name/description."""

    extras: list[str] = []
    skipping_required_key = False
    for line in lines:
        is_top_level = bool(line.strip()) and line[:1] not in {" ", "\t"}
        if is_top_level:
            key, separator, _value = line.partition(":")
            if separator and key.strip() in {"name", "description"}:
                skipping_required_key = True
                continue
            skipping_required_key = False
            extras.append(line.rstrip())
            continue
        if skipping_required_key:
            # Drop continuation lines belonging to a removed block scalar.
            continue
        extras.append(line.rstrip())

    while extras and not extras[0].strip():
        extras.pop(0)
    while extras and not extras[-1].strip():
        extras.pop()
    return extras


def _derive_description(body: str, name: str) -> str:
    """Use the first prose paragraph as the skill description."""

    paragraph: list[str] = []
    in_code_block = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not line:
            if paragraph:
                break
            continue
        if (
            line.startswith("#")
            or line.startswith("<!--")
            or line.startswith("|")
            or line.startswith("---")
        ):
            if paragraph:
                break
            continue
        line = re.sub(r"^(?:[-*+>]|\d+[.)])\s+", "", line)
        if line:
            paragraph.append(line)
        if len(" ".join(paragraph)) >= 200:
            break

    text = " ".join(paragraph).strip()
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 200:
        text = text[:197].rstrip() + "..."
    return text or f"使用 {name} 技能完成相关任务。"


def _yaml_scalar(value: str) -> str:
    """Render a string as a YAML scalar, quoting it only when necessary."""

    if _can_be_plain_scalar(value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _can_be_plain_scalar(value: str) -> bool:
    if not value or value != value.strip():
        return False
    if any(character in value for character in "\n\r\t"):
        return False
    if value[0] in _PLAIN_SCALAR_UNSAFE_START:
        return False
    if ": " in value or " #" in value or value.endswith(":"):
        return False
    if _YAML_IMPLICIT_SCALAR.match(value):
        return False
    return value.casefold() not in _PLAIN_SCALAR_RESERVED


def _remove_empty_directory(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


# -- frontmatter parsing ---------------------------------------------------


def _find_skill_file(directory: Path) -> Path | None:
    exact = directory / SKILL_FILE
    if exact.is_file():
        return exact
    # Tolerate case differences such as skill.md on case-sensitive systems.
    for entry in directory.iterdir():
        if entry.is_file() and entry.name.casefold() == SKILL_FILE.casefold():
            return entry
    return None


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a markdown document into frontmatter metadata and body."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return {}, text

    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            return _parse_frontmatter(lines[1:index]), "\n".join(lines[index + 1 :])
    return {}, text


def _parse_frontmatter(lines: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[:1] in {" ", "\t"}:
            index += 1
            continue

        key, separator, value = line.partition(":")
        if not separator:
            index += 1
            continue

        key = key.strip()
        value = value.strip()
        index += 1

        if _BLOCK_SCALAR_PATTERN.match(value):
            block: list[str] = []
            while index < len(lines):
                following = lines[index]
                if following.strip() and following[:1] not in {" ", "\t"}:
                    break
                block.append(following.strip())
                index += 1
            while block and not block[-1]:
                block.pop()
            joiner = "\n" if value.startswith("|") else " "
            metadata[key] = joiner.join(block).strip()
        else:
            metadata[key] = _unquote(value)
    return metadata


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


# -- path and text helpers -------------------------------------------------


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _search_skill_directory(skill_dir: Path, raw: Path) -> list[Path]:
    """Recursively match a relative path or bare filename inside a skill."""

    parts = tuple(part for part in raw.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        return []

    exact_target = Path(*parts).as_posix().casefold()
    bare_name = parts[-1].casefold() if len(parts) == 1 else None

    matches: list[Path] = []
    for candidate in skill_dir.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            relative = candidate.relative_to(skill_dir).as_posix()
        except ValueError:
            continue
        folded = relative.casefold()
        if bare_name is not None:
            if candidate.name.casefold() == bare_name:
                matches.append(candidate.resolve())
        elif folded == exact_target or folded.endswith("/" + exact_target):
            matches.append(candidate.resolve())

    return sorted(set(matches))


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data:
        raise SkillError(f"不是纯文本文件：{path}")
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text.replace("\r\n", "\n").replace("\r", "\n")
    raise SkillError(f"无法按 UTF-8 或 GBK 解码：{path}")
