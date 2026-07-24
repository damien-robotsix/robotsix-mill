"""Tests for :mod:`robotsix_mill.agents.periodic_base`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robotsix_mill.agents import (
    base,
    explore,
    fs_tools,
    jscpd_tool,
    overlays,
    retry,
    yaml_loader,
)
from robotsix_mill.agents.periodic_base import (
    _count_active_proposals,
    run_periodic_agent,
)
from robotsix_mill.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        forge_remote_url="https://git.example.com/test/repo",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_tool(name: str):
    """Return a callable with a ``__name__`` attribute."""

    def _fake(*_args, **_kwargs):
        return None

    _fake.__name__ = name
    return _fake


def _fake_result(output=None):
    """Build a mock agent.run_sync result with the expected shape."""
    if output is None:
        output = MagicMock()
        output.draft_titles = [f"title_{i}" for i in range(10)]
        output.draft_bodies = [f"body_{i}" for i in range(10)]
        output.gap_ids = [f"gap_{i}" for i in range(10)]
    result = MagicMock()
    result.output = output
    return result


def _default_definition():
    return MagicMock(
        model=None,
        system_prompt="base prompt",
        web=False,
        library_knowledge=False,
        report_issue=False,
        read_ticket=False,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
        output_type="",
        retries=0,
        module="",
        skills=None,
        modules=False,
        inject_agent_md=False,
    )


def _setup_patches(monkeypatch, **overrides):
    """Install the standard set of patches for the periodic pipeline.

    Returns a dict with the mock objects so tests can inspect them.
    """
    mocks: dict[str, MagicMock] = {}

    # load_agent_definition
    mock_load = MagicMock(return_value=_default_definition())
    mocks["load_agent_definition"] = mock_load
    monkeypatch.setattr(yaml_loader, "load_agent_definition", mock_load)

    # build_fs_tools
    fake_fs_tools = overrides.get(
        "build_fs_tools", [_make_fake_tool("read_file"), _make_fake_tool("list_dir")]
    )
    mock_build_fs = MagicMock(return_value=fake_fs_tools)
    mocks["build_fs_tools"] = mock_build_fs
    monkeypatch.setattr(fs_tools, "build_fs_tools", mock_build_fs)

    # make_explore_tool
    mock_explore = MagicMock(return_value=_make_fake_tool("explore_tool"))
    mocks["make_explore_tool"] = mock_explore
    monkeypatch.setattr(explore, "make_explore_tool", mock_explore)

    # make_jscpd_tool
    mock_jscpd = MagicMock(return_value=_make_fake_tool("jscpd_tool"))
    mocks["make_jscpd_tool"] = mock_jscpd
    monkeypatch.setattr(jscpd_tool, "make_jscpd_tool", mock_jscpd)

    # load_overlay
    mock_load_overlay = MagicMock(return_value="")
    mocks["load_overlay"] = mock_load_overlay
    monkeypatch.setattr(overlays, "load_overlay", mock_load_overlay)

    # build_agent_from_definition
    mock_agent = MagicMock()
    mock_agent.run_sync.return_value = _fake_result()
    mock_build_agent = MagicMock(return_value=mock_agent)
    mocks["build_agent_from_definition"] = mock_build_agent
    mocks["agent"] = mock_agent
    monkeypatch.setattr(base, "build_agent_from_definition", mock_build_agent)

    # _safe_close
    mock_safe_close = MagicMock()
    mocks["_safe_close"] = mock_safe_close
    monkeypatch.setattr(base, "_safe_close", mock_safe_close)

    # run_agent: run make_run on the (mock) handle, return its result
    mock_run_agent = MagicMock(
        side_effect=lambda agent, make_run, **kw: make_run(agent)
    )
    mocks["run_agent"] = mock_run_agent
    monkeypatch.setattr(retry, "run_agent", mock_run_agent)

    return mocks


# ---------------------------------------------------------------------------
# Basic pipeline invocation
# ---------------------------------------------------------------------------


def test_basic_pipeline(settings, monkeypatch, tmp_path):
    """A representative call exercises the full pipeline and clips output."""
    mocks = _setup_patches(monkeypatch)

    result = run_periodic_agent(
        settings=settings,
        definition_name="audit",
        max_gaps=3,
        repo_dir=tmp_path,
        memory="some memory",
        recent_proposals="prior proposals",
        prompt_tail="Do the thing.",
        include_forge_url=True,
    )

    # Clipping applied
    assert len(result.draft_titles) == 3
    assert len(result.draft_bodies) == 3
    assert len(result.gap_ids) == 3

    # Agent built from the loaded definition (model is now resolved from the
    # definition's level inside build_agent, not passed as model_name here).
    build_args, build_kwargs = mocks["build_agent_from_definition"].call_args
    assert build_args[1] is mocks["load_agent_definition"].return_value
    assert "model_name" not in build_kwargs

    # Tools built
    mocks["build_fs_tools"].assert_called_once()
    mocks["make_explore_tool"].assert_called_once()

    # Prompt includes memory and tail
    call_arg = mocks["agent"].run_sync.call_args[0][0]
    assert "Do the thing." in call_arg
    assert "some memory" in call_arg

    # what= label
    assert mocks["run_agent"].call_args[1]["what"] == "audit"


def test_verification_gate_injected_unconditionally(settings, monkeypatch, tmp_path):
    """The verification gate is appended to every detector's prompt,
    regardless of the include_forge_url flag."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="module_curator",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        include_forge_url=False,
    )

    prompt = mocks["agent"].run_sync.call_args[0][0]
    assert "verify every concrete claim against the live tree" in prompt
    assert "resolves to ZERO existing files" in prompt
    assert "`path/to/file.py:LINE`" in prompt


