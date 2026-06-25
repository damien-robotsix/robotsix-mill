"""FastAPI application for the central deployment & lifecycle server.

Exposes /health (liveness) and /ready (readiness) endpoints.  The
lifecycle API endpoints are added by sibling tickets.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

STARTED_AT: datetime = datetime.now(timezone.utc)


def create_app() -> FastAPI:
    """Build and return the deploy-server FastAPI application."""
    app = FastAPI(
        title="robotsix-deploy",
        version="0.1.0",
        description="Central deployment & lifecycle server for the robotsix suite.",
        openapi_tags=[
            {
                "name": "Health",
                "description": "Liveness, readiness, and service health probes",
            },
        ],
    )

    # Store boot time on app state for uptime reporting.
    app.state.started_at = STARTED_AT

    @app.get("/health", tags=["Health"])
    def health(request: Request) -> dict[str, object]:
        """Liveness probe — lightweight, always returns ok."""
        started_at: datetime = request.app.state.started_at
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
        return {
            "status": "ok",
            "started_at": started_at.isoformat(),
            "uptime_seconds": int(uptime),
        }

    @app.get("/ready", tags=["Health"])
    async def ready(request: Request) -> JSONResponse:
        """Readiness probe — returns 200 when the service is ready to
        accept traffic, 503 otherwise.

        Currently a no-op (always ready); downstream checks (broker,
        database) are added by sibling tickets.
        """
        started_at: datetime = request.app.state.started_at
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
        return JSONResponse(
            content={
                "status": "ready",
                "started_at": started_at.isoformat(),
                "uptime_seconds": int(uptime),
            },
        )

    return app
