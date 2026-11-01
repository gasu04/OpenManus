"""On-demand agent-skill discovery and reading for OpenManus.

OpenManus extends through MCP tools rather than a native skills runtime. This
module supplies the small generic mechanism needed to use agent skills safely:
the system prompt receives only names and descriptions, while ``SkillsTool``
loads a skill's instructions and references only when a task needs them.

Key classes / functions:
    SkillsTool: list installed skills and read path-confined Markdown files.

Usage:
    tool = SkillsTool()
    index = tool.prompt_index()
    result = await tool.execute(command="read", name="reverse-engineer-anything")

Dependencies: Python 3.11+ standard library, Pydantic, and ``app.config``.
Tier 2: invalid paths fail loudly with ``ToolError``; no files are modified.
"""

import re
from pathlib import Path
from typing import Any

from pydantic import Field

from app.config import config
from app.exceptions import ToolError
from app.tool.base import BaseTool, ToolResult

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FRONT_MATTER_FIELD_RE = re.compile(r"^(name|description):\s*(.+?)\s*$")


class SkillsTool(BaseTool):
    """List and read agent skills without putting them all in the prompt.

    Attributes:
        root: Directory containing one subdirectory per skill.
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
        if not self.root.is_dir():
            return ""
        entries = []
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            try:
                directory.resolve().relative_to(self.root)
                metadata = self._metadata(directory)
            except (OSError, ToolError, ValueError):
                continue
            if metadata:
                entries.append(f"- {metadata['name']}: {metadata['description']}")
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
            skills = []
            if self.root.is_dir():
                for directory in sorted(
                    self.root.iterdir(), key=lambda item: item.name
                ):
                    if directory.is_dir():
                        try:
                            metadata = self._metadata(directory)
                        except ToolError:
                            continue
                        if metadata:
                            skills.append(metadata)
            return ToolResult(output={"skills": skills})
        if command != "read":
            raise ToolError(f"Unknown skills command: {command}")
        directory = self._skill_dir(name)
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


__all__ = ["SkillsTool"]
