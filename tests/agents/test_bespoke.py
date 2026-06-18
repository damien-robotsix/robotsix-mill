"""Tests for the bespoke agent runner.

Bespoke agents are operator-authored YAML files committed to a managed
repo. The runner reads the definition, builds a read-only tool palette,
and executes one pass. This module tests the runner's contract:
MAX_DRAFTS clipping, tool palette assembly, model selection, and the
web_knowledge flag.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robotsix_mill.agents.bespoke import BespokeResult, MAX_DRAFTS, run_bespoke_agent
from robotsix_mill.agents.bespoke_loader import BespokeAgentDefinition


# ---------------------------------------------------------------------------
#  Result schema
# ---------------------------------------------------------------------------


class TestBespokeResult:
    def test_defaults_are_empty(self):
        r = BespokeResult()
        assert r.updated_memory == ""
        assert r.draft_titles == []
        assert r.draft_bodies == []
        assert r.gap_ids == []

    def test_fields_accept_values(self):
        r = BespokeResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )
        assert r.updated_memory == "mem"
        assert r.draft_titles == ["t1"]
        assert r.draft_bodies == ["b1"]
        assert r.gap_ids == ["g1"]


# ---------------------------------------------------------------------------
#  MAX_DRAFTS clipping
# ---------------------------------------------------------------------------


def test_clips_draft_lists_to_max_drafts(settings, monkeypatch):
    """When the agent returns more than MAX_DRAFTS draft titles/bodies
    or gap_ids, the runner clips each list in-place."""
    n = MAX_DRAFTS + 5
    fat = BespokeResult(
        draft_titles=[f"t{i}" for i in range(n)],
        draft_bodies=[f"b{i}" for i in range(n)],
        gap_ids=[f"g{i}" for i in range(n)],
    )

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry, "run_agent", lambda agent, fn, **k: SimpleNamespace(output=fat)
    )

    definition = BespokeAgentDefinition(
        name="test-agent",
        interval_seconds=3600,
        system_prompt="You are a checker.",
    )

    out = run_bespoke_agent(
        settings=settings,
        definition=definition,
    )

    assert len(out.draft_titles) == MAX_DRAFTS
    assert len(out.draft_bodies) == MAX_DRAFTS
    assert len(out.gap_ids) == MAX_DRAFTS
    # Clipping keeps the first MAX_DRAFTS elements.
    assert out.draft_titles == [f"t{i}" for i in range(MAX_DRAFTS)]
    assert out.draft_bodies == [f"b{i}" for i in range(MAX_DRAFTS)]
    assert out.gap_ids == [f"g{i}" for i in range(MAX_DRAFTS)]


# ---------------------------------------------------------------------------
#  Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_result(settings, monkeypatch):
    """A normal pass returns the BespokeResult from the agent."""
    fat = BespokeResult(
        updated_memory="ledger entry",
        draft_titles=["draft 1"],
        draft_bodies=["body 1"],
        gap_ids=["g1"],
    )

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry, "run_agent", lambda agent, fn, **k: SimpleNamespace(output=fat)
    )

    definition = BespokeAgentDefinition(
        name="test-happy",
        interval_seconds=3600,
        system_prompt="You are a checker.",
    )

    out = run_bespoke_agent(
        settings=settings,
        definition=definition,
        memory="previous memory",
        recent_proposals="no recent proposals",
    )

    assert out.updated_memory == "ledger entry"
    assert out.draft_titles == ["draft 1"]
    assert out.draft_bodies == ["body 1"]
    assert out.gap_ids == ["g1"]


# ---------------------------------------------------------------------------
#  Level selection
# ---------------------------------------------------------------------------


def test_uses_definition_level_when_set(settings, monkeypatch):
    """When the definition specifies a level, it is passed to build_agent."""
    captured = {}

    def fake_build_agent(settings, **kwargs):
        captured.update(kwargs)
        return object()

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", fake_build_agent)
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry,
        "run_agent",
        lambda agent, fn, **k: SimpleNamespace(output=BespokeResult()),
    )

    definition = BespokeAgentDefinition(
        name="custom-level",
        interval_seconds=3600,
        system_prompt="You are a checker.",
        level=2,
    )

    run_bespoke_agent(settings=settings, definition=definition)

    assert captured["level"] == 2


def test_defaults_to_level_1_when_unset(settings, monkeypatch):
    """When the definition omits level, it defaults to 1 (cheap) and that
    is what reaches build_agent."""
    captured = {}

    def fake_build_agent(settings, **kwargs):
        captured.update(kwargs)
        return object()

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", fake_build_agent)
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry,
        "run_agent",
        lambda agent, fn, **k: SimpleNamespace(output=BespokeResult()),
    )

    definition = BespokeAgentDefinition(
        name="default-level",
        interval_seconds=3600,
        system_prompt="You are a checker.",
    )

    run_bespoke_agent(settings=settings, definition=definition)

    assert captured["level"] == 1


# ---------------------------------------------------------------------------
#  web_knowledge flag
# ---------------------------------------------------------------------------


def test_web_knowledge_passed_to_build_agent(settings, monkeypatch):
    """The definition's web_knowledge flag is forwarded to build_agent."""
    captured = {}

    def fake_build_agent(settings, **kwargs):
        captured.update(kwargs)
        return object()

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", fake_build_agent)
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry,
        "run_agent",
        lambda agent, fn, **k: SimpleNamespace(output=BespokeResult()),
    )

    definition = BespokeAgentDefinition(
        name="web-flag",
        interval_seconds=3600,
        system_prompt="You are a checker.",
        web_knowledge=True,
    )

    run_bespoke_agent(settings=settings, definition=definition)

    assert captured["web_knowledge"] is True


