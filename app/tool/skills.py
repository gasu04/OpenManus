"""On-demand agent-skill discovery and reading for OpenManus.

OpenManus extends through MCP tools rather than a native skills runtime. This
module supplies the small generic mechanism needed to use agent skills safely:
the system prompt receives only names and descriptions, while ``SkillsTool``
loads a skill's instructions and references only when a task needs them.

Key classes / functions:
    SkillsTool: list installed skills and read path-confined Markdown files.
    read_disabled_skills: load local activation preferences safely.
    set_skill_enabled: persist one skill's activation state atomically.

Usage:
    tool = SkillsTool()
    index = tool.prompt_index()
    result = await tool.execute(command="read", name="reverse-engineer-anything")

Dependencies: Python 3.11+ standard library, Pydantic, and ``app.config``.
Tier 2: invalid paths fail loudly with ``ToolError``; no files are modified.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from app.config import config
from app.exceptions import ToolError
from app.tool.base import BaseTool, ToolResult

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FRONT_MATTER_FIELD_RE = re.compile(r"^(name|description):\s*(.+?)\s*$")


def read_disabled_skills(state_file: Path) -> set[str]:
    """Read disabled skill names without silently enabling skills on corruption.

    Args:
        state_file: JSON file containing ``{"disabled": ["skill-name"]}``.

    Returns:
        Valid skill directory names, or an empty set when no state file exists.

    Raises:
        ToolError: If an existing file is unreadable, malformed, or contains
            invalid entries. This prevents a damaged preferences file from
            silently re-enabling skills the operator disabled.

    Example:
        >>> read_disabled_skills(Path("missing-skills-state.json"))
        set()
    """
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError("Could not read skills state") from exc
    if not isinstance(data, dict) or not isinstance(data.get("disabled"), list):
        raise ToolError("Invalid skills state")
    disabled = data["disabled"]
    if any(
        not isinstance(name, str) or not _NAME_RE.fullmatch(name)
        for name in disabled
    ):
        raise ToolError("Invalid skills state")
    return set(disabled)


def set_skill_enabled(state_file: Path, name: str, enabled: bool) -> None:
    """Persist one skill's enabled state with an atomic file replacement.

    Args:
        state_file: JSON preferences file to update.
        name: Skill directory name, restricted to the loader's safe alphabet.
        enabled: Whether the skill should be available to agents.

    Raises:
        ToolError: If the name is invalid or state cannot be read or written.

    Example:
        >>> set_skill_enabled(Path("skills_state.json"), "research", False)
    """
    if not _NAME_RE.fullmatch(name):
        raise ToolError("Invalid skill name")
    disabled = read_disabled_skills(state_file)
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    temporary = state_file.with_name(f".{state_file.name}.tmp")
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"disabled": sorted(disabled)}, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_file)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ToolError("Could not save skills state") from exc


class SkillsTool(BaseTool):
    """List and read agent skills without putting them all in the prompt.

    Attributes:
        root: Directory containing one subdirectory per skill.
        state_file: Local JSON file containing disabled skill names.
        max_chars: Maximum characters returned by one read or prompt index.
    """

    name: str = "skills"
    description: str = (
        "Discover and read installed agent skills. Use command='list' to see "
        "available skills, then command='read' with a skill name to load its "
        "instructions. Optionally pass a relative Markdown path such as "
        "references/javascript-applications.md. Use this before specialized work."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["list", "read"],
                    "description": "List skills or read one skill document.",
                },
                "name": {
                    "type": "string",
                    "description": "Skill directory name, required for read.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional relative Markdown file inside the skill.",
                },
            },
            "required": ["command"],
        }
    )
    root: Path = Field(default_factory=lambda: config.skills_root)
    state_file: Path = Field(default_factory=lambda: config.skills_state_file)
    max_chars: int = Field(default_factory=lambda: config.skills_max_chars)

    def _skill_dir(self, name: str) -> Path:
        """Return a path-confined skill directory or raise ``ToolError``."""
        if not _NAME_RE.fullmatch(name):
            raise ToolError("Invalid skill name")
        candidate = (self.root / name).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("Skill path escapes the skills directory") from exc
        if not candidate.is_dir():
            raise ToolError(f"Skill not found: {name}")
        return candidate

    def _read_limited(self, path: Path) -> str:
        """Read UTF-8 Markdown with a per-file cap and an explicit marker."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Could not read skill document: {path.name}") from exc
        if len(text) > self.max_chars:
            return (
                text[: self.max_chars] + "\n\n[Skill document truncated by size limit]"
            )
        return text

    def _metadata(self, directory: Path) -> dict[str, str]:
        """Read only front-matter metadata, never the full skill body."""
        directory = self._skill_dir(directory.name)
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            return {}
        try:
            with skill_file.open(encoding="utf-8") as handle:
                header = handle.read(8192)
        except OSError:
            return {}
        metadata: dict[str, str] = {}
        for line in header.splitlines():
            match = _FRONT_MATTER_FIELD_RE.match(line)
            if match:
                metadata[match.group(1)] = match.group(2)
        if "name" in metadata and "description" in metadata:
            return metadata
        return {}

    def installed_skills(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        """Return valid skill metadata and activation state.

        Args:
            include_disabled: If false, omit disabled skills for agent-facing
                discovery. The web portal passes true to render every skill.

        Returns:
            A name-sorted list with directory ``id``, display ``name``,
            ``description``, and boolean ``enabled`` for each valid skill.

        Example:
            >>> SkillsTool().installed_skills(include_disabled=False)
            []
        """
        if not self.root.is_dir():
            return []
        disabled = read_disabled_skills(self.state_file)
        entries: list[dict[str, Any]] = []
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            try:
                directory.resolve().relative_to(self.root)
                metadata = self._metadata(directory)
            except (OSError, ToolError, ValueError):
                continue
            if not metadata:
                continue
            enabled = directory.name not in disabled
            if enabled or include_disabled:
                entries.append({**metadata, "id": directory.name, "enabled": enabled})
        return entries

    def prompt_index(self) -> str:
        """Return the compact skills index suitable for a system prompt.

        Returns:
            A short prompt fragment with skill names/descriptions, or an empty
            string when no valid skills are installed. Full instructions are
            deliberately excluded.

        Example:
            >>> isinstance(SkillsTool().prompt_index(), str)
            True
        """
        entries = [
            f"- {skill['name']}: {skill['description']}"
            for skill in self.installed_skills(include_disabled=False)
        ]
        if not entries:
            return ""
        index = (
            "\n\nInstalled agent skills (load instructions on demand with the "
            "`skills` tool; do not assume a skill applies to ordinary source analysis):\n"
            + "\n".join(entries)
        )
        return index[: self.max_chars]

    async def execute(self, **kwargs: Any) -> ToolResult:
        """List installed skills or read a path-confined skill document.

        Args:
            kwargs: ``command`` (``list`` or ``read``), ``name`` (skill
                directory name required for ``read``), and optional ``path``
                (relative Markdown file). If ``path`` is omitted, the
                OpenManus preamble (when present) and SKILL.md are returned.

        Returns:
            A ``ToolResult`` containing JSON for ``list`` or Markdown for ``read``.

        Raises:
            ToolError: For an unknown command/skill, an invalid path, a missing
                document, non-string arguments, or an unreadable file.

        Example:
            >>> result = await SkillsTool().execute(command="read", name="reverse-engineer-anything")
        """
        command = kwargs.get("command")
        name = kwargs.get("name", "")
        path = kwargs.get("path", "")
        if not all(isinstance(value, str) for value in (command, name, path)):
            raise ToolError("Skills command, name, and path must be strings")
        if command == "list":
            return ToolResult(
                output={"skills": self.installed_skills(include_disabled=False)}
            )
        if command != "read":
            raise ToolError(f"Unknown skills command: {command}")
        directory = self._skill_dir(name)
        if name in read_disabled_skills(self.state_file):
            raise ToolError(f"Skill is disabled: {name}")
        if path:
            candidate = (directory / path).resolve()
            try:
                candidate.relative_to(directory)
            except ValueError as exc:
                raise ToolError("Document path escapes the skill") from exc
            if candidate.suffix.lower() != ".md" or not candidate.is_file():
                raise ToolError("Skill document not found")
            return ToolResult(output=self._read_limited(candidate))
        documents = []
        preamble = directory / "OPENMANUS.md"
        if preamble.is_file():
            documents.append(self._read_limited(preamble))
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            raise ToolError(f"Skill has no SKILL.md: {name}")
        documents.append(self._read_limited(skill_file))
        combined = "\n\n---\n\n".join(documents)
        if len(combined) > self.max_chars:
            combined = (
                combined[: self.max_chars]
                + "\n\n[Skill document truncated by size limit]"
            )
        return ToolResult(output=combined)


__all__ = ["SkillsTool", "read_disabled_skills", "set_skill_enabled"]
