"""Unit tests for ``token_metrics_runner``.

All Langfuse I/O is monkeypatched (the runner imports
``list_all_traces_since`` at module scope), so the suite is hermetic —
no real Langfuse instance is required.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from robotsix_mill.agents.runners import token_metrics_runner as mod
from robotsix_mill.config import Settings
from robotsix_mill.config.settings import LangfuseProjectCredentials


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "data"),
        langfuse={
            "host": "https://lf.example.com",
            "projects": {
                "robotsix-mill": LangfuseProjectCredentials(
                    public_key="pk-test",
                    secret_key="sk-test",
                    project_id="",
                )
            },
        },
    )


def _step_usage(**overrides) -> str:
    data = {
        "stage_name": "review",
        "model_name": "model-x",
        "input_tokens": 100,
        "output_tokens": 50,
    }
    data.update(overrides)
    return json.dumps(data)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_percentile_nearest_rank():
    assert mod._percentile([], 50.0) == 0.0
    assert mod._percentile([1, 2, 3, 4], 50.0) == 2.0
    assert mod._percentile([10, 20, 30, 40, 50], 95.0) == 50.0


def test_token_statistics_empty():
    assert mod._token_statistics([]) == {
        "count": 0,
        "sum": 0.0,
        "mean": 0.0,
        "min": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "max": 0.0,
    }


def test_token_statistics_sorted_percentiles():
    assert mod._token_statistics([4, 1, 3, 2]) == {
        "count": 4,
        "sum": 10.0,
        "mean": 2.5,
        "min": 1.0,
        "p50": 2.0,
        "p95": 4.0,
        "max": 4.0,
    }


# ---------------------------------------------------------------------------
# aggregate_step_usage
# ---------------------------------------------------------------------------


def test_aggregate_step_usage_groups_by_stage_and_model():
    traces = [
        {
            "observations": [
                {"metadata": {"mill.step_usage": _step_usage(input_tokens=100)}},
                {"metadata": {"mill.step_usage": _step_usage(input_tokens=300)}},
                {
                    "metadata": {
                        "mill.step_usage": _step_usage(
                            stage_name="ci_fix", input_tokens=200, output_tokens=80
                        )
                    }
                },
            ]
        }
    ]
    buckets = mod.aggregate_step_usage(traces)
    assert [b["stage"] for b in buckets] == ["ci_fix", "review"]

    review = buckets[1]
    assert review["model"] == "model-x"
    assert review["calls"] == 2
    assert review["input_tokens"]["count"] == 2
    assert review["input_tokens"]["sum"] == 400.0
    assert review["input_tokens"]["max"] == 300.0
    assert review["output_tokens"]["sum"] == 100.0

    ci_fix = buckets[0]
    assert ci_fix["input_tokens"]["sum"] == 200.0
    assert ci_fix["output_tokens"]["p95"] == 80.0


def test_aggregate_step_usage_unknown_keys_and_trace_metadata_fallback():
    traces = [
        {
            # No stage/model keys → "unknown" bucket from trace-level metadata.
            "metadata": {
                "mill.step_usage": json.dumps({"input_tokens": 10, "output_tokens": 5})
            },
        },
        {
            # Malformed and empty observations must be ignored, not crash.
            "observations": [
                {"metadata": {"mill.step_usage": "not json"}},
                {"metadata": {}},
            ]
        },
    ]
    buckets = mod.aggregate_step_usage(traces)
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["stage"] == "unknown"
    assert bucket["model"] == "unknown"
    assert bucket["calls"] == 1
    assert bucket["input_tokens"]["sum"] == 10.0
    assert bucket["output_tokens"]["sum"] == 5.0


def test_aggregate_step_usage_non_numeric_tokens_coerced_to_zero():
    traces = [
        {
            "observations": [
                {
                    "metadata": {
                        "mill.step_usage": json.dumps(
                            {
                                "stage_name": "review",
                                "model_name": "model-x",
                                "input_tokens": "nope",
                                "output_tokens": None,
                            }
                        )
                    }
                }
            ]
        }
    ]
    buckets = mod.aggregate_step_usage(traces)
    assert buckets[0]["input_tokens"]["sum"] == 0.0
    assert buckets[0]["output_tokens"]["sum"] == 0.0


# ---------------------------------------------------------------------------
# run_token_metrics_aggregation
# ---------------------------------------------------------------------------


def test_run_writes_daily_snapshot(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    traces = [
        {
            "observations": [
                {"metadata": {"mill.step_usage": _step_usage(input_tokens=100)}}
            ]
        }
    ]
    monkeypatch.setattr(mod, "list_all_traces_since", lambda *a, **k: traces)

    result = mod.run_token_metrics_aggregation(settings=s, window_seconds=86400)

    assert result.written is True
    assert result.traces_seen == 1
    assert result.steps_seen == 1
    assert result.path is not None
    assert result.path.parent == s.data_dir / "token_metrics"
    assert result.path.name == f"{datetime.now(UTC).date().isoformat()}.json"

    data = json.loads(result.path.read_text(encoding="utf-8"))
    assert data["traces_seen"] == 1
    assert data["steps_seen"] == 1
    assert data["buckets"][0]["stage"] == "review"
    assert data["buckets"][0]["input_tokens"]["p95"] == 100.0


def test_run_skips_when_tracing_disabled(tmp_path, monkeypatch):
    s = Settings(data_dir=str(tmp_path / "data"))
    called: list[tuple] = []
    monkeypatch.setattr(
        mod, "list_all_traces_since", lambda *a, **k: called.append(a) or []
    )

    result = mod.run_token_metrics_aggregation(settings=s)

    assert result.written is False
    assert result.traces_seen == 0
    assert result.steps_seen == 0
    assert called == []
    assert not (s.data_dir / "token_metrics").exists()


def test_run_skips_when_langfuse_block_missing(tmp_path, monkeypatch):
    s = Settings(
        data_dir=str(tmp_path / "data"),
        langfuse={"host": "https://lf.example.com", "projects": {}},
    )
    called: list[tuple] = []
    monkeypatch.setattr(
        mod, "list_all_traces_since", lambda *a, **k: called.append(a) or []
    )

    result = mod.run_token_metrics_aggregation(settings=s)

    assert result.written is False
    assert called == []


def test_run_graceful_on_list_failure(tmp_path, monkeypatch):
    s = _settings(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "list_all_traces_since", boom)

    result = mod.run_token_metrics_aggregation(settings=s)

    assert result.written is False
    assert result.traces_seen == 0
    assert result.steps_seen == 0
    assert result.buckets == []
