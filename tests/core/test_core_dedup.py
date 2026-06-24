"""Tests for the shared ticket-dedup primitives in ``robotsix_mill.core.dedup``.

These exercise ``find_prior_matching_ticket`` directly (no runner, no
LLM): file-path body match, fingerprint title match, recency-window
exclusion, ERRORED/declined-CLOSED exclusion, ``exclude_ids`` exclusion,
and multi-source vs single-source candidate querying.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select as _select

from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core.db import session as db_session
from robotsix_mill.core.models import SourceKind, Ticket, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.core.dedup import (
    _ci_draft_fingerprint,
    _describe_recent_signal,
    _extract_concern_tokens,
    _extract_paths,
    _scope_paths,
    annotate_child_body,
    find_child_overlaps,
    find_inflight_overlap,
    find_prior_matching_ticket,
    normalize,
    paths_excluding_out_of_scope,
)

_BOARD = "test-board"
_TARGET_PATH = "src/robotsix_mill/foo.py"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    _reset_secrets()
    from robotsix_mill.config import _reset_repos_config
    from robotsix_mill.core import db

    db.reset_engine()
    _reset_repos_config()
    return Settings(data_dir=str(tmp_path))


def _svc(settings):
    return TicketService(settings, board_id=_BOARD)


def _seed(settings, title, body="", source=SourceKind.TRACE_REVIEW):
    svc = _svc(settings)
    ticket = svc.create(title=title, description=body, source=source)
    return svc, ticket


def _backdate(settings, ticket_id, when):
    with db_session(settings, _BOARD) as s:
        row = s.exec(_select(Ticket).where(Ticket.id == ticket_id)).first()
        assert row is not None
        row.created_at = when
        s.add(row)
        s.commit()


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_strips_punctuation_and_case():
    assert normalize("Trace-Review: Tool Errors!") == "trace review tool errors"


# ---------------------------------------------------------------------------
# find_prior_matching_ticket
# ---------------------------------------------------------------------------


def test_file_path_body_match(settings):
    svc, ticket = _seed(
        settings,
        title="some unrelated title",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    svc.transition(ticket.id, State.DONE, note="merged")

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "a symptom that does not appear in any title",
        settings,
        _now(),
    )
    assert match is not None
    assert match.id == ticket.id


def test_fingerprint_title_match_without_file_path(settings):
    _seed(
        settings,
        title=("tool_error — claude code returned an error result success"),
        body="unrelated body that does not name any code locus",
    )
    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [],
        "Claude Code returned an error result: success",
        settings,
        _now(),
    )
    assert match is not None


def test_recency_window_excludes_older_matches(settings):
    svc, ticket = _seed(
        settings,
        title="tool_error — earlier wrapper bug",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    svc.transition(ticket.id, State.DONE, note="merged")
    _backdate(settings, ticket.id, _now() - timedelta(days=30))

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "earlier wrapper bug",
        settings,
        _now(),
        lookback_days=7,
    )
    assert match is None


def test_errored_candidate_excluded(settings):
    svc, ticket = _seed(
        settings,
        title="tool_error — earlier wrapper bug",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    svc.transition(ticket.id, State.ERRORED, note="fix attempt failed")

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "earlier wrapper bug",
        settings,
        _now(),
    )
    assert match is None


def test_declined_closed_candidate_excluded(settings):
    svc, ticket = _seed(
        settings,
        title="tool_error — earlier wrapper bug",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    # DRAFT → CLOSED directly (never DONE) = declined draft.
    svc.transition(ticket.id, State.CLOSED, note="declined as noise")

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "earlier wrapper bug",
        settings,
        _now(),
    )
    assert match is None


def test_closed_after_done_candidate_matches(settings):
    svc, ticket = _seed(
        settings,
        title="tool_error — earlier wrapper bug",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    svc.transition(ticket.id, State.DONE, note="merged")
    svc.transition(ticket.id, State.CLOSED, note="retrospected")

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "earlier wrapper bug",
        settings,
        _now(),
    )
    assert match is not None
    assert match.id == ticket.id


def test_exclude_ids_excludes_candidate(settings):
    svc, ticket = _seed(
        settings,
        title="tool_error — earlier wrapper bug",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    svc.transition(ticket.id, State.DONE, note="merged")

    # Without exclusion it matches…
    assert (
        find_prior_matching_ticket(
            _svc(settings), _BOARD, [_TARGET_PATH], "x", settings, _now()
        )
        is not None
    )
    # …with the id excluded it does not.
    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "x",
        settings,
        _now(),
        exclude_ids={ticket.id},
    )
    assert match is None


def test_multi_source_vs_single_source_querying(settings):
    # Seed a single META-sourced ticket whose title carries the fingerprint.
    _seed(
        settings,
        title="meta: extraction — shared dedup helper across repos",
        body="unrelated body",
        source=SourceKind.META,
    )
    fingerprint = "shared dedup helper across repos"

    # sources=None → matches across ALL sources.
    assert (
        find_prior_matching_ticket(
            _svc(settings), _BOARD, [], fingerprint, settings, _now()
        )
        is not None
    )

    # sources=[META] → the union includes META, so it matches.
    assert (
        find_prior_matching_ticket(
            _svc(settings),
            _BOARD,
            [],
            fingerprint,
            settings,
            _now(),
            sources=[SourceKind.META],
        )
        is not None
    )

    # sources=[TRACE_REVIEW] → META candidate is filtered out, no match.
    assert (
        find_prior_matching_ticket(
            _svc(settings),
            _BOARD,
            [],
            fingerprint,
            settings,
            _now(),
            sources=[SourceKind.TRACE_REVIEW],
        )
        is None
    )


# ---------------------------------------------------------------------------
# find_child_overlaps (epic-decomposition pre-filing dedup)
# ---------------------------------------------------------------------------


def test_child_overlaps_flags_recent_shipped_ticket(settings):
    """A proposed child whose body names a file already addressed by a
    recent shipped ticket is flagged (the 1b28/4c84 class)."""
    svc, prior = _seed(
        settings,
        title="gate-or-remove jscpd in CI",
        # The shared path is in the candidate's declared scope, so a lone
        # shared path still corroborates under the strict-scope rule.
        body="## Scope\n\nchanges .github/workflows/ci.yml to drop the jscpd step",
    )
    svc.transition(prior.id, State.DONE, note="merged")

    notes = find_child_overlaps(
        _svc(settings),
        "EPIC-1",
        ["jscpd: gate or remove the duplicate-detection step"],
        ["Gate-or-remove `npx jscpd@4 ... || true` in .github/workflows/ci.yml"],
        settings,
        _now(),
    )
    assert len(notes) == 1
    assert notes[0] is not None
    assert prior.id in notes[0]
    assert "ci.yml" in notes[0]


def test_child_overlaps_flags_in_batch_sibling(settings):
    """The later of two siblings sharing an extracted file path is
    flagged (the c8b9/6971 class)."""
    notes = find_child_overlaps(
        _svc(settings),
        "EPIC-1",
        [
            "Trivy gate severity — document SARIF decision",
            "Trivy SARIF upload — observability vs gating",
        ],
        [
            "Decide gate severity; note the SARIF call in CONTRIBUTING.md",
            "Audit the Trivy SARIF upload, documented in CONTRIBUTING.md",
        ],
        settings,
        _now(),
    )
    assert notes[0] is None
    assert notes[1] is not None
    assert "sibling #0" in notes[1]
    assert "CONTRIBUTING.md" in notes[1]


def test_child_overlaps_unflagged_when_distinct(settings):
    """Non-overlapping children (distinct paths, distinct titles) are
    not flagged."""
    notes = find_child_overlaps(
        _svc(settings),
        "EPIC-1",
        ["Add retry to the queue consumer", "Refactor the config loader"],
        [
            "Touch src/robotsix_mill/runtime/worker.py only",
            "Touch src/robotsix_mill/config.py only",
        ],
        settings,
        _now(),
    )
    assert notes == [None, None]


def test_child_overlaps_excludes_epic_and_existing_children(settings):
    """The epic and its already-existing children must not self-match the
    recent-ticket check."""
    svc = _svc(settings)
    epic = svc.create(title="The epic", description="", kind=TicketKind.EPIC)
    existing = svc.create(
        title="existing child",
        description="edits .github/workflows/ci.yml already",
        parent_id=epic.id,
    )
    # A new proposed child referencing the same path as the EXISTING child
    # must not be flagged by the recent-ticket check (existing children
    # are excluded) — and there is no earlier sibling in this batch.
    notes = find_child_overlaps(
        svc,
        epic.id,
        ["brand new scope"],
        ["also touches .github/workflows/ci.yml"],
        settings,
        _now(),
    )
    assert notes == [None]
    assert existing.id  # referenced to keep linter quiet


def test_annotate_child_body_prepends_warning():
    out = annotate_child_body("original body text", "Possible duplicate of T-9")
    assert out.startswith("> [!warning] Possible duplicate of T-9")
    assert "original body text" in out


def test_annotate_child_body_custom_source_desc():
    out = annotate_child_body(
        "body", "Possible duplicate of T-9", source_desc="draft-intake pre-refine dedup"
    )
    assert "draft-intake pre-refine dedup" in out
    assert "epic-decomposition" not in out


def test_find_prior_suppress_title_only_match_true_returns_none(settings):
    """When suppress_title_only_match=True, a candidate whose title contains
    the fingerprint but shares no file path is NOT returned."""
    _seed(
        settings,
        title="CI failure: CI on main (liveness probe timeout)",
        body="body that names no code locus at all",
    )
    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [],
        "CI failure: CI on main — liveness probe timeout",
        settings,
        _now(),
        suppress_title_only_match=True,
    )
    assert match is None


def test_find_prior_suppress_title_only_match_false_returns_candidate(settings):
    """When suppress_title_only_match=False (default), a title-only match
    still returns the candidate — existing behaviour preserved."""
    svc, ticket = _seed(
        settings,
        title="CI failure: CI on main (liveness probe timeout)",
        body="body that names no code locus at all",
    )
    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [],
        "CI failure: CI on main — liveness probe timeout",
        settings,
        _now(),
        suppress_title_only_match=False,
    )
    assert match is not None
    assert match.id == ticket.id


# ---------------------------------------------------------------------------
# find_inflight_overlap (draft-intake pre-refine dedup)
# ---------------------------------------------------------------------------


def test_inflight_overlap_flags_concurrent_ready_ticket(settings):
    """A fresh draft whose body names a file already targeted by a
    CONCURRENT in-flight (READY, never DONE) ticket is flagged — the
    structural gap the refine dedup guard cannot close."""
    svc, prior = _seed(
        settings,
        title="rework the login form",
        # The candidate declares the shared path under ``## Scope`` so a
        # lone shared path still flags under the strict-scope rule.
        body=(
            f"## Scope\n\nchanges {_TARGET_PATH} to `validate_input`, "
            "`sanitize`, and `normalize`"
        ),
    )
    # In-flight, NOT terminal: the dedup guard would reject this.
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix the login form validation",
        (
            f"also edits {_TARGET_PATH} for `validate_input`, "
            "`sanitize`, and `normalize`"
        ),
        settings,
        _now(),
    )
    assert note is not None
    assert prior.id in note
    assert _TARGET_PATH in note


def test_inflight_overlap_unflagged_when_distinct(settings):
    """A draft with no path/title overlap against any recent ticket is
    not flagged."""
    svc, prior = _seed(
        settings,
        title="rework the login form",
        body=f"changes {_TARGET_PATH} to validate input",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "add a totally unrelated metrics dashboard",
        "touches src/robotsix_mill/runtime/metrics.py only",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_excludes_self(settings):
    """A draft must not self-match: passing its own id as *ticket_id*
    excludes it from the candidate pool."""
    svc, draft = _seed(
        settings,
        title="rework the login form",
        body=f"changes {_TARGET_PATH} to validate input",
    )
    note = find_inflight_overlap(
        _svc(settings),
        draft.id,
        draft.title,
        f"changes {_TARGET_PATH} to validate input",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_prose_only_multi_segment_path(settings):
    """Occurrence 1 (multi-segment prose path): a draft and a recent
    candidate both merely MENTION ``runtime/worker.py`` in prose — neither
    declares it as modified — with non-overlapping titles must NOT flag."""
    svc, prior = _seed(
        settings,
        title="ruff config: pin the import-sort rule",
        body="The runner lives in runtime/worker.py, but this only edits ruff config.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "system-design doc for the scheduler",
        "Background: runtime/worker.py drives the loop; this adds ARCHITECTURE.md.",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_prose_only_bare_filename(settings):
    """Occurrence 2 (bare filename prose path): a draft listing ``config.py``
    under an Out-of-scope heading (NOT modified) and a recent candidate that
    only mentions ``config.py`` in prose, with non-overlapping titles, must
    NOT flag."""
    svc, prior = _seed(
        settings,
        title="board cleanup memory path override",
        body="Overrides the runner memory path; names config.py in passing only.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "gitlab forge adapter stub",
        "## Out of scope / constraints\n\nThis draft does NOT modify config.py.",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_two_shared_prose_paths(settings):
    """Corroboration rule relaxed: ≥2 distinct shared paths no longer
    flag unconditionally — the concern-token gate now applies.  Two
    tickets that merely mention the same two files in prose, with no
    backtick-enclosed concern tokens on either side, must NOT flag."""
    svc, prior = _seed(
        settings,
        title="rework the login form",
        body=f"prose mentioning {_TARGET_PATH} and runtime/worker.py",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "an entirely unrelated summary line",
        f"also names {_TARGET_PATH} and runtime/worker.py in prose",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_same_path_disjoint_concerns(settings):
    """Mode 2: two tickets that both declare the same file under
    ``## Scope`` but name completely different code symbols
    (`` `new_model()` `` vs `` `.secrets.baseline` ``) must NOT flag —
    the file-path overlap is a false positive when concerns differ."""
    svc, prior = _seed(
        settings,
        title="add tests for `OpenRouterProvider.new_model()`",
        body=f"## Scope\n\nAdd test cases in {_TARGET_PATH} for `new_model()`.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix `.secrets.baseline` auth",
        f"## Scope\n\nFix `.secrets.baseline` handling in {_TARGET_PATH}.",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_flags_on_same_path_shared_concern(settings):
    """Mode 2: two tickets that declare the same file AND share at least
    3 concern tokens must still flag."""
    svc, prior = _seed(
        settings,
        title="add tests for `new_model()`, `validate()`, and `sanitize()`",
        body=(
            f"## Scope\n\nAdd test cases in {_TARGET_PATH} for "
            "`new_model()`, `validate()`, and `sanitize()`."
        ),
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "refactor `new_model()`, `validate()`, and `sanitize()` to use async",
        (
            f"## Scope\n\nRefactor `new_model()`, `validate()`, "
            f"and `sanitize()` in {_TARGET_PATH}."
        ),
        settings,
        _now(),
    )
    assert note is not None
    assert prior.id in note
    assert "new_model()" in note or "`new_model()`" in note


def test_inflight_overlap_unflagged_on_punctuation_concern_token(settings):
    """Regression: a lone shared punctuation backtick token (`` ``, ``)
    does NOT trigger a dedup warning — it's filtered as non-substantive
    (the 20260609T154505Z false-positive class)."""
    svc, prior = _seed(
        settings,
        title="rename the `,` helper to `separator`",
        body=f"## Scope\n\nEdit {_TARGET_PATH}: rename `,` to `separator`.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "use `,` as a delimiter",
        f"## Scope\n\nCall `,` in {_TARGET_PATH} for delimited output.",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_two_paths_disjoint_concerns(settings):
    """Regression: two tickets that share two file paths but have
    completely disjoint concern tokens (different symbols within those
    files) do NOT flag — the 20260609T150007Z false-positive class
    (``board.js`` / ``board.css`` as routine frontend paths)."""
    svc, prior = _seed(
        settings,
        title="enable `copy_paste` periodic workflow",
        body="## Scope\n\nAdd `copy_paste` to board.js and board.css.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix CSS rules for `dark_mode`",
        body="## Scope\n\nTweak board.js and board.css for `dark_mode`.",
        settings=settings,
        now=_now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_same_path_no_concern_tokens_on_one_side(
    settings,
):
    """Mode 2 tightened: when one side has concern tokens but the other
    does not, the path match is suppressed — absence of symbols on one
    side means the overlap is 0 < concern_min_overlap (3)."""
    svc, prior = _seed(
        settings,
        title="rework the login form",
        body=f"## Scope\n\nchanges {_TARGET_PATH} to validate input",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix `login_form` validation",
        f"## Scope\n\nalso edits {_TARGET_PATH} for `login_form` validation",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_same_path_no_concern_tokens_either_side(
    settings,
):
    """Mode 2 tightened: when neither side has concern tokens, the path
    match is suppressed — 0 < concern_min_overlap (3), so the overlap
    is insufficient."""
    svc, prior = _seed(
        settings,
        title="rework the login form",
        body=f"## Scope\n\nchanges {_TARGET_PATH} to validate input",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix the login form validation",
        f"## Scope\n\nalso edits {_TARGET_PATH} for validation",
        settings,
        _now(),
    )
    assert note is None


# ---------------------------------------------------------------------------
# Single-segment path guard (bare filenames need corroborating concern overlap)
# ---------------------------------------------------------------------------


def test_inflight_overlap_unflagged_on_single_segment_disjoint_concerns(settings):
    """Single-segment path `server.py`, disjoint concern tokens
    (`new_model` vs `.secrets.baseline`) → no match (regression test
    for the existing disjoint-concern suppression)."""
    svc, prior = _seed(
        settings,
        title="add `new_model` endpoint",
        body="## Scope\n\nAdd `new_model` handling in server.py.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix `.secrets.baseline` auth",
        "## Scope\n\nFix `.secrets.baseline` in server.py.",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_single_segment_shared_concern(settings):
    """Single-segment path `server.py`, single shared concern token
    `new_model` → no match (1 < concern_min_overlap=3 — single-segment
    bare filenames need multiple corroborating concerns to flag)."""
    svc, prior = _seed(
        settings,
        title="add `new_model` endpoint",
        body="## Scope\n\nAdd `new_model` handling in server.py.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "refactor `new_model` to use async",
        "## Scope\n\nRefactor `new_model` in server.py.",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_single_segment_draft_concern_only(settings):
    """Single-segment path `server.py`, draft names `login_form` but
    candidate has no concern tokens → no match (bare filenames need
    corroboration on both sides)."""
    svc, prior = _seed(
        settings,
        title="rework the login form",
        body="## Scope\n\nchanges server.py to validate input",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix `login_form` validation",
        "## Scope\n\nedits server.py for `login_form` validation",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_single_segment_no_concern_tokens(settings):
    """Single-segment path `server.py`, no concern tokens on either side
    → no match (bare filenames need corroboration)."""
    svc, prior = _seed(
        settings,
        title="rework the login form",
        body="## Scope\n\nchanges server.py to validate input",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix the login form validation",
        "## Scope\n\nedits server.py for validation",
        settings,
        _now(),
    )
    assert note is None


def test_inflight_overlap_unflagged_on_title_only_no_path_overlap(settings):
    """Regression test for the langfuse-vs-6116 incident: a draft whose
    normalized title fingerprint overlaps a recent ticket's title but
    that shares no file path and no concern symbol MUST NOT be flagged
    by the draft-intake pre-refine dedup advisory.

    The candidate (like ticket 6116) mentions ``runtime/worker.py`` while
    the draft names ``src/robotsix_mill/langfuse/client.py`` — no shared
    paths, no shared concern tokens.  The title fingerprint alone
    (``langfuse_inspect_trace async bug`` vs ``...liveness probe...``)
    must not trigger the advisory."""
    svc, prior = _seed(
        settings,
        title="add Kubernetes liveness/readiness probe endpoints",
        body="## Scope\n\nAdd liveness/readiness probes in runtime/worker.py.",
    )
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "langfuse_inspect_trace async bug",
        "## Scope\n\nFix async handling in src/robotsix_mill/langfuse/client.py.",
        settings,
        _now(),
    )
    assert note is None


def test_scope_paths_extracts_only_declared_sections():
    """``_scope_paths`` returns paths under ``## Scope`` / ``## Acceptance``
    sections and ``file_map`` blocks, and excludes prose-only and
    Out-of-scope paths."""
    body = (
        "## Problem\n\nWe touch runtime/worker.py in passing.\n\n"
        "## Scope\n\nEdit src/robotsix_mill/config.py here.\n\n"
        "## Acceptance criteria\n\n- tests/test_x.py passes\n\n"
        "## Out of scope / constraints\n\nDo NOT touch docs/foo.md.\n"
    )
    paths = _scope_paths(body)
    assert "src/robotsix_mill/config.py" in paths
    assert "tests/test_x.py" in paths
    assert "runtime/worker.py" not in paths  # prose / Problem section
    assert "docs/foo.md" not in paths  # Out-of-scope section


def test_scope_paths_includes_fenced_file_map_block():
    """A fenced ```file_map``` block counts as a declared-modification
    section; prose paths outside it do not."""
    body = "## Problem\n\nprose names a.py here\n\n```file_map\nsrc/b.py\n```\n"
    paths = _scope_paths(body)
    assert "src/b.py" in paths
    assert "a.py" not in paths


def test_reprocess_flags_but_still_creates_children(settings, monkeypatch):
    """Call-site integration: the worker re-process path flags an
    in-batch duplicate child but still creates BOTH children, with the
    advisory note present on the flagged one (never silently dropped)."""
    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult
    from robotsix_mill.runtime.worker import _run_epic_reprocess

    svc = _svc(settings)
    epic = svc.create(title="parent epic", description="", kind=TicketKind.EPIC)

    fake = EpicBreakdownResult(
        child_titles=["First Trivy child", "Second Trivy child"],
        child_bodies=[
            "Work documented in CONTRIBUTING.md for the first child",
            "Work documented in CONTRIBUTING.md for the second child",
        ],
        epic_body=None,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        lambda **kwargs: fake,
    )

    _run_epic_reprocess(epic.id, "please regenerate", settings)

    children = svc.list_children(epic.id)
    assert len(children) == 2, "both children must be created, none dropped"
    bodies = [svc.workspace(c).read_description() for c in children]
    # Exactly one child (the later sibling) carries the advisory note.
    flagged = [b for b in bodies if "[!warning]" in b]
    assert len(flagged) == 1
    assert "CONTRIBUTING.md" in flagged[0]


# ---------------------------------------------------------------------------
# A. find_prior_matching_ticket — uncovered branches
# ---------------------------------------------------------------------------


def test_find_prior_returns_none_and_logs_on_exception(settings, monkeypatch, caplog):
    """Candidate retrieval blowing up is best-effort: the matcher
    returns None and logs the failure rather than raising into the
    caller (lines 113-115)."""
    svc = _svc(settings)

    def _boom(*a, **k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(svc, "recent_tickets", _boom)

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.core.dedup"):
        result = find_prior_matching_ticket(
            svc, _BOARD, [_TARGET_PATH], "any symptom", settings, _now()
        )

    assert result is None
    assert any(
        "find_prior_matching_ticket failed" in r.getMessage() for r in caplog.records
    )


def test_find_prior_skips_candidate_with_null_created_at(settings):
    """A candidate whose ``created_at`` is None is skipped — it can't be
    placed in the recency window (lines 76-77)."""
    svc, ticket = _seed(
        settings,
        title="some unrelated title",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    svc.transition(ticket.id, State.DONE, note="merged")
    _backdate(settings, ticket.id, None)

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "a symptom that does not appear in any title",
        settings,
        _now(),
    )
    assert match is None


def test_find_prior_normalizes_naive_created_at(settings, monkeypatch):
    """A tz-naive ``created_at`` within the lookback window is normalized
    to UTC-aware before the comparison, so the candidate still matches
    (line 80).

    The DB's ``TZDateTime`` column rejects naive writes, so the naive
    value is injected on an in-memory candidate returned by a stubbed
    ``recent_tickets`` — matched by title fingerprint (no body read)."""
    svc, ticket = _seed(
        settings,
        title="trace review tool error wrapper bug",
        body="prior body that names no code locus",
    )
    live = svc.get(ticket.id)
    live.created_at = datetime.now() - timedelta(days=1)  # noqa: DTZ005 — naive
    assert live.created_at.tzinfo is None
    monkeypatch.setattr(svc, "recent_tickets", lambda **k: [live])

    match = find_prior_matching_ticket(
        svc,
        _BOARD,
        [],
        "wrapper bug",
        settings,
        _now(),
    )
    assert match is not None
    assert match.id == ticket.id


# ---------------------------------------------------------------------------
# B. _extract_paths
# ---------------------------------------------------------------------------


def test_extract_paths_multi_segment_token():
    assert _extract_paths("see tests/foo/test_bar.py for the repro") == [
        "tests/foo/test_bar.py"
    ]


def test_extract_paths_bare_filename_with_extension():
    assert _extract_paths("edit ci.yml and CONTRIBUTING.md please") == [
        "ci.yml",
        "CONTRIBUTING.md",
    ]


def test_extract_paths_dedups_first_seen_order():
    text = "touch src/a/b.py then src/a/b.py again and finally docs/c.md"
    assert _extract_paths(text) == ["src/a/b.py", "docs/c.md"]


def test_extract_paths_ignores_tokens_without_extension():
    assert _extract_paths("run npx jscpd against the duplicate code") == []


def test_extract_paths_empty_input_returns_empty():
    assert _extract_paths("") == []
    assert _extract_paths(None) == []


def test_extract_paths_rejects_dotted_prose_fragments():
    # Single-segment dotted prose like ``e.g`` / ``i.e`` must not be
    # mistaken for file paths — these produced spurious duplicate
    # advisories linking unrelated tickets.
    assert _extract_paths("caught at refine (e.g. the i.e. case)") == []


def test_extract_paths_keeps_real_paths_alongside_prose():
    text = "fix runtime/worker.py and ci.yml, see CONTRIBUTING.md (e.g. this)"
    assert _extract_paths(text) == [
        "runtime/worker.py",
        "ci.yml",
        "CONTRIBUTING.md",
    ]


# ---------------------------------------------------------------------------
# _extract_concern_tokens
# ---------------------------------------------------------------------------


def test_extract_concern_tokens_backtick_enclosed():
    assert _extract_concern_tokens(
        "add tests for `OpenRouterProvider.new_model()`"
    ) == {"OpenRouterProvider.new_model()"}


def test_extract_concern_tokens_multiple():
    tokens = _extract_concern_tokens(
        "refactor `login()` to call `validate()` and `sanitize()`"
    )
    assert tokens == {"login()", "validate()", "sanitize()"}


def test_extract_concern_tokens_no_backticks():
    assert _extract_concern_tokens("rework the login form") == set()


def test_extract_concern_tokens_empty():
    assert _extract_concern_tokens("") == set()
    assert _extract_concern_tokens(None) == set()


def test_extract_concern_tokens_dotted():
    assert _extract_concern_tokens("fix `.secrets.baseline` auth") == {
        ".secrets.baseline"
    }


def test_extract_concern_tokens_filters_punctuation_only():
    """Punctuation-only backtick tokens — e.g. `` ``, ``, `` `-` ``,
    `` `...` `` — are excluded because they carry no semantic signal."""
    assert _extract_concern_tokens("use `,` as a separator and `-` for flags") == set()


def test_extract_concern_tokens_filters_path_like():
    """Backtick-enclosed file paths — e.g. `` `board.js` ``,
    `` `src/foo.py` `` — are excluded: they restate the file, not the
    concern within it."""
    assert _extract_concern_tokens(
        "edit `board.js` and `board.css` to add `copy_paste` support"
    ) == {"copy_paste"}


def test_extract_concern_tokens_keeps_code_symbols_alongside_paths():
    """Code symbols survive filtering even when file-path-like tokens
    appear in the same text."""
    tokens = _extract_concern_tokens(
        "refactor `src/robotsix_board/__init__.py` to export `render_board`"
    )
    assert tokens == {"render_board"}


# ---------------------------------------------------------------------------
# C. find_child_overlaps / _describe_recent_signal — uncovered branches
# ---------------------------------------------------------------------------


def test_child_overlaps_empty_lists_returns_empty(settings):
    """No proposed children → no notes (the missing/empty child-lists
    case)."""
    assert find_child_overlaps(_svc(settings), "EPIC-1", [], [], settings, _now()) == []


def test_child_overlaps_flags_in_batch_sibling_by_title(settings):
    """Two siblings with no shared extracted path but overlapping
    normalized titles flag the later one by title (lines 251-255)."""
    notes = find_child_overlaps(
        _svc(settings),
        "EPIC-1",
        ["Add retry logic", "Add retry logic to the queue consumer"],
        ["first child body with no path token", "second child body with no path token"],
        settings,
        _now(),
    )
    assert notes[0] is None
    assert notes[1] is not None
    assert "overlapping title" in notes[1]
    assert "sibling #0" in notes[1]


def test_child_overlaps_list_children_failure_non_fatal(settings, monkeypatch, caplog):
    """A failure enumerating existing children is logged but does not
    crash the overlap check (lines 205-206)."""
    svc = _svc(settings)

    def _boom(*a, **k):
        raise RuntimeError("children query failed")

    monkeypatch.setattr(svc, "list_children", _boom)

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.core.dedup"):
        notes = find_child_overlaps(
            svc,
            "EPIC-1",
            ["child one", "child two"],
            ["body one", "body two"],
            settings,
            _now(),
        )

    assert len(notes) == 2
    assert any("list_children failed" in r.getMessage() for r in caplog.records)


def test_child_overlaps_outer_exception_returns_all_none(settings, monkeypatch, caplog):
    """An unexpected failure inside the per-child loop yields an all-None
    result of the correct length so children are still filed (lines
    260-262)."""
    import robotsix_mill.core.dedup as dedup_mod

    def _boom(*a, **k):
        raise RuntimeError("matcher exploded")

    monkeypatch.setattr(dedup_mod, "find_prior_matching_ticket", _boom)

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.core.dedup"):
        notes = find_child_overlaps(
            _svc(settings),
            "EPIC-1",
            ["a", "b", "c"],
            ["x", "y", "z"],
            settings,
            _now(),
        )

    assert notes == [None, None, None]
    assert any("find_child_overlaps failed" in r.getMessage() for r in caplog.records)


def test_child_overlaps_recent_match_described_as_title_overlap(settings):
    """A recent ticket matched via title (no shared path) is described as
    a 'title overlap' by ``_describe_recent_signal`` (the paths-empty
    fallback)."""
    svc, prior = _seed(
        settings,
        title="trace review tool error wrapper bug fix",
        body="prior body that names no code locus at all",
    )
    svc.transition(prior.id, State.DONE, note="merged")

    notes = find_child_overlaps(
        _svc(settings),
        "EPIC-1",
        ["trace review tool error wrapper bug fix"],
        ["child body without any file path token"],
        settings,
        _now(),
    )
    assert notes[0] is not None
    assert "title overlap" in notes[0]
    assert prior.id in notes[0]


def test_describe_recent_signal_except_falls_back_to_title_overlap(
    settings, monkeypatch, caplog
):
    """When the description lookup inside ``_describe_recent_signal``
    raises, the note still falls back to 'title overlap' and the failure
    is logged (lines 153-155)."""
    svc, prior = _seed(
        settings,
        title="trivy sarif upload audit decision",
        # The candidate declares the shared path under ``## Scope`` so the
        # strict-scope single-path branch fires; the path-signal lookup in
        # ``_describe_recent_signal`` is what we force to raise below.
        body="## Scope\n\nprior body modifying config/foo.yml here",
    )
    svc.transition(prior.id, State.DONE, note="merged")

    orig_read = Workspace.read_description
    calls = {"n": 0}

    def _flaky_read(self):
        calls["n"] += 1
        # The first read happens inside find_prior_matching_ticket's
        # path check; the second is _describe_recent_signal's lookup,
        # which we force to raise.
        if calls["n"] >= 2:
            raise RuntimeError("workspace read failed")
        return orig_read(self)

    monkeypatch.setattr(Workspace, "read_description", _flaky_read)

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.core.dedup"):
        notes = find_child_overlaps(
            _svc(settings),
            "EPIC-1",
            ["trivy sarif upload audit decision"],
            ["this child names config/foo.yml as its only path"],
            settings,
            _now(),
        )

    assert notes[0] is not None
    assert "title overlap" in notes[0]
    assert any(
        "_describe_recent_signal failed" in r.getMessage() for r in caplog.records
    )


def test_describe_recent_signal_direct_title_fallback(settings):
    """Direct unit cover: with empty paths, the signal is 'title
    overlap' regardless of body content."""
    svc, prior = _seed(settings, title="some prior", body="some body")
    assert _describe_recent_signal(prior, [], settings, _BOARD) == "title overlap"