def test_validate_artifact_tool_wired_for_clone_scoped_run(
    settings, monkeypatch, tmp_path
):
    """When repo_dir is not None, the validate_artifact tool is present in
    the constructed tool list (unconditionally, no include_* flag)."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test_gap",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
    )

    tools_arg = mocks["build_agent_from_definition"].call_args[1]["tools"]
    tool_names = [t.__name__ for t in tools_arg]
    assert "validate_artifact" in tool_names


# ---------------------------------------------------------------------------
# repo_dir=None: no tools
# ---------------------------------------------------------------------------


def test_no_repo_dir_no_tools(settings, monkeypatch):
    """When repo_dir is None, no fs/explore/jscpd tools are built."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=None,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
    )

    mocks["build_fs_tools"].assert_not_called()
    mocks["make_explore_tool"].assert_not_called()
    mocks["make_jscpd_tool"].assert_not_called()

    # Agent still built, just with empty tools
    _, build_kwargs = mocks["build_agent_from_definition"].call_args
    assert build_kwargs["tools"] == []


# ---------------------------------------------------------------------------
# include_forge_url
# ---------------------------------------------------------------------------


def test_include_forge_url_injects_section(settings, monkeypatch, tmp_path):
    """When include_forge_url=True, the prompt contains the forge URL section."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        include_forge_url=True,
    )

    prompt = mocks["agent"].run_sync.call_args[0][0]
    assert "forge-remote-url" in prompt
    assert "https://git.example.com/test/repo" in prompt


def test_no_forge_url_when_false(settings, monkeypatch, tmp_path):
    """When include_forge_url=False, no forge URL section in prompt."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        include_forge_url=False,
    )
    prompt = mocks["agent"].run_sync.call_args[0][0]
    assert "forge-remote-url" not in prompt


# ---------------------------------------------------------------------------
# include_jscpd
# ---------------------------------------------------------------------------


def test_include_jscpd_adds_tool(settings, monkeypatch, tmp_path):
    """When include_jscpd=True, the jscpd tool is appended to the tool list."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        include_jscpd=True,
    )

    mocks["make_jscpd_tool"].assert_called_once()
    tools_arg = mocks["build_agent_from_definition"].call_args[1]["tools"]
    tool_names = [t.__name__ for t in tools_arg]
    assert "jscpd_tool" in tool_names


def test_no_jscpd_when_false(settings, monkeypatch, tmp_path):
    """When include_jscpd=False, jscpd is not created."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        include_jscpd=False,
    )
    mocks["make_jscpd_tool"].assert_not_called()


