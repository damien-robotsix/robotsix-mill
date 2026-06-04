"""Tests for the shared ticket-dedup primitives in ``robotsix_mill.dedup``.

These exercise ``find_prior_matching_ticket`` directly (no runner, no
LLM): file-path body match, fingerprint title match, recency-window
exclusion, ERRORED/declined-CLOSED exclusion, ``exclude_ids`` exclusion,
and multi-source vs single-source candidate querying.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select as _select

from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core.db import session as db_session
from robotsix_mill.core.models import SourceKind, Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.dedup import (
    annotate_child_body,
    find_child_overlaps,
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
