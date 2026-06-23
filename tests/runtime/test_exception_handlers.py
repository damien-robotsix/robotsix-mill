"""Tests for the centralised FastAPI exception handlers.

Build a throwaway FastAPI app, register the handlers exactly as
``create_app()`` does, and assert that domain exceptions raised inside
route handlers are translated to the expected RFC 9457 responses.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pytest import LogCaptureFixture

from robotsix_mill.core.service import TransitionError
from robotsix_mill.forge.base import NotConfiguredError
from robotsix_mill.runtime.exception_handlers import (
    catchall_handler,
    not_configured_error_handler,
    request_validation_error_handler,
    transition_error_handler,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(TransitionError, transition_error_handler)
    app.add_exception_handler(NotConfiguredError, not_configured_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(Exception, catchall_handler)

    @app.get("/transition")
    def _transition() -> dict:
        raise TransitionError("bad transition")

    @app.get("/not-configured")
    def _not_configured() -> dict:
        raise NotConfiguredError("forge not configured")

    @app.get("/boom")
    def _boom() -> dict:
        raise RuntimeError("kaboom: secret leaked")

    @app.post("/validated")
    def _validated(q: str = Query(..., min_length=5)) -> dict[str, str]:
        return {"q": q}

    return app


def assert_problem_details(
    body: dict[str, Any],
    status: int,
    title: str,
    detail: str,
) -> None:
    """Assert the response body is a valid RFC 9457 Problem Details envelope."""
    assert body["type"] == "about:blank"
    assert body["title"] == title
    assert body["status"] == status
    assert body["detail"] == detail
    assert body.get("instance") is None
    assert body.get("trace_id") is None  # no OTel context in tests


def assert_problem_content_type(resp: Any) -> None:
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_transition_error_maps_to_409() -> None:
    client = TestClient(_app())
    resp = client.get("/transition")
    assert resp.status_code == 409
    assert_problem_content_type(resp)
    assert_problem_details(resp.json(), 409, "Conflict", "bad transition")


def test_not_configured_error_maps_to_503() -> None:
    client = TestClient(_app())
    resp = client.get("/not-configured")
    assert resp.status_code == 503
    assert_problem_content_type(resp)
    assert_problem_details(
        resp.json(), 503, "Service Unavailable", "forge not configured"
    )


def test_catchall_sanitises_unexpected_exception(caplog: LogCaptureFixture) -> None:
    # raise_server_exceptions=False so the catch-all response is returned
    # to the client instead of being re-raised into the test.
    client = TestClient(_app(), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert_problem_content_type(resp)
    body = resp.json()
    assert_problem_details(body, 500, "Internal Server Error", "Internal server error")
    assert "kaboom" not in resp.text
    # ...but the full exception is logged for operator forensics.
    assert any("Unhandled exception" in r.message for r in caplog.records)


def test_request_validation_error_maps_to_422() -> None:
    client = TestClient(_app())
    resp = client.post("/validated", json={"q": "ab"})
    assert resp.status_code == 422
    assert_problem_content_type(resp)
    body = resp.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Unprocessable Entity"
    assert body["status"] == 422
    assert body["detail"] == "Request validation failed"
    assert body.get("instance") is None
    assert body.get("trace_id") is None
    errors = body.get("errors")
    assert isinstance(errors, list)
    assert len(errors) > 0