# ---------------------------------------------------------------------------
# include_run_command
# ---------------------------------------------------------------------------


def test_include_run_command_adds_to_fs_filter(settings, monkeypatch, tmp_path):
    """When include_run_command=True, run_command tool is included in tools."""
    mocks = _setup_patches(
        monkeypatch,
        build_fs_tools=[
            _make_fake_tool("read_file"),
            _make_fake_tool("list_dir"),
            _make_fake_tool("run_command"),
            _make_fake_tool("edit_file"),
        ],
    )

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        include_run_command=True,
    )
    tools_arg = mocks["build_agent_from_definition"].call_args[1]["tools"]
    tool_names = [t.__name__ for t in tools_arg]
    assert "run_command" in tool_names
    assert "edit_file" not in tool_names  # not in filter


def test_no_run_command_when_false(settings, monkeypatch, tmp_path):
    """When include_run_command=False, run_command is excluded."""
    mocks = _setup_patches(
        monkeypatch,
        build_fs_tools=[
            _make_fake_tool("read_file"),
            _make_fake_tool("list_dir"),
            _make_fake_tool("run_command"),
        ],
    )

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        include_run_command=False,
    )
    tools_arg = mocks["build_agent_from_definition"].call_args[1]["tools"]
    tool_names = [t.__name__ for t in tools_arg]
    assert "run_command" not in tool_names


# ---------------------------------------------------------------------------
# usage_limits forwarding
# ---------------------------------------------------------------------------


def test_usage_limits_forwarded(settings, monkeypatch, tmp_path):
    """When usage_limits is passed, it is forwarded to agent.run_sync()."""
    mocks = _setup_patches(monkeypatch)
    fake_limits = MagicMock()

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        usage_limits=fake_limits,
    )

    call_kwargs = mocks["agent"].run_sync.call_args[1]
    assert "usage_limits" in call_kwargs
    assert call_kwargs["usage_limits"] is fake_limits


def test_no_usage_limits_when_none(settings, monkeypatch, tmp_path):
    """When usage_limits=None, it's not forwarded to agent.run_sync()."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        usage_limits=None,
    )
    call_kwargs = mocks["agent"].run_sync.call_args[1]
    assert "usage_limits" not in call_kwargs


# ---------------------------------------------------------------------------
# extra_roots forwarding
# ---------------------------------------------------------------------------


def test_extra_roots_forwarded(settings, monkeypatch, tmp_path):
    """When extra_roots is provided, it is forwarded to build_fs_tools."""
    mocks = _setup_patches(monkeypatch)
    extra = [tmp_path / "extra1"]

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        extra_roots=extra,
    )

    _, fs_kwargs = mocks["build_fs_tools"].call_args
    assert fs_kwargs.get("extra_roots") == extra


def test_extra_roots_none_when_not_provided(settings, monkeypatch, tmp_path):
    """When extra_roots is None, None is passed to build_fs_tools."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="test",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
        extra_roots=None,
    )
    _, fs_kwargs = mocks["build_fs_tools"].call_args
    assert fs_kwargs.get("extra_roots") is None


# ---------------------------------------------------------------------------
# overlay key
# ---------------------------------------------------------------------------


