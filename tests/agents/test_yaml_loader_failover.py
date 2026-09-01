"""``load_and_run_agent`` runs inside llmio's provider-failover loop.

A provider can be unavailable for reasons unrelated to the request — an
outage, or a Claude subscription whose usage credits are exhausted until
they reset. The capability level never changes: a provider-shaped failure
on the default slot reruns the SAME level on the OpenRouter fallback slot
(when ``provider_failover_enabled`` is on). These tests pin the slot
switch, the opt-in gate, and the validator hook that keeps a hollow
success from being returned.
"""

from __future__ import annotations

import httpx
import pytest
from robotsix_llmio.claude_sdk._errors import ClaudeSDKUsageExhaustedError

from robotsix_mill.agents.yaml_loader import load_and_run_agent


class _Settings:
    """Minimal settings stand-in: only the failover opt-in is read."""

    def __init__(self, failover: bool = False) -> None:
        self.provider_failover_enabled = failover


class _FakeAgent:
    """Stands in for an AgentHandle — only needs to be closeable."""

    def __init__(self, binding) -> None:
        self.binding = binding

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _spy_build(builds: list):
    """Record each (level, provider) build_agent_from_definition sees.

    ``tier_binding`` is ``None`` on the normal attempt (plain level
    resolution) and carries the forced slot binding on the cross-slot
    attempt; resolve the effective provider either way.
    """

    def _build(
        settings, definition, *, tools, level, tier_binding, repo_dir, **overrides
    ):
        from robotsix_llmio.core.factory import default_tier_config

        binding = tier_binding or default_tier_config().for_level(level)
        builds.append((level, binding.provider))
        return _FakeAgent(binding)

    return _build


def _run_script(outcomes: dict[str, list]):
    """run_agent stand-in scripted per provider prefix of the built binding."""

    def _run(agent, make_run, *, what):
        outcome = outcomes[agent.binding.provider].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _run


@pytest.fixture(autouse=True)
def _spies(monkeypatch):
    """Patch the build/run seams; each test wires its own script."""
    yield


def _wire(monkeypatch, builds, outcomes):
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        _spy_build(builds),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.retry.run_agent",
        _run_script(outcomes),
    )


def test_provider_failure_reruns_same_level_on_fallback_slot(monkeypatch):
    """Exhausted default slot → the agent is rebuilt on the OpenRouter slot
    at the SAME level and the run returns (failover opted in)."""
    builds: list = []
    _wire(
        monkeypatch,
        builds,
        {
            "claudeSDK": [ClaudeSDKUsageExhaustedError("weekly limit")],
            "openrouter": ["rescued"],
        },
    )

    result = load_and_run_agent(
        settings=_Settings(failover=True),
        definition_name="retrospect",
        prompt="go",
        what="test-run",
    )

    assert result == "rescued"
    levels = [lvl for lvl, _ in builds]
    assert levels[0] == levels[1]  # the level NEVER changes
    assert [prov for _, prov in builds] == ["claudeSDK", "openrouter"]


def test_no_failover_when_disabled(monkeypatch):
    """Without the opt-in, the default-slot failure propagates unchanged."""
    builds: list = []
    _wire(
        monkeypatch,
        builds,
        {"claudeSDK": [ClaudeSDKUsageExhaustedError("weekly limit")]},
    )

    with pytest.raises(ClaudeSDKUsageExhaustedError):
        load_and_run_agent(
            settings=_Settings(failover=False),
            definition_name="retrospect",
            prompt="go",
            what="test-run",
        )
    assert [prov for _, prov in builds] == ["claudeSDK"]


def test_success_builds_once_on_default_slot(monkeypatch):
    builds: list = []
    _wire(monkeypatch, builds, {"claudeSDK": ["fine"]})

    result = load_and_run_agent(
        settings=_Settings(failover=True),
        definition_name="retrospect",
        prompt="go",
        what="test-run",
    )

    assert result == "fine"
    assert [prov for _, prov in builds] == ["claudeSDK"]


def test_validate_failure_surfaces_without_cross_provider_retry(monkeypatch):
    """A result the validator rejects is task-shaped: it must surface as a
    real error, never be retried on the other provider (a weaker model
    re-running a doomed task would just spend twice)."""
    builds: list = []
    _wire(monkeypatch, builds, {"claudeSDK": ["hollow"]})

    def _reject(result):
        raise ValueError("no children")

    with pytest.raises(ValueError, match="no children"):
        load_and_run_agent(
            settings=_Settings(failover=True),
            definition_name="retrospect",
            prompt="go",
            what="test-run",
            validate=_reject,
        )
    assert [prov for _, prov in builds] == ["claudeSDK"]


def test_transient_outage_also_fails_over(monkeypatch):
    """A transient-classified failure that outlived its local retries is
    provider-shaped and crosses to the fallback slot."""
    builds: list = []
    _wire(
        monkeypatch,
        builds,
        {
            "claudeSDK": [httpx.ReadTimeout("provider down")],
            "openrouter": ["rescued"],
        },
    )

    result = load_and_run_agent(
        settings=_Settings(failover=True),
        definition_name="retrospect",
        prompt="go",
        what="test-run",
    )
    assert result == "rescued"
