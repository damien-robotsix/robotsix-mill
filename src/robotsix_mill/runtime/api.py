"""FastAPI app = the management-plane service.

It owns the DB, the in-process worker, and the HTTP surface the CLI (and
a future web frontend) use. Emitting a ticket enqueues it; the worker
picks it up immediately and chains it through the pipeline.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import RepoConfig, Settings
from .lifespan import create_lifespan, setup_logging  # noqa: F401 — re-exported
from . import routes


def create_app(repo_config: RepoConfig, settings: Settings | None = None) -> FastAPI:
    """Build and return a fully-wired FastAPI application.

    *repo_config* is the per-repository configuration resolved from the
    ``--repo-id`` CLI argument — it is **required**; there is no global
    fallback.  *settings* may be ``None``, in which case ``Settings()``
    (from env) is used.  The returned app has all routes registered and
    the lifespan configured.
    """
    setup_logging()
    settings = settings or Settings()
    app = FastAPI(title="robotsix-mill", lifespan=create_lifespan(settings, repo_config))
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(routes.router)
    return app
