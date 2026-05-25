"""Tests for ExpertManager — expert lifecycle, caching, tool resolution."""

from pathlib import Path
from unittest import mock

import pytest

from robotsix_mill.agents.expert_loader import ExpertDefinition
from robotsix_mill.config import Settings


# ── helpers ──────────────────────────────────────────────────────────


class FakeAgentHandle:
    """A lightweight fake AgentHandle for testing.

    Tracks whether ``close()`` was called and carries a sentinel name
    so tests can distinguish different instances.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_definition(**overrides) -> ExpertDefinition:
    """Minimal valid ExpertDefinition with *overrides* applied."""
    defaults: dict = dict(
        domain="test-domain",
        module_paths=["src/**/*.py"],
        system_prompt="You are a test expert.",
    )
    defaults.update(overrides)
    return ExpertDefinition.model_validate(defaults)


def _patch_build_agent(monkeypatch):
    """Monkeypatch ``build_agent`` to capture kwargs and return a fake.

    Returns a dict ``{"captured": [], "handle": FakeAgentHandle}`` so
    tests can inspect captured kwargs and the returned handle.
    """
    state: dict = {"captured": [], "handle": None}

    def fake_build_agent(settings, **kwargs):
        handle = FakeAgentHandle()
        state["captured"].append(kwargs)
        state["handle"] = handle
        return handle

    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent", fake_build_agent
    )
    return state


def _fake_fs_tool(name: str):
    """Return a function whose ``__name__`` is *name*."""

    def _tool(*args, **kwargs):
        return f"fs_tool:{name}"

    _tool.__name__ = name
    return _tool


def _patch_build_fs_tools(monkeypatch, tool_names=None):
    """Monkeypatch ``build_fs_tools`` to return fake functions with
    specific ``__name__`` values.

    *tool_names* defaults to the standard six: read_file, write_file,
    edit_file, delete_file, list_dir, run_command.
    """
    if tool_names is None:
        tool_names = [
            "read_file", "write_file", "edit_file",
            "delete_file", "list_dir", "run_command",
        ]
    fake_tools = [_fake_fs_tool(n) for n in tool_names]

    monkeypatch.setattr(
        "robotsix_mill.agents.fs_tools.build_fs_tools",
        lambda repo_dir, settings, pre_seeded=None, extra_roots=None: fake_tools,
    )
    return fake_tools


def _patch_make_explore_tool(monkeypatch):
    """Monkeypatch ``make_explore_tool`` to return a simple fake."""
    explore_fn = _fake_fs_tool("explore")

    monkeypatch.setattr(
        "robotsix_mill.agents.explore.make_explore_tool",
        lambda settings, repo_dir, extra_roots=None: explore_fn,
    )
    return explore_fn


# ── create_expert: builds a valid agent ──────────────────────────────


def test_create_expert_builds_agent(monkeypatch):
    """create_expert calls build_agent with correct kwargs."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)
    _patch_make_explore_tool(monkeypatch)

    settings = Settings()
    repo_dir = Path("/tmp/test-repo")
    mgr = ExpertManager(settings, repo_dir)

    definition = _make_definition(
        domain="python-backend",
        system_prompt="Expert in Python backend.",
        model="anthropic/claude-sonnet",
        skills=["board"],
        tools=["explore", "read_file", "list_dir", "run_command"],
    )

    result = mgr.create_expert(definition)

    # Verify returned handle.
    assert result is state["handle"]
    assert len(state["captured"]) == 1

    kwargs = state["captured"][0]
    assert kwargs["system_prompt"] == "Expert in Python backend."
    assert kwargs["model_name"] == "anthropic/claude-sonnet"
    assert kwargs["skills"] == ["board"]
    assert kwargs["name"] == "expert:python-backend"

    # Tools — should be [read_file, list_dir, run_command, explore].
    tool_names = {t.__name__ for t in kwargs["tools"]}
    assert tool_names == {"read_file", "list_dir", "run_command", "explore"}


