"""FastAPI lifespan management for robotsix-mill.

Provides ``setup_logging()`` (called once at import time) and
``create_lifespan(settings)`` which returns an async context manager
suitable for ``FastAPI(lifespan=...)``.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncContextManager, Callable

from fastapi import FastAPI

from ..config import ReposRegistry, Settings
from ..core import db
from ..core.service import TicketService
from ..stages import StageContext
from . import tracing
from .deep_review_store import DeepReviewStore
from .run_registry import RunRegistry
from .worker import Worker


def setup_logging() -> None:
    """Surface ``robotsix_mill.*`` logs on stdout.

    Without this, app logs (worker/audit/stages/notify) propagate to a
    root logger that uvicorn leaves handler-less, so they vanish from
    docker logs — masking failures (e.g. a silently-crashing /audit
    background thread).  Idempotent.
    """
    root = logging.getLogger("robotsix_mill")
    if any(getattr(h, "_mill", False) for h in root.handlers):
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h._mill = True  # marker for idempotency
    root.addHandler(h)
    root.setLevel(logging.INFO)
    # keep propagate=True: uvicorn leaves the real root handler-less so
    # there's no double-logging, and pytest's caplog needs propagation.


# Called at import time so logging is configured before any lifespan or
# route code logs — the idempotency guard makes this safe to repeat.
setup_logging()


def create_lifespan(
    settings: Settings,
    repos: ReposRegistry,
    single_repo_id: str | None = None,
) -> Callable[[FastAPI], AsyncContextManager]:
    """Build a FastAPI lifespan callable that performs the same startup
    and shutdown steps as the original inline ``@asynccontextmanager``:

    - Initialise the DB and tracing.
    - Construct ``TicketService``, ``StageContext``, and ``Worker``.
    - Store them on ``app.state`` for route handlers.
    - Start the worker and requeue unfinished tickets.
    - On shutdown, gracefully stop the worker.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Initialize each registered repo's DB so per-board services
        # have schema available without lazy-init races. The
        # default-board DB at <data_dir>/mill.db is never created
        # eagerly — every ticket lives in a per-repo DB.
        for rc in repos.repos.values():
            db.init_db(settings, rc.board_id)

        # Purge a stray <data_dir>/mill.db that some legacy code path
        # may have lazily materialised on a previous run. Only safe
        # when (a) multi-repo is configured (per-repo DBs are the
        # source of truth), (b) the file has zero ticket rows (no
        # real data to lose), AND (c) no engine has been opened on
        # board_id="" yet — an engine already in the cache means
        # some caller is actively using the file (typical in test
        # setups that call init_db(settings) directly), and deleting
        # underneath it would silently break that caller. Logs the
        # action so the operator can correlate it with the warning
        # emitted by get_engine when the file was created.
        if repos.repos and "" not in db._engines:
            stray = settings.data_dir / "mill.db"
            if stray.exists():
                try:
                    import sqlite3

                    with sqlite3.connect(stray) as conn:
                        try:
                            row_count = conn.execute(
                                "SELECT count(*) FROM ticket"
                            ).fetchone()[0]
                        except sqlite3.OperationalError:
                            # No ticket table → file has only the
                            # empty schema; safe to nuke.
                            row_count = 0
                    if row_count == 0:
                        stray.unlink()
                        logging.getLogger("robotsix_mill.lifespan").info(
                            "lifespan: purged empty stray %s "
                            "(multi-repo mode; per-repo DBs are authoritative)",
                            stray,
                        )
                except Exception:  # noqa: BLE001
                    logging.getLogger("robotsix_mill.lifespan").exception(
                        "lifespan: failed to purge stray %s",
                        stray,
                    )
        # In single-repo mode use the specified repo; in multi-repo mode
        # pick the first repo as the initial repo_config for the worker.
        if single_repo_id is not None:
            repo_config = repos.repos[single_repo_id]
        else:
            repo_config = next(iter(repos.repos.values()))
        service = TicketService(settings, board_id=repo_config.board_id)
        ctx = StageContext(settings=settings, service=service, repo_config=repo_config)
        app.state.repos = repos
        app.state.single_repo_id = single_repo_id
        # Per-repo run registries — each repo's audit/health/etc. run
        # log lands in <data_dir>/<board_id>/runs.json.
        run_registries: dict[str, RunRegistry] = {
            rc.board_id: RunRegistry(
                settings.data_dir / rc.board_id / "runs.json",
            )
            for rc in repos.repos.values()
        }
        # Default registry for the worker's own (board-less) periodic
        # ticks — points at the lead repo's registry so legacy
        # callers without repo context still record somewhere.
        default_registry = run_registries[repo_config.board_id]
        worker = Worker(ctx, default_registry)
        app.state.settings = settings
        app.state.service = service
        app.state.worker = worker
        app.state.run_registry = default_registry
        app.state.run_registries = run_registries
        app.state.deep_review_results = {}
        app.state.deep_review_store = DeepReviewStore(
            settings.data_dir / "deep_review_results.json"
        )
        tracing.install_signal_handlers()
        worker.start()
        worker.requeue_unfinished()  # resume anything left mid-pipeline
        try:
            yield
        finally:
            await worker.stop()

    return lifespan