def test_overlay_key_matches_definition_name(settings, monkeypatch, tmp_path):
    """The overlay key passed to load_overlay matches definition_name."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="bc_check",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="props",
        prompt_tail="Tail.",
    )

    mocks["load_overlay"].assert_called_once_with(tmp_path, "bc_check")


# ---------------------------------------------------------------------------
# definition_override seam (per-repo .robotsix-mill/periodic/<name>.yaml)
# ---------------------------------------------------------------------------


def test_definition_override_bypasses_builtin_load_and_overlay(
    settings, monkeypatch, tmp_path
):
    """When the supervisor passes a resolved override, run_periodic_agent must
    use it verbatim — NOT load the built-in yaml, NOT apply the legacy .md
    overlay — and build the agent from the override (its prompt + level)."""
    mocks = _setup_patches(monkeypatch)

    override = _default_definition()
    override.system_prompt = "MERGED REPO PROMPT"
    override.level = 2

    run_periodic_agent(
        settings=settings,
        definition_name="audit",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="",
        recent_proposals="",
        prompt_tail="go",
        definition_override=override,
    )

    # built-in yaml NOT loaded; legacy overlay NOT consulted
    mocks["load_agent_definition"].assert_not_called()
    mocks["load_overlay"].assert_not_called()
    # agent built from the override, with its prompt; the override definition
    # itself is forwarded so build_agent resolves the model from its level.
    args, kwargs = mocks["build_agent_from_definition"].call_args
    assert kwargs["system_prompt"] == "MERGED REPO PROMPT"
    assert args[1] is override


# ---------------------------------------------------------------------------
# _count_active_proposals
# ---------------------------------------------------------------------------


def test_count_active_proposals_empty_block():
    """Empty block (no recent proposals) → 0."""
    block = "<recent_proposals>\n(no recent proposals)\n</recent_proposals>"
    assert _count_active_proposals(block) == 0


def test_count_active_proposals_all_terminal():
    """Only done/closed states → 0."""
    block = (
        "<recent_proposals>\n"
        "[done] 20250101T000000Z-old-ticket-a1b2 | Some done ticket\n"
        "[closed] 20250101T000000Z-old-ticket-c3d4 | Some closed ticket\n"
        "</recent_proposals>"
    )
    assert _count_active_proposals(block) == 0


def test_count_active_proposals_mixed():
    """Mix of active + terminal → counts only active."""
    block = (
        "<recent_proposals>\n"
        "[draft] 20250101T000000Z-t1-a1b2 | Draft ticket\n"
        "[done] 20250101T000000Z-t2-c3d4 | Done ticket\n"
        "[blocked] 20250101T000000Z-t3-e5f6 | Blocked ticket\n"
        "[closed] 20250101T000000Z-t4-g7h8 | Closed ticket\n"
        "[ready] 20250101T000000Z-t5-i9j0 | Ready ticket\n"
        "</recent_proposals>"
    )
    assert _count_active_proposals(block) == 3  # draft, blocked, ready


def test_count_active_proposals_all_active():
    """Only active states (draft, ready, blocked, human_issue_approval, etc.) → counts all."""
    block = (
        "<recent_proposals>\n"
        "[draft] 20250101T000000Z-t1-a1b2 | Draft\n"
        "[ready] 20250101T000000Z-t2-c3d4 | Ready\n"
        "[blocked] 20250101T000000Z-t3-e5f6 | Blocked\n"
        "[human_issue_approval] 20250101T000000Z-t4-g7h8 | HIA\n"
        "[code_review] 20250101T000000Z-t5-i9j0 | CR\n"
        "</recent_proposals>"
    )
    assert _count_active_proposals(block) == 5


def test_count_active_proposals_empty_string():
    """Empty string → 0."""
    assert _count_active_proposals("") == 0


def test_count_active_proposals_warning_injected(settings, monkeypatch, tmp_path):
    """When recent_proposals contains active proposals, the system prompt
    includes the Active Proposals warning block."""
    mocks = _setup_patches(monkeypatch)

    active_block = (
        "<recent_proposals>\n"
        "[draft] 20250101T000000Z-t1-a1b2 | Draft ticket\n"
        "[ready] 20250101T000000Z-t2-c3d4 | Ready ticket\n"
        "</recent_proposals>"
    )

    run_periodic_agent(
        settings=settings,
        definition_name="audit",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals=active_block,
        prompt_tail="Tail.",
    )

    _, build_kwargs = mocks["build_agent_from_definition"].call_args
    prompt = build_kwargs["system_prompt"]
    assert "## ⚠️ Active Proposals" in prompt
    assert "There are currently **2** active proposal(s)" in prompt
    assert "states other than `done` / `closed`" in prompt


def test_count_active_proposals_no_warning_when_none_active(
    settings, monkeypatch, tmp_path
):
    """When all proposals are terminal, the system prompt does NOT include
    the Active Proposals warning."""
    mocks = _setup_patches(monkeypatch)

    terminal_block = (
        "<recent_proposals>\n"
        "[done] 20250101T000000Z-t1-a1b2 | Done ticket\n"
        "[closed] 20250101T000000Z-t2-c3d4 | Closed ticket\n"
        "</recent_proposals>"
    )

    run_periodic_agent(
        settings=settings,
        definition_name="audit",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals=terminal_block,
        prompt_tail="Tail.",
    )

    _, build_kwargs = mocks["build_agent_from_definition"].call_args
    prompt = build_kwargs["system_prompt"]
    assert "## ⚠️ Active Proposals" not in prompt


def test_count_active_proposals_no_warning_empty_proposals(
    settings, monkeypatch, tmp_path
):
    """When recent_proposals is the empty placeholder, the system prompt
    does NOT include the Active Proposals warning."""
    mocks = _setup_patches(monkeypatch)

    run_periodic_agent(
        settings=settings,
        definition_name="audit",
        max_gaps=5,
        repo_dir=tmp_path,
        memory="mem",
        recent_proposals="<recent_proposals>\n(no recent proposals)\n</recent_proposals>",
        prompt_tail="Tail.",
    )

    _, build_kwargs = mocks["build_agent_from_definition"].call_args
    prompt = build_kwargs["system_prompt"]
    assert "## ⚠️ Active Proposals" not in prompt


# ---------------------------------------------------------------------------
# Claude SDK degenerate result handling
# ---------------------------------------------------------------------------


def test_claude_sdk_degenerate_result_returns_empty(settings, monkeypatch, tmp_path):
    """When the Claude SDK raises its degenerate 'error result: success'
    exception, run_periodic_agent catches it and returns an empty
    PeriodicAgentResult with preserved memory."""
    mocks = _setup_patches(monkeypatch)

    # Override run_agent to raise the degenerate Claude SDK exception.
    degenerate_exc = Exception("Claude Code returned an error result: success")
    mocks["run_agent"].side_effect = degenerate_exc

    result = run_periodic_agent(
        settings=settings,
        definition_name="survey",
        max_gaps=3,
        repo_dir=tmp_path,
        memory="preserved memory",
        recent_proposals="prior proposals",
        prompt_tail="Do the thing.",
        include_forge_url=True,
    )

    # Should return an empty result with preserved memory.
    assert result.updated_memory == "preserved memory"
    assert result.draft_titles == []
    assert result.draft_bodies == []
    assert result.gap_ids == []
    assert "degenerate" in result.summary.lower()

    # Agent was still built and cleaned up.
    mocks["build_agent_from_definition"].assert_called_once()
    mocks["_safe_close"].assert_called_once()


def test_claude_sdk_degenerate_result_in_cause_chain(settings, monkeypatch, tmp_path):
    """The degenerate-result detector walks the cause chain, so a wrapped
    exception should also be caught."""
    mocks = _setup_patches(monkeypatch)

    degenerate_exc = Exception("Claude Code returned an error result: success")
    outer = RuntimeError("agent run failed")
    outer.__cause__ = degenerate_exc
    mocks["run_agent"].side_effect = outer

    result = run_periodic_agent(
        settings=settings,
        definition_name="survey",
        max_gaps=3,
        repo_dir=tmp_path,
        memory="preserved memory",
        recent_proposals="prior proposals",
        prompt_tail="Do the thing.",
    )

    assert result.updated_memory == "preserved memory"
    assert result.draft_titles == []
    assert "degenerate" in result.summary.lower()


def test_non_degenerate_exception_propagates(settings, monkeypatch, tmp_path):
    """A non-degenerate exception is NOT caught — it propagates normally."""
    mocks = _setup_patches(monkeypatch)

    real_error = RuntimeError("genuine failure")
    mocks["run_agent"].side_effect = real_error

    with pytest.raises(RuntimeError, match="genuine failure"):
        run_periodic_agent(
            settings=settings,
            definition_name="survey",
            max_gaps=3,
            repo_dir=tmp_path,
            memory="mem",
            recent_proposals="props",
            prompt_tail="Tail.",
        )

    # Agent cleanup still runs (finally block).
    mocks["_safe_close"].assert_called_once()
