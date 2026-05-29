"""Tests for skill injection into agent prompts."""

import logging
import pytest

from robotsix_mill.agents.base import compose_prompt
from robotsix_mill.agents.tool_registry import ToolInfo, ToolRegistry
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    return Settings(**env)


@pytest.fixture(autouse=True)
def _clear_registry():
    ToolRegistry._tools.clear()
    yield
    ToolRegistry._tools.clear()


# ── skill injection ───────────────────────────────────────────────────

def test_skill_injected_after_system_prompt(tmp_path):
    """Skill content appears after the system prompt under a ``##
    Skills`` heading, with YAML frontmatter stripped. (Previously
    asserted skills came BEFORE a now-removed tool table; the
    table is gone but the skills positioning is unchanged.)"""
    s = _settings(tmp_path, skills_dir=str(tmp_path / "skills"))

    # Create a real skill file
    skill_dir = tmp_path / "skills" / "board"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: board\n---\n\nBoard interaction guidance here.\n"
    )

    ToolRegistry.register(ToolInfo(
        name="read_file", description="Read a file.",
        category="fs", parameters={"path": "str"},
    ))

    result = compose_prompt(s, "SYSTEM PROMPT", skills=["board"])

    # System prompt comes first.
    assert result.startswith("SYSTEM PROMPT")

    # Skills section appears, body included.
    assert "## Skills" in result
    assert "Board interaction guidance here." in result
    assert "name: board" not in result


def test_missing_skill_logs_warning_and_continues(tmp_path, caplog):
    """When a skill file doesn't exist, compose_prompt logs a warning
    and returns the prompt without crashing."""
    s = _settings(tmp_path, skills_dir=str(tmp_path / "skills"))

    with caplog.at_level(logging.WARNING):
        result = compose_prompt(s, "BASE", skills=["nonexistent"])

    assert "Skill file not found" in caplog.text
    assert result.startswith("BASE")
    # No ## Skills heading when no skills loaded
    assert "## Skills" not in result


def test_frontmatter_stripped_from_skill_content(tmp_path):
    """The YAML frontmatter (--- ... ---) is removed from injected content."""
    s = _settings(tmp_path, skills_dir=str(tmp_path / "skills"))

    skill_dir = tmp_path / "skills" / "board"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: board\ndescription: board interaction\n---\n"
        "\nActual body content.\n"
        "\nMore content here.\n"
    )

    ToolRegistry.register(ToolInfo(
        name="run_command", description="Run a shell command.",
        category="shell", parameters={"command": "str"},
    ))

    result = compose_prompt(s, "PROMPT", skills=["board"])

    # Frontmatter fields are stripped
    assert "name: board" not in result
    assert "description: board interaction" not in result

    # Body content is present
    assert "Actual body content." in result
    assert "More content here." in result
    assert "## Skills" in result


def test_compose_prompt_backward_compatible_no_skills(tmp_path):
    """compose_prompt() without skills parameter works exactly as before."""
    s = _settings(tmp_path)

    ToolRegistry.register(ToolInfo(
        name="read_file", description="Read a file.",
        category="fs", parameters={"path": "str"},
    ))

    result = compose_prompt(s, "BASE PROMPT")
    assert result.startswith("BASE PROMPT")
    assert "## Skills" not in result


def test_compose_prompt_skills_none_same_as_omitted(tmp_path):
    """Explicit skills=None produces the same output as omitting it."""
    s = _settings(tmp_path)

    ToolRegistry.register(ToolInfo(
        name="read_file", description="Read a file.",
        category="fs", parameters={"path": "str"},
    ))

    result = compose_prompt(s, "BASE", skills=None)
    assert "## Skills" not in result


def test_multiple_skills_injected(tmp_path):
    """Multiple skills are all injected, separated by double newlines."""
    s = _settings(tmp_path, skills_dir=str(tmp_path / "skills"))

    for name in ("board", "vcs"):
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n\nContent for {name}.\n"
        )

    result = compose_prompt(s, "PROMPT", skills=["board", "vcs"])

    assert "Content for board." in result
    assert "Content for vcs." in result

    # Order: board content before vcs content
    board_pos = result.index("Content for board.")
    vcs_pos = result.index("Content for vcs.")
    assert board_pos < vcs_pos


def test_skill_with_only_frontmatter_injects_nothing(tmp_path):
    """A skill file with only frontmatter and no body still doesn't
    crash, but adds no content."""
    s = _settings(tmp_path, skills_dir=str(tmp_path / "skills"))

    skill_dir = tmp_path / "skills" / "empty"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: empty\n---\n")

    result = compose_prompt(s, "PROMPT", skills=["empty"])

    # ## Skills header only appears if we have non-empty content
    assert "## Skills" not in result
