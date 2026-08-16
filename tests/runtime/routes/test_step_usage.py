"""Tests for ``GET /metrics/step-usage`` server-side aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app
from robotsix_mill.runtime.step_usage_store import aggregate, record


@pytest.fixture
def client(settings, repos_registry):
    """Single-repo TestClient for the step-usage endpoint."""
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


def _record(
    *,
    stage: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    ts: datetime,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> dict:
    return {
        "stage_name": stage,
        "model_name": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "request_count": 1,
        "retry_count": 0,
        "timestamp": ts.isoformat(),
    }


def test_aggregate_input_token_percentiles(settings):
    """aggregate computes count/sum/mean/p50/p95/max per stage×model."""
    now = datetime.now(UTC)
    base = now - timedelta(hours=1)
    for n in (100, 200, 300):
        record(
            _record(
                stage="implement",
                model="deepseek-v4-pro",
                input_tokens=n,
                output_tokens=n // 10,
                ts=base,
            ),
            data_dir=settings.data_dir,
        )

    result = aggregate(
        data_dir=settings.data_dir,
        since=now - timedelta(hours=2),
        until=now + timedelta(minutes=1),
        stage="implement",
        model="deepseek-v4-pro",
    )

    assert result["record_count"] == 3
    (group,) = result["groups"]
    assert group["stage_name"] == "implement"
    assert group["model_name"] == "deepseek-v4-pro"
    assert group["call_count"] == 3
    assert group["input_tokens"] == {
        "sum": 600.0,
        "mean": 200.0,
        "p50": 200.0,
        "p95": 290.0,
        "max": 300.0,
    }
    assert group["output_tokens"]["max"] == 30.0
    assert group["cache_read_share"] == 0.0


def test_aggregate_cache_read_share(settings):
    """cache_read_share is cache-read tokens divided by total input."""
    now = datetime.now(UTC)
    record(
        _record(
            stage="implement",
            model="deepseek-v4-pro",
            input_tokens=100,
            output_tokens=10,
            cache_read=40,
            ts=now - timedelta(minutes=30),
        ),
        data_dir=settings.data_dir,
    )
    record(
        _record(
            stage="implement",
            model="deepseek-v4-pro",
            input_tokens=100,
            output_tokens=10,
            cache_read=60,
            ts=now - timedelta(minutes=20),
        ),
        data_dir=settings.data_dir,
    )

    result = aggregate(
        data_dir=settings.data_dir,
        since=now - timedelta(hours=1),
        until=now + timedelta(minutes=1),
    )
    (group,) = result["groups"]
    assert group["cache_read_input_tokens"] == 100
    assert group["cache_read_share"] == 0.5


def test_endpoint_returns_aggregates_without_prompt_payloads(client, settings):
    """One HTTP call returns stage×model aggregates and no prompt text."""
    now = datetime.now(UTC)
    base = now - timedelta(hours=1)
    record(
        _record(
            stage="implement",
            model="deepseek-v4-pro",
            input_tokens=100,
            output_tokens=10,
            ts=base,
        ),
        data_dir=settings.data_dir,
    )
    record(
        _record(
            stage="implement",
            model="deepseek-v4-pro",
            input_tokens=300,
            output_tokens=30,
            ts=base + timedelta(minutes=5),
        ),
        data_dir=settings.data_dir,
    )
    record(
        _record(
            stage="refine",
            model="other-model",
            input_tokens=50,
            output_tokens=5,
            ts=base,
        ),
        data_dir=settings.data_dir,
    )

    r = client.get(
        "/metrics/step-usage",
        params={
            "since": (now - timedelta(hours=2)).isoformat(),
            "stage": "implement",
            "model": "deepseek-v4-pro",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["record_count"] == 2
    assert len(body["groups"]) == 1
    group = body["groups"][0]
    assert group["stage_name"] == "implement"
    assert group["model_name"] == "deepseek-v4-pro"
    assert group["call_count"] == 2
    assert group["input_tokens"]["mean"] == 200.0
    assert group["input_tokens"]["max"] == 300.0
    # Compact payload: no prompt/args/tool-call text anywhere.
    assert "prompt" not in r.text.lower()
    assert "tool_calls" not in r.text


def test_endpoint_treats_empty_filters_as_unset(client, settings):
    """Empty ``stage``/``model`` query values filter nothing, like omission."""
    now = datetime.now(UTC)
    record(
        _record(
            stage="implement",
            model="deepseek-v4-pro",
            input_tokens=100,
            output_tokens=10,
            ts=now - timedelta(minutes=30),
        ),
        data_dir=settings.data_dir,
    )
    record(
        _record(
            stage="refine",
            model="deepseek-v4-pro",
            input_tokens=50,
            output_tokens=5,
            ts=now - timedelta(minutes=20),
        ),
        data_dir=settings.data_dir,
    )

    r = client.get("/metrics/step-usage", params={"stage": "", "model": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["record_count"] == 2
    assert len(body["groups"]) == 2


def test_endpoint_defaults_to_24h_window(client, settings):
    """Omitting `since`/`until` aggregates the trailing 24 hours."""
    now = datetime.now(UTC)
    record(
        _record(
            stage="implement",
            model="deepseek-v4-pro",
            input_tokens=100,
            output_tokens=10,
            ts=now - timedelta(hours=1),
        ),
        data_dir=settings.data_dir,
    )

    r = client.get("/metrics/step-usage")
    assert r.status_code == 200
    body = r.json()
    assert body["record_count"] == 1
    assert body["window_seconds"] == pytest.approx(24 * 3600, rel=0.01)


def test_endpoint_rejects_bad_since(client):
    r = client.get("/metrics/step-usage", params={"since": "not-a-date"})
    assert r.status_code == 400
    assert "Invalid ISO-8601" in r.json()["detail"]


def test_endpoint_rejects_inverted_window(client):
    now = datetime.now(UTC)
    r = client.get(
        "/metrics/step-usage",
        params={
            "since": now.isoformat(),
            "until": (now - timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 400
    assert "must be earlier" in r.json()["detail"]