# ---------------------------------------------------------------------------
# _ci_draft_fingerprint
# ---------------------------------------------------------------------------

_DRAFT_BODY_SAMPLE = """\
**Workflow:** CI
**Branch:** main
**Run:** [1234567890](https://github.com/owner/repo/actions/runs/1234567890)
**Commit:** `abc123def456`
**Created:** 2024-01-15T10:30:45.123456+00:00

Error: process completed with exit code 1.
error: could not find `--no-emit-project` flag
some/path/to/file.rs:42: unexpected token
  |
42 |   let x = ;
  |           ^ expected expression
  |
"""

_DRAFT_BODY_DIFFERENT_ERROR = """\
**Workflow:** CI
**Branch:** main
**Run:** [9999999999](https://github.com/owner/repo/actions/runs/9999999999)
**Commit:** `deadbeef9999`
**Created:** 2024-06-14T22:11:33Z

Error: process completed with exit code 1.
error: missing `contents:read` permission
Please add the following to your workflow:
permissions:
  contents: read
"""

_DRAFT_BODY_SAME_ERROR_DIFFERENT_RUN = """\
**Workflow:** CI
**Branch:** main
**Run:** [8888888888](https://github.com/owner/repo/actions/runs/8888888888)
**Commit:** `feedfeed8888`
**Created:** 2024-06-15T08:00:00.000000+00:00

Error: process completed with exit code 1.
error: could not find `--no-emit-project` flag
some/path/to/file.rs:42: unexpected token
  |
42 |   let x = ;
  |           ^ expected expression
  |
"""