def test_web_knowledge_false_passed_to_build_agent(settings, monkeypatch):
    """When web_knowledge is explicitly False, it is forwarded as False."""
    captured = {}

    def fake_build_agent(settings, **kwargs):
        captured.update(kwargs)
        return object()

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", fake_build_agent)
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry,
        "run_agent",
        lambda agent, fn, **k: SimpleNamespace(output=BespokeResult()),
    )

    definition = BespokeAgentDefinition(
        name="web-flag-false",
        interval_seconds=3600,
        system_prompt="You are a checker.",
        web_knowledge=False,
    )

    run_bespoke_agent(settings=settings, definition=definition)

    assert captured["web_knowledge"] is False


# ---------------------------------------------------------------------------
#  _safe_close is called (success and error paths)
# ---------------------------------------------------------------------------


def test_safe_close_called(settings, monkeypatch):
    """_safe_close is called on the agent handle when run_agent succeeds."""
    handle = object()
    close_calls = []

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: handle)
    monkeypatch.setattr(base, "_safe_close", lambda agent: close_calls.append(agent))
    monkeypatch.setattr(
        retry,
        "run_agent",
        lambda agent, fn, **k: SimpleNamespace(output=BespokeResult()),
    )

    definition = BespokeAgentDefinition(
        name="safe-close",
        interval_seconds=3600,
        system_prompt="You are a checker.",
    )

    run_bespoke_agent(settings=settings, definition=definition)

    assert close_calls == [handle]


def test_safe_close_called_on_error(settings, monkeypatch):
    """_safe_close is still called when run_agent raises."""
    handle = object()
    close_calls = []

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: handle)
    monkeypatch.setattr(base, "_safe_close", lambda agent: close_calls.append(agent))

    def raise_error(agent, fn, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(retry, "run_agent", raise_error)

    definition = BespokeAgentDefinition(
        name="safe-close-error",
        interval_seconds=3600,
        system_prompt="You are a checker.",
    )

    with pytest.raises(RuntimeError, match="boom"):
        run_bespoke_agent(settings=settings, definition=definition)

    assert close_calls == [handle]


# ---------------------------------------------------------------------------
#  Agent name
# ---------------------------------------------------------------------------


def test_agent_name_prefixes_bespoke(settings, monkeypatch):
    """build_agent receives name='bespoke:{definition.name}'."""
    captured = {}

    def fake_build_agent(settings, **kwargs):
        captured.update(kwargs)
        return object()

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", fake_build_agent)
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry,
        "run_agent",
        lambda agent, fn, **k: SimpleNamespace(output=BespokeResult()),
    )

    definition = BespokeAgentDefinition(
        name="my-checker",
        interval_seconds=3600,
        system_prompt="You are a checker.",
    )

    run_bespoke_agent(settings=settings, definition=definition)

    assert captured["name"] == "bespoke:my-checker"


# ---------------------------------------------------------------------------
#  output_type is PromptedOutput(BespokeResult)
# ---------------------------------------------------------------------------


def test_output_type_is_prompted_bespoke_result(settings, monkeypatch):
    """build_agent receives a PromptedOutput(BespokeResult) as output_type."""
    from pydantic_ai import PromptedOutput

    captured = {}

    def fake_build_agent(settings, **kwargs):
        captured.update(kwargs)
        return object()

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", fake_build_agent)
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry,
        "run_agent",
        lambda agent, fn, **k: SimpleNamespace(output=BespokeResult()),
    )

    definition = BespokeAgentDefinition(
        name="output-type-test",
        interval_seconds=3600,
        system_prompt="You are a checker.",
    )

    run_bespoke_agent(settings=settings, definition=definition)

    ot = captured["output_type"]
    assert isinstance(ot, PromptedOutput)
    assert ot.outputs is BespokeResult
