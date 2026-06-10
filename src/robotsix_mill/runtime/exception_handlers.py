"""FastAPI exception handlers — map domain exceptions to HTTP responses."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..core.service import TransitionError
from ..forge.base import NotConfiguredError


async def transition_error_handler(
    request: Request, exc: TransitionError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


async def not_configured_error_handler(
    request: Request, exc: NotConfiguredError
) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


async def catchall_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: sanitise unexpected exceptions.

    Never leak stack traces to clients.  Log the full exception for
    operator forensics but return a safe 500 body.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.exception(
        "Unhandled exception in request %s %s", request.method, request.url
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