def test_ci_draft_fingerprint_is_stable():
    """Same error content → same fingerprint across multiple calls."""
    fp1 = _ci_draft_fingerprint(_DRAFT_BODY_SAMPLE)
    fp2 = _ci_draft_fingerprint(_DRAFT_BODY_SAMPLE)
    assert fp1 == fp2
    assert len(fp1) == 16
    assert all(c in "0123456789abcdef" for c in fp1)


def test_ci_draft_fingerprint_distinguishes_different_errors():
    """Different root causes produce different fingerprints."""
    fp1 = _ci_draft_fingerprint(_DRAFT_BODY_SAMPLE)
    fp2 = _ci_draft_fingerprint(_DRAFT_BODY_DIFFERENT_ERROR)
    assert fp1 != fp2


def test_ci_draft_fingerprint_immune_to_run_specific_noise():
    """Same error, different run/commit/timestamp → same fingerprint."""
    fp1 = _ci_draft_fingerprint(_DRAFT_BODY_SAMPLE)
    fp2 = _ci_draft_fingerprint(_DRAFT_BODY_SAME_ERROR_DIFFERENT_RUN)
    assert fp1 == fp2


def test_ci_draft_fingerprint_strips_ansi_escapes():
    """ANSI escape sequences in the log body are stripped before hashing."""
    body_with_ansi = _DRAFT_BODY_SAMPLE.replace(
        "error: could not find",
        "\x1b[31merror\x1b[0m: could not find",
    )
    fp1 = _ci_draft_fingerprint(_DRAFT_BODY_SAMPLE)
    fp2 = _ci_draft_fingerprint(body_with_ansi)
    assert fp1 == fp2


