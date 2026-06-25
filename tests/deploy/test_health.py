"""Tests for the deploy-server health endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    """The /health liveness probe returns status=ok with uptime."""
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "ok"
    assert "uptime_seconds" in payload
    assert isinstance(payload["uptime_seconds"], int)
    assert payload["uptime_seconds"] >= 0


def test_ready_returns_ready(client: TestClient) -> None:
    """The /ready readiness probe returns status=ready with uptime."""
    resp = client.get("/ready")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "ready"
    assert "uptime_seconds" in payload
    assert isinstance(payload["uptime_seconds"], int)
    assert payload["uptime_seconds"] >= 0
