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
from robotsix_mill.stages.refine.helpers import (
    _advisory_candidate_id,
    _strip_advisory_block,
    verify_claim,
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
# _resolve_next_state — triage_note rejection gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", refine_module._TRIAGE_REJECTION_PATTERNS)
def test_resolve_next_state_triage_note_rejection(pattern):
    """Every rejection pattern triggers HUMAN_ISSUE_APPROVAL for a
    deterministic auto-approve source when present in the triage note."""
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        "## Problem\nA genuine, real spec body",
        "t1",
        source="test_gap",
        triage_note=f"SKIP: the entire gap assertion is {pattern}",
    )
    assert state is State.HUMAN_ISSUE_APPROVAL
    assert "REJECTED" in (note or "")
    assert pattern in (note or "")


def test_resolve_next_state_triage_note_clean_passes():
    """A triage note with no rejection signal does NOT block auto-approve."""
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        "## Problem\nA genuine, real spec body",
        "t1",
        source="audit",
        triage_note="spec is already precise and implementation-ready",
    )
    assert state is State.READY
    assert note is not None and "APPROVE" in note


def test_resolve_next_state_triage_note_none_passes():
    """triage_note=None (default) behaves identically to before."""
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        "## Problem\nA genuine, real spec body",
        "t1",
        source="audit",
        triage_note=None,
    )
    assert state is State.READY
    assert note is not None and "APPROVE" in note


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


# ---------------------------------------------------------------------------
# _AUTO_APPROVE_SOURCES — module-level constant and deterministic routing
# ---------------------------------------------------------------------------


def test_auto_approve_sources_is_module_level_constant():
    """_AUTO_APPROVE_SOURCES is importable from refine_module, is a set,
    and contains exactly the seven expected source strings."""
    sources = refine_module._AUTO_APPROVE_SOURCES
    assert isinstance(sources, set)
    expected = {
        "test_gap",
        "audit",
        "agent_check",
        "bc_check",
        "completeness_check",
        "module_curator",
        "copy_paste",
    }
    assert sources == expected


def test_auto_approve_sources_resolve_next_state_deterministic():
    """For every source in _AUTO_APPROVE_SOURCES, _resolve_next_state
    returns State.READY and an APPROVE note when auto_approve_enabled=True
    and require_approval=True — guarding against accidental value drift."""
    for source in refine_module._AUTO_APPROVE_SOURCES:
        state, note = refine_module._resolve_next_state(
            _ctx(auto_approve_enabled=True),
            "## Problem\nA genuine, real spec body",
            "t1",
            source=source,
        )
        assert state is State.READY, f"source={source!r} unexpected state {state}"
        assert note is not None and "APPROVE" in note, (
            f"source={source!r} note missing APPROVE: {note!r}"
        )


# ---------------------------------------------------------------------------
# _draft_has_complete_spec
# ---------------------------------------------------------------------------


def test_draft_has_complete_spec_true_problem_plus_scope():
    draft = "## Problem\n\nThe widget does not retry on 503.\n\n## Scope\n\nAdd retry logic to loader.py.\n\n## Acceptance criteria\n\n- Retries up to 3 times.\n"
    assert refine_module._draft_has_complete_spec(draft) is True


def test_draft_has_complete_spec_true_problem_plus_acceptance_criteria():
    draft = "## Problem\n\nThe widget does not retry on 503.\n\n## Acceptance criteria\n\n- Retries up to 3 times.\n"
    assert refine_module._draft_has_complete_spec(draft) is True


def test_draft_has_complete_spec_true_problem_plus_acceptance():
    draft = "## Problem\n\nThe widget does not retry on 503.\n\n## Acceptance\n\n- Retries up to 3 times.\n"
    assert refine_module._draft_has_complete_spec(draft) is True


def test_draft_has_complete_spec_true_different_heading_levels():
    draft = "# Problem\n\n## Some detail\n\n### Scope\n\nFix it.\n"
    assert refine_module._draft_has_complete_spec(draft) is True