def test_ci_draft_fingerprint_handles_empty_body():
    """An empty body produces a fingerprint (hash of empty string)."""
    fp = _ci_draft_fingerprint("")
    assert len(fp) == 16
    # Repeated calls produce the same fingerprint.
    assert _ci_draft_fingerprint("") == fp


def test_ci_draft_fingerprint_handles_metadata_only_body():
    """A body with only metadata lines (no log content) produces a stable
    fingerprint (hash of empty text after stripping all lines)."""
    meta_only = (
        "**Workflow:** CI\n"
        "**Branch:** main\n"
        "**Run:** [1](https://github.com/o/r/actions/runs/1)\n"
        "**Commit:** `abc`\n"
        "**Created:** 2024-01-01T00:00:00Z\n"
    )
    fp = _ci_draft_fingerprint(meta_only)
    assert len(fp) == 16
    assert _ci_draft_fingerprint(meta_only) == fp


# ---------------------------------------------------------------------------
# label-based dedup in find_prior_matching_ticket
# ---------------------------------------------------------------------------


def test_label_dedup_match_returns_candidate(settings):
    """When a candidate carries one of the dedup_labels, it is returned
    immediately — even when file paths and titles don't match."""
    svc, ticket = _seed(
        settings,
        title="completely unrelated title",
        body="body that does NOT name any target file",
    )
    svc.transition(ticket.id, State.READY, note="refined")
    # Store a ci_fp label on the candidate.
    svc.set_labels(ticket.id, ["ci_fp:abc123def4567890"])

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        ["non/existent/path.py"],
        "a symptom that does not appear in any title",
        settings,
        _now(),
        dedup_labels=["ci_fp:abc123def4567890"],
    )
    assert match is not None
    assert match.id == ticket.id


