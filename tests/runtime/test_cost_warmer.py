"""Tests for the worker's background cost-warmer loop.

The warmer walks every non-archived ticket and calls
``langfuse_client.session_cost`` so the board's cache-only
``session_cost_cached`` reads come back populated. Tests exercise one
cycle of the loop (driven manually with ``asyncio`` so we don't have to
wait the configured interval) with the Langfuse seam monkeypatched —
no network, no real timing dependency.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import Worker
from robotsix_mill.stages import StageContext


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MILL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    db.reset_engine()
    from robotsix_mill.config import _reset_repos_config, _reset_secrets
    _reset_secrets()
    _reset_repos_config()
    return Settings(
        MILL_COST_WARMER_INTERVAL_SECONDS=30,
        MILL_COST_WARMER_PACE_MS=0,
    )


@pytest.fixture
def worker(settings):
    svc = TicketService(settings, board_id="")
    db.init_db(settings)
    ctx = StageContext(settings=settings, service=svc, repo_config=None)
    return Worker(ctx, run_registry=None)


async def _run_one_cycle(worker: Worker, monkeypatch):
    """Drive the loop just long enough for one cycle to complete.

    The loop's ``while True: ... await asyncio.sleep(interval - elapsed)``
    pauses for the remaining interval at the end of each cycle; we
    cancel the task during that final sleep so the cycle's work is
    observable but the loop doesn't run forever.
    """
    # Bypass the random initial-delay jitter so the test runs deterministically.
    monkeypatch.setattr(
        worker, "_initial_delay", lambda kind, interval: 0,
    )
    task = asyncio.create_task(worker._cost_warmer_loop())
    # Yield repeatedly to let the cycle progress; cancel as soon as
    # the loop pauses on its end-of-cycle sleep.
    for _ in range(200):
        await asyncio.sleep(0.005)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_cost_warmer_refreshes_every_non_archived_ticket(
    settings, worker, monkeypatch,
):
    """One cycle visits each non-archived ticket exactly once."""
    svc = TicketService(settings, board_id="")
    t1 = svc.create("a", "draft body")
    t2 = svc.create("b", "draft body")

    visited: list[str] = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda settings, ticket_id, repo_config=None: visited.append(ticket_id) or 0.42,
    )

    asyncio.run(_run_one_cycle(worker, monkeypatch))

    assert sorted(visited) == sorted([t1.id, t2.id])


def test_cost_warmer_skips_old_terminal_tickets(
    settings, worker, monkeypatch,
):
    """CLOSED / EPIC_CLOSED tickets older than 24h are not refreshed —
    their cost is final and warming them on every cycle is wasted."""
    svc = TicketService(settings, board_id="")
    fresh = svc.create("fresh", "body")
    old = svc.create("old-closed", "body")
    svc.transition(old.id, State.CLOSED, "done")

    # Backdate the closed ticket past the 24h skip window.
    from robotsix_mill.core.models import Ticket
    long_ago = datetime.now(timezone.utc) - timedelta(days=3)
    with db.session(settings) as s:
        row = s.get(Ticket, old.id)
        row.updated_at = long_ago
        s.add(row)
        s.commit()

    visited: list[str] = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda settings, ticket_id, repo_config=None: visited.append(ticket_id) or 0.0,
    )

    asyncio.run(_run_one_cycle(worker, monkeypatch))

    # Only the fresh ticket was visited.
    assert visited == [fresh.id]


def test_cost_warmer_survives_per_ticket_failure(
    settings, worker, monkeypatch,
):
    """A Langfuse error on one ticket must not stop the loop from
    refreshing the rest of the batch."""
    svc = TicketService(settings, board_id="")
    t1 = svc.create("a", "body")
    t2 = svc.create("b", "body")
    t3 = svc.create("c", "body")

    visited: list[str] = []

    def flaky(settings, ticket_id, repo_config=None):
        visited.append(ticket_id)
        if ticket_id == t2.id:
            raise RuntimeError("Langfuse 500")
        return 0.10

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost", flaky,
    )

    asyncio.run(_run_one_cycle(worker, monkeypatch))

    # All three tickets were attempted despite the middle one raising.
    assert sorted(visited) == sorted([t1.id, t2.id, t3.id])


def test_cost_warmer_survives_listing_failure(
    settings, worker, monkeypatch,
):
    """A listing failure on one repo must not stop the loop or other
    repos from being processed. (Single-repo test: the listing
    failure simply yields an empty cycle.)"""
    monkeypatch.setattr(
        TicketService, "list",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("DB down")),
    )
    seen: list = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda *a, **k: seen.append(1) or 0.0,
    )
    asyncio.run(_run_one_cycle(worker, monkeypatch))
    assert seen == []  # listing failed, no tickets to warm