def test_draft_has_complete_spec_true_leading_whitespace():
    draft = "  ## Problem\n\n   ## Scope\n\nFix it.\n"
    assert refine_module._draft_has_complete_spec(draft) is True


def test_draft_has_complete_spec_false_only_problem():
    draft = (
        "## Problem\n\nThe widget does not retry on 503.\n\nWe should look into this."
    )
    assert refine_module._draft_has_complete_spec(draft) is False


def test_draft_has_complete_spec_false_raw_error_dump():
    draft = (
        "============================= FAILURES =============================\n"
        "FAILED tests/test_x.py::test_foo - AssertionError: expected 1 got 2\n"
        "========================= short test summary ========================\n"
        "FAILED tests/test_x.py::test_foo\n"
    )
    assert refine_module._draft_has_complete_spec(draft) is False


def test_draft_has_complete_spec_false_empty():
    assert refine_module._draft_has_complete_spec("") is False


def test_draft_has_complete_spec_false_whitespace():
    assert refine_module._draft_has_complete_spec("   \n\t  ") is False


def test_draft_has_complete_spec_false_prose_not_headings():
    draft = "The problem is that the scope is unclear and the acceptance criteria are not defined."
    assert refine_module._draft_has_complete_spec(draft) is False


def test_draft_has_complete_spec_false_heading_in_code_block():
    draft = "Here is a code block:\n\n```\n## Problem\n## Scope\n```\n\nBut no real headings."
    # Headings inside code blocks are NOT real headings in Markdown, but the
    # heuristic is deliberately simple — line-anchored regex only.  These
    # would match as headings.  Document the limitation: a CI ticket with
    # headings inside a fenced code block would be admitted to the fast-path.
    # This is acceptable: the bounded auto-approve classifier still runs as
    # a safety gate, and the cost win from skipping the refine LLM
    # outweighs the false-positive risk from this edge case.
    assert refine_module._draft_has_complete_spec(draft) is True


def test_draft_has_complete_spec_case_insensitive():
    draft = "## problem\n\n## scope\n\nFix it.\n"
    assert refine_module._draft_has_complete_spec(draft) is True


def test_draft_has_complete_spec_importable_from_refine_module():
    """_draft_has_complete_spec is re-exported from the refine package."""
    from robotsix_mill.stages.refine import _draft_has_complete_spec  # noqa: F811

    assert callable(_draft_has_complete_spec)
    assert _draft_has_complete_spec("## Problem\n\n## Scope\n\nx") is True


# ---------------------------------------------------------------------------
# _advisory_candidate_id / _strip_advisory_block
# ---------------------------------------------------------------------------


def test_advisory_candidate_id_extracts_candidate_id():
    text = (
        "> [!warning] Possible duplicate of 20260622T151555Z-foo-bar-d95e "
        "('Some ticket title') — matched on file path `src/foo.py`\n"
        ">\n"
        "> _Advisory flag from draft-intake pre-refine dedup; "
        "verify and close as duplicate during refine if confirmed._\n"
        "\n"
        "## Problem\n\nThe real draft body starts here.\n"
    )
    assert _advisory_candidate_id(text) == "20260622T151555Z-foo-bar-d95e"


def test_advisory_candidate_id_no_advisory_returns_none():
    assert _advisory_candidate_id("## Problem\nJust a normal draft body.") is None


def test_advisory_candidate_id_empty_text_returns_none():
    assert _advisory_candidate_id("") is None


def test_advisory_candidate_id_non_dedup_warning_block():
    """A [!warning] block without 'Possible duplicate of' returns None."""
    text = (
        "> [!warning] This is some other warning\n"
        ">\n"
        "> Just a warning, not a dedup advisory.\n"
        "\n"
        "## Problem\nReal body.\n"
    )
    assert _advisory_candidate_id(text) is None