def test_label_dedup_ci_fp_mismatch_suppresses_title_fallback(settings):
    """When dedup_labels contains a ci_fp:* label that the candidate does
    NOT have, the candidate is skipped — even when titles match."""
    svc, ticket = _seed(
        settings,
        title="CI failure: CI on main",
        body="body that names no file paths",
    )
    svc.transition(ticket.id, State.READY, note="refined")
    # Candidate has a DIFFERENT ci_fp label.
    svc.set_labels(ticket.id, ["ci_fp:1111222233334444"])

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [],
        "CI failure: CI on main",
        settings,
        _now(),
        dedup_labels=["ci_fp:aaaabbbbccccdddd"],
    )
    # The candidate has a ci_fp:* label but it DOESN'T match —
    # title fallback is suppressed, so no match.
    assert match is None


def test_label_dedup_non_ci_fp_label_no_match_falls_through(settings):
    """When dedup_labels contains a non-ci_fp label that doesn't match,
    the candidate still falls through to file-path/title checks (the
    suppression only applies to ci_fp:* labels)."""
    svc, ticket = _seed(
        settings,
        title="CI failure: CI on main",
        body=f"prior body mentioning {_TARGET_PATH}",
    )
    svc.transition(ticket.id, State.READY, note="refined")

    # dedup_labels has a non-ci_fp label — no match, so fall through.
    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [_TARGET_PATH],
        "unrelated fingerprint",
        settings,
        _now(),
        dedup_labels=["some:other:label"],
    )
    # Falls through to file-path check, which matches.
    assert match is not None
    assert match.id == ticket.id


