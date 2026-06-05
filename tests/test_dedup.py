"""Tests for the shared ticket-dedup primitives in ``robotsix_mill.dedup``.

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
from robotsix_mill.core.models import SourceKind, Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.dedup import (
    _describe_recent_signal,
    _extract_paths,
    annotate_child_body,
    find_child_overlaps,
    find_inflight_overlap,
    find_prior_matching_ticket,
    normalize,
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
        title=(
            "trace-review: tool_error — claude code returned an error result success"
        ),
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
        title="trace-review: tool_error — earlier wrapper bug",
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
        title="trace-review: tool_error — earlier wrapper bug",
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
        title="trace-review: tool_error — earlier wrapper bug",
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
        title="trace-review: tool_error — earlier wrapper bug",
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
        title="trace-review: tool_error — earlier wrapper bug",
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
        body="changes .github/workflows/ci.yml to drop the jscpd step",
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
    epic = svc.create(title="The epic", description="", kind="epic")
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
        body=f"changes {_TARGET_PATH} to validate input",
    )
    # In-flight, NOT terminal: the dedup guard would reject this.
    svc.transition(prior.id, State.READY, note="refined")

    note = find_inflight_overlap(
        _svc(settings),
        "NEW-DRAFT",
        "fix the login form validation",
        f"also edits {_TARGET_PATH} for validation",
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


def test_reprocess_flags_but_still_creates_children(settings, monkeypatch):
    """Call-site integration: the worker re-process path flags an
    in-batch duplicate child but still creates BOTH children, with the
    advisory note present on the flagged one (never silently dropped)."""
    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult
    from robotsix_mill.runtime.worker import _run_epic_reprocess

    svc = _svc(settings)
    epic = svc.create(title="parent epic", description="", kind="epic")

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

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.dedup"):
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

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.dedup"):
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
    import robotsix_mill.dedup as dedup_mod

    def _boom(*a, **k):
        raise RuntimeError("matcher exploded")

    monkeypatch.setattr(dedup_mod, "find_prior_matching_ticket", _boom)

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.dedup"):
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
        body="prior body with no matching path token here",
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

    with caplog.at_level(logging.ERROR, logger="robotsix_mill.dedup"):
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
