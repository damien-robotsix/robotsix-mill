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
    """Level 4 fails → the agent is rebuilt at level 3 and the run returns."""
    levels: list[int] = []
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        _spy_build(levels),
    )

    def _run_agent(agent, make_run, *, what="model call", sleep=None):
        if agent.level == 4:
            raise RuntimeError("You're out of usage credits")
        return f"ran-at-level-{agent.level}"

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _run_agent)

    out = load_and_run_agent(
        settings=object(),
        definition_name="epic_breakdown",  # level: 4
        prompt="break this down",
        what="epic-breakdown",
    )

    assert out == "ran-at-level-3"
    # Rebuilt per tier: the level selects the provider, so the level-3
    # attempt cannot reuse the level-4 agent.
    assert levels == [4, 3]


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

    def _reject_level_4(result) -> None:
        if result == 4:
            raise ValueError("returned zero children")

    out = load_and_run_agent(
        settings=object(),
        definition_name="epic_breakdown",
        prompt="break this down",
        what="epic-breakdown",
        validate=_reject_level_4,
    )

    assert out == 3
    assert levels == [4, 3]


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
    assert levels == [4]
