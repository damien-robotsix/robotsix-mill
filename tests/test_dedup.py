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
from robotsix_mill.dedup import find_prior_matching_ticket, normalize

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
