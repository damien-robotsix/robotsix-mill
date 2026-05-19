"""FastAPI app = the management-plane service.

It owns the DB, the in-process worker, and the HTTP surface the CLI (and
a future web frontend) use. Emitting a ticket enqueues it; the worker
picks it up immediately and chains it through the pipeline.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..config import Settings
from .lifespan import create_lifespan, setup_logging  # noqa: F401 — re-exported
from . import routes


def create_app(settings: Settings | None = None) -> FastAPI:  # noqa: C901  # TODO: extract route registration and lifespan into separate functions
    """Build and return a fully-wired FastAPI application.

    *settings* may be ``None``, in which case ``Settings()`` (from env)
    is used.  The returned app has all routes registered and the
    lifespan configured.
    """
    setup_logging()
    settings = settings or Settings()
    app = FastAPI(title="robotsix-mill", lifespan=create_lifespan(settings))
    app.include_router(routes.router)
    return app
