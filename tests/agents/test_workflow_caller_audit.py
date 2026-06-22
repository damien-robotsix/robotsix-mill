"""Tests for :mod:`robotsix_mill.agents.workflow_caller_audit`."""

from __future__ import annotations

import contextlib
from pathlib import Path

from robotsix_mill.agents.tool_registry import ToolRegistry
from robotsix_mill.agents import workflow_caller_audit
from robotsix_mill.agents.workflow_caller_audit import (
    CANONICAL_ORG,
    MISSING_PERMISSION,
    WRONG_ORG,
    audit_workflow_callers,
    make_workflow_caller_audit_tool,
)


def _write_workflow(repo_dir: Path, name: str, content: str) -> Path:
    wf_dir = repo_dir / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    path = wf_dir / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# wrong-org detection
# ---------------------------------------------------------------------------


def test_wrong_org_detected(tmp_path):
    content = (
        "name: CI\n"
        "on: [push]\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  tests:\n"
        "    permissions:\n"
        "      contents: read\n"
        "      security-events: write\n"
        "    uses: robotsix/robotsix-mill/.github/workflows/python-ci.yml@main\n"
    )
    _write_workflow(tmp_path, "ci.yml", content)

    findings = audit_workflow_callers(tmp_path)

    wrong = [f for f in findings if f.kind == WRONG_ORG]
    assert len(wrong) == 1
    f = wrong[0]
    assert f.file == ".github/workflows/ci.yml"
    # The offending ``uses:`` line is line 10 in the content above.
    assert f.line == 10
    assert (
        f.correct_form
        == f"uses: {CANONICAL_ORG}/robotsix-mill/.github/workflows/python-ci.yml@main"
    )
    assert "damien-robotsix" in f.correct_form
    assert ".github/workflows/ci.yml" in f.message


# ---------------------------------------------------------------------------
# missing-permission detection
# ---------------------------------------------------------------------------


def test_missing_permission_detected_no_block(tmp_path):
    content = (
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  tests:\n"
        "    uses: damien-robotsix/robotsix-mill/.github/workflows/python-ci.yml@main\n"
    )
    _write_workflow(tmp_path, "ci.yml", content)

    findings = audit_workflow_callers(tmp_path)

    # Correct org → no wrong-org finding.
    assert not [f for f in findings if f.kind == WRONG_ORG]

    missing = [f for f in findings if f.kind == MISSING_PERMISSION]
    assert len(missing) == 1
    f = missing[0]
    assert f.file == ".github/workflows/ci.yml"
    assert f.line == 5
    assert "permissions:" in f.correct_form
    assert "contents: read" in f.correct_form
    assert "security-events: write" in f.correct_form


def test_missing_permission_detected_partial_block(tmp_path):
    """A block lacking ``security-events: write`` is still a finding."""
    content = (
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  tests:\n"
        "    permissions:\n"
        "      contents: read\n"
        "    uses: damien-robotsix/robotsix-mill/.github/workflows/python-ci.yml@main\n"
    )
    _write_workflow(tmp_path, "ci.yml", content)

    findings = audit_workflow_callers(tmp_path)

    missing = [f for f in findings if f.kind == MISSING_PERMISSION]
    assert len(missing) == 1
    assert "security-events: write" in missing[0].correct_form


# ---------------------------------------------------------------------------
# clean-caller no-finding cases
# ---------------------------------------------------------------------------


def test_clean_cross_repo_caller_no_findings(tmp_path):
    content = (
        "name: CI\n"
        "on: [push]\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  tests:\n"
        "    permissions:\n"
        "      contents: read\n"
        "      security-events: write\n"
        "    uses: damien-robotsix/robotsix-mill/.github/workflows/python-ci.yml@main\n"
    )
    _write_workflow(tmp_path, "ci.yml", content)

    assert audit_workflow_callers(tmp_path) == []


def test_clean_local_caller_no_findings(tmp_path):
    """Mill's own local ``./.github/workflows/...`` form is ignored."""
    content = (
        "name: CI\n"
        "on: [push]\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  tests:\n"
        "    permissions:\n"
        "      contents: read\n"
        "      security-events: write\n"
        "    uses: ./.github/workflows/python-ci.yml\n"
    )
    _write_workflow(tmp_path, "ci.yml", content)

    assert audit_workflow_callers(tmp_path) == []


def test_docs_caller_requires_contents_write(tmp_path):
    """``python-docs.yml`` requires ``contents: write``; ``read`` is a finding."""
    content = (
        "name: Docs\n"
        "on: [push]\n"
        "jobs:\n"
        "  docs:\n"
        "    permissions:\n"
        "      contents: read\n"
        "    uses: damien-robotsix/robotsix-mill/.github/workflows/python-docs.yml@main\n"
    )
    _write_workflow(tmp_path, "docs.yml", content)

    missing = [
        f for f in audit_workflow_callers(tmp_path) if f.kind == MISSING_PERMISSION
    ]
    assert len(missing) == 1
    assert "contents: write" in missing[0].correct_form


def test_no_workflows_dir(tmp_path):
    assert audit_workflow_callers(tmp_path) == []


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------


def test_make_tool_registers_in_registry(tmp_path):
    ToolRegistry._tools.pop("audit_workflow_callers", None)
    make_workflow_caller_audit_tool(tmp_path)
    matches = [
        t for t in ToolRegistry.list_tools() if t.name == "audit_workflow_callers"
    ]
    assert len(matches) == 1
    info = matches[0]
    assert info.category == "exploration"
    assert info.parameters == {}


def test_tool_returns_findings_text(tmp_path):
    content = (
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  tests:\n"
        "    uses: robotsix/robotsix-mill/.github/workflows/python-ci.yml@main\n"
    )
    _write_workflow(tmp_path, "ci.yml", content)

    tool = make_workflow_caller_audit_tool(tmp_path)
    text = tool()
    assert "WRONG_ORG" in text
    assert ".github/workflows/ci.yml" in text
    assert "damien-robotsix" in text


def test_tool_clean_repo_text(tmp_path):
    tool = make_workflow_caller_audit_tool(tmp_path)
    assert "no broken reusable-workflow callers" in tool()


# ── trace_stage child-span test ────────────────────────────────────────


def test_trace_stage_audit_workflow_callers_emits_span(tmp_path, monkeypatch):
    """audit_workflow_callers opens a child span named 'audit_workflow_callers' via trace_stage."""
    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(workflow_caller_audit, "trace_stage", fake_trace_stage)
    tool = make_workflow_caller_audit_tool(tmp_path)
    result = tool()
    assert "no broken reusable-workflow callers" in result
    assert spans == ["audit_workflow_callers"]
