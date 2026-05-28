"""Tests for the trace-review runner.

Phase-1 classifier tests are pure (synthetic trace dicts + observation
lists). Phase-2 / orchestrator tests monkeypatch the Langfuse client
and the inspector seam so no LLM / network is involved.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from robotsix_mill.agents.trace_inspector import (
    TraceFinding, TraceInspectResult,
)
from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.trace_review_runner import (
    _classify_trace,
    _load_watermark,
    _normalize,
    _save_watermark,
    run_trace_review_pass,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MILL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    _reset_secrets()
    from robotsix_mill.core import db
    from robotsix_mill.config import _reset_repos_config
    db.reset_engine()
    _reset_repos_config()
    return Settings()


# ---------------------------------------------------------------------------
# Phase-1 classifier
# ---------------------------------------------------------------------------


def _trace(**overrides):
    base = {
        "id": "t1",
        "name": "implement",
        "sessionId": "ticket-x",
        "totalCost": 0.05,
    }
    base.update(overrides)
    return base


def _obs(name: str, **overrides):
    base = {
        "name": name,
        "input": None,
        "output": None,
        "level": None,
        "statusMessage": None,
    }
    base.update(overrides)
    return base


class TestClassifier:
    """``_classify_trace`` flags traces by deterministic criteria."""

    def test_clean_trace_no_flags(self, settings):
        flags = _classify_trace(_trace(), settings, observations=[])
        assert flags.flags == []
        assert flags.flagged is False

    def test_cost_outlier_above_threshold(self, settings):
        flags = _classify_trace(
            _trace(totalCost=2.50), settings, observations=[],
        )
        assert any("cost_outlier" in f for f in flags.flags)

    def test_cost_outlier_at_threshold_is_not_flagged(self, settings):
        # Strictly > threshold; equal is below.
        flags = _classify_trace(
            _trace(totalCost=settings.trace_review_cost_threshold_usd),
            settings, observations=[],
        )
        assert not any("cost_outlier" in f for f in flags.flags)

    def test_observation_storm(self, settings):
        n = settings.trace_review_max_observations + 1
        obs = [_obs("read_file") for _ in range(n)]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("observation_storm" in f for f in flags.flags)

    def test_tool_error_in_output(self, settings):
        obs = [_obs("run_command", output="error: command failed")]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

    def test_traceback_in_status_message_is_a_tool_error(self, settings):
        obs = [_obs(
            "read_file",
            statusMessage="Traceback (most recent call last):\n  ...",
        )]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

    def test_rejected_generation_marker(self, settings):
        obs = [_obs(
            "chat deepseek/deepseek-v4-pro",
            level="WARNING",
            statusMessage=(
                "model produced 2636 output token(s) but no "
                "gen_ai.output.messages was set — pydantic-ai likely "
                "rejected the response"
            ),
        )]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("rejected_generations" in f for f in flags.flags)

    def test_explore_storm(self, settings):
        obs = [_obs("explore run") for _ in range(6)]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("explore_storm" in f for f in flags.flags)

    def test_ask_user_loop(self, settings):
        obs = [_obs("ask_user"), _obs("ask_user")]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("ask_user_loop" in f for f in flags.flags)

    def test_repeated_tool_flag(self, settings):
        n = settings.trace_review_max_repeated_tool + 1
        obs = [_obs("read_file") for _ in range(n)]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("repeated_tool read_file" in f for f in flags.flags)

    def test_observations_none_only_runs_summary_flags(self, settings):
        """When observations are unavailable (Langfuse fetch failed),
        only cost-level flags fire — no observation-level scan."""
        flags = _classify_trace(
            _trace(totalCost=2.0), settings, observations=None,
        )
        assert any("cost_outlier" in f for f in flags.flags)
        assert not any("tool_errors" in f for f in flags.flags)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------


class TestWatermark:
    def test_load_returns_none_when_file_missing(self, settings):
        assert _load_watermark(settings, "") is None

    def test_save_and_load_roundtrip(self, settings):
        when = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        _save_watermark(settings, "", when)
        loaded = _load_watermark(settings, "")
        assert loaded == when

    def test_per_board_isolation(self, settings):
        a = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        b = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)
        _save_watermark(settings, "board-a", a)
        _save_watermark(settings, "board-b", b)
        assert _load_watermark(settings, "board-a") == a
        assert _load_watermark(settings, "board-b") == b


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------


def test_normalize_strips_punctuation_and_case():
    assert _normalize("Trace-Review: Tool Errors!") == (
        "trace review tool errors"
    )


# ---------------------------------------------------------------------------
# Orchestrator (run_trace_review_pass)
# ---------------------------------------------------------------------------


class TestRunTraceReviewPass:
    """End-to-end orchestrator tests with seams monkeypatched."""

    def test_no_traces_returns_empty_result(self, settings, monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [],
        )
        result = run_trace_review_pass(session_id="sess-1")
        assert result.drafts_created == []
        assert result.traces_scanned == 0
        assert result.traces_flagged == 0

    def test_clean_traces_are_dropped_without_inspector(
        self, settings, monkeypatch,
    ):
        """Phase-1 catches everything; the LLM seam is never called."""
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.01)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {"observations": []},
        )
        inspector_calls: list = []
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: inspector_calls.append(kw)
            or TraceInspectResult(findings=[]),
        )
        result = run_trace_review_pass(session_id="sess-2")
        assert result.traces_scanned == 1
        assert result.traces_flagged == 0
        assert inspector_calls == []

    def test_flagged_trace_inspector_findings_become_drafts(
        self, settings, monkeypatch,
    ):
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=2.50)],  # cost outlier
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {"observations": []},
        )
        finding = TraceFinding(
            category="tool_error",
            symptom="run_command kept failing on uv lock",
            root_cause="sandbox has no network",
            proposed_solution="put uv sync in CI",
            confidence="high",
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(findings=[finding]),
        )

        result = run_trace_review_pass(session_id="sess-3")
        assert result.traces_flagged == 1
        assert len(result.drafts_created) == 1
        d = result.drafts_created[0]
        assert "tool_error" in d["title"]
        # Verify it landed on the board with the right source.
        svc = TicketService(settings, board_id="")
        all_tickets = svc.list()
        review_tickets = [t for t in all_tickets if t.source == SourceKind.TRACE_REVIEW]
        assert len(review_tickets) == 1
        body = svc.workspace(review_tickets[0]).read_description()
        assert "run_command kept failing" in body
        assert "put uv sync in CI" in body
        assert "Inspector confidence" in body

    def test_dedup_against_existing_open_trace_review_ticket(
        self, settings, monkeypatch,
    ):
        # Pre-seed an open trace-review ticket with the same normalized
        # title the inspector would produce.
        svc = TicketService(settings, board_id="")
        svc.create(
            title="trace-review: tool_error — run_command kept failing on uv lock",
            description="seed",
            source=SourceKind.TRACE_REVIEW,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=2.50)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {"observations": []},
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(findings=[TraceFinding(
                category="tool_error",
                symptom="run_command kept failing on uv lock",
                root_cause="x",
                proposed_solution="y",
            )]),
        )
        result = run_trace_review_pass(session_id="sess-4")
        assert result.traces_flagged == 1
        # No new draft — dedup against the existing open ticket fired.
        assert result.drafts_created == []

    def test_watermark_advances_on_each_run(self, settings, monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [],
        )
        before = _load_watermark(settings, "")
        assert before is None
        run_trace_review_pass(session_id="sess-5")
        after = _load_watermark(settings, "")
        assert after is not None
        assert after.tzinfo is not None  # UTC

    def test_inspector_error_logged_but_does_not_crash(
        self, settings, monkeypatch,
    ):
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=2.50)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {"observations": []},
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(
                error="trace too large for the model context",
            ),
        )
        result = run_trace_review_pass(session_id="sess-6")
        assert result.traces_flagged == 1
        assert result.drafts_created == []
