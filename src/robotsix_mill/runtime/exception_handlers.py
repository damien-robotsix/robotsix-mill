"""FastAPI exception handlers — map domain exceptions to HTTP responses.

All handlers return RFC 9457 Problem Details JSON bodies.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..core.service import AmbiguousTicketId, TransitionError
from ..forge.base import NotConfiguredError
from .errors import ProblemDetail
from .tracing import get_current_trace_id


async def transition_error_handler(
    request: Request, exc: TransitionError
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=ProblemDetail(
            title="Conflict",
            status=409,
            detail=str(exc),
            trace_id=get_current_trace_id(),
        ).model_dump(),
        media_type="application/problem+json",
    )


async def ambiguous_ticket_id_handler(
    request: Request, exc: AmbiguousTicketId
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=ProblemDetail(
            title="Conflict",
            status=409,
            detail=str(exc),
            trace_id=get_current_trace_id(),
        ).model_dump(),
        media_type="application/problem+json",
    )


async def not_configured_error_handler(
    request: Request, exc: NotConfiguredError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=ProblemDetail(
            title="Service Unavailable",
            status=503,
            detail=str(exc),
            trace_id=get_current_trace_id(),
        ).model_dump(),
        media_type="application/problem+json",
    )


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
        content=ProblemDetail(
            title="Internal Server Error",
            status=500,
            detail="Internal server error",
            trace_id=get_current_trace_id(),
        ).model_dump(),
        media_type="application/problem+json",
    )


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Map FastAPI/Pydantic validation errors to RFC 9457 (422)."""
    return JSONResponse(
        status_code=422,
        content=ProblemDetail(
            title="Unprocessable Entity",
            status=422,
            detail="Request validation failed",
            errors=jsonable_encoder(exc.errors()),
            trace_id=get_current_trace_id(),
        ).model_dump(),
        media_type="application/problem+json",
    )
