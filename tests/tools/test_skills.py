"""Unit tests for OpenManus's on-demand skills loader.

The fixture creates temporary skill directories only. It verifies metadata
indexing, preambles, reference reads, truncation, missing documents, and path
confinement; no MCP server, model call, or network access is involved.

Run: .venv/bin/python -m pytest tests/tools/test_skills.py -v
Dependencies: pytest, pytest-asyncio, and the OpenManus tool modules.
Tier 2: path-validation failures are explicit ``ToolError`` exceptions.
"""

from pathlib import Path

import pytest

from app.agent.manus import Manus
from app.exceptions import ToolError
from app.tool.skills import SkillsTool, read_disabled_skills, set_skill_enabled

SKILL_NAME = "reverse-engineer-anything"
DESCRIPTION = "Reverse engineer packaged artifacts with evidence."


@pytest.fixture(autouse=True)
def isolated_skills_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Keep every test away from the operator's real activation preferences."""
    monkeypatch.setenv(
        "OPENMANUS_SKILLS_STATE_FILE", str(tmp_path / "skills_state.json")
    )


def make_skill(root: Path, name: str = SKILL_NAME, preamble: bool = True) -> Path:
    """Create one small valid skill with a reference document."""
    directory = root / name
    (directory / "references").mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: {DESCRIPTION}\nmetadata:\n  version: "49"\n---\n\n'
        "# Full instructions\nThis body must not appear in the prompt index.\n",
        encoding="utf-8",
    )
    if preamble:
        (directory / "OPENMANUS.md").write_text(
            "# Host safety rules\n", encoding="utf-8"
        )
    (directory / "references" / "guide.md").write_text(
        "# Focused guide\n", encoding="utf-8"
    )
    return directory


def test_skills_prompt_index_contains_metadata_not_full_instructions(tmp_path: Path):
    make_skill(tmp_path)
    tool = SkillsTool(root=tmp_path)

    index = tool.prompt_index()

    assert SKILL_NAME in index and DESCRIPTION in index
    assert "This body must not appear" not in index


def test_skills_prompt_index_empty_for_missing_or_invalid_skills(tmp_path: Path):
    (tmp_path / "not-a-skill").mkdir()
    assert SkillsTool(root=tmp_path).prompt_index() == ""
    assert SkillsTool(root=tmp_path / "missing").prompt_index() == ""


@pytest.mark.asyncio
async def test_skills_list_returns_name_and_description(tmp_path: Path):
    make_skill(tmp_path)
    make_skill(tmp_path, name="another-skill", preamble=False)

    result = await SkillsTool(root=tmp_path).execute(command="list")

    names = {entry["name"] for entry in result.output["skills"]}
    assert names == {SKILL_NAME, "another-skill"}
    assert all(entry["description"] == DESCRIPTION for entry in result.output["skills"])


@pytest.mark.asyncio
async def test_skills_read_includes_host_preamble_before_upstream_instructions(
    tmp_path: Path,
):
    make_skill(tmp_path)

    result = await SkillsTool(root=tmp_path).execute(command="read", name=SKILL_NAME)

    assert result.output.startswith("# Host safety rules")
    assert result.output.index("# Host safety rules") < result.output.index(
        "# Full instructions"
    )


@pytest.mark.asyncio
async def test_skills_read_reference_is_confined_to_markdown_documents(tmp_path: Path):
    make_skill(tmp_path)
    tool = SkillsTool(root=tmp_path)

    result = await tool.execute(
        command="read", name=SKILL_NAME, path="references/guide.md"
    )

    assert result.output == "# Focused guide\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,path",
    [
        ("../outside", ""),
        (SKILL_NAME, "../../secrets.md"),
        (SKILL_NAME, "/etc/passwd"),
        (SKILL_NAME, "references/missing.md"),
        (SKILL_NAME, "references"),
        ("missing-skill", ""),
    ],
)
async def test_skills_invalid_names_and_paths_raise_tool_error(
    tmp_path: Path, name: str, path: str
):
    make_skill(tmp_path)

    with pytest.raises(ToolError):
        await SkillsTool(root=tmp_path).execute(command="read", name=name, path=path)