def test_label_dedup_none_preserves_existing_behavior(settings):
    """When dedup_labels is None (the default), existing behavior is
    completely unchanged."""
    svc, ticket = _seed(
        settings,
        title="CI failure: CI on main",
        body="body that names no file paths",
    )
    svc.transition(ticket.id, State.READY, note="refined")

    # Without dedup_labels, the title fingerprint match still works.
    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [],
        "CI failure: CI on main",
        settings,
        _now(),
    )
    assert match is not None
    assert match.id == ticket.id


def test_label_dedup_empty_list_falls_through(settings):
    """An empty dedup_labels list is falsy and falls through to the
    existing checks."""
    svc, ticket = _seed(
        settings,
        title="CI failure: CI on main",
        body="some body",
    )
    svc.transition(ticket.id, State.READY, note="refined")

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [],
        "CI failure: CI on main",
        settings,
        _now(),
        dedup_labels=[],
    )
    assert match is not None  # title match still works


def test_label_dedup_ci_fp_mismatch_without_candidate_labels(settings):
    """When dedup_labels has a ci_fp:* label but the candidate has NO
    labels at all, the candidate is skipped — title fallback suppressed."""
    svc, ticket = _seed(
        settings,
        title="CI failure: CI on main",
        body="body with no labels",
    )
    svc.transition(ticket.id, State.READY, note="refined")
    # Candidate has no labels at all.

    match = find_prior_matching_ticket(
        _svc(settings),
        _BOARD,
        [],
        "CI failure: CI on main",
        settings,
        _now(),
        dedup_labels=["ci_fp:aaaabbbbccccdddd"],
    )
    # No labels on candidate → can't match ci_fp:* → suppression kicks in.
    assert match is None


