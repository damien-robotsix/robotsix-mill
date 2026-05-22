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

from ..config import Settings
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
    h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
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
        db.init_db(settings)
        tracing.init(settings)
        service = TicketService(settings)
        ctx = StageContext(settings=settings, service=service)
        run_registry = RunRegistry(settings.data_dir / "runs.json")
        worker = Worker(ctx, run_registry)
        app.state.settings = settings
        app.state.service = service
        app.state.worker = worker
        app.state.run_registry = run_registry
        app.state.deep_review_results = {}
        app.state.deep_review_store = DeepReviewStore(
            settings.data_dir / "deep_review_results.json"
        )
        worker.start()
        worker.requeue_unfinished()  # resume anything left mid-pipeline
        try:
            yield
        finally:
            await worker.stop()

    return lifespan