@pytest.mark.asyncio
async def test_skills_symlink_escaping_the_skill_is_rejected(tmp_path: Path):
    directory = make_skill(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("do not read this", encoding="utf-8")
    (directory / "references" / "escape.md").symlink_to(outside)

    with pytest.raises(ToolError):
        await SkillsTool(root=tmp_path).execute(
            command="read", name=SKILL_NAME, path="references/escape.md"
        )


@pytest.mark.asyncio
async def test_skills_list_and_index_ignore_directory_symlinks_outside_root(
    tmp_path: Path,
):
    make_skill(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: injected\ndescription: must not be listed\n---\n", encoding="utf-8"
    )
    (tmp_path / "linked-skill").symlink_to(outside, target_is_directory=True)
    tool = SkillsTool(root=tmp_path)

    result = await tool.execute(command="list")

    assert [entry["name"] for entry in result.output["skills"]] == [SKILL_NAME]
    assert "injected" not in tool.prompt_index()


@pytest.mark.asyncio
async def test_skills_read_applies_an_explicit_size_limit(tmp_path: Path):
    make_skill(tmp_path)

    result = await SkillsTool(root=tmp_path, max_chars=40).execute(
        command="read", name=SKILL_NAME
    )

    assert "truncated by size limit" in result.output
    assert len(result.output) < 100


@pytest.mark.asyncio
async def test_skills_unknown_command_raises_tool_error(tmp_path: Path):
    with pytest.raises(ToolError):
        await SkillsTool(root=tmp_path).execute(command="delete")


@pytest.mark.asyncio
async def test_skills_disabled_skill_is_hidden_from_index_list_and_read(tmp_path: Path):
    make_skill(tmp_path)
    state_file = tmp_path / "state.json"
    set_skill_enabled(state_file, SKILL_NAME, False)
    tool = SkillsTool(root=tmp_path, state_file=state_file)

    assert tool.prompt_index() == ""
    result = await tool.execute(command="list")
    assert result.output["skills"] == []
    with pytest.raises(ToolError, match="disabled"):
        await tool.execute(command="read", name=SKILL_NAME)

    set_skill_enabled(state_file, SKILL_NAME, True)
    assert SKILL_NAME in tool.prompt_index()
    assert read_disabled_skills(state_file) == set()


@pytest.mark.asyncio
async def test_skills_corrupt_state_raises_instead_of_enabling(tmp_path: Path):
    make_skill(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text("not json", encoding="utf-8")
    tool = SkillsTool(root=tmp_path, state_file=state_file)

    with pytest.raises(ToolError, match="Could not read"):
        read_disabled_skills(state_file)
    with pytest.raises(ToolError, match="Could not read"):
        tool.prompt_index()
    with pytest.raises(ToolError, match="Could not read"):
        await tool.execute(command="list")


@pytest.mark.parametrize(
    "contents",
    [
        '{"disabled": ["valid-skill", "../escape", 7]}',
        '{"disabled": "valid-skill"}',
        "[]",
    ],
)
def test_skills_state_rejects_invalid_names_and_wrong_shapes(
    tmp_path: Path, contents: str
):
    state_file = tmp_path / "state.json"
    state_file.write_text(contents, encoding="utf-8")
    with pytest.raises(ToolError, match="Invalid skills state"):
        read_disabled_skills(state_file)


def test_skills_state_read_os_error_raises_tool_error(tmp_path: Path):
    with pytest.raises(ToolError, match="Could not read"):
        read_disabled_skills(tmp_path)


def test_skills_state_rejects_invalid_name_and_write_errors(tmp_path: Path):
    state_file = tmp_path / "state.json"
    with pytest.raises(ToolError, match="Invalid skill name"):
        set_skill_enabled(state_file, "../escape", False)

    temporary = state_file.with_name(f".{state_file.name}.tmp")
    temporary.mkdir()
    with pytest.raises(ToolError, match="Could not save"):
        set_skill_enabled(state_file, SKILL_NAME, False)


def test_manus_new_instances_and_live_agents_refresh_skill_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    make_skill(tmp_path)
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("OPENMANUS_SKILLS_DIR", str(tmp_path))
    monkeypatch.setenv("OPENMANUS_SKILLS_STATE_FILE", str(state_file))
    agent = Manus()
    assert SKILL_NAME in agent.system_prompt

    set_skill_enabled(state_file, SKILL_NAME, False)
    agent.refresh_skills_prompt()
    assert SKILL_NAME not in agent.system_prompt
    assert SKILL_NAME not in Manus().system_prompt

    set_skill_enabled(state_file, SKILL_NAME, True)
    agent.refresh_skills_prompt()
    assert SKILL_NAME in agent.system_prompt


@pytest.mark.asyncio
async def test_manus_refresh_reconfigures_registered_skill_tool(tmp_path: Path):
    make_skill(tmp_path)
    override_state = tmp_path / "webapp-override.json"
    agent = Manus()
    registered = agent.available_tools.tool_map["skills"]
    assert registered.state_file != override_state

    set_skill_enabled(override_state, SKILL_NAME, False)
    agent.refresh_skills_prompt(SkillsTool(root=tmp_path, state_file=override_state))

    assert registered.root == tmp_path
    assert registered.state_file == override_state
    assert SKILL_NAME not in agent.system_prompt
    with pytest.raises(ToolError, match="disabled"):
        await registered.execute(command="read", name=SKILL_NAME)


def test_manus_registers_the_skills_tool_and_compact_index():
    agent = Manus()

    assert "skills" in agent.available_tools.tool_map
    assert SKILL_NAME in agent.system_prompt
    assert "This body must not appear" not in agent.system_prompt
