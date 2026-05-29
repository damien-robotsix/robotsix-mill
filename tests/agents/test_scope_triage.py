"""Unit tests for the scope-triage agent module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from robotsix_mill.agents.scope_triage import ScopeTriageVerdict, run_scope_triage_agent


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

def test_scope_triage_verdict_model_valid():
    """A valid ScopeTriageVerdict validates without error."""
    v = ScopeTriageVerdict(
        action="EXPAND",
        justification="new test file is a legitimate consequence",
        expand_files=["tests/test_foo.py"],
    )
    assert v.action == "EXPAND"
    assert v.expand_files == ["tests/test_foo.py"]


def test_scope_triage_verdict_model_invalid_action():
    """An invalid action raises ValidationError."""
    with pytest.raises(ValidationError):
        ScopeTriageVerdict(action="INVALID", justification="bad")


def test_scope_triage_verdict_model_expand_files_defaults_empty():
    """expand_files defaults to an empty list when not provided."""
    v = ScopeTriageVerdict(action="ESCALATE", justification="unsure")
    assert v.expand_files == []


# ---------------------------------------------------------------------------
# Agent call tests (monkeypatch pattern from test_refine.py)
# ---------------------------------------------------------------------------

def test_expand_for_new_test_file(monkeypatch):
    """A dif that adds a new test file → EXPAND with expand_files populated."""
    from robotsix_mill.config import Settings
    import robotsix_mill.agents.scope_triage as scope_triage
    import robotsix_mill.agents.base as base_mod

    def fake_build_agent(settings, definition, tools, model_name):
        class FakeAgent:
            def run_sync(self, msg):
                assert "````ticket-spec" in msg
                assert "````file-map" in msg
                assert "````out-of-scope-files" in msg
                assert "````diff-summaries" in msg
                return type("R", (), {
                    "output": ScopeTriageVerdict(
                        action="EXPAND",
                        justification="New test file is a legitimate consequence of the ticket",
                        expand_files=["tests/test_feature.py"],
                    ),
                })()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)
    from unittest.mock import MagicMock
    scope_triage.load_agent_definition = MagicMock(return_value=type("D", (), {"model": None})())
    scope_triage.call_with_retry = lambda fn, **kw: fn()

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp", scope_triage_model="test/model"),
        ticket_spec="Add feature X to foo.py",
        file_map=["src/foo.py"],
        out_of_scope_files=["tests/test_feature.py"],
        diff_summaries={"tests/test_feature.py": "+def test_feature():"},
    )
    assert result.action == "EXPAND"
    assert result.expand_files == ["tests/test_feature.py"]


def test_reject_for_unrelated_module(monkeypatch):
    """A diff touching an unrelated module → REJECT."""
    from robotsix_mill.config import Settings
    import robotsix_mill.agents.scope_triage as scope_triage
    import robotsix_mill.agents.base as base_mod

    def fake_build_agent(settings, definition, tools, model_name):
        class FakeAgent:
            def run_sync(self, msg):
                return type("R", (), {
                    "output": ScopeTriageVerdict(
                        action="REJECT",
                        justification="Unrelated module — scope creep",
                    ),
                })()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)
    from unittest.mock import MagicMock
    scope_triage.load_agent_definition = MagicMock(return_value=type("D", (), {"model": None})())
    scope_triage.call_with_retry = lambda fn, **kw: fn()

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp", scope_triage_model="test/model"),
        ticket_spec="Add feature X to foo.py",
        file_map=["src/foo.py"],
        out_of_scope_files=["src/retry_ui.py"],
        diff_summaries={"src/retry_ui.py": "-def retry_ui(): ..."},
    )
    assert result.action == "REJECT"


def test_escalate_for_ambiguous(monkeypatch):
    """A vague spec with an adjacent out-of-scope file → ESCALATE."""
    from robotsix_mill.config import Settings
    import robotsix_mill.agents.scope_triage as scope_triage
    import robotsix_mill.agents.base as base_mod

    def fake_build_agent(settings, definition, tools, model_name):
        class FakeAgent:
            def run_sync(self, msg):
                return type("R", (), {
                    "output": ScopeTriageVerdict(
                        action="ESCALATE",
                        justification="Ambiguous spec — cannot confidently classify",
                    ),
                })()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)
    from unittest.mock import MagicMock
    scope_triage.load_agent_definition = MagicMock(return_value=type("D", (), {"model": None})())
    scope_triage.call_with_retry = lambda fn, **kw: fn()

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp", scope_triage_model="test/model"),
        ticket_spec="Improve error handling",
        file_map=["src/errors.py"],
        out_of_scope_files=["src/logging.py"],
        diff_summaries={"src/logging.py": "+import logging"},
    )
    assert result.action == "ESCALATE"


def test_prompt_includes_all_sections(monkeypatch):
    """The user prompt contains all four required XML sections."""
    from robotsix_mill.config import Settings
    import robotsix_mill.agents.scope_triage as scope_triage
    import robotsix_mill.agents.base as base_mod

    captured_msg: list[str] = []

    def fake_build_agent(settings, definition, tools, model_name):
        class FakeAgent:
            def run_sync(self, msg):
                captured_msg.append(msg)
                return type("R", (), {
                    "output": ScopeTriageVerdict(
                        action="EXPAND",
                        justification="test",
                    ),
                })()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)
    from unittest.mock import MagicMock
    scope_triage.load_agent_definition = MagicMock(return_value=type("D", (), {"model": None})())
    scope_triage.call_with_retry = lambda fn, **kw: fn()

    run_scope_triage_agent(
        settings=Settings(data_dir="/tmp", scope_triage_model="test/model"),
        ticket_spec="Add feature X",
        file_map=["src/foo.py"],
        out_of_scope_files=["tests/test_foo.py"],
        diff_summaries={"tests/test_foo.py": "+def test_foo():"},
    )

    msg = captured_msg[0]
    assert "````ticket-spec" in msg
    assert "````file-map" in msg
    assert "````out-of-scope-files" in msg
    assert "````diff-summaries" in msg
