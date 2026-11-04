"""Refine deliverable-exists gate: a remediation draft whose
"Create new test file: <path>" deliverable already exists at the target
HEAD closes as ``already satisfied`` without running implement
(robotsix-ui ``collect.test.ts`` remediation, 2026-10-10)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from robotsix_mill.core.constants import ALREADY_SATISFIED_PREFIX
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.refine.core import RefineStage
from robotsix_mill.stages.refine.deliverable_exists import (
    check_deliverables_exist,
    extract_create_file_paths,
)

_DRAFT = (
    "## Finding 1\nAdd unit tests for `src/config-panel/collect.ts`.\n\n"
    "## Required work\n- Create new test file: `src/config-panel/collect.test.ts`\n"
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    """A clone whose ``origin/main`` already contains collect.test.ts."""
    origin = tmp_path / "origin"
    (origin / "src" / "config-panel").mkdir(parents=True)
    _git(origin, "init", "-q", "-b", "main")
    (origin / "src" / "config-panel" / "collect.ts").write_text("x\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "init")
    (origin / "src" / "config-panel" / "collect.test.ts").write_text("t\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "test: add collect tests")
    intro = _git(origin, "rev-parse", "HEAD")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    return work, intro


def test_extract_create_file_paths():
    draft = (
        "Create new test file: `a/b.test.ts`\n"
        "create a new file src/x.py.\n"
        "Create new file: `/etc/passwd.txt` and create file ../up.py\n"
        "Create new test file: `a/b.test.ts`\n"
    )
    assert extract_create_file_paths(draft) == ["a/b.test.ts", "src/x.py"]
    assert extract_create_file_paths("Add tests for src/x.py") == []


def test_existing_deliverable_returns_already_satisfied(clone):
    work, intro = clone
    note = check_deliverables_exist(_DRAFT, work, "main")
    assert note is not None
    assert note.startswith(ALREADY_SATISFIED_PREFIX)
    assert "src/config-panel/collect.test.ts" in note
    assert f"base {intro[:12]}" in note
    assert f"added in {intro[:12]}" in note


def test_missing_deliverable_proceeds(clone):
    work, _ = clone
    draft = _DRAFT + "- Create new test file: `src/config-panel/other.test.ts`\n"
    assert check_deliverables_exist(draft, work, "main") is None
    assert check_deliverables_exist("Fix a bug in collect.ts", work, "main") is None
    assert check_deliverables_exist(_DRAFT, None, "main") is None


def _ticket(**kw):
    base = {
        "id": "T1",
        "source": SourceKind.AGENT,
        "board_id": "b",
        "branch": None,
        "kind": TicketKind.TASK,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _gate(ticket, work, monkeypatch):
    monkeypatch.setattr("robotsix_mill.config.target_branch_for", lambda s, rc: "main")
    ctx = SimpleNamespace(repo_config=None)
    return RefineStage._run_deliverable_exists_gate(
        ctx, ticket, _DRAFT, work, SimpleNamespace()
    )


def test_gate_closes_remediation_draft_done(clone, monkeypatch):
    work, _ = clone
    ticket = _ticket()
    outcome = _gate(ticket, work, monkeypatch)
    assert outcome is not None
    assert outcome.next_state == State.DONE
    # The implementation guard must keep it DONE (no detour to implement).
    guarded = RefineStage._guard_implementation_done(None, ticket, outcome)
    assert guarded.next_state == State.DONE
    assert guarded.note.startswith(ALREADY_SATISFIED_PREFIX)


@pytest.mark.parametrize(
    "kw",
    [{"source": SourceKind.USER}, {"board_id": "meta"}, {"branch": "mill/T1"}],
)
def test_gate_skips_exempt_tickets(clone, monkeypatch, kw):
    work, _ = clone
    assert _gate(_ticket(**kw), work, monkeypatch) is None


def test_guard_still_redirects_other_done_notes():
    out = RefineStage._guard_implementation_done(
        None, _ticket(), Outcome(State.DONE, "looks fine")
    )
    assert out.next_state == State.READY