def test_strip_advisory_block_removes_advisory():
    body = "## Problem\n\nThe real draft body.\n"
    text = (
        "> [!warning] Possible duplicate of 20260622T151555Z-foo-bar-d95e "
        "('Some ticket title') — matched on file path `src/foo.py`\n"
        ">\n"
        "> _Advisory flag from draft-intake pre-refine dedup; "
        "verify and close as duplicate during refine if confirmed._\n"
        "\n"
    ) + body
    assert _strip_advisory_block(text) == body


def test_strip_advisory_block_no_advisory_noop():
    text = "## Problem\nJust a normal draft body."
    assert _strip_advisory_block(text) == text


def test_strip_advisory_block_empty_text():
    assert _strip_advisory_block("") == ""


def test_strip_advisory_block_non_dedup_warning_left_intact():
    """A [!warning] block without 'Possible duplicate of' is NOT stripped."""
    text = (
        "> [!warning] This is some other warning\n"
        ">\n"
        "> Just a warning.\n"
        "\n"
        "## Problem\nReal body.\n"
    )
    assert _strip_advisory_block(text) == text


def test_strip_advisory_block_anchor_not_in_leading_block():
    """'Possible duplicate of' appears later in the body but not in the
    leading blockquote — the advisory strip is a no-op."""
    text = (
        "> [!warning] Some unrelated warning block\n"
        ">\n"
        "> This is not a dedup advisory.\n"
        "\n"
        "Now the phrase Possible duplicate of 20260622T... appears in prose.\n"
    )
    assert _strip_advisory_block(text) == text


def test_strip_advisory_block_idempotent():
    """Stripping an already-stripped body is a no-op."""
    body = "## Problem\nThe real body.\n"
    assert _strip_advisory_block(body) == body
    assert _strip_advisory_block(_strip_advisory_block(body)) == body


# ---------------------------------------------------------------------------
# verify_claim
# ---------------------------------------------------------------------------


def test_verify_claim_no_repo_dir_returns_true():
    """No repo to verify against — allow (best-effort)."""
    assert verify_claim("fixed in PR #346", ["ci.yml"], None) is True


def test_verify_claim_no_target_files_returns_true():
    """No target files — nothing to verify, allow."""
    assert verify_claim("fixed in PR #346", [], Path("/tmp/does-not-exist")) is True


def test_verify_claim_no_pr_or_sha_returns_true():
    """No concrete reference in the claim — nothing to verify."""
    assert (
        verify_claim(
            "this was already resolved", ["ci.yml"], Path("/tmp/does-not-exist")
        )
        is True
    )


def test_verify_claim_pr_confirmed(tmp_path):
    """A PR reference whose merge commit touches the target file → True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "ci.yml").write_text("name: CI\n")
    subprocess.run(["git", "-C", str(repo), "add", "ci.yml"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "Merge PR #346: fix CI"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )

    assert verify_claim("fixed in PR #346", ["ci.yml"], repo) is True


def test_verify_claim_pr_not_touching_target_file(tmp_path):
    """A PR whose merge commit does NOT touch the target file → False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "Merge PR #346: update docs"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )

    assert verify_claim("fixed in PR #346", ["ci.yml"], repo) is False


def test_verify_claim_commit_sha_confirmed(tmp_path):
    """A commit SHA that touches the target file → True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "ci.yml").write_text("name: CI\n")
    subprocess.run(["git", "-C", str(repo), "add", "ci.yml"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fix CI"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )

    assert verify_claim(f"fixed in {sha}", ["ci.yml"], repo) is True


def test_verify_claim_commit_sha_not_touching_target(tmp_path):
    """A commit SHA that does NOT touch the target file → False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "update docs"], check=True
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )

    assert verify_claim(f"fixed in {sha}", ["ci.yml"], repo) is False


