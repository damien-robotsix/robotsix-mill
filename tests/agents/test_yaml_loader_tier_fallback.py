"""``load_and_run_agent`` runs inside llmio's tier-fallback loop.

A tier can be unavailable for reasons unrelated to the request — a provider
outage, or a Claude subscription whose usage credits are exhausted until they
reset. Observed 2026-08-09: epic-breakdown, pinned to level 4, died at turn 1
on exhausted credits while level 3 (a different model with its own limit) was
answering. These tests pin the escalation and the validator hook that keeps a
weaker tier from returning a hollow success.
"""

from __future__ import annotations

import pytest
from robotsix_llmio.core.cooldown import reset_health_tracker

from robotsix_mill.agents.yaml_loader import load_and_run_agent


class _Settings:
    """Minimal settings stand-in: only the fallback opt-in is read."""

    def __init__(self, paid_fallback: bool = False) -> None:
        self.claude_exhaustion_paid_fallback = paid_fallback


class _FakeAgent:
    """Stands in for an AgentHandle — only needs to be closeable."""

    def __init__(self, level: int) -> None:
        self.level = level

    def close(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture(autouse=True)
def _fresh_health_tracker():
    """llmio's cooldown tracker is process-global; keep tests order-free."""
    reset_health_tracker()
    yield
    reset_health_tracker()


def _spy_build(levels: list[int]):
    def _build(settings, definition, *, tools, level, repo_dir, **overrides):
        levels.append(level)
        return _FakeAgent(level)

    return _build


def test_falls_back_to_next_tier_when_the_starting_tier_fails(monkeypatch):
    """Level 5 fails → the agent is rebuilt at level 4 and the run returns
    (paid fallback opted in — a Claude start otherwise does not fall back)."""
    levels: list[int] = []
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        _spy_build(levels),
    )

    def _run_agent(agent, make_run, *, what="model call", sleep=None):
        if agent.level == 5:
            raise RuntimeError("You're out of usage credits")
        return f"ran-at-level-{agent.level}"

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _run_agent)

    out = load_and_run_agent(
        settings=_Settings(paid_fallback=True),
        definition_name="epic_breakdown",  # level: 5
        prompt="break this down",
        what="epic-breakdown",
    )

    assert out == "ran-at-level-4"
    # Rebuilt per tier: the level selects the provider, so the level-4
    # attempt cannot reuse the level-5 agent.
    assert levels == [5, 4]


def test_validate_failure_is_treated_as_a_tier_failure(monkeypatch):
    """A result that parsed but is unusable escalates instead of returning.

    This is the epic-breakdown failure mode: a weaker tier emits the
    structured output under the wrong keys, which parses into zero children.
    """
    levels: list[int] = []
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        _spy_build(levels),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.retry.run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: agent.level,
    )

    def _reject_level_5(result) -> None:
        if result == 5:
            raise ValueError("returned zero children")

    out = load_and_run_agent(
        settings=_Settings(paid_fallback=True),
        definition_name="epic_breakdown",
        prompt="break this down",
        what="epic-breakdown",
        validate=_reject_level_5,
    )

    assert out == 4
    assert levels == [5, 4]


def test_no_fallback_when_the_first_tier_succeeds(monkeypatch):
    """The common path stays one build, one run."""
    levels: list[int] = []
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        _spy_build(levels),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.retry.run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: "ok",
    )

    out = load_and_run_agent(
        settings=object(),
        definition_name="epic_breakdown",
        prompt="break this down",
        what="epic-breakdown",
    )

    assert out == "ok"
    assert levels == [5]


def test_claude_start_does_not_fall_back_by_default(monkeypatch):
    """A Claude-backed start (level 5) with the paid fallback off raises the
    original failure after ONE attempt: no sibling Claude level (dead for the
    same quota), no keyed OpenRouter level (real money). The worker parks."""
    levels: list[int] = []
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        _spy_build(levels),
    )

    def _run_agent(agent, make_run, *, what="model call", sleep=None):
        raise RuntimeError("You've hit your session limit · resets 9:20am (UTC)")

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _run_agent)

    with pytest.raises(RuntimeError, match="session limit"):
        load_and_run_agent(
            settings=_Settings(),
            definition_name="epic_breakdown",  # level: 5 (Claude)
            prompt="break this down",
            what="epic-breakdown",
        )
    assert levels == [5]


def test_keyed_start_still_falls_back(monkeypatch):
    """A keyed (OpenRouter) start keeps llmio's fallback: level 1 → level 2."""
    levels: list[int] = []
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        _spy_build(levels),
    )

    def _run_agent(agent, make_run, *, what="model call", sleep=None):
        if agent.level == 1:
            raise RuntimeError("upstream 502")
        return f"ran-at-level-{agent.level}"

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _run_agent)

    out = load_and_run_agent(
        settings=_Settings(),
        definition_name="epic_breakdown",
        level=1,
        prompt="break this down",
        what="epic-breakdown",
    )
    assert out == "ran-at-level-2"
    assert levels == [1, 2]