# ---------------------------------------------------------------------------
# paths_excluding_out_of_scope
# ---------------------------------------------------------------------------


def test_paths_excluding_out_of_scope_heading_form_exclusion():
    """Paths under a ``## Out of scope`` heading are excluded."""
    body = (
        "## Scope\n\n"
        "- src/robotsix_mill/core/db.py\n\n"
        "## Out of scope\n\n"
        "src/robotsix_mill/core/db.py\n\n"
        "## Another heading\n\n"
        "more text mentioning ci.yml\n"
    )
    result = paths_excluding_out_of_scope(body)
    # The db.py path appears in BOTH Scope and Out of scope sections,
    # so it's still returned (in-scope occurrence wins).
    assert "src/robotsix_mill/core/db.py" in result
    # ci.yml from after the out-of-scope section is captured.
    assert "ci.yml" in result
    # First-seen order: Scope's db.py then ci.yml.
    assert result == ["src/robotsix_mill/core/db.py", "ci.yml"]


def test_paths_excluding_out_of_scope_inline_bold_form_exclusion():
    """Paths under an inline ``**Explicitly out of scope …**`` marker are excluded."""
    body = (
        "## Scope\n\n"
        "- src/robotsix_llmio/core/sqlite_utils.py\n\n"
        "**Explicitly out of scope — consumer migrations:** src/robotsix_mill/core/db.py\n\n"
        "## Another heading\n\n"
        "more text\n"
    )
    result = paths_excluding_out_of_scope(body)
    assert "src/robotsix_llmio/core/sqlite_utils.py" in result
    assert "src/robotsix_mill/core/db.py" not in result
    assert result == ["src/robotsix_llmio/core/sqlite_utils.py"]


