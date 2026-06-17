"""Focused unit tests for ``src/robotsix_mill/stages/refine/helpers.py``.

These helpers were previously exercised only indirectly through the full
RefineStage integration tests, leaving state-routing, log-parsing, and
deploy-verification edge cases covered only incidentally. The tests here
drive each helper directly:

1. ``_spec_is_degenerate`` — empty/None/whitespace/placeholder specs
2. ``_rationale_claims_external_fix`` — external-fix claim detection
3. ``_resolve_next_state`` — success / gated / source / triage routing
4. ``_verify_cited_fix_at_head`` — commit-lookup edge cases
5. log-parsing helpers — ``_tail_file`` / ``_build_deployed_log_summary``
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import AutoApproveResult
from robotsix_mill.core.states import State
from robotsix_mill.stages import refine as refine_module


# ---------------------------------------------------------------------------
# _triage_reason_rejects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "the entire gap assertion is factually wrong — no change is needed",
        "No change is needed because the file already exists.",
        "This draft's assertion is factually wrong.",
        "No change needed — the reusable workflow already exists.",
    ],
)
def test_triage_reason_rejects_true(reason):
    assert refine_module._triage_reason_rejects(reason) is True


def test_triage_reason_rejects_false():
    assert refine_module._triage_reason_rejects("already a precise spec") is False
    assert refine_module._triage_reason_rejects("needs refinement") is False
    assert refine_module._triage_reason_rejects("") is False
    assert (
        refine_module._triage_reason_rejects(
            "the draft can be used as-is with minor tweaks"
        )
        is False
    )


# ---------------------------------------------------------------------------
# _spec_is_degenerate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        None,
        "",
        "   ",
        "\n\t  \n",
        "(see spec above)",
        "see the spec above",
        "as written above",
        "TBD",
        "todo",
        "...",  # only punctuation -> normalizes to empty
    ],
)
def test_spec_is_degenerate_true(spec):
    assert refine_module._spec_is_degenerate(spec) is True


def test_spec_is_degenerate_real_spec_false():
    spec = (
        "## Problem\nThe widget loader does not retry on transient 503s. "
        "Add a bounded exponential backoff with a max of three attempts."
    )
    assert refine_module._spec_is_degenerate(spec) is False


def test_spec_is_degenerate_long_string_never_matches():
    # A >120-char string is presumed to be a real spec even if it would
    # otherwise contain a placeholder phrase.
    spec = "see spec above " * 20  # well over 120 chars
    assert len(spec) > 120
    assert refine_module._spec_is_degenerate(spec) is False


# ---------------------------------------------------------------------------
# _rationale_claims_external_fix
# ---------------------------------------------------------------------------


def test_rationale_empty_false():
    assert refine_module._rationale_claims_external_fix("") is False
    assert refine_module._rationale_claims_external_fix("   ") is False


@pytest.mark.parametrize(
    "rationale",
    [
        "This was already implemented in a sibling ticket.",
        "Closing as a duplicate of an earlier ticket.",
        "The change was already merged last week.",
        "They shipped the fix in another PR.",
    ],
)
def test_rationale_external_fix_phrase_fires(rationale):
    assert refine_module._rationale_claims_external_fix(rationale) is True


def test_rationale_ref_plus_verb_fires():
    # Commit SHA + resolved verb co-occurrence (no canned phrase).
    assert (
        refine_module._rationale_claims_external_fix("Fixed in commit deadbeef.")
        is True
    )
    assert (
        refine_module._rationale_claims_external_fix(
            "The change was landed in abc1234 during the migration."
        )
        is True
    )
    # PR / MR number reference + resolved verb co-occurrence.
    assert (
        refine_module._rationale_claims_external_fix(
            "Fixed by PR #1386 — uv copied into the base stage…"
        )
        is True
    )
    assert (
        refine_module._rationale_claims_external_fix("Addressed by #42 last week.")
        is True
    )
    assert refine_module._rationale_claims_external_fix("Merged in MR !15.") is True


def test_rationale_ref_without_verb_does_not_fire():
    assert (
        refine_module._rationale_claims_external_fix(
            "See ticket 20260101T000000Z for background context."
        )
        is False
    )
    assert (
        refine_module._rationale_claims_external_fix(
            "See PR #1234 for background context."
        )
        is False
    )


def test_rationale_false_positive_marker_suppresses_fuzzy_rule():
    # Marker suppresses the ref+verb co-occurrence rule -> keeps closing DONE.
    assert (
        refine_module._rationale_claims_external_fix(
            "The reported function does not exist; nothing was fixed in deadbeef."
        )
        is False
    )


def test_rationale_info_only_marker_suppresses_fuzzy_rule():
    assert (
        refine_module._rationale_claims_external_fix(
            "This is information-only: documenting why deadbeef resolved it."
        )
        is False
    )


# ---------------------------------------------------------------------------
# _resolve_next_state
# ---------------------------------------------------------------------------


def _ctx(require_approval=True, auto_approve_enabled=False):
    return SimpleNamespace(
        settings=SimpleNamespace(
            require_approval=require_approval,
            auto_approve_enabled=auto_approve_enabled,
        )
    )


def test_resolve_next_state_no_approval_required():
    state, note = refine_module._resolve_next_state(
        _ctx(require_approval=False), "## Problem\nReal spec", "t1"
    )
    assert state is State.READY
    assert note is None


def test_resolve_next_state_degenerate_spec_gated():
    state, note = refine_module._resolve_next_state(_ctx(), "(see spec above)", "t1")
    assert state is State.HUMAN_ISSUE_APPROVAL
    assert note is None


def test_resolve_next_state_auto_approve_disabled():
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=False), "## Problem\nA genuine, real spec body", "t1"
    )
    assert state is State.HUMAN_ISSUE_APPROVAL
    assert note is None


def test_resolve_next_state_deterministic_source():
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        "## Problem\nA genuine, real spec body",
        "t1",
        source="test_gap",
    )
    assert state is State.READY
    assert note is not None and "auto-approve: APPROVE" in note
    assert "test_gap" in note


def test_resolve_next_state_triage_approve(monkeypatch):
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda *, settings, spec, **kw: AutoApproveResult(
            decision="APPROVE", reason="no design decision"
        ),
    )
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        "## Problem\nA genuine, real spec body",
        "t1",
    )
    assert state is State.READY
    assert note == "auto-approve: APPROVE — no design decision"


def test_resolve_next_state_triage_needs_approval(monkeypatch):
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda *, settings, spec, **kw: AutoApproveResult(
            decision="NEEDS_APPROVAL", reason="design decision present"
        ),
    )
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        "## Problem\nA genuine, real spec body",
        "t1",
    )
    assert state is State.HUMAN_ISSUE_APPROVAL
    assert note == "auto-approve: NEEDS_APPROVAL — design decision present"


def test_resolve_next_state_triage_error_falls_back(monkeypatch):
    def _boom(*, settings, spec, **kw):
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(refining, "triage_auto_approve", _boom)
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        "## Problem\nA genuine, real spec body",
        "t1",
    )
    assert state is State.HUMAN_ISSUE_APPROVAL
    assert note == "auto-approve: triage failed — falling back to human approval"


# ---------------------------------------------------------------------------
# _verify_cited_fix_at_head
# ---------------------------------------------------------------------------


def _git(repo: Path, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_verify_cited_fix_no_repo_dir():
    assert refine_module._verify_cited_fix_at_head(None, "fixed in deadbeef") is False


def test_verify_cited_fix_no_sha(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert refine_module._verify_cited_fix_at_head(repo, "no commit hash here") is False


def test_verify_cited_fix_ancestor_true(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _commit(repo, "a.txt", "hello")
    # Make the commit an ancestor of origin/main.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert refine_module._verify_cited_fix_at_head(repo, f"fixed in {sha}") is True


def test_verify_cited_fix_unknown_sha_false(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "a.txt", "hello")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # A well-formed but non-existent SHA -> not a commit object.
    assert (
        refine_module._verify_cited_fix_at_head(repo, "fixed in abcdef0123456") is False
    )


def test_verify_cited_fix_not_ancestor_false(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, "a.txt", "hello")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # A later commit not reachable from origin/main.
    later = _commit(repo, "b.txt", "world")
    assert later != base
    assert refine_module._verify_cited_fix_at_head(repo, f"fixed in {later}") is False


# ---------------------------------------------------------------------------
# _tail_file
# ---------------------------------------------------------------------------


def test_tail_file_returns_last_lines(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    assert refine_module._tail_file(f, 3) == "line8\nline9\nline10"


def test_tail_file_fewer_lines_than_max(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("only\n")
    assert refine_module._tail_file(f, 100) == "only"


def test_tail_file_missing_returns_empty(tmp_path):
    assert refine_module._tail_file(tmp_path / "nope.log", 5) == ""


# ---------------------------------------------------------------------------
# _build_deployed_log_summary
# ---------------------------------------------------------------------------


def test_build_summary_missing_folder():
    out = refine_module._build_deployed_log_summary(
        Path("/definitely/not/here"), "/configured/path"
    )
    assert "folder could not be listed" in out
    assert "/configured/path" in out


def test_build_summary_empty_folder(tmp_path):
    out = refine_module._build_deployed_log_summary(tmp_path, "/configured/path")
    assert "folder is empty" in out


def test_build_summary_lists_file_with_preview(tmp_path):
    (tmp_path / "app.log").write_text("first\nsecond\nthird\n")
    out = refine_module._build_deployed_log_summary(tmp_path, "/configured/path")
    assert "/configured/path" in out
    assert "`app.log`" in out
    # Tail preview is included for a small text-safe file.
    assert "third" in out


def test_build_summary_truncates_entry_list(tmp_path):
    # 25 entries -> only 20 listed, plus a "… and 5 more entries" note.
    for i in range(25):
        (tmp_path / f"f{i:02d}.log").write_text("x\n")
    out = refine_module._build_deployed_log_summary(tmp_path, "/configured/path")
    assert "… and 5 more entries" in out