def test_verify_claim_external_fix_phrase_with_recent_commit(tmp_path):
    """External-fix phrase with a recent commit touching target → True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "ci.yml").write_text("name: CI\n")
    subprocess.run(["git", "-C", str(repo), "add", "ci.yml"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fix CI"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )

    assert verify_claim("this was already fixed at HEAD", ["ci.yml"], repo) is True


def test_verify_claim_external_fix_phrase_no_recent_commit(tmp_path):
    """External-fix phrase but no recent commit touches target → False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    # Create a commit that does NOT touch ci.yml so origin/main can exist.
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )

    assert verify_claim("this was already fixed at HEAD", ["ci.yml"], repo) is False


def test_verify_claim_multiple_targets_one_matches(tmp_path):
    """One target file matches → True even if others don't."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "ci.yml").write_text("name: CI\n")
    subprocess.run(["git", "-C", str(repo), "add", "ci.yml"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "Merge PR #346: fix CI"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )

    assert (
        verify_claim("fixed in PR #346", ["ci.yml", "other.yml", "README.md"], repo)
        is True
    )


def test_verify_claim_importable_from_refine_module():
    """verify_claim is re-exported from the refine package for monkeypatch targets."""
    from robotsix_mill.stages.refine import verify_claim as vc

    assert callable(vc)
    assert vc("no refs here", [], Path("/tmp/does-not-exist")) is True


# ---------------------------------------------------------------------------
# _is_doc_only_change
# ---------------------------------------------------------------------------


class TestIsDocOnlyChange:
    """Tests for the :func:`_is_doc_only_change` heuristic."""

    def test_docs_dir_only(self):
        draft = "Update `docs/guide.md` and `docs/foo/bar.md`"
        assert refine_module._is_doc_only_change(draft) is True

    def test_md_files(self):
        draft = "Fix a typo in `README.md` and `CHANGELOG.md`"
        assert refine_module._is_doc_only_change(draft) is True

    def test_docs_and_md_mixed(self):
        draft = "Add `docs/new-page.md` and update `CONTRIBUTING.md`"
        assert refine_module._is_doc_only_change(draft) is True

    def test_py_file_not_doc_only(self):
        draft = "Fix `src/robotsix_mill/core.py` import"
        assert refine_module._is_doc_only_change(draft) is False

    def test_js_file_not_doc_only(self):
        draft = "Update `src/static/board.js` event handler"
        assert refine_module._is_doc_only_change(draft) is False

    def test_ts_file_not_doc_only(self):
        draft = "Refactor `src/app/main.ts`"
        assert refine_module._is_doc_only_change(draft) is False

    def test_yaml_file_not_doc_only(self):
        draft = "Change `config/settings.yaml` defaults"
        assert refine_module._is_doc_only_change(draft) is False

    def test_yml_file_not_doc_only(self):
        draft = "Update `.github/workflows/ci.yml`"
        assert refine_module._is_doc_only_change(draft) is False

    def test_mixed_code_and_docs_not_doc_only(self):
        draft = "Update `docs/guide.md` and `src/main.py`"
        assert refine_module._is_doc_only_change(draft) is False

    def test_no_file_paths_not_doc_only(self):
        draft = "Fix the bug in the widget loader"
        assert refine_module._is_doc_only_change(draft) is False

    def test_empty_draft_not_doc_only(self):
        assert refine_module._is_doc_only_change("") is False

    def test_title_included_in_check(self):
        draft = "Update the thing"
        title = "Fix `docs/setup.md` instructions"
        assert refine_module._is_doc_only_change(draft, title=title) is True

    def test_title_with_code_file_not_doc_only(self):
        draft = "Update the thing"
        title = "Fix `src/core.py` import"
        assert refine_module._is_doc_only_change(draft, title=title) is False

    def test_changelog_md_is_doc_only(self):
        draft = "Update `CHANGELOG.md` with release notes"
        assert refine_module._is_doc_only_change(draft) is True

    def test_unknown_extension_not_doc_only(self):
        draft = "Update `assets/logo.png`"
        assert refine_module._is_doc_only_change(draft) is False