def test_paths_excluding_out_of_scope_path_in_both_sections_returned():
    """A path in BOTH an in-scope section and an out-of-scope section is
    still returned because of the in-scope occurrence."""
    body = (
        "## Scope\n\n"
        "Edit src/foo.py to add the new helper.\n\n"
        "## Out of scope\n\n"
        "Do NOT touch src/foo.py in this ticket.\n"
    )
    result = paths_excluding_out_of_scope(body)
    assert "src/foo.py" in result


def test_paths_excluding_out_of_scope_no_marker_behaves_like_extract_paths():
    """When there are no out-of-scope markers, the function behaves like
    ``_extract_paths``."""
    from robotsix_mill.core.dedup import _extract_paths

    body = "Fix runtime/worker.py and ci.yml, see CONTRIBUTING.md (e.g. this)"
    result = paths_excluding_out_of_scope(body)
    expected = _extract_paths(body)
    assert result == expected


def test_paths_excluding_out_of_scope_inline_until_blank_line():
    """Inline exclusion stops at the next blank line, then paths are captured again."""
    body = (
        "## Scope\n\n"
        "src/robotsix_llmio/core/foo.py\n\n"
        "**Explicitly out of scope:**\n"
        "- src/robotsix_mill/core/db.py\n"
        "- src/robotsix_mill/core/other.py\n"
        "\n"
        "Real content mentioning ci.yml\n"
    )
    result = paths_excluding_out_of_scope(body)
    assert "src/robotsix_llmio/core/foo.py" in result
    assert "src/robotsix_mill/core/db.py" not in result
    assert "src/robotsix_mill/core/other.py" not in result
    assert "ci.yml" in result


def test_paths_excluding_out_of_scope_heading_exclusion_until_next_heading():
    """Heading-based exclusion runs until the next heading."""
    body = (
        "## Scope\n\n"
        "src/keep.py\n\n"
        "### Explicitly out of scope\n\n"
        "src/discard.py\n\n"
        "src/also_discard.py\n\n"
        "## Acceptance criteria\n\n"
        "src/keep2.py\n"
    )
    result = paths_excluding_out_of_scope(body)
    assert "src/keep.py" in result
    assert "src/discard.py" not in result
    assert "src/also_discard.py" not in result
    assert "src/keep2.py" in result


def test_paths_excluding_out_of_scope_bullet_out_of_scope_marker():
    """``- Out of scope: …`` bullet-style inline marker is recognised."""
    body = (
        "## Scope\n\n"
        "src/robotsix_llmio/core/foo.py\n\n"
        "- Out of scope: src/robotsix_mill/core/db.py\n\n"
        "More real content\n"
    )
    result = paths_excluding_out_of_scope(body)
    assert "src/robotsix_llmio/core/foo.py" in result
    assert "src/robotsix_mill/core/db.py" not in result


def test_paths_excluding_out_of_scope_empty_text_returns_empty():
    """Empty or None text returns an empty list."""
    assert paths_excluding_out_of_scope("") == []
    assert paths_excluding_out_of_scope(None) == []
