"""Tests for hash-chain integrity verification via verify_runner."""

from __future__ import annotations


from robotsix_mill.core import db
from robotsix_mill.core.models import TicketEvent
from robotsix_mill.core.states import State
from robotsix_mill.core.service import _event_hash
from robotsix_mill.runners.verify_runner import (
    VerifyResult,
    run_verify_pass,
)


# ---------------------------------------------------------------------------
# _event_hash — pure function (imported from core.service)
# ---------------------------------------------------------------------------


def test_event_hash_deterministic():
    """Same inputs produce the same hash each time."""
    h1 = _event_hash("t1", "draft", "created", "2025-01-01T00:00:00+00:00", None)
    h2 = _event_hash("t1", "draft", "created", "2025-01-01T00:00:00+00:00", None)
    assert h1 == h2


def test_event_hash_hex_format():
    """Hash is 64 lowercase hex characters."""
    h = _event_hash("t1", "draft", None, "2025-01-01T00:00:00+00:00", None)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# run_verify_pass — integration with real DB
# ---------------------------------------------------------------------------


def test_verify_no_events(monkeypatch, tmp_path):
    """Empty DB → zero events, zero tickets, zero breaks."""
    from robotsix_mill.config import Settings

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")

    # Monkeypatch Settings() inside the runner to return our settings.
    monkeypatch.setattr(
        "robotsix_mill.runners.verify_runner.Settings",
        lambda: s,
    )

    result = run_verify_pass("session-empty")
    assert result.total_events == 0
    assert result.tickets_verified == 0
    assert result.breaks == []
    db.reset_engine()


def test_verify_clean_chain(monkeypatch, tmp_path):
    """A freshly created chain (via service) verifies clean."""
    from robotsix_mill.config import Settings
    from robotsix_mill.core.service import TicketService

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")
    # Use default board "" so the verify runner finds the events.
    svc = TicketService(s, board_id="test-board")

    t = svc.create("clean chain")
    svc.transition(t.id, State.READY, note="refined")
    svc.transition(t.id, State.DELIVERABLE, note="spec done")

    monkeypatch.setattr("robotsix_mill.runners.verify_runner.Settings", lambda: s)

    result = run_verify_pass("session-clean")
    assert result.total_events == 3
    assert result.tickets_verified == 1
    assert result.breaks == []
    db.reset_engine()


def test_verify_corrupted_hash(monkeypatch, tmp_path):
    """A tampered hash column is detected as a break."""
    from robotsix_mill.config import Settings
    from robotsix_mill.core.service import TicketService

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")
    svc = TicketService(s, board_id="test-board")

    t = svc.create("tampered")
    svc.transition(t.id, State.READY)

    # Corrupt the second event's hash.
    with db.session(s, "test-board") as sess:
        ev = sess.exec(
            __import__("sqlmodel")
            .select(TicketEvent)
            .where(TicketEvent.ticket_id == t.id)
            .order_by(TicketEvent.id.desc())
        ).first()
        ev.hash = "0" * 64
        sess.add(ev)
        sess.commit()

    monkeypatch.setattr("robotsix_mill.runners.verify_runner.Settings", lambda: s)

    result = run_verify_pass("session-tampered")
    assert result.total_events >= 2
    assert result.tickets_verified == 0
    assert len(result.breaks) >= 1
    assert result.breaks[0]["ticket_id"] == t.id
    assert result.breaks[0]["field"] == "hash"
    db.reset_engine()


def test_verify_corrupted_prev_hash(monkeypatch, tmp_path):
    """A tampered prev_hash is detected as a break.

    When prev_hash is corrupted, the recomputed hash (which includes
    prev_hash in its payload) also won't match — so the break is
    reported as a 'hash' mismatch.  Either field is fine; the point
    is that tampering IS detected.
    """
    from robotsix_mill.config import Settings
    from robotsix_mill.core.service import TicketService

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")
    svc = TicketService(s, board_id="test-board")

    t = svc.create("prev tampered")
    svc.transition(t.id, State.READY)

    # Corrupt the second event's prev_hash.
    with db.session(s, "test-board") as sess:
        ev = sess.exec(
            __import__("sqlmodel")
            .select(TicketEvent)
            .where(TicketEvent.ticket_id == t.id)
            .order_by(TicketEvent.id.desc())
        ).first()
        ev.prev_hash = "0" * 64
        sess.add(ev)
        sess.commit()

    monkeypatch.setattr("robotsix_mill.runners.verify_runner.Settings", lambda: s)

    result = run_verify_pass("session-prev-tampered")
    assert len(result.breaks) >= 1
    # Either 'hash' or 'prev_hash' — both indicate tampering was detected.
    assert result.breaks[0]["field"] in ("hash", "prev_hash")
    assert result.breaks[0]["ticket_id"] == t.id
    db.reset_engine()


def test_verify_skips_empty_hash_events(monkeypatch, tmp_path):
    """Events with empty hash (pre-migration) are skipped, not flagged."""
    from robotsix_mill.config import Settings

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")

    # Insert a bare event with empty hash (simulating pre-migration).
    with db.session(s, "test-board") as sess:
        sess.add(
            TicketEvent(
                ticket_id="legacy-ticket",
                state=State.DRAFT,
                note="old event",
                prev_hash=None,
                hash="",
            )
        )
        sess.commit()

    monkeypatch.setattr("robotsix_mill.runners.verify_runner.Settings", lambda: s)

    result = run_verify_pass("session-legacy")
    assert result.total_events == 1
    # Legacy events are skipped entirely; there's nothing to "verify",
    # so tickets_verified stays 0 for a chain that is only legacy events.
    assert result.breaks == []
    db.reset_engine()


def test_verify_single_ticket_filter(monkeypatch, tmp_path):
    """--ticket-id restricts verification to a single ticket's chain."""
    from robotsix_mill.config import Settings
    from robotsix_mill.core.service import TicketService

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")
    svc = TicketService(s, board_id="test-board")

    t1 = svc.create("ticket one")
    svc.transition(t1.id, State.READY)
    t2 = svc.create("ticket two")

    monkeypatch.setattr("robotsix_mill.runners.verify_runner.Settings", lambda: s)

    result_all = run_verify_pass("session-all")
    assert result_all.total_events == 3  # 2 from t1 + 1 from t2

    result_filtered = run_verify_pass("session-filtered", ticket_id=t2.id)
    assert result_filtered.total_events == 1
    assert result_filtered.tickets_verified == 1
    db.reset_engine()


def test_verify_result_dataclass_defaults():
    """VerifyResult fields have correct defaults."""
    r = VerifyResult()
    assert r.total_events == 0
    assert r.tickets_verified == 0
    assert r.breaks == []
