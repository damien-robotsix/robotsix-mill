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
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    db.reset_engine()
    import robotsix_mill.config as _cfg
    from robotsix_mill.config import (
        RepoConfig,
        ReposRegistry,
        _reset_repos_config,
        _reset_secrets,
    )

    _reset_secrets()
    _reset_repos_config()
    s = Settings(
        data_dir=str(tmp_path),
        cost_warmer_interval_seconds=30,
    )
    # Single-repo deployments now register at least one repo; the
    # cost-warmer iterates ``get_repos_config().repos`` and has no
    # legacy board-less fallback.
    _cfg._repos_config = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test-project",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            )
        }
    )
    return s


@pytest.fixture
def worker(settings):
    svc = TicketService(settings, board_id="test-board")
    db.init_db(settings, board_id="test-board")
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
        worker,
        "_initial_delay",
        lambda kind, interval: 0,
    )
    # Enablement is now presence-based (.robotsix-mill/periodic/cost_warmer.yaml);
    # default the warming-logic tests to "enabled for every repo". Tests that
    # exercise the gating itself override this before calling.
    if not getattr(worker, "_presence_overridden", False):
        monkeypatch.setattr(worker, "_has_periodic_presence", lambda rc, name: True)
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
    settings,
    worker,
    monkeypatch,
):
    """One cycle visits each non-archived ticket exactly once."""
    svc = TicketService(settings, board_id="test-board")
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
    settings,
    worker,
    monkeypatch,
):
    """CLOSED / EPIC_CLOSED tickets older than 24h are not refreshed —
    their cost is final and warming them on every cycle is wasted."""
    svc = TicketService(settings, board_id="test-board")
    fresh = svc.create("fresh", "body")
    old = svc.create("old-closed", "body")
    svc.transition(old.id, State.CLOSED, "done")

    # Backdate the closed ticket past the 24h skip window.
    from robotsix_mill.core.models import Ticket

    long_ago = datetime.now(timezone.utc) - timedelta(days=3)
    with db.session(settings, "test-board") as s:
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
    settings,
    worker,
    monkeypatch,
):
    """A Langfuse error on one ticket must not stop the loop from
    refreshing the rest of the batch."""
    svc = TicketService(settings, board_id="test-board")
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
        "robotsix_mill.langfuse_client.session_cost",
        flaky,
    )

    asyncio.run(_run_one_cycle(worker, monkeypatch))

    # All three tickets were attempted despite the middle one raising.
    assert sorted(visited) == sorted([t1.id, t2.id, t3.id])


def test_cost_warmer_survives_listing_failure(
    settings,
    worker,
    monkeypatch,
):
    """A listing failure on one repo must not stop the loop or other
    repos from being processed. (Single-repo test: the listing
    failure simply yields an empty cycle.)"""
    monkeypatch.setattr(
        TicketService,
        "list",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("DB down")),
    )
    seen: list = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda *a, **k: seen.append(1) or 0.0,
    )
    asyncio.run(_run_one_cycle(worker, monkeypatch))
    assert seen == []  # listing failed, no tickets to warm


def test_cost_warmer_skips_repo_without_presence_file(
    settings,
    worker,
    monkeypatch,
):
    """A repo WITHOUT a .robotsix-mill/periodic/cost_warmer.yaml presence file
    is skipped, while peers that ship one continue to be warmed. Without this,
    a repo that never opted into cost warming would still pay the Langfuse hit
    on every cycle."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.config import RepoConfig, ReposRegistry

    _cfg._repos_config = ReposRegistry(
        repos={
            "kept": RepoConfig(
                repo_id="kept",
                board_id="kept-board",
                langfuse_project_name="kept",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
            ),
            "skipped": RepoConfig(
                repo_id="skipped",
                board_id="skipped-board",
                langfuse_project_name="skipped",
                langfuse_public_key="pk2",
                langfuse_secret_key="sk2",
            ),
        }
    )

    # Seed one ticket in each board so a *warming* cycle would visit it.
    kept_svc = TicketService(settings, board_id="kept-board")
    skipped_svc = TicketService(settings, board_id="skipped-board")
    k = kept_svc.create("k", "body")
    skipped_svc.create("s", "body")

    visited: list[str] = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda settings, ticket_id, repo_config=None: visited.append(ticket_id) or 0.0,
    )

    # Presence file only for "kept" → only it is warmed.
    monkeypatch.setattr(
        worker,
        "_has_periodic_presence",
        lambda rc, name: rc is not None and rc.repo_id == "kept",
    )
    worker._presence_overridden = True  # tell _run_one_cycle not to override

    try:
        asyncio.run(_run_one_cycle(worker, monkeypatch))
    finally:
        _cfg._reset_repos_config()

    # Only the kept repo's ticket was visited; the presence-less repo was
    # filtered out before TicketService.list() even ran.
    assert visited == [k.id]
