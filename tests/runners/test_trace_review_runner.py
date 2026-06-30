"""Tests for the trace-review runner.

Phase-1 classifier tests are pure (synthetic trace dicts + observation
lists). Phase-2 / orchestrator tests monkeypatch the Langfuse client
and the inspector seam so no LLM / network is involved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from robotsix_mill.agents.trace_inspector import (
    TraceFinding,
    TraceInspectResult,
)
from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.runners.trace_review_runner import (
    _Baselines,
    _classify_trace,
    _compute_baselines,
    _load_watermark,
    _median,
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
        "robotsix_mill.runners.trace_review_runner.Settings",
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
    # Model real Langfuse data: "chat …" names are GENERATION (LLM
    # model calls); everything else defaults to a SPAN (tool-call /
    # container observation). Callers may override ``type``.
    base = {
        "name": name,
        "type": "GENERATION" if name.startswith("chat ") else "SPAN",
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

    def test_cheap_high_volume_skips_cost_outlier(self, settings):
        """$0.92 across 2718 obs ($0.00034/obs) is NOT flagged."""
        baselines = self._baselines(
            cost_threshold=0.90,
            cost_median=0.30,
        )
        flags = _classify_trace(
            _trace(totalCost=0.92),
            settings,
            observations=[_obs("chat gpt-4") for _ in range(2718)],
            baselines=baselines,
        )
        assert not any("cost_outlier" in f for f in flags.flags)

    def test_expensive_low_volume_still_flagged(self, settings):
        """$0.92 across 50 obs ($0.0184/obs) IS flagged."""
        baselines = self._baselines(
            cost_threshold=0.90,
            cost_median=0.30,
        )
        flags = _classify_trace(
            _trace(totalCost=0.92),
            settings,
            observations=[_obs("chat gpt-4") for _ in range(50)],
            baselines=baselines,
        )
        assert any("cost_outlier" in f for f in flags.flags)

    def test_zero_observations_falls_through_to_cost_check(self, settings):
        """0 observations skips per-obs guard (division by zero), normal check applies."""
        baselines = self._baselines(
            cost_threshold=0.90,
            cost_median=0.30,
        )
        flags = _classify_trace(
            _trace(totalCost=2.50),
            settings,
            observations=[],
            baselines=baselines,
        )
        assert any("cost_outlier" in f for f in flags.flags)

    def test_zero_cost_skips_both_guard_and_check(self, settings):
        """totalCost=0 means per_obs_cost=0 < threshold, so guard skips
        the cost_outlier check entirely. Even if cost_threshold is
        non-None and 0 > threshold would be false anyway, this
        confirms no exception is raised."""
        baselines = self._baselines(
            cost_threshold=0.01,
            cost_median=0.0,
        )
        flags = _classify_trace(
            _trace(totalCost=0.0),
            settings,
            observations=[_obs("read_file") for _ in range(10)],
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

    def test_run_command_exit_zero_skips_error_regex(self, settings):
        """A successful run_command (exit=0) whose output happens to
        contain 'error:' (e.g. grep matching test source lines) must
        NOT produce a false-positive tool_errors flag."""
        obs = [
            _obs(
                "run_command",
                output=(
                    "exit=0\n"
                    "tests/runners/test_trace_review_runner.py:227: "
                    'obs = [_obs("run_command", output="error: command failed")]\n'
                ),
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    def test_run_command_success_message_skips_error_regex(self, settings):
        """The success-with-no-output sentence also skips the regex."""
        obs = [
            _obs(
                "run_command",
                output="Your command ran successfully and did not produce any output.",
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    def test_traceback_in_status_message_is_a_tool_error(self, settings):
        obs = [
            _obs(
                "read_file",
                statusMessage="Traceback (most recent call last):\n  ...",
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

    def test_explore_storm(self, settings):
        obs = [_obs("explore run") for _ in range(6)]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("explore_storm" in f for f in flags.flags)

    def test_ask_user_loop(self, settings):
        obs = [_obs("ask_user"), _obs("ask_user")]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("ask_user_loop" in f for f in flags.flags)

    def test_ask_user_pause_sentinel_is_not_a_tool_error(self, settings):
        """The __ASK_USER_PAUSE__ sentinel is the expected happy-path
        return value — it must not be flagged as a tool error."""
        obs = [_obs("ask_user", output="__ASK_USER_PAUSE__")]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    def test_ask_user_error_output_skipped_by_tool_name(self, settings):
        """ask_user observations are excluded from the error scan by tool
        name — even an output containing an error token (e.g. "Error: no
        active ticket session") must NOT produce a tool_errors flag."""
        obs = [_obs("ask_user", output="Error: no active ticket session")]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    def test_ask_user_status_message_error_skipped_by_tool_name(self, settings):
        """An ask_user observation whose statusMessage contains an error
        token must still be skipped (the carve-out is by tool name, not
        by output field)."""
        obs = [
            _obs(
                "ask_user",
                output="__ASK_USER_PAUSE__",
                statusMessage="Traceback (most recent call last):\n  ...",
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    def test_non_exempt_tool_still_flags_error(self, settings):
        """Guard: a non-exempt tool (e.g. read_file) with an error token
        in its output still produces a tool_errors flag — the ask_user
        carve-out didn't disable error detection generally."""
        obs = [_obs("read_file", output="Traceback (most recent call last)")]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

    # -- read_file path-not-found carve-out -----------------------------------

    def test_read_file_path_not_found_skips_error_regex(self, settings):
        """A read_file observation whose output is the benign
        path-not-found guidance (begins "error:" but is user-friendly
        navigation, not a failure) must NOT produce a tool_errors flag."""
        obs = [
            _obs(
                "read_file",
                output=(
                    "error: 'src/robotsix_mill/sandbox.py' does not exist — "
                    "try list_dir('src/robotsix_mill') to find the correct path"
                ),
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    def test_read_file_traceback_still_flags_error(self, settings):
        """A read_file observation whose output contains a real Traceback
        (indicating a crash, not a path-not-found guidance) MUST still
        produce a tool_errors flag — the carve-out keys on the specific
        "does not exist — try" substring, not on the tool name alone."""
        obs = [
            _obs(
                "read_file",
                output=(
                    "Traceback (most recent call last):\n"
                    "  File ..., line ..., in read_file\n"
                    "ValueError: ..."
                ),
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

    # -- ticket_description carve-out -----------------------------------------

    def test_ticket_description_skips_error_regex(self, settings):
        """A ticket_description observation whose output contains an error
        token (e.g. "UsageLimitExceeded") that originates from the body of
        an existing ticket being read — NOT from a tool failure — must NOT
        produce a tool_errors flag."""
        obs = [
            _obs(
                "ticket_description",
                output=(
                    "## Symptom\n\n"
                    "The tool_errors flag fired because "
                    "UsageLimitExceeded appeared in the output...\n\n"
                ),
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    # -- run_command empty-output carve-out (defensive) -----------------------

    def test_run_command_empty_output_skips_error_regex(self, settings):
        """A run_command observation whose output is the benign
        empty-output failure message ("The command failed with exit code
        1 and produced no output.") must NOT produce a tool_errors flag.

        NOTE: as of writing this string does not match _TOOL_ERR_PATTERNS,
        so the carve-out is purely defensive.  The test still validates
        that the classifier bypasses the regex for this message, keeping
        the classifier robust if patterns change in the future.
        """
        obs = [
            _obs(
                "run_command",
                output="The command failed with exit code 1 and produced no output.",
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert not any("tool_errors" in f for f in flags.flags)

    def test_run_command_real_stderr_still_flags_error(self, settings):
        """A run_command observation whose output is of the form
        exit=<rc>\\n<stderr> with a non-zero exit code and real error
        content in stderr MUST still produce a tool_errors flag.

        The carve-out only suppresses the empty-output failure message;
        it must not blanket-suppress all non-zero exit returns.
        """
        obs = [
            _obs(
                "run_command",
                output="exit=1\nsrc/app.py:12: error: expected int, got str",
            )
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert any("tool_errors" in f for f in flags.flags)

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

    def test_incomplete_trace_when_last_gen_is_not_chat(self, settings):
        """Latest GENERATION is not a "chat " completion → incomplete_trace.

        A trailing tool-call SPAN does not count; the trace is incomplete
        because the most recent GENERATION never produced a final answer.
        """
        obs = [
            _obs("chat deepseek-v4", endTime="2026-05-30T12:00:10+00:00"),
            _obs(
                "summary",
                type="GENERATION",
                endTime="2026-05-30T12:00:20+00:00",
            ),
            _obs("read_file", endTime="2026-05-30T12:00:30+00:00"),
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert "incomplete_trace" in flags.flags

    def test_no_incomplete_trace_when_last_gen_is_chat(self, settings):
        """Latest GENERATION is a chat completion → no incomplete_trace,
        even with a trailing tool-call SPAN."""
        obs = [
            _obs("read_file", endTime="2026-05-30T12:00:05+00:00"),
            _obs("chat deepseek-v4", endTime="2026-05-30T12:00:10+00:00"),
        ]
        flags = _classify_trace(_trace(), settings, observations=obs)
        assert "incomplete_trace" not in flags.flags

    def test_incomplete_trace_suppressed_for_trailing_tool_span(self, settings):
        """Reported false positive: a "chat …" GENERATION plus a root/agent
        SPAN whose endTime sorts strictly later must NOT flag incomplete."""
        obs = [
            _obs("chat haiku", endTime="2026-06-12T11:37:21.879+00:00"),
            _obs(
                "retrospect",
                type="SPAN",
                endTime="2026-06-12T11:37:21.983+00:00",
            ),
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

    def test_restart_correlated_with_observations_and_incomplete_gen(self, settings):
        """restart_correlated fires via observation path when the latest
        GENERATION is incomplete (non-"chat ") and timestamps align.

        The trailing ``run_command`` SPAN supplies the latest endTime for
        restart correlation (via the unfiltered ``_extract_trace_end_time``)
        but does not itself drive ``incomplete_trace``.
        """
        started = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        obs_end = datetime(2026, 5, 30, 12, 0, 45, tzinfo=timezone.utc)
        obs = [
            _obs("chat deepseek-v4", endTime="2026-05-30T12:00:10+00:00"),
            _obs(
                "summary",
                type="GENERATION",
                endTime="2026-05-30T12:00:20+00:00",
            ),
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

    # -- refine_cost_alert -------------------------------------------------

    def test_refine_cost_alert_above_threshold(self, settings):
        """A refine trace exceeding the absolute threshold is flagged."""
        settings.refine_cost_alert_threshold = 0.50
        obs = [_obs("chat gpt-4")]
        flags = _classify_trace(
            _trace(name="refine", totalCost=2.82),
            settings,
            observations=obs,
        )
        assert any("refine_cost_alert" in f for f in flags.flags)
        assert "$2.82" in " ".join(flags.flags)
        assert "$0.50" in " ".join(flags.flags)

    def test_refine_cost_alert_below_threshold_not_flagged(self, settings):
        """A refine trace under the threshold is not flagged."""
        settings.refine_cost_alert_threshold = 0.50
        obs = [_obs("chat gpt-4")]
        flags = _classify_trace(
            _trace(name="refine", totalCost=0.12),
            settings,
            observations=obs,
        )
        assert not any("refine_cost_alert" in f for f in flags.flags)

    def test_refine_cost_alert_non_refine_trace_ignored(self, settings):
        """Only traces named 'refine' are checked — other names are ignored."""
        settings.refine_cost_alert_threshold = 0.50
        obs = [_obs("chat gpt-4")]
        flags = _classify_trace(
            _trace(name="implement", totalCost=5.00),
            settings,
            observations=obs,
        )
        assert not any("refine_cost_alert" in f for f in flags.flags)

    def test_refine_cost_alert_zero_threshold_disables(self, settings):
        """A zero threshold disables the alert entirely."""
        settings.refine_cost_alert_threshold = 0.0
        obs = [_obs("chat gpt-4")]
        flags = _classify_trace(
            _trace(name="refine", totalCost=100.0),
            settings,
            observations=obs,
        )
        assert not any("refine_cost_alert" in f for f in flags.flags)

    def test_refine_cost_alert_coexists_with_cost_outlier(self, settings):
        """Both flags can fire on the same trace."""
        settings.refine_cost_alert_threshold = 0.50
        baselines = self._baselines(
            cost_threshold=1.00,
            cost_median=0.30,
        )
        obs = [_obs("chat gpt-4")]
        flags = _classify_trace(
            _trace(name="refine", totalCost=2.82),
            settings,
            observations=obs,
            baselines=baselines,
        )
        assert any("refine_cost_alert" in f for f in flags.flags)
        assert any("cost_outlier" in f for f in flags.flags)


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
# Orchestrator (run_trace_review_pass)
# ---------------------------------------------------------------------------


class TestRunTraceReviewPass:
    """End-to-end orchestrator tests with seams monkeypatched."""

    def test_no_traces_returns_empty_result(self, settings, monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
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
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.01)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
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
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
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

    def test_classifier_flags_forwarded_to_inspector(
        self,
        settings,
        monkeypatch,
    ):
        # A single tool error flags the trace (binary flag, no baseline
        # needed). Capture the kwargs the runner forwards to the
        # inspector and assert classifier_flags == the trace's flags.
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        captured: dict = {}

        def _capture(**kw):
            captured.update(kw)
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            _capture,
        )

        result = run_trace_review_pass(
            session_id="sess-flags", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert "classifier_flags" in captured
        # Forwarded verbatim from the classifier's _TraceFlags.flags. The
        # lone observation is a tool-call SPAN (no GENERATION), so only the
        # tool-error flag fires — the incomplete-trace tail flag does not.
        assert captured["classifier_flags"] == ["tool_errors (1)"]

    def test_dedup_against_existing_open_trace_review_ticket(
        self,
        settings,
        monkeypatch,
    ):
        # Pre-seed an open trace-review ticket with the same normalized
        # title the inspector would produce.
        svc = TicketService(settings, board_id="test-board")
        svc.create(
            title="tool_error — run_command kept failing on uv lock",
            description="seed",
            source=SourceKind.TRACE_REVIEW,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
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
            "robotsix_mill.langfuse.client.list_all_traces_since",
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
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
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
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
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

    # -- inspector cap (trace_review_max_inspections_per_run) --------

    def test_inspector_cap_top_n_by_cost(self, settings, monkeypatch):
        """When flagged traces exceed the inspector cap, only the
        highest-cost flagged traces are inspected; lower-cost traces
        are skipped.  flagged_count in the result still reports the
        total pre-cap count."""
        import json as _json

        # 6 traces all with tool errors (binary flag) so all are flagged.
        # Distinct costs so we can verify top-N selection.
        traces = [
            _trace(id="t-lowest", totalCost=0.01),
            _trace(id="t-low", totalCost=0.05),
            _trace(id="t-mid", totalCost=0.20),
            _trace(id="t-high", totalCost=0.50),
            _trace(id="t-higher", totalCost=0.80),
            _trace(id="t-highest", totalCost=1.20),
        ]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )

        def _detail(_s, trace_id, **kw):
            return {
                "id": trace_id,
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ],
            }

        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            _detail,
        )

        # Cap at 3 — only the three most expensive should be inspected.
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=3,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )

        inspected_ids: list[str] = []

        def _capture(**kw):
            # The inspector receives trace_data (JSON string), not trace_id
            # directly.  Parse the id from the JSON blob.
            trace_data = _json.loads(kw["trace_data"])
            inspected_ids.append(trace_data.get("id", ""))
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            _capture,
        )

        result = run_trace_review_pass(
            session_id="sess-cap-topn", repo_config=_test_repo_config()
        )
        # All 6 flagged (pre-cap count), only 3 inspected.
        assert result.traces_flagged == 6
        assert len(inspected_ids) == 3
        # Highest-cost traces by descending cost: t-highest ($1.20),
        # t-higher ($0.80), t-high ($0.50).
        assert set(inspected_ids) == {"t-highest", "t-higher", "t-high"}

    def test_inspector_cap_zero_is_unlimited(self, settings, monkeypatch):
        """cap=0 → all flagged traces are inspected (unbounded)."""
        import json as _json

        traces = [
            _trace(id="t1", totalCost=0.01),
            _trace(id="t2", totalCost=0.01),
            _trace(id="t3", totalCost=0.01),
        ]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=0,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        inspected_ids: list[str] = []

        def _capture(**kw):
            trace_data = _json.loads(kw["trace_data"])
            inspected_ids.append(trace_data.get("id", ""))
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            _capture,
        )

        result = run_trace_review_pass(
            session_id="sess-cap-zero", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 3
        assert len(inspected_ids) == 3

    def test_inspector_cap_no_truncation_when_flagged_le_cap(
        self, settings, monkeypatch
    ):
        """When flagged count ≤ cap, every flagged trace is inspected
        — no truncation (regression test)."""
        import json as _json

        traces = [
            _trace(id="t1", totalCost=0.01),
            _trace(id="t2", totalCost=0.02),
        ]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=5,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        inspected_ids: list[str] = []

        def _capture(**kw):
            trace_data = _json.loads(kw["trace_data"])
            inspected_ids.append(trace_data.get("id", ""))
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            _capture,
        )

        result = run_trace_review_pass(
            session_id="sess-cap-nocut", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 2
        assert len(inspected_ids) == 2

    def test_inspector_cap_logs_skipped_count(self, settings, monkeypatch, caplog):
        """A log.info message reports how many flagged traces were
        skipped due to the inspector cap."""
        traces = [_trace(id=f"t{i}", totalCost=0.01) for i in range(10)]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=3,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(),
        )

        import logging

        caplog.set_level(logging.INFO, logger="robotsix_mill.trace_review")
        run_trace_review_pass(
            session_id="sess-cap-log", repo_config=_test_repo_config()
        )
        # Expect a log line like: "inspection cap of 3 reached — 7 flagged
        # traces not inspected this run"
        cap_logs = [
            r.getMessage()
            for r in caplog.records
            if "inspection cap of 3 reached" in r.getMessage()
        ]
        assert len(cap_logs) == 1
        assert "7 flagged traces not inspected this run" in cap_logs[0]

    def test_inspector_cap_watermark_still_advances(self, settings, monkeypatch):
        """Watermark advances past all processed traces even when some
        are skipped from inspection — they don't stall convergence."""
        traces = [_trace(id=f"t{i}", totalCost=0.01) for i in range(6)]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=2,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(),
        )

        rc = _test_repo_config()
        before = _load_watermark(capped, rc.board_id)
        assert before is None

        run_trace_review_pass(session_id="sess-cap-wm", repo_config=rc)

        after = _load_watermark(capped, rc.board_id)
        assert after is not None
        assert after.tzinfo is not None  # UTC


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
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: s,
        )
        source_rc = get_repos_config().repos["source-repo"]

        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(id="t1", totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
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


class TestPreFilingDedup:
    """Pre-filing dedup helper ``_find_prior_matching_ticket`` and its
    integration into ``run_trace_review_pass``."""

    _TARGET_PATH = "src/robotsix_mill/foo.py"
    _FINDING_SYMPTOM = "wrapper at src/robotsix_mill/foo.py raised on tool error"

    def _patch_seams(self, monkeypatch, finding):
        """Wire up langfuse + inspector seams so a single flagged trace
        produces the given finding."""
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(findings=[finding]),
        )

    def _finding(self, target_files=None, symptom=None):
        return TraceFinding(
            category="tool_error",
            symptom=symptom if symptom is not None else self._FINDING_SYMPTOM,
            root_cause="x",
            proposed_solution=f"fix the wrapper in {self._TARGET_PATH}",
            target_files=(
                target_files if target_files is not None else [self._TARGET_PATH]
            ),
            confidence="medium",
        )

    def _seed_prior_ticket(
        self,
        settings,
        title,
        body,
    ):
        svc = TicketService(settings, board_id="test-board")
        ticket = svc.create(
            title=title,
            description=body,
            source=SourceKind.TRACE_REVIEW,
        )
        return svc, ticket

    def test_skip_when_prior_ticket_is_done(self, settings, monkeypatch, caplog):
        svc, ticket = self._seed_prior_ticket(
            settings,
            title="tool_error — earlier wrapper bug",
            body=f"prior body mentioning {self._TARGET_PATH}",
        )
        from robotsix_mill.core.states import State

        svc.transition(ticket.id, State.DONE, note="merged")

        self._patch_seams(monkeypatch, self._finding())

        import logging

        caplog.set_level(logging.INFO, logger="robotsix_mill.trace_review")
        result = run_trace_review_pass(
            session_id="sess-dedup-done",
            repo_config=_test_repo_config(),
        )
        assert result.drafts_created == []
        # Prior ticket id mentioned in the skip log.
        assert any(ticket.id in rec.getMessage() for rec in caplog.records)

    def test_skip_when_prior_ticket_is_draft(self, settings, monkeypatch):
        self._seed_prior_ticket(
            settings,
            title="tool_error — earlier wrapper bug",
            body=f"prior body mentioning {self._TARGET_PATH}",
        )
        # Leave it in DRAFT (default state after service.create).
        self._patch_seams(monkeypatch, self._finding())
        result = run_trace_review_pass(
            session_id="sess-dedup-draft",
            repo_config=_test_repo_config(),
        )
        assert result.drafts_created == []

    def test_file_when_no_prior_match(self, settings, monkeypatch):
        # No seed at all.
        self._patch_seams(monkeypatch, self._finding())
        result = run_trace_review_pass(
            session_id="sess-no-prior",
            repo_config=_test_repo_config(),
        )
        assert len(result.drafts_created) == 1

    def test_file_when_only_match_is_closed_declined(self, settings, monkeypatch):
        svc, ticket = self._seed_prior_ticket(
            settings,
            title="tool_error — earlier wrapper bug",
            body=f"prior body mentioning {self._TARGET_PATH}",
        )
        from robotsix_mill.core.states import State

        # DRAFT → CLOSED directly (no DONE in history) = declined draft.
        svc.transition(ticket.id, State.CLOSED, note="declined as noise")

        self._patch_seams(monkeypatch, self._finding())
        result = run_trace_review_pass(
            session_id="sess-closed-declined",
            repo_config=_test_repo_config(),
        )
        # A fresh draft IS filed — the closed/declined ticket should not suppress.
        assert len(result.drafts_created) == 1

    def test_skip_when_match_is_closed_after_done(self, settings, monkeypatch):
        svc, ticket = self._seed_prior_ticket(
            settings,
            title="tool_error — earlier wrapper bug",
            body=f"prior body mentioning {self._TARGET_PATH}",
        )
        from robotsix_mill.core.states import State

        # DRAFT → DONE → CLOSED (merged then closed).
        svc.transition(ticket.id, State.DONE, note="merged")
        svc.transition(ticket.id, State.CLOSED, note="retrospected")

        self._patch_seams(monkeypatch, self._finding())
        result = run_trace_review_pass(
            session_id="sess-closed-done",
            repo_config=_test_repo_config(),
        )
        assert result.drafts_created == []

    def test_recency_window_excludes_older_matches(self, settings, monkeypatch):
        from datetime import timedelta

        svc, ticket = self._seed_prior_ticket(
            settings,
            title="tool_error — earlier wrapper bug",
            body=f"prior body mentioning {self._TARGET_PATH}",
        )
        from robotsix_mill.core.states import State

        svc.transition(ticket.id, State.DONE, note="merged")

        # Backdate the ticket's created_at to 30 days ago — well outside
        # the default 7-day window.
        from robotsix_mill.core.db import session as db_session
        from robotsix_mill.core.models import Ticket as _Ticket
        from sqlmodel import select as _select

        old = datetime.now(timezone.utc) - timedelta(days=30)
        with db_session(settings, "test-board") as s:
            row = s.exec(_select(_Ticket).where(_Ticket.id == ticket.id)).first()
            assert row is not None
            row.created_at = old
            s.add(row)
            s.commit()

        self._patch_seams(monkeypatch, self._finding())
        result = run_trace_review_pass(
            session_id="sess-recency",
            repo_config=_test_repo_config(),
        )
        # Older-than-window match is ignored; a fresh draft is filed.
        assert len(result.drafts_created) == 1

    def test_symptom_fingerprint_matches_without_file_path(self, settings, monkeypatch):
        # Seed a ticket whose title carries the symptom fingerprint, but
        # whose body does NOT mention the candidate's target file (n/a
        # here since target_files=[]).
        self._seed_prior_ticket(
            settings,
            title=("tool_error — claude code returned an error result success"),
            body="unrelated body that does not name any code locus",
        )
        finding = self._finding(
            target_files=[],
            symptom="Claude Code returned an error result: success",
        )
        self._patch_seams(monkeypatch, finding)
        result = run_trace_review_pass(
            session_id="sess-fingerprint",
            repo_config=_test_repo_config(),
        )
        # The fingerprint substring match suppresses the new draft.
        assert result.drafts_created == []


class TestPathExistenceGuard:
    """Deterministic guard that suppresses findings whose cited source
    paths all fail to resolve on HEAD (the documented false-positive
    mode), without over-blocking findings that cite real or no paths."""

    # tests/runners/test_trace_review_runner.py → repo root.
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    _MISSING_PATH = (
        "src/robotsix_llmio/agents/claude_sdk/ticket_pipeline/stages/dedup.py"
    )
    _EXISTING_PATH = "src/robotsix_mill/agents/dedup.py"

    def _patch_seams(self, monkeypatch, finding):
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(totalCost=0.10)],
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(findings=[finding]),
        )

    def _resolve_to_repo_root(self, monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner._resolve_target_repo_dir",
            lambda *a, **kw: self._REPO_ROOT,
        )

    def test_finding_with_only_missing_path_is_suppressed(self, settings, monkeypatch):
        self._resolve_to_repo_root(monkeypatch)
        finding = TraceFinding(
            category="optimization",
            symptom="dedup stage is slow",
            root_cause=f"the loop in {self._MISSING_PATH} re-scans every ticket",
            proposed_solution=f"add an early-return guard in {self._MISSING_PATH}",
            target_files=[self._MISSING_PATH],
            confidence="medium",
        )
        self._patch_seams(monkeypatch, finding)
        result = run_trace_review_pass(
            session_id="sess-missing-path",
            repo_config=_test_repo_config(),
        )
        assert result.traces_flagged == 1
        # No draft — the only cited path does not exist on HEAD.
        assert result.drafts_created == []

    def test_finding_with_existing_path_is_filed(self, settings, monkeypatch):
        self._resolve_to_repo_root(monkeypatch)
        finding = TraceFinding(
            category="optimization",
            symptom="dedup stage is slow",
            root_cause=f"the loop in {self._EXISTING_PATH} re-scans every ticket",
            proposed_solution=f"add an early-return guard in {self._EXISTING_PATH}",
            target_files=[self._EXISTING_PATH],
            confidence="medium",
        )
        self._patch_seams(monkeypatch, finding)
        result = run_trace_review_pass(
            session_id="sess-existing-path",
            repo_config=_test_repo_config(),
        )
        # The cited path exists → guard does not over-block.
        assert len(result.drafts_created) == 1

    def test_finding_with_no_cited_paths_is_filed(self, settings, monkeypatch):
        self._resolve_to_repo_root(monkeypatch)
        finding = TraceFinding(
            category="agent_limitation",
            symptom="agent looped without converging",
            root_cause="the model retried the same approach repeatedly",
            proposed_solution="add a convergence check to break the loop",
            target_files=[],
            confidence="medium",
        )
        self._patch_seams(monkeypatch, finding)
        result = run_trace_review_pass(
            session_id="sess-no-paths",
            repo_config=_test_repo_config(),
        )
        # Guard is inert when no concrete path is cited.
        assert len(result.drafts_created) == 1

    def test_guard_is_noop_when_repo_dir_unresolved(self, settings, monkeypatch):
        # Resolver returns None (no checkout on disk) → guard no-op,
        # so even a finding citing a non-existent path is filed exactly
        # as before this guard existed.
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner._resolve_target_repo_dir",
            lambda *a, **kw: None,
        )
        finding = TraceFinding(
            category="optimization",
            symptom="dedup stage is slow",
            root_cause=f"the loop in {self._MISSING_PATH} re-scans every ticket",
            proposed_solution=f"add an early-return guard in {self._MISSING_PATH}",
            target_files=[self._MISSING_PATH],
            confidence="medium",
        )
        self._patch_seams(monkeypatch, finding)
        result = run_trace_review_pass(
            session_id="sess-unresolved",
            repo_config=_test_repo_config(),
        )
        assert len(result.drafts_created) == 1


class TestTraceReviewMemoryCap:
    """The per-run trace cap bounds memory + advances the watermark
    incrementally (regression for the unbounded-window memory explosion)."""

    def test_window_capped_processes_newest_and_advances_to_now(
        self, settings, monkeypatch
    ):
        rc = _test_repo_config()
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_traces_per_run=3,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        # 6 traces t0..t5 (oldest→newest by timestamp), supplied newest-first
        # to exercise the defensive sort when a backend over-returns.
        traces = [
            _trace(
                id=f"t{i}",
                timestamp=f"2026-06-19T0{i}:00:00+00:00",
                totalCost=0.01,
            )
            for i in range(6)
        ]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: list(reversed(traces)),
        )
        detail_ids: list[str] = []

        def _detail(_settings, trace_id, **kw):
            detail_ids.append(trace_id)
            return {"observations": []}

        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            _detail,
        )

        before = datetime.now(timezone.utc)
        run_trace_review_pass(session_id="sess-cap", repo_config=rc)

        # Only the NEWEST 3 traces had their detail loaded — the older backlog
        # (t0–t2) is skipped, not queued for a later run.
        assert sorted(detail_ids) == ["t3", "t4", "t5"]
        # Watermark advanced to ~now (the older backlog is dropped, not
        # incrementally drained), so the next run only sees newer traces.
        wm = _load_watermark(capped, rc.board_id)
        assert wm is not None
        assert wm >= before
        assert wm.isoformat() != "2026-06-19T02:00:00+00:00"

    def test_window_under_cap_fetches_all(self, settings, monkeypatch):
        rc = _test_repo_config()
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: [_trace(id="only", totalCost=0.01)],
        )
        detail_ids: list[str] = []
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda _s, tid, **kw: detail_ids.append(tid) or {"observations": []},
        )
        run_trace_review_pass(session_id="sess-small", repo_config=rc)
        assert detail_ids == ["only"]


class TestInspectionCapAndCostOrdering:
    """``trace_review_max_inspections_per_run`` directly bounds LLM
    inspector calls (AC2) and flagged traces are inspected in
    descending cost order so the limited budget targets the most
    expensive / anomalous traces first (AC3)."""

    def test_inspection_cap_respected(self, settings, monkeypatch):
        """Given N>cap flagged traces, the inspector is called at most
        ``trace_review_max_inspections_per_run`` times (AC2)."""
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=2,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        # 5 traces, all flagged via the tool-errors binary flag so no
        # baseline needed.
        traces = [_trace(id=f"t{i}", totalCost=float(i)) for i in range(5)]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        inspector_calls: list[dict] = []

        def fake_inspect(**kw):
            inspector_calls.append(kw)
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            fake_inspect,
        )

        rc = _test_repo_config()
        result = run_trace_review_pass(session_id="sess-cap2", repo_config=rc)
        assert result.traces_flagged == 5
        # Cap 2 → at most 2 inspections.
        assert len(inspector_calls) == 2

    def test_inspection_cap_zero_disables(self, settings, monkeypatch):
        """``trace_review_max_inspections_per_run=0`` disables the cap —
        all flagged traces are inspected."""
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=0,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        traces = [_trace(id=f"t{i}", totalCost=float(i)) for i in range(5)]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        inspector_calls: list[dict] = []

        def fake_inspect(**kw):
            inspector_calls.append(kw)
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            fake_inspect,
        )

        rc = _test_repo_config()
        result = run_trace_review_pass(session_id="sess-cap0", repo_config=rc)
        assert result.traces_flagged == 5
        # Zero disables → all 5 inspected.
        assert len(inspector_calls) == 5

    def test_flagged_traces_inspected_in_cost_desc_order(self, settings, monkeypatch):
        """Flagged traces are sorted by totalCost descending before
        inspection, so the highest-cost ones get the limited LLM budget
        (AC3)."""
        capped = Settings(
            data_dir=settings.data_dir,
            trace_review_max_inspections_per_run=1,
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.trace_review_runner.Settings",
            lambda: capped,
        )
        # 3 flagged traces with costs 0.01, 0.90, 0.05 — the cap=1
        # inspection should target the $0.90 trace.
        traces = [
            _trace(id="t-cheap", totalCost=0.01),
            _trace(id="t-expensive", totalCost=0.90),
            _trace(id="t-mid", totalCost=0.05),
        ]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: {
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
        )
        inspector_calls: list[dict] = []

        def fake_inspect(**kw):
            inspector_calls.append(kw)
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            fake_inspect,
        )

        rc = _test_repo_config()
        result = run_trace_review_pass(session_id="sess-order", repo_config=rc)
        assert result.traces_flagged == 3
        # Cap 1 → only the most expensive trace ($0.90) is inspected.
        assert len(inspector_calls) == 1
        # The inspected trace is the cost outlier: its classifier_flags
        # carry the cost_outlier marker with the $0.90 value.
        flags = inspector_calls[0]["classifier_flags"]
        assert any("cost_outlier" in f and "$0.90" in f for f in flags)