def test_create_expert_minimal_tools(monkeypatch):
    """create_expert with a minimal tool list (no explore)."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(
        domain="minimal",
        tools=["read_file", "write_file"],
    )

    mgr.create_expert(definition)

    kwargs = state["captured"][0]
    tool_names = {t.__name__ for t in kwargs["tools"]}
    assert tool_names == {"read_file", "write_file"}
    assert "explore" not in tool_names


def test_create_expert_default_tools(monkeypatch):
    """create_expert with the default tools list (explore, read_file, list_dir)."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)
    _patch_make_explore_tool(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(
        domain="default-tools",
        # default tools = ["explore", "read_file", "list_dir"]
    )

    mgr.create_expert(definition)

    kwargs = state["captured"][0]
    tool_names = {t.__name__ for t in kwargs["tools"]}
    assert tool_names == {"explore", "read_file", "list_dir"}


# ── caching by domain ────────────────────────────────────────────────


def test_create_expert_caches_by_domain(monkeypatch):
    """Second call with same domain returns identical handle; build_agent
    is called only once."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)
    _patch_make_explore_tool(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(domain="cached")
    h1 = mgr.create_expert(definition)
    h2 = mgr.create_expert(definition)

    assert h1 is h2
    assert len(state["captured"]) == 1


def test_create_expert_different_definitions_same_domain(monkeypatch):
    """Two ExpertDefinition objects with the same domain key — second
    call returns cached handle (first-definition wins)."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    d1 = _make_definition(domain="shared", system_prompt="first")
    d2 = _make_definition(domain="shared", system_prompt="second")

    h1 = mgr.create_expert(d1)
    h2 = mgr.create_expert(d2)

    assert h1 is h2
    assert len(state["captured"]) == 1
    # First definition's system_prompt was used.
    assert state["captured"][0]["system_prompt"] == "first"


# ── different domains → different agents ─────────────────────────────


def test_create_expert_different_domains(monkeypatch):
    """Two distinct domains produce two distinct AgentHandle instances."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)
    _patch_make_explore_tool(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    d_a = _make_definition(domain="domain-a", system_prompt="A")
    d_b = _make_definition(domain="domain-b", system_prompt="B")

    h_a = mgr.create_expert(d_a)
    h_b = mgr.create_expert(d_b)

    assert h_a is not h_b
    assert len(state["captured"]) == 2

    # Verify correct kwargs for each.
    prompts = {c["system_prompt"] for c in state["captured"]}
    assert prompts == {"A", "B"}


# ── get_expert ───────────────────────────────────────────────────────


def test_get_expert_returns_none_for_unknown(monkeypatch):
    """get_expert returns None when domain is not cached."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    assert mgr.get_expert("nonexistent") is None


def test_get_expert_returns_cached(monkeypatch):
    """get_expert returns the cached handle after create_expert."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(domain="cached")
    h = mgr.create_expert(definition)

    assert mgr.get_expert("cached") is h


def test_get_expert_does_not_create(monkeypatch):
    """get_expert never triggers build_agent."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    mgr.get_expert("anything")
    mgr.get_expert("anything-else")
    assert len(state["captured"]) == 0


# ── remove_expert ────────────────────────────────────────────────────


def test_remove_expert_closes_and_uncaches(monkeypatch):
    """remove_expert calls close() and removes from cache."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(domain="to-remove")
    h = mgr.create_expert(definition)

    assert mgr.get_expert("to-remove") is h
    assert not h.closed

    mgr.remove_expert("to-remove")

    assert h.closed
    assert mgr.get_expert("to-remove") is None


def test_remove_expert_noop_on_unknown(monkeypatch):
    """remove_expert is a no-op for uncached domains — no error."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    # Should not raise.
    mgr.remove_expert("nonexistent")


# ── close_all ────────────────────────────────────────────────────────


def test_close_all_closes_everything(monkeypatch):
    """close_all closes every cached agent and empties the cache."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    d_a = _make_definition(domain="a")
    d_b = _make_definition(domain="b")
    h_a = mgr.create_expert(d_a)
    h_b = mgr.create_expert(d_b)

    assert not h_a.closed
    assert not h_b.closed

    mgr.close_all()

    assert h_a.closed
    assert h_b.closed
    assert mgr.get_expert("a") is None
    assert mgr.get_expert("b") is None


def test_close_all_on_empty_cache(monkeypatch):
    """close_all on an empty cache is a no-op."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    _patch_build_agent(monkeypatch)
    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    # Should not raise.
    mgr.close_all()
    # Cache is still empty.
    assert mgr.get_expert("anything") is None


# ── load_definitions ─────────────────────────────────────────────────


def test_load_definitions_parses_yaml_files(tmp_path, monkeypatch):
    """load_definitions scans a directory of .yaml files and returns
    {domain: ExpertDefinition}."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    # Create two YAML definition files.
    defs_dir = tmp_path / "expert_defs"
    defs_dir.mkdir()

    (defs_dir / "alpha.yaml").write_text(
        "domain: alpha\n"
        "description: First expert\n"
        "module_paths:\n"
        "  - src/alpha/**/*.py\n"
        "system_prompt: You are Alpha.\n"
    )
    (defs_dir / "beta.yaml").write_text(
        "domain: beta\n"
        "module_paths:\n"
        "  - src/beta/**/*.py\n"
        "system_prompt: You are Beta.\n"
    )

    result = mgr.load_definitions(defs_dir)

    assert set(result.keys()) == {"alpha", "beta"}
    assert result["alpha"].domain == "alpha"
    assert result["alpha"].system_prompt == "You are Alpha."
    assert result["beta"].domain == "beta"
    assert result["beta"].system_prompt == "You are Beta."


def test_load_definitions_missing_directory(monkeypatch):
    """load_definitions raises FileNotFoundError when the directory
    does not exist."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    _patch_build_agent(monkeypatch)
    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    with pytest.raises(FileNotFoundError, match="not found"):
        mgr.load_definitions(Path("/nonexistent/path"))


def test_load_definitions_empty_directory(tmp_path, monkeypatch):
    """load_definitions raises FileNotFoundError when the directory
    contains no .yaml files."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    _patch_build_agent(monkeypatch)
    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    empty_dir = tmp_path / "empty_defs"
    empty_dir.mkdir()

    with pytest.raises(FileNotFoundError, match="No YAML definition"):
        mgr.load_definitions(empty_dir)


def test_load_definitions_defaults_to_expert_definitions(monkeypatch):
    """load_definitions with no argument uses the repo-root
    expert_definitions/ directory."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    # The real expert_definitions/ should exist and contain at least
    # python-backend.yaml.
    result = mgr.load_definitions()

    assert "python-backend" in result
    assert result["python-backend"].domain == "python-backend"


# ── model fallback ───────────────────────────────────────────────────


def test_model_fallback_empty_string(monkeypatch):
    """When definition.model is '' (default), settings.model is used."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings(MILL_MODEL="test/coordinator-model")
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(model="")
    mgr.create_expert(definition)

    assert state["captured"][0]["model_name"] == "test/coordinator-model"


def test_model_explicit_overrides_settings(monkeypatch):
    """Explicit definition.model takes precedence over settings.model."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings(MILL_MODEL="test/coordinator-model")
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(model="anthropic/claude-opus")
    mgr.create_expert(definition)

    assert state["captured"][0]["model_name"] == "anthropic/claude-opus"


def test_model_fallback_default_field(monkeypatch):
    """When definition.model is not set at all (uses default ''), fallback
    to settings.model."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings(MILL_MODEL="fallback/model")
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    # Don't pass model at all — rely on ExpertDefinition default ("").
    definition = _make_definition()  # model defaults to ""
    mgr.create_expert(definition)

    assert state["captured"][0]["model_name"] == "fallback/model"


# ── _safe_close integration ──────────────────────────────────────────


def test_remove_expert_calls_safe_close(monkeypatch):
    """remove_expert closes the agent via _safe_close."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    definition = _make_definition(domain="close-test")
    h = mgr.create_expert(definition)

    assert not h.closed
    mgr.remove_expert("close-test")
    assert h.closed


def test_close_all_calls_safe_close_on_each(monkeypatch):
    """close_all closes every cached agent."""
    from robotsix_mill.agents.expert_manager import ExpertManager

    state = _patch_build_agent(monkeypatch)
    _patch_build_fs_tools(monkeypatch)

    settings = Settings()
    mgr = ExpertManager(settings, Path("/tmp/test-repo"))

    handles = []
    for domain in ("x", "y", "z"):
        d = _make_definition(domain=domain)
        h = mgr.create_expert(d)
        handles.append(h)
        assert not h.closed

    mgr.close_all()

    for h in handles:
        assert h.closed
    assert len(mgr._cache) == 0
