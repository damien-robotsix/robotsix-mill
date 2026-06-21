"""Unit tests for the test-scope agent module.

Mirrors ``tests/agents/test_scope_triage.py`` in its monkeypatch pattern.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# Import renamed to avoid pytest collecting TestScopeVerdict (which starts
# with "Test") as a test class.
from robotsix_mill.agents.test_scope import TestScopeVerdict as _TestScopeVerdict
from robotsix_mill.agents.test_scope import run_test_scope_agent


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


def test_test_scope_verdict_model_valid() -> None:
    """A valid TestScopeVerdict validates without error."""
    v = _TestScopeVerdict(
        needs_full_suite=False,
        rationale="documentation-only change",
    )
    assert v.needs_full_suite is False
    assert v.rationale == "documentation-only change"


def test_test_scope_verdict_model_true() -> None:
    """needs_full_suite=True validates."""
    v = _TestScopeVerdict(
        needs_full_suite=True,
        rationale="config file read at runtime",
    )
    assert v.needs_full_suite is True


def test_test_scope_verdict_missing_field_raises() -> None:
    """Missing required field raises ValidationError."""
    with pytest.raises(ValidationError):
        _TestScopeVerdict(needs_full_suite=True)  # noqa: B026 — deliberate missing field


# ---------------------------------------------------------------------------
# Agent call tests (monkeypatch pattern from test_scope_triage.py)
# ---------------------------------------------------------------------------


def _install_mocks(monkeypatch):
    """Install shared mocks for load_agent_definition, run_agent, and
    _safe_close.  Returns the base module for further patching."""
    from unittest.mock import MagicMock
    import robotsix_mill.agents.yaml_loader as yaml_loader_mod
    import robotsix_mill.agents.retry as retry_mod
    import robotsix_mill.agents.base as base_mod

    monkeypatch.setattr(
        yaml_loader_mod,
        "load_agent_definition",
        MagicMock(return_value=type("D", (), {"level": 1})()),
    )
    monkeypatch.setattr(
        retry_mod,
        "run_agent",
        lambda agent, make_run, **kw: make_run(agent),
    )
    monkeypatch.setattr(base_mod, "_safe_close", lambda agent: None)
    return base_mod


def test_agent_says_skip(monkeypatch) -> None:
    """Agent returns needs_full_suite=False → returned verbatim."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)

    # Ensure API key is "present".
    monkeypatch.setattr(
        "robotsix_mill.config.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": "sk-fake"})(),
    )

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                assert "Changed files" in msg
                assert "Diff stat" in msg
                assert "Ticket intent" in msg
                return type(
                    "R",
                    (),
                    {
                        "output": _TestScopeVerdict(
                            needs_full_suite=False,
                            rationale="documentation-only change",
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    result = run_test_scope_agent(
        settings=Settings(data_dir="/tmp"),
        changed_files=["docs/x.md"],
        diff_stat=" docs/x.md | 2 +-",
        ticket_summary="Fix typo in docs",
    )
    assert result.needs_full_suite is False
    assert "documentation-only" in result.rationale


def test_agent_says_run(monkeypatch) -> None:
    """Agent returns needs_full_suite=True → returned verbatim."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)

    monkeypatch.setattr(
        "robotsix_mill.config.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": "sk-fake"})(),
    )

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                return type(
                    "R",
                    (),
                    {
                        "output": _TestScopeVerdict(
                            needs_full_suite=True,
                            rationale="config file is loaded at runtime",
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    result = run_test_scope_agent(
        settings=Settings(data_dir="/tmp"),
        changed_files=["config.json"],
        diff_stat=" config.json | 10 +++++",
        ticket_summary="Update runtime config",
    )
    assert result.needs_full_suite is True
    assert "loaded at runtime" in result.rationale


def test_missing_api_key_fails_safe(monkeypatch) -> None:
    """Missing openrouter_api_key → needs_full_suite=True, no exception."""
    from robotsix_mill.config import Settings

    # No API key — get_secrets returns a stub without the key.
    monkeypatch.setattr(
        "robotsix_mill.config.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": None})(),
    )

    result = run_test_scope_agent(
        settings=Settings(data_dir="/tmp"),
        changed_files=["docs/x.md"],
        diff_stat=" docs/x.md | 2 +-",
        ticket_summary="Fix typo",
    )
    assert result.needs_full_suite is True
    assert "no API key" in result.rationale


def test_agent_error_fails_safe(monkeypatch) -> None:
    """When load_and_run_agent raises → needs_full_suite=True, no exception."""
    from robotsix_mill.config import Settings

    monkeypatch.setattr(
        "robotsix_mill.config.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": "sk-fake"})(),
    )

    # Make load_and_run_agent raise
    def _raise(**kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(
        "robotsix_mill.agents.yaml_loader.load_and_run_agent",
        _raise,
    )

    result = run_test_scope_agent(
        settings=Settings(data_dir="/tmp"),
        changed_files=["docs/x.md"],
        diff_stat=" docs/x.md | 2 +-",
        ticket_summary="Fix typo",
    )
    assert result.needs_full_suite is True
    assert "defaulting to full suite" in result.rationale


def test_prompt_includes_all_sections(monkeypatch) -> None:
    """The user prompt contains all three required sections."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)

    monkeypatch.setattr(
        "robotsix_mill.config.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": "sk-fake"})(),
    )

    captured_msg: list[str] = []

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                captured_msg.append(msg)
                return type(
                    "R",
                    (),
                    {
                        "output": _TestScopeVerdict(
                            needs_full_suite=False,
                            rationale="doc-only",
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    run_test_scope_agent(
        settings=Settings(data_dir="/tmp"),
        changed_files=["docs/x.md"],
        diff_stat=" docs/x.md | 1 +",
        ticket_summary="Fix typo",
    )

    msg = captured_msg[0]
    assert "Changed files" in msg
    assert "Diff stat" in msg
    assert "Ticket intent" in msg
