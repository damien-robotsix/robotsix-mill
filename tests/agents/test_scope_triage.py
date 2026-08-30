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


def _install_mocks(monkeypatch):
    """Install shared mocks for load_agent_definition, run_agent, and
    _safe_close.  Returns the base module for further patching."""
    from unittest.mock import MagicMock

    import robotsix_mill.agents.base as base_mod
    import robotsix_mill.agents.retry as retry_mod
    import robotsix_mill.agents.yaml_loader as yaml_loader_mod

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


def test_expand_for_new_test_file(monkeypatch):
    """A dif that adds a new test file → EXPAND with expand_files populated."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                assert "````ticket-spec" in msg
                assert "````file-map" in msg
                assert "````out-of-scope-files" in msg
                assert "````diff-summaries" in msg
                return type(
                    "R",
                    (),
                    {
                        "output": ScopeTriageVerdict(
                            action="EXPAND",
                            justification="New test file is a legitimate consequence of the ticket",
                            expand_files=["tests/test_feature.py"],
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp"),
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

    base_mod = _install_mocks(monkeypatch)

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                return type(
                    "R",
                    (),
                    {
                        "output": ScopeTriageVerdict(
                            action="REJECT",
                            justification="Unrelated module — scope creep",
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp"),
        ticket_spec="Add feature X to foo.py",
        file_map=["src/foo.py"],
        out_of_scope_files=["src/retry_ui.py"],
        diff_summaries={"src/retry_ui.py": "-def retry_ui(): ..."},
    )
    assert result.action == "REJECT"


def test_escalate_for_ambiguous(monkeypatch):
    """A vague spec with an adjacent out-of-scope file → ESCALATE."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                return type(
                    "R",
                    (),
                    {
                        "output": ScopeTriageVerdict(
                            action="ESCALATE",
                            justification="Ambiguous spec — cannot confidently classify",
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp"),
        ticket_spec="Improve error handling",
        file_map=["src/errors.py"],
        out_of_scope_files=["src/logging.py"],
        diff_summaries={"src/logging.py": "+import logging"},
    )
    assert result.action == "ESCALATE"


def test_prompt_includes_all_sections(monkeypatch):
    """The user prompt contains all four required XML sections."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)
    captured_msg: list[str] = []

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                captured_msg.append(msg)
                return type(
                    "R",
                    (),
                    {
                        "output": ScopeTriageVerdict(
                            action="EXPAND",
                            justification="test",
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    run_scope_triage_agent(
        settings=Settings(data_dir="/tmp"),
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


# ---------------------------------------------------------------------------
# Regression tests: companion-file rules in system prompt
# ---------------------------------------------------------------------------


def _load_scope_triage_system_prompt() -> str:
    """Load the scope-triage agent definition YAML and return its system_prompt."""
    from pathlib import Path

    from robotsix_mill.agents.yaml_loader import load_agent_definition

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent / "agent_definitions" / "scope_triage.yaml"
    )
    return definition.system_prompt


def test_system_prompt_covers_module_registration_companion():
    """The scope-triage prompt must treat docs/modules.yaml new-file
    registrations as EXPAND companions, not scope creep."""
    prompt = _load_scope_triage_system_prompt()
    low = prompt.lower()

    # Must explicitly cover registering a new file path under an existing module.
    assert "existing module" in low
    assert "module-registration check" in low
    # Must treat it as a direct mechanical consequence.
    assert "direct mechanical consequence" in low
    # Must still reject genuinely new module stanzas.
    assert "registers a new module entry" in low


def test_expand_for_companion_files(monkeypatch):
    """A new-file ticket whose implement adds docs/modules.yaml +
    accompanying doc file → plumbing routes EXPAND correctly."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                return type(
                    "R",
                    (),
                    {
                        "output": ScopeTriageVerdict(
                            action="EXPAND",
                            justification=(
                                "scope-triage EXPAND: companion files are legitimate\n\n"
                                "- docs/modules.yaml: module-registration compliance — "
                                "registering the new file is a direct mechanical consequence\n"
                                "- docs/notes/20260823T164252Z.md: accompanying doc "
                                "required by policy\n\n"
                                "No unrelated files were modified. "
                                "(added: docs/modules.yaml, docs/notes/20260823T164252Z.md)"
                            ),
                            expand_files=[
                                "docs/modules.yaml",
                                "docs/notes/20260823T164252Z.md",
                            ],
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp"),
        ticket_spec=(
            "Enable triage_boilerplate periodic workflow\n\n"
            "Create the file `.robotsix-mill/periodic/triage_boilerplate.yaml`"
        ),
        file_map=[".robotsix-mill/periodic/triage_boilerplate.yaml"],
        out_of_scope_files=[
            "docs/modules.yaml",
            "docs/notes/20260823T164252Z.md",
        ],
        diff_summaries={
            "docs/modules.yaml": "+  - .robotsix-mill/periodic/triage_boilerplate.yaml",
            "docs/notes/20260823T164252Z.md": (
                "Enable triage_boilerplate periodic workflow"
            ),
        },
    )
    assert result.action == "EXPAND"
    assert "docs/modules.yaml" in result.expand_files
    assert any("docs/notes" in f for f in result.expand_files)


def test_reject_for_companions_plus_unrelated_edit(monkeypatch):
    """Companion files + an unrelated edit (pyproject.toml) → REJECT
    (the unrelated edit is still scope creep)."""
    from robotsix_mill.config import Settings

    base_mod = _install_mocks(monkeypatch)

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        class FakeAgent:
            def run_sync(self, msg):
                return type(
                    "R",
                    (),
                    {
                        "output": ScopeTriageVerdict(
                            action="REJECT",
                            justification=(
                                "scope-triage REJECT: pyproject.toml malware-check=false "
                                "is an unrelated change not authorized by the spec. "
                                "The docs/modules.yaml and docs/notes entries are "
                                "legitimate companions, but the unrelated edit makes "
                                "the overall diff scope creep."
                            ),
                        ),
                    },
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent_from_definition", fake_build_agent)

    result = run_scope_triage_agent(
        settings=Settings(data_dir="/tmp"),
        ticket_spec=(
            "Enable triage_boilerplate periodic workflow\n\n"
            "Create the file `.robotsix-mill/periodic/triage_boilerplate.yaml`"
        ),
        file_map=[".robotsix-mill/periodic/triage_boilerplate.yaml"],
        out_of_scope_files=[
            "docs/modules.yaml",
            "docs/notes/20260823T164252Z.md",
            "pyproject.toml",
        ],
        diff_summaries={
            "docs/modules.yaml": "+  - .robotsix-mill/periodic/triage_boilerplate.yaml",
            "docs/notes/20260823T164252Z.md": (
                "Enable triage_boilerplate periodic workflow"
            ),
            "pyproject.toml": "-malware-check = true\n+malware-check = false",
        },
    )
    assert result.action == "REJECT"
