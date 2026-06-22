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
from ..core.service import TransitionError
from ..forge.base import NotConfiguredError
from .exception_handlers import (
    catchall_handler,
    not_configured_error_handler,
    transition_error_handler,
)
from .lifespan import create_lifespan, setup_logging  # noqa: F401 — re-exported
from .middleware import RequestIDMiddleware
from . import routes


def _patch_prometheus_instrumentator() -> None:
    """Patch prometheus_fastapi_instrumentator to tolerate FastAPI 0.138+.

    FastAPI 0.138+ wraps included routers in _IncludedRouter objects that
    carry ``.routes`` but no ``.path``.  prometheus_fastapi_instrumentator
    8.0.0's ``_get_route_name`` assumes every route has ``.path`` and
    raises ``AttributeError`` when it encounters an _IncludedRouter.

    This replaces ``_get_route_name`` with a version that recurses into
    any route that matches but lacks a ``.path`` attribute.
    """
    import prometheus_fastapi_instrumentator.routing as _pfi

    def _get_route_name(scope, routes, route_name=None):
        for route in routes:
            match, child_scope = route.matches(scope)
            if match == _pfi.Match.FULL:
                child_scope = {**scope, **child_scope}
                if hasattr(route, "routes") and not hasattr(route, "path"):
                    child_name = _get_route_name(child_scope, route.routes, route_name)
                    return child_name if child_name is not None else route_name
                route_name = route.path
                if isinstance(route, _pfi.Mount) and route.routes:
                    child_route_name = _get_route_name(
                        child_scope, route.routes, route_name
                    )
                    if child_route_name is None:
                        route_name = None
                    else:
                        route_name += child_route_name
                return route_name
            elif match == _pfi.Match.PARTIAL and route_name is None:
                route_name = getattr(route, "path", None)
        return None

    _pfi._get_route_name = _get_route_name


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

    # Centralised domain-exception → HTTP mapping.  Register concrete
    # handlers before the catch-all so the parent ``Exception`` handler
    # only sees genuinely unexpected errors.
    app.add_exception_handler(TransitionError, transition_error_handler)
    app.add_exception_handler(NotConfiguredError, not_configured_error_handler)
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
    # the shared library's assets.  When robotsix-board is not yet
    # installed, fall back to the bundled legacy static files.
    try:
        from robotsix_board import static_dir as board_static_dir
    except ImportError:
        board_static_dir = None

    if board_static_dir is not None:
        app.mount(
            "/static",
            StaticFiles(directory=str(board_static_dir())),
            name="board-static",
        )
    else:
        # Fallback: serve the legacy bundled board.js / board.css from
        # mill's own static directory until robotsix-board is installed.
        legacy_static = Path(__file__).parent / "static"
        app.mount(
            "/static",
            StaticFiles(directory=str(legacy_static)),
            name="legacy-static",
        )

    app.include_router(routes.router)

    # Prometheus metrics: auto-discovers all routes, exports request
    # counts, latency histograms (dual-bucket), and request/response sizes
    # at GET /metrics (excluded from OpenAPI schema).
    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(app)
        _patch_prometheus_instrumentator()
    except ImportError:
        import logging

        logging.warning(
            "prometheus_fastapi_instrumentator not installed — "
            "/metrics endpoint unavailable"
        )

    return app
