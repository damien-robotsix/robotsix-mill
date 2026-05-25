"""FastAPI app = the management-plane service.

It owns the DB, the in-process worker, and the HTTP surface the CLI (and
a future web frontend) use. Emitting a ticket enqueues it; the worker
picks it up immediately and chains it through the pipeline.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import ReposRegistry, Settings
from .lifespan import create_lifespan, setup_logging  # noqa: F401 — re-exported
from . import routes


def create_app(
    repos: ReposRegistry,
    settings: Settings | None = None,
    single_repo_id: str | None = None,
) -> FastAPI:
    """Build and return a fully-wired FastAPI application.

    *repos* is the :class:`ReposRegistry` holding all configured repos.
    *settings* may be ``None``, in which case ``Settings()`` (from env)
    is used.  *single_repo_id* scopes the process to one repo (optional;
    when ``None`` the process serves all repos).  The returned app has
    all routes registered and the lifespan configured.
    """
    setup_logging()
    settings = settings or Settings()
    app = FastAPI(
        title="robotsix-mill",
        lifespan=create_lifespan(settings, repos, single_repo_id=single_repo_id),
    )
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(routes.router)
    return app
