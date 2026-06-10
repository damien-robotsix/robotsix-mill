"""Tests for the centralised FastAPI exception handlers.

Build a throwaway FastAPI app, register the handlers exactly as
``create_app()`` does, and assert that domain exceptions raised inside
route handlers are translated to the expected HTTP responses.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from robotsix_mill.core.service import TransitionError
from robotsix_mill.forge.base import NotConfiguredError
from robotsix_mill.runtime.exception_handlers import (
    catchall_handler,
    not_configured_error_handler,
    transition_error_handler,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(TransitionError, transition_error_handler)
    app.add_exception_handler(NotConfiguredError, not_configured_error_handler)
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

    return app


def test_transition_error_maps_to_409() -> None:
    client = TestClient(_app())
    resp = client.get("/transition")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "bad transition"}


def test_not_configured_error_maps_to_503() -> None:
    client = TestClient(_app())
    resp = client.get("/not-configured")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "forge not configured"}


def test_catchall_sanitises_unexpected_exception(caplog) -> None:
    # raise_server_exceptions=False so the catch-all response is returned
    # to the client instead of being re-raised into the test.
    client = TestClient(_app(), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    # The safe body never leaks the underlying exception message.
    assert resp.json() == {"detail": "Internal server error"}
    assert "kaboom" not in resp.text
    # ...but the full exception is logged for operator forensics.
    assert any("Unhandled exception" in r.message for r in caplog.records)