# ---------------------------------------------------------------------------
# Noise suppression (self-referential skip, per-obs-cost, REQUIRES_HUMAN_REVIEW)
# ---------------------------------------------------------------------------


class TestNoiseSuppression:
    """Acceptance tests for the three noise-suppression guards:

    1. Self-referential trace skip: a flagged trace named
       ``trace_inspector`` is skipped before the LLM inspector runs.
    2. Per-observation cost-outlier suppression: ``optimization``
       findings whose symptom matches the token-count regex produce no
       draft.
    3. ``REQUIRES_HUMAN_REVIEW:`` suppression: ``optimization``
       findings with a non-concrete proposed_solution prefix produce no
       draft.
    Plus two regression guards confirming that legitimate tool_error
    and concrete optimization findings still file.
    """

    @staticmethod
    def _patch_seams(monkeypatch, traces, detail_dict, inspector_results):
        """Wire up the Langfuse client + inspector seams for a single run.

        *traces*: list of trace dicts for ``list_all_traces_since``.
        *detail_dict*: dict returned by ``fetch_trace_detail`` for every
            trace.
        *inspector_results*: list of ``TraceInspectResult`` returned by
            ``run_trace_inspector`` in call order.
        """
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda *a, **kw: detail_dict,
        )

        calls: list[dict] = []

        def _fake_inspector(**kw):
            idx = len(calls)
            calls.append(kw)
            if idx < len(inspector_results):
                return inspector_results[idx]
            return TraceInspectResult()

        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            _fake_inspector,
        )
        return calls  # inspector-call log for assertions

    # -- self-referential ---------------------------------------------------

    def test_self_referential_trace_skipped(self, settings, monkeypatch):
        """AC1: a flagged trace with name="trace_inspector" causes the
        inspector to be skipped and zero drafts filed."""
        inspector_calls = self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-self", name="trace_inspector", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="tool_error",
                            symptom="The trace inspector agent … hit a UsageLimitExceeded …",
                            root_cause="budget",
                            proposed_solution="increase budget",
                            confidence="low",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-self-ref", repo_config=_test_repo_config()
        )
        # The trace WAS flagged (tool error in observations) …
        assert result.traces_flagged == 1
        # … but the inspector was never called (continue before the call).
        assert len(inspector_calls) == 0
        # … and no draft was created.
        assert result.drafts_created == []

    def test_non_inspector_trace_not_skipped(self, settings, monkeypatch):
        """Guard: a flagged trace whose name is NOT in the self-referential
        set still proceeds to inspection."""
        inspector_calls = self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-ok", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="tool_error",
                            symptom="run_command failed",
                            root_cause="x",
                            proposed_solution="y",
                            confidence="high",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-not-self", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert len(inspector_calls) == 1
        assert len(result.drafts_created) == 1

    # -- per-obs-cost -------------------------------------------------------

    def test_per_obs_cost_optimization_suppressed(self, settings, monkeypatch):
        """AC2: an optimization finding whose symptom matches the
        per-observation token-count pattern produces no draft."""
        self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-cost", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="optimization",
                            symptom="Observation chat-4o consumed 12000 input tokens",
                            root_cause="large context window",
                            proposed_solution="trim context",
                            confidence="low",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-per-obs-cost", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert result.drafts_created == []

    def test_per_obs_cost_pattern_case_insensitive(self, settings, monkeypatch):
        """The regex is case-insensitive — 'OBSERVATION' and 'Tokens'
        still match."""
        self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-cost2", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="optimization",
                            symptom="OBSERVATION explore run CONSUMED 500 output Tokens",
                            root_cause="x",
                            proposed_solution="y",
                            confidence="low",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-per-obs-cost-ci", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert result.drafts_created == []

    def test_per_obs_cost_only_suppresses_optimization_category(
        self, settings, monkeypatch
    ):
        """The regex guard only fires when category == 'optimization'.
        A tool_error finding with the same symptom text still files."""
        self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-err", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="tool_error",
                            symptom="Observation chat-4o consumed 12000 input tokens",
                            root_cause="x",
                            proposed_solution="y",
                            confidence="high",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-per-obs-not-opt", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert len(result.drafts_created) == 1

    # -- REQUIRES_HUMAN_REVIEW ----------------------------------------------

    def test_requires_human_review_optimization_suppressed(self, settings, monkeypatch):
        """AC3: an optimization finding whose proposed_solution starts
        with 'REQUIRES_HUMAN_REVIEW:' produces no draft."""
        self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-hr", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="optimization",
                            symptom="excessive sub-agent calls",
                            root_cause="no batching",
                            proposed_solution="REQUIRES_HUMAN_REVIEW: needs architecture decision",
                            confidence="low",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-requires-human", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert result.drafts_created == []

    def test_requires_human_review_only_suppresses_optimization(
        self, settings, monkeypatch
    ):
        """The REQUIRES_HUMAN_REVIEW guard only fires for optimization
        category. A tool_error with the same prefix still files."""
        self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-hr-err", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="tool_error",
                            symptom="run_command failed",
                            root_cause="x",
                            proposed_solution="REQUIRES_HUMAN_REVIEW: needs ops",
                            confidence="high",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-requires-human-toolerr", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert len(result.drafts_created) == 1

    # -- regression guards --------------------------------------------------

    def test_tool_error_still_files_regression(self, settings, monkeypatch):
        """AC4: a tool_error finding with a concrete proposed_solution
        still files a draft (existing path, regression guard)."""
        self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-reg", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="tool_error",
                            symptom="run_command kept failing on uv lock",
                            root_cause="sandbox has no network",
                            proposed_solution="put uv sync in CI",
                            confidence="high",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-reg-toolerr", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert len(result.drafts_created) == 1
        assert "tool_error" in result.drafts_created[0]["title"]

    def test_observation_storm_still_files_regression(self, settings, monkeypatch):
        """An observation_storm trace flagged via the relative baseline
        still produces a draft when the inspector returns a concrete
        finding — the new noise guards don't suppress it."""
        # Batch of 5 traces so the baseline machinery activates.
        traces = [_trace(id=f"t{i}", totalCost=0.10) for i in range(4)] + [
            _trace(id="t-storm", totalCost=0.10),
        ]
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.list_all_traces_since",
            lambda *a, **kw: traces,
        )

        # t-storm has 50 observations; the other 4 have 5 each.
        # Median = 5, threshold = 5 × 3.0 = 15 → 50 > 15 flagged.
        def _detail(_s, trace_id, **kw):
            n = 50 if trace_id == "t-storm" else 5
            return {"observations": [_obs("read_file") for _ in range(n)]}

        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            _detail,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.trace_inspector.run_trace_inspector",
            lambda **kw: TraceInspectResult(
                findings=[
                    TraceFinding(
                        category="optimization",
                        symptom="trace generated 50 observations vs baseline 15 — excessive tool calls",
                        root_cause="excessive tool calls",
                        proposed_solution="batch reads",
                        confidence="medium",
                    )
                ]
            ),
        )
        result = run_trace_review_pass(
            session_id="sess-reg-obsstorm", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert len(result.drafts_created) == 1

    def test_concrete_optimization_still_files(self, settings, monkeypatch):
        """AC5: an optimization finding with a symptom unrelated to
        token counts and a concrete proposed_solution still files."""
        self._patch_seams(
            monkeypatch,
            traces=[_trace(id="t-conc", name="implement", totalCost=0.10)],
            detail_dict={
                "observations": [
                    _obs("run_command", output="error: command failed"),
                ]
            },
            inspector_results=[
                TraceInspectResult(
                    findings=[
                        TraceFinding(
                            category="optimization",
                            symptom="excessive sub-agent calls in the loop",
                            root_cause="no batching of explore calls",
                            proposed_solution="batch explore calls into parallel_explore",
                            confidence="medium",
                        )
                    ]
                )
            ],
        )
        result = run_trace_review_pass(
            session_id="sess-conc-opt", repo_config=_test_repo_config()
        )
        assert result.traces_flagged == 1
        assert len(result.drafts_created) == 1
        assert "optimization" in result.drafts_created[0]["title"]
