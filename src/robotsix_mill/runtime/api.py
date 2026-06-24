"""FastAPI app = the management-plane service.

It owns the DB, the in-process worker, and the HTTP surface the CLI (and
a future web frontend) use. Emitting a ticket enqueues it; the worker
picks it up immediately and chains it through the pipeline.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from ..config import ReposRegistry, Settings
from ..core.service import TransitionError
from ..forge.base import NotConfiguredError
from .exception_handlers import (
    catchall_handler,
    not_configured_error_handler,
    request_validation_error_handler,
    transition_error_handler,
)
from .lifespan import create_lifespan, setup_logging  # noqa: F401 — re-exported
from .middleware import RequestIDMiddleware
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

    _pyproject = tomllib.loads(
        (Path(__file__).parent.parent.parent.parent / "pyproject.toml").read_text()
    )
    _project = _pyproject["project"]

    app = FastAPI(
        title="robotsix-mill",
        version=_project["version"],
        description=_project["description"],
        contact={
            "name": _project["authors"][0]["name"],
            "url": "https://github.com/damien-robotsix/robotsix-mill",
        },
        license_info={
            "name": "MIT",
            "url": "https://spdx.org/licenses/MIT.html",
        },
        servers=[
            {"url": "http://127.0.0.1:8077", "description": "Local development"},
        ],
        openapi_tags=[
            {
                "name": "Health",
                "description": "Liveness, readiness, and service health probes",
            },
            {
                "name": "Tickets",
                "description": "Ticket CRUD, transitions, events, and metadata",
            },
            {"name": "Comments", "description": "Ticket comment management"},
            {"name": "Epics", "description": "Epic grouping and management"},
            {"name": "Passes", "description": "Solver passes and ticket processing"},
            {"name": "Traces", "description": "Execution traces and agent run history"},
            {"name": "Candidates", "description": "Merge request candidate inspection"},
            {"name": "Agents", "description": "Agent lifecycle and status"},
            {
                "name": "Board",
                "description": "Board card management and workflow transitions",
            },
        ],
        lifespan=create_lifespan(settings, repos, single_repo_id=single_repo_id),
    )

    # Centralised domain-exception → HTTP mapping.  Register concrete
    # handlers before the catch-all so the parent ``Exception`` handler
    # only sees genuinely unexpected errors.
    app.add_exception_handler(TransitionError, transition_error_handler)
    app.add_exception_handler(NotConfiguredError, not_configured_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(Exception, catchall_handler)

    app.add_middleware(RequestIDMiddleware)

    # Mill-specific static assets (board-mill.js, board-mill.css) are
    # served from a sub-path.  This mount must come BEFORE the /static
    # mount so the more-specific /static/mill prefix takes priority.
    mill_static = Path(__file__).parent / "static"
    app.mount(
        "/static/mill",
        StaticFiles(directory=str(mill_static)),
        name="mill-static",
    )

    # robotsix-board ships the shared board.js / board.css.  Mount its
    # static directory at /static so the board HTML links resolve to
    # the shared library's assets.
    from robotsix_board import static_dir as board_static_dir

    app.mount(
        "/static",
        StaticFiles(directory=str(board_static_dir())),
        name="board-static",
    )

    app.include_router(routes.router)

    # Prometheus metrics: auto-discovers all routes, exports request
    # counts, latency histograms (dual-bucket), and request/response sizes
    # at GET /metrics (excluded from OpenAPI schema).
    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(app)
    except ImportError:
        import logging

        logging.warning(
            "prometheus_fastapi_instrumentator not installed — "
            "/metrics endpoint unavailable"
        )

    return app
