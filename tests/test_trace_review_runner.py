"""Tests for the trace-review runner.

Phase-1 classifier tests are pure (synthetic trace dicts + observation
lists). Phase-2 / orchestrator tests monkeypatch the Langfuse client
and the inspector seam so no LLM / network is involved.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from robotsix_mill.agents.trace_inspector import (
    TraceFinding,
    TraceInspectResult,
)
from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.trace_review_runner import (
    _Baselines,
    _classify_trace,
    _compute_baselines,
    _load_watermark,
    _median,
    _normalize,
    _save_watermark,
    run_trace_review_pass,
)


def _test_repo_config():
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    _reset_secrets()
    from robotsix_mill.core import db
    from robotsix_mill.config import _reset_repos_config

    db.reset_engine()
    _reset_repos_config()
    s = Settings(data_dir=str(tmp_path))
    # The runner reconstructs Settings() internally — patch its module-
    # level reference so it picks up the test's data_dir instead of the
    # YAML-defaulted one.
    monkeypatch.setattr(
        "robotsix_mill.trace_review_runner.Settings",
        lambda: s,
    )
    return s


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


class TestMedian:
    def test_median_empty(self):
        assert _median([]) == 0.0

    def test_median_odd_count(self):
        assert _median([1.0, 2.0, 3.0]) == 2.0

    def test_median_even_count(self):
        assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_median_robust_to_outliers(self):
        # Median is unaffected by a single huge outlier; mean would be.
        assert _median([1, 1, 1, 1, 100]) == 1.0


class TestComputeBaselines:
    def test_small_batch_returns_none(self, settings):
        traces = [_trace(id="t1", totalCost=0.5)]
        baselines = _compute_baselines(traces, {"t1": []}, settings)
        assert baselines.cost_threshold is None
        assert baselines.obs_threshold is None

    def test_baseline_is_median_times_multiplier(self, settings):
        traces = [
            _trace(id="t1", totalCost=0.10),
            _trace(id="t2", totalCost=0.20),
            _trace(id="t3", totalCost=0.30),
            _trace(id="t4", totalCost=0.40),
            _trace(id="t5", totalCost=0.50),
        ]
        # Each trace has a different observation count.
        obs_by_id = {
            "t1": [_obs("read_file") for _ in range(5)],
            "t2": [_obs("read_file") for _ in range(10)],
            "t3": [_obs("read_file") for _ in range(15)],
            "t4": [_obs("read_file") for _ in range(20)],
            "t5": [_obs("read_file") for _ in range(25)],
        }
        baselines = _compute_baselines(traces, obs_by_id, settings)
        # Median cost is 0.30, multiplier 3.0 → threshold 0.90.
        assert baselines.cost_median == pytest.approx(0.30)
        assert baselines.cost_threshold == pytest.approx(0.90)
        # Median observations is 15, multiplier 3.0 → threshold 45.
        assert baselines.obs_median == 15
        assert baselines.obs_threshold == 45

    def test_zero_median_suppresses_threshold(self, settings):
        # If every trace cost $0 the relative flag has no baseline.
        traces = [_trace(id=f"t{i}", totalCost=0.0) for i in range(5)]
        baselines = _compute_baselines(traces, {}, settings)
        assert baselines.cost_threshold is None


class TestClassifier:
    """``_classify_trace`` flags traces by deterministic criteria."""

    def _baselines(
        self, cost_threshold=None, obs_threshold=None, cost_median=None, obs_median=None
    ):
        return _Baselines(
            cost_threshold=cost_threshold,
            obs_threshold=obs_threshold,
            cost_median=cost_median,
            obs_median=obs_median,
        )

    def test_clean_trace_no_flags(self, settings):
        flags = _classify_trace(_trace(), settings, observations=[])
        assert flags.flags == []
        assert flags.flagged is False

    def test_cost_outlier_above_relative_threshold(self, settings):
        baselines = self._baselines(
            cost_threshold=1.00,
            cost_median=0.30,
            obs_threshold=45,
            obs_median=15,
        )
        flags = _classify_trace(
            _trace(totalCost=2.50),
            settings,
            observations=[],
            baselines=baselines,
        )
        assert any("cost_outlier" in f for f in flags.flags)
        # The flag string carries the median + multiplier for context.
        assert any("median" in f for f in flags.flags if "cost_outlier" in f)

    def test_cost_at_threshold_is_not_flagged(self, settings):
        baselines = self._baselines(
            cost_threshold=1.00,
            cost_median=0.30,
        )
        flags = _classify_trace(
            _trace(totalCost=1.00),
            settings,
            observations=[],
            baselines=baselines,
        )
        assert not any("cost_outlier" in f for f in flags.flags)

    def test_cost_threshold_none_suppresses_flag(self, settings):
        """Small-batch baselines have ``None`` thresholds; even a huge
        cost shouldn't flag as a relative outlier."""
        baselines = self._baselines(cost_threshold=None)
        flags = _classify_trace(
            _trace(totalCost=100.0),
            settings,
            observations=[],
            baselines=baselines,
        )
        assert not any("cost_outlier" in f for f in flags.flags)

    def test_observation_storm_relative(self, settings):
        baselines = self._baselines(
            obs_threshold=45,
            obs_median=15,
            cost_threshold=1.0,
            cost_median=0.3,
        )
        obs = [_obs("read_file") for _ in range(50)]
        flags = _classify_trace(
            _trace(),
            settings,
            observations=obs,
            baselines=baselines,
        )
        assert any("observation_storm" in f for f in flags.flags)

    def test_tool_error_in_output(self, settings):
        obs = [_obs("run_command", output="error: command failed")]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

    def test_traceback_in_status_message_is_a_tool_error(self, settings):
        obs = [
            _obs(
                "read_file",
                statusMessage="Traceback (most recent call last):\n  ...",
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

    def test_rejected_generation_marker(self, settings):
        obs = [
            _obs(
                "chat deepseek/deepseek-v4-pro",
                level="WARNING",
                statusMessage=(
                    "model produced 2636 output token(s) but no "
                    "gen_ai.output.messages was set — pydantic-ai likely "
                    "rejected the response"
                ),
            )
        ]
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
        observation-level flags don't fire; the cost flag still fires
        if the baseline thresholds are present."""
        baselines = self._baselines(
            cost_threshold=1.00,
            cost_median=0.30,
            obs_threshold=45,
            obs_median=15,
        )
        flags = _classify_trace(
            _trace(totalCost=2.0),
            settings,
            observations=None,
            baselines=baselines,
        )
        assert any("cost_outlier" in f for f in flags.flags)
        assert not any("tool_errors" in f for f in flags.flags)

    # -- incomplete_trace / restart_correlated --------------------------------

    def test_incomplete_trace_when_output_is_null(self, settings):
        """observations=None + output=None → incomplete_trace fires."""
        flags = _classify_trace(
            _trace(output=None),
            settings,
            observations=None,
        )
        assert "incomplete_trace" in flags.flags

    def test_incomplete_trace_when_output_is_empty_string(self, settings):
        """observations=None + output='' → incomplete_trace fires."""
        flags = _classify_trace(
            _trace(output="   "),
            settings,
            observations=None,
        )
        assert "incomplete_trace" in flags.flags

    def test_incomplete_trace_when_output_present_suppresses_flag(self, settings):
        """observations=None but output is non-empty → no incomplete_trace."""
        flags = _classify_trace(
            _trace(output="All done."),
            settings,
            observations=None,
        )
        assert "incomplete_trace" not in flags.flags

    def test_incomplete_trace_when_last_obs_is_tool_call(self, settings):
        """Last observation is a tool call (not chat) → incomplete_trace."""
        obs = [
            _obs("chat deepseek-v4"),
            _obs("read_file"),
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert "incomplete_trace" in flags.flags

    def test_no_incomplete_trace_when_last_obs_is_chat(self, settings):
        """Last observation is a chat generation → no incomplete_trace."""
        obs = [
            _obs("read_file"),
            _obs("chat deepseek-v4"),
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert "incomplete_trace" not in flags.flags

    def test_restart_correlated_within_window(self, settings):
        """Trace ends within the correlation window → restart_correlated."""
        started = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        trace_end = datetime(2026, 5, 30, 12, 0, 30, tzinfo=timezone.utc)
        trace = _trace(
            output=None,
            endTime=trace_end.isoformat(),
        )
        flags = _classify_trace(
            trace,
            settings,
            observations=None,
            started_at=started,
        )
        assert "incomplete_trace" in flags.flags
        assert "restart_correlated" in flags.flags

    def test_restart_correlated_outside_window(self, settings):
        """Trace ends outside the correlation window → no restart_correlated."""
        started = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        trace_end = datetime(2026, 5, 30, 12, 5, 0, tzinfo=timezone.utc)
        trace = _trace(
            output=None,
            endTime=trace_end.isoformat(),
        )
        flags = _classify_trace(
            trace,
            settings,
            observations=None,
            started_at=started,
        )
        assert "incomplete_trace" in flags.flags
        assert "restart_correlated" not in flags.flags

    def test_restart_correlated_with_observations_and_last_tool_call(self, settings):
        """restart_correlated fires via observation path when last obs is a
        tool call and timestamps align."""
        started = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        obs_end = datetime(2026, 5, 30, 12, 0, 45, tzinfo=timezone.utc)
        obs = [
            _obs("chat deepseek-v4", endTime="2026-05-30T12:00:10+00:00"),
            _obs("run_command", endTime=obs_end.isoformat()),
        ]
        flags = _classify_trace(
            _trace(),
            settings,
            observations=obs,
            started_at=started,
        )
        assert "incomplete_trace" in flags.flags
        assert "restart_correlated" in flags.flags

    def test_restart_correlated_not_fired_without_incomplete_trace(self, settings):
        """restart_correlated should not fire on its own — it's a sub-flag
        of incomplete_trace only."""
        started = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        trace_end = datetime(2026, 5, 30, 12, 0, 30, tzinfo=timezone.utc)
        # Last obs is a chat → no incomplete_trace → no restart_correlated.
        obs = [
            _obs("chat deepseek-v4", endTime=trace_end.isoformat()),
        ]
        flags = _classify_trace(
            _trace(),
            settings,
            observations=obs,
            started_at=started,
        )
        assert "incomplete_trace" not in flags.flags
        assert "restart_correlated" not in flags.flags


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
    assert _normalize("Trace-Review: Tool Errors!") == ("trace review tool errors")


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
        result = run_trace_review_pass(
            session_id="sess-1", repo_config=_test_repo_config()
        )
        assert result.drafts_created == []
        assert result.traces_scanned == 0
        assert result.traces_flagged == 0

    def test_clean_traces_are_dropped_without_inspector(
        self,
        settings,
        monkeypatch,
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
            lambda **kw: inspector_calls.append(kw) or TraceInspectResult(findings=[]),
        )
        result = run_trace_review_pass(
            session_id="sess-2", repo_config=_test_repo_config()
        )
        assert result.traces_scanned == 1
        assert result.traces_flagged == 0
        assert inspector_calls == []

    def test_flagged_trace_inspector_findings_become_drafts(
        self,
        settings,
        monkeypatch,
    ):
        # Use a binary flag (tool_errors) so the test doesn't depend on
        # the batch-relative baseline machinery — a single tool error in
        # a 1-trace batch is enough to flag.
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
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

        result = run_trace_review_pass(
            session_id="sess-3", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert len(result.drafts_created) == 1
        d = result.drafts_created[0]
        assert "tool_error" in d["title"]
        # Verify it landed on the board with the right source.
        svc = TicketService(settings, board_id="test-board")
        all_tickets = svc.list()
        review_tickets = [t for t in all_tickets if t.source == SourceKind.TRACE_REVIEW]
        assert len(review_tickets) == 1
        body = svc.workspace(review_tickets[0]).read_description()
        assert "run_command kept failing" in body
        assert "put uv sync in CI" in body
        assert "Inspector confidence" in body

    def test_dedup_against_existing_open_trace_review_ticket(
        self,
        settings,
        monkeypatch,
    ):
        # Pre-seed an open trace-review ticket with the same normalized
        # title the inspector would produce.
        svc = TicketService(settings, board_id="test-board")
        svc.create(
            title="trace-review: tool_error — run_command kept failing on uv lock",
            description="seed",
            source=SourceKind.TRACE_REVIEW,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(
                findings=[
                    TraceFinding(
                        category="tool_error",
                        symptom="run_command kept failing on uv lock",
                        root_cause="x",
                        proposed_solution="y",
                    )
                ]
            ),
        )
        result = run_trace_review_pass(
            session_id="sess-4", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        # No new draft — dedup against the existing open ticket fired.
        assert result.drafts_created == []

    def test_watermark_advances_on_each_run(self, settings, monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [],
        )
        before = _load_watermark(settings, "test-board")
        assert before is None
        run_trace_review_pass(session_id="sess-5", repo_config=_test_repo_config())
        after = _load_watermark(settings, "test-board")
        assert after is not None
        assert after.tzinfo is not None  # UTC

    def test_relative_cost_outlier_in_a_batch(
        self,
        settings,
        monkeypatch,
    ):
        """In a batch where the median cost is $0.10, a $1.00 trace
        is 10× the median and gets flagged via the relative criterion
        even though the absolute number isn't astronomical."""
        traces = [_trace(id=f"t{i}", totalCost=0.10) for i in range(4)] + [
            _trace(id="t5", totalCost=1.00)
        ]  # 10× median = outlier
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {"observations": []},
        )
        inspector_calls: list = []
        finding = TraceFinding(
            category="optimization",
            symptom="trace is expensive",
            root_cause="excessive sub-agent calls",
            proposed_solution="batch them",
            confidence="medium",
        )

        def fake_inspect(**kw):
            inspector_calls.append(kw)
            return TraceInspectResult(findings=[finding])

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            fake_inspect,
        )

        result = run_trace_review_pass(
            session_id="sess-rel", repo_config=_test_repo_config()
        )
        assert result.traces_scanned == 5
        # Only t5 (the $1 trace) is the outlier — the other four are
        # near the median.
        assert result.traces_flagged == 1
        assert len(inspector_calls) == 1
        assert len(result.drafts_created) == 1

    def test_inspector_error_logged_but_does_not_crash(
        self,
        settings,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(
                error="trace too large for the model context",
            ),
        )
        result = run_trace_review_pass(
            session_id="sess-6", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert result.drafts_created == []


class TestTargetRepoRouting:
    """``trace_review_target_repo_id`` overrides the destination board
    so findings from every repo's traces land on one place — typically
    the mill maintenance repo — instead of scattered across each
    application repo's board."""

    def test_target_repo_routes_drafts_to_configured_board(
        self,
        tmp_path,
        monkeypatch,
    ):
        # Two repos: source (where the trace came from) and target
        # (where the draft should land).
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        _reset_secrets()
        from robotsix_mill.core import db
        from robotsix_mill.config import (
            _reset_repos_config,
            get_repos_config,
            RepoConfig,
            ReposRegistry,
        )
        import robotsix_mill.config as _cfg

        db.reset_engine()
        _reset_repos_config()

        # Manually pin a repos registry so get_repos_config returns it.
        _cfg._repos_config = ReposRegistry(
            repos={
                "source-repo": RepoConfig(
                    repo_id="source-repo",
                    board_id="source-board",
                    langfuse_project_name="src",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                ),
                "mill-repo": RepoConfig(
                    repo_id="mill-repo",
                    board_id="mill-board",
                    langfuse_project_name="mill",
                    langfuse_public_key="pk2",
                    langfuse_secret_key="sk2",
                ),
            }
        )

        s = Settings(
            data_dir=str(tmp_path),
            trace_review_target_repo_id="mill-repo",
        )
        # The runner reconstructs Settings() internally — patch it so
        # the test's data_dir + target_repo_id propagate.
        monkeypatch.setattr(
            "robotsix_mill.trace_review_runner.Settings",
            lambda: s,
        )
        source_rc = get_repos_config().repos["source-repo"]

        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.list_all_traces_since",
            lambda *a, **kw: [_trace(id="t1", totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: failed"),
                ]
            },
        )
        finding = TraceFinding(
            category="tool_error",
            symptom="x",
            root_cause="y",
            proposed_solution="z",
            confidence="medium",
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(findings=[finding]),
        )

        result = run_trace_review_pass(
            session_id="sess",
            repo_config=source_rc,
        )
        assert len(result.drafts_created) == 1

        # Draft lives on the TARGET board, not the source board.
        target_svc = TicketService(s, board_id="mill-board")
        target_tickets = target_svc.list()
        assert len(target_tickets) == 1
        assert target_tickets[0].source == SourceKind.TRACE_REVIEW

        source_svc = TicketService(s, board_id="source-board")
        assert source_svc.list() == []

        # Clean up the pinned registry so other tests don't see it.
        _reset_repos_config()
