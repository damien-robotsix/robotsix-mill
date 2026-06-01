"""FastAPI lifespan management for robotsix-mill.

Provides ``setup_logging()`` (called once at import time) and
``create_lifespan(settings)`` which returns an async context manager
suitable for ``FastAPI(lifespan=...)``.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncContextManager, Callable

from fastapi import FastAPI

from ..config import ReposRegistry, Settings
from ..core import db
from ..core.service import TicketService
from ..stages import StageContext
from . import tracing
from .broadcaster import BoardBroadcaster
from .run_registry import RunRegistry
from .worker import Worker


def setup_logging() -> None:
    """Surface ``robotsix_mill.*`` logs on stdout.

    Without this, app logs (worker/audit/stages/notify) propagate to a
    root logger that uvicorn leaves handler-less, so they vanish from
    docker logs — masking failures (e.g. a silently-crashing /audit
    background thread).  Idempotent.
    """
    # Configure both mill's own logger AND robotsix_llmio's, so the
    # extracted LLM-I/O library's logs (esp. the claude_sdk per-turn stream
    # feedback) surface in docker logs instead of vanishing at a handler-less
    # root.
    for logger_name in ("robotsix_mill", "robotsix_llmio"):
        lg = logging.getLogger(logger_name)
        if any(getattr(h, "_mill", False) for h in lg.handlers):
            continue
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        h._mill = True  # marker for idempotency
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    # keep propagate=True: uvicorn leaves the real root handler-less so
    # there's no double-logging, and pytest's caplog needs propagation.


# Called at import time so logging is configured before any lifespan or
# route code logs — the idempotency guard makes this safe to repeat.
setup_logging()

# Module-level process start time, set at the beginning of the lifespan
# startup phase. Accessible without an ``app`` reference so the
# trace-review runner can import it directly for restart correlation.
_process_started_at: datetime | None = None


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
        # have schema available without lazy-init races.
        # Every ticket lives in a per-repo DB.
        for rc in repos.repos.values():
            db.init_db(settings, rc.board_id)

        # Record process start time for health endpoint and restart
        # correlation in trace-review (incomplete traces ending near
        # this time are likely restart kills, not agent-loop bugs).
        global _process_started_at
        _process_started_at = datetime.now(timezone.utc)
        app.state.started_at = _process_started_at

        # In single-repo mode use the specified repo; in multi-repo mode
        # pick the first repo as the initial repo_config for the worker.
        if single_repo_id is not None:
            repo_config = repos.repos[single_repo_id]
        else:
            repo_config = next(iter(repos.repos.values()))
        service = TicketService(settings, board_id=repo_config.board_id)
        broadcaster = BoardBroadcaster()
        service._on_transition = broadcaster.broadcast_sync
        ctx = StageContext(settings=settings, service=service, repo_config=repo_config)
        app.state.repos = repos
        app.state.single_repo_id = single_repo_id
        app.state.broadcaster = broadcaster
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
        tracing.install_signal_handlers()
        worker.start()
        worker.requeue_unfinished()  # resume anything left mid-pipeline
        try:
            yield
        finally:
            await worker.stop()

    return lifespan
