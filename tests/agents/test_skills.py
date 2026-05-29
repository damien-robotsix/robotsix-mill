"""Tests for skill injection into agent prompts."""

import logging
from pathlib import Path

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

    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="Read a file.",
            category="fs",
            parameters={"path": "str"},
        )
    )

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

    ToolRegistry.register(
        ToolInfo(
            name="run_command",
            description="Run a shell command.",
            category="shell",
            parameters={"command": "str"},
        )
    )

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

    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="Read a file.",
            category="fs",
            parameters={"path": "str"},
        )
    )

    result = compose_prompt(s, "BASE PROMPT")
    assert result.startswith("BASE PROMPT")
    assert "## Skills" not in result


def test_compose_prompt_skills_none_same_as_omitted(tmp_path):
    """Explicit skills=None produces the same output as omitting it."""
    s = _settings(tmp_path)

    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="Read a file.",
            category="fs",
            parameters={"path": "str"},
        )
    )

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


# ── module-map injection ──────────────────────────────────────────────


def _make_modules_yaml(tmp_path, modules: list[dict]) -> Path:
    """Write a minimal modules.yaml into *tmp_path*."""
    import yaml

    modules_path = tmp_path / "docs" / "modules.yaml"
    modules_path.parent.mkdir(parents=True, exist_ok=True)
    modules_path.write_text(yaml.dump({"modules": modules}), encoding="utf-8")
    return modules_path


def test_module_map_injected_when_modules_true(tmp_path, monkeypatch):
    """When modules=True, compose_prompt reads docs/modules.yaml and
    injects a ## Module Map section."""
    s = _settings(tmp_path, skills_dir=str(tmp_path / "skills"))

    modules_path = _make_modules_yaml(
        tmp_path,
        [
            {
                "id": "config",
                "description": "App configuration layer.",
                "paths": ["src/config.py", "src/config_loader.py"],
                "dependencies": [],
            },
            {
                "id": "core",
                "description": "Core domain model.",
                "paths": ["src/core/models.py"],
                "dependencies": ["config"],
            },
        ],
    )

    # Patch pathlib.Path so that Path("docs/modules.yaml") resolves
    # to our test fixture inside tmp_path.
    import pathlib

    _OrigPath = pathlib.Path

    def _patched_path(*args, **kwargs):
        p = _OrigPath(*args, **kwargs)
        if str(p) == "docs/modules.yaml":
            return _OrigPath(str(modules_path))
        return p

    monkeypatch.setattr(pathlib, "Path", _patched_path)

    result = compose_prompt(s, "SYSTEM PROMPT", modules=True)

    assert result.startswith("SYSTEM PROMPT")
    assert "## Module Map" in result
    assert "### config" in result
    assert "App configuration layer." in result
    assert "- `src/config.py`" in result
    assert "### core" in result
    assert "Core domain model." in result
    assert "Depends on: config" in result


def test_module_map_not_injected_when_modules_false(tmp_path):
    """When modules=False (default), no ## Module Map section is injected."""
    s = _settings(tmp_path)

    result = compose_prompt(s, "BASE PROMPT", modules=False)

    assert "## Module Map" not in result
    assert result == "BASE PROMPT"


def test_module_map_not_injected_when_modules_omitted(tmp_path):
    """When modules is not passed (defaults False), no ## Module Map
    section is injected."""
    s = _settings(tmp_path)

    result = compose_prompt(s, "BASE PROMPT")

    assert "## Module Map" not in result
    assert result == "BASE PROMPT"


def test_module_map_tiered_view_above_20_modules(tmp_path, monkeypatch):
    """When taxonomy has >20 modules, only top-level (no dependencies)
    modules are listed with a pointer to docs/modules.yaml."""
    s = _settings(tmp_path)

    # Create 25 modules; first 2 have no deps, the rest depend on them
    large_taxonomy: list[dict] = [
        {
            "id": "foundation_a",
            "description": "Base A.",
            "paths": ["a.py"],
            "dependencies": [],
        },
        {
            "id": "foundation_b",
            "description": "Base B.",
            "paths": ["b.py"],
            "dependencies": [],
        },
    ]
    for i in range(23):
        large_taxonomy.append(
            {
                "id": f"leaf_{i}",
                "description": f"Leaf {i}.",
                "paths": [f"leaf_{i}.py"],
                "dependencies": ["foundation_a"],
            }
        )

    modules_path = _make_modules_yaml(tmp_path, large_taxonomy)

    # Patch pathlib.Path so that Path("docs/modules.yaml") resolves
    # to our test fixture inside tmp_path.
    import pathlib

    _OrigPath = pathlib.Path

    def _patched_path(*args, **kwargs):
        p = _OrigPath(*args, **kwargs)
        if str(p) == "docs/modules.yaml":
            return _OrigPath(str(modules_path))
        return p

    monkeypatch.setattr(pathlib, "Path", _patched_path)

    result = compose_prompt(s, "PROMPT", modules=True)

    assert "## Module Map" in result
    assert "### foundation_a" in result
    assert "### foundation_b" in result
    # Leaf modules should NOT be listed
    assert "### leaf_0" not in result
    # Pointer to the file
    assert "docs/modules.yaml" in result


def test_module_map_missing_yaml_logs_warning(tmp_path, caplog, monkeypatch):
    """When docs/modules.yaml doesn't exist, a warning is logged and
    the prompt is returned unchanged."""
    import logging

    s = _settings(tmp_path)

    # Ensure Path("docs/modules.yaml") resolves to a non-existent path
    nonexistent = tmp_path / "nonexistent" / "modules.yaml"

    import pathlib

    _OrigPath = pathlib.Path

    def _patched_path(*args, **kwargs):
        p = _OrigPath(*args, **kwargs)
        if str(p) == "docs/modules.yaml":
            return _OrigPath(str(nonexistent))
        return p

    monkeypatch.setattr(pathlib, "Path", _patched_path)

    with caplog.at_level(logging.WARNING):
        result = compose_prompt(s, "PROMPT", modules=True)

    assert "Cannot load module taxonomy" in caplog.text
    assert "## Module Map" not in result
    assert result == "PROMPT"


def test_module_map_skills_and_modules_both_injected(tmp_path, monkeypatch):
    """Both skills and module-map can be injected together."""
    s = _settings(tmp_path, skills_dir=str(tmp_path / "skills"))

    # Create a skill
    skill_dir = tmp_path / "skills" / "board"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: board\n---\n\nBoard interaction guidance here.\n"
    )

    # Create modules.yaml
    modules_path = _make_modules_yaml(
        tmp_path,
        [
            {
                "id": "config",
                "description": "Config layer.",
                "paths": ["src/config.py"],
                "dependencies": [],
            },
        ],
    )

    # Patch pathlib.Path
    import pathlib

    _OrigPath = pathlib.Path

    def _patched_path(*args, **kwargs):
        p = _OrigPath(*args, **kwargs)
        if str(p) == "docs/modules.yaml":
            return _OrigPath(str(modules_path))
        return p

    monkeypatch.setattr(pathlib, "Path", _patched_path)

    result = compose_prompt(s, "PROMPT", skills=["board"], modules=True)

    assert result.startswith("PROMPT")
    assert "## Skills" in result
    assert "Board interaction guidance here." in result
    assert "## Module Map" in result
    assert "### config" in result
