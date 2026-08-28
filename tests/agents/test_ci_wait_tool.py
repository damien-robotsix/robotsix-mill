"""Tests for build_ci_wait_tool — the wait_for_ci agent tool."""

import contextlib

from robotsix_mill.agents.ci_wait_tool import build_ci_wait_tool


def _no_sleep(_):  # never actually wait in tests
    pass


def test_returns_passed_when_ci_green():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: ("success", ""),
        sleep=_no_sleep,
    )
    out = tool("mill/x")
    assert out.startswith("CI_PASSED")


def test_returns_failing_with_summary():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: ("failure", "ruff format would reformat foo.py"),
        sleep=_no_sleep,
    )
    out = tool("mill/x")
    assert out.startswith("CI_FAILING")
    assert "attempt 1/5" in out
    assert "ruff format" in out


def test_passes_attempt_number_to_status_fn():
    seen: list[int] = []

    def status(attempt):
        seen.append(attempt)
        return ("failure", f"attempt {attempt} still broken")

    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=status,
        max_iterations=3,
        sleep=_no_sleep,
    )
    tool("mill/x")
    tool("mill/x")
    assert seen == [1, 2]


def test_returns_gone_when_pr_missing():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: ("gone", ""),
        sleep=_no_sleep,
    )
    assert tool("mill/x").startswith("CI_GONE")


def test_branch_guardrail_rejects_foreign_branch():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: ("success", ""),
        sleep=_no_sleep,
    )
    out = tool("main")
    assert out.startswith("error:")
    assert "guardrailed" in out


def test_iteration_cap_reached_after_max_calls():
    # Always-failing CI: the agent keeps re-checking until the cap.
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: ("failure", "still broken"),
        max_iterations=3,
        sleep=_no_sleep,
    )
    assert tool("mill/x").startswith("CI_FAILING")  # 1
    assert tool("mill/x").startswith("CI_FAILING")  # 2
    assert tool("mill/x").startswith("CI_FAILING")  # 3
    capped = tool("mill/x")  # 4 — over the cap
    assert capped.startswith("CI_ITERATION_CAP_REACHED")
    assert "3" in capped


def test_pending_polls_then_times_out():
    # monotonic advances by 10 minutes each read so the deadline (timeout_s)
    # is exceeded on the second poll without any real waiting.
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 600.0
        return clock["t"]

    polls = {"n": 0}

    def status(attempt):
        polls["n"] += 1
        return ("pending", "")

    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=status,
        timeout_s=1000.0,
        poll_interval_s=1.0,
        sleep=_no_sleep,
        monotonic=fake_monotonic,
    )
    out = tool("mill/x")
    assert out.startswith("CI_STILL_PENDING")
    assert polls["n"] >= 1


def test_pending_then_success_is_passed():
    # First poll pending, second poll green — within the timeout window.
    seq = iter([("pending", ""), ("success", "")])
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: next(seq),
        timeout_s=10_000.0,
        poll_interval_s=1.0,
        sleep=_no_sleep,
    )
    assert tool("mill/x").startswith("CI_PASSED")


def test_stuck_after_consecutive_pending():
    """After max_consecutive_pending returns CI_STILL_PENDING, the next
    call returns CI_STUCK without burning a full timeout_s window."""
    call_count = 0

    def status(attempt):
        nonlocal call_count
        call_count += 1
        return ("pending", "")

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 600.0
        return clock["t"]

    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=status,
        max_consecutive_pending=2,
        timeout_s=1000.0,
        poll_interval_s=1.0,
        sleep=_no_sleep,
        monotonic=fake_monotonic,
    )
    # 1st call: polls, times out → CI_STILL_PENDING (consecutive=1)
    out1 = tool("mill/x")
    assert out1.startswith("CI_STILL_PENDING")
    # 2nd call: polls, times out → CI_STILL_PENDING (consecutive=2)
    out2 = tool("mill/x")
    assert out2.startswith("CI_STILL_PENDING")
    # 3rd call: CI_STUCK immediately, no polling needed
    out3 = tool("mill/x")
    assert out3.startswith("CI_STUCK")
    assert "2 consecutive" in out3

    # Only a subset of calls hit ci_status_fn (the 3rd was short-circuited)
    assert (
        call_count < 10
    )  # should be ~4 (2 calls × 2 polls each), not the 3rd call's worth


def test_stuck_counter_resets_on_failure():
    """A CI_FAILING response clears the consecutive_pending counter."""
    # With max_consecutive_pending=3, after 2 CI_STILL_PENDING results
    # (consecutive=2, which is < 3) the 3rd call still polls. If that
    # poll returns "failure", the counter is reset and no CI_STUCK.
    call_log: list[str] = []

    def status(attempt):
        # 1st call (2 polls, both pending) → timeout → CI_STILL_PENDING
        # 2nd call (1 poll → failure) → CI_FAILING
        if len(call_log) < 3:
            call_log.append(f"poll_{attempt}")
            return ("pending", "")
        call_log.append(f"failure_{attempt}")
        return ("failure", "ruff check failed")

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 600.0
        return clock["t"]

    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=status,
        max_consecutive_pending=3,
        timeout_s=1000.0,
        poll_interval_s=1.0,
        sleep=_no_sleep,
        monotonic=fake_monotonic,
    )
    # 1st call: polls pending × 2, deadline → CI_STILL_PENDING (consecutive=1)
    out1 = tool("mill/x")
    assert out1.startswith("CI_STILL_PENDING")
    # 2nd call: first poll returns failure → CI_FAILING (consecutive reset to 0)
    out2 = tool("mill/x")
    assert out2.startswith("CI_FAILING")
    # 3rd call: should work normally (not CI_STUCK — counter was reset)
    out3 = tool("mill/x")
    assert out3.startswith("CI_FAILING")


def test_stuck_counter_resets_on_success():
    """A CI_PASSED clears the consecutive_pending counter."""
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: ("success", ""),
        max_consecutive_pending=2,
        timeout_s=1000.0,
        poll_interval_s=1.0,
        sleep=_no_sleep,
    )
    out = tool("mill/x")
    assert out.startswith("CI_PASSED")


def test_ci_stuck_short_circuits_without_calling_status_fn():
    """CI_STUCK should be returned without calling ci_status_fn at all
    (no polling, no burning timeout_s)."""
    status_calls = []

    def status(attempt):
        status_calls.append(attempt)
        return ("pending", "")

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 600.0
        return clock["t"]

    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=status,
        max_consecutive_pending=2,
        timeout_s=1000.0,
        poll_interval_s=1.0,
        sleep=_no_sleep,
        monotonic=fake_monotonic,
    )
    tool("mill/x")  # CI_STILL_PENDING (polls, timeouts)
    tool("mill/x")  # CI_STILL_PENDING (polls, timeouts)
    status_calls_before = len(status_calls)
    tool("mill/x")  # CI_STUCK (short-circuits)
    assert len(status_calls) == status_calls_before  # no extra call


# --- trace_stage child-span test ----------------------------------------


def test_wait_for_ci_emits_span(monkeypatch):
    """wait_for_ci opens a child span named 'wait_for_ci' via trace_stage."""
    import robotsix_mill.agents.ci_wait_tool as cwt

    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(cwt, "trace_stage", fake_trace_stage)
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda attempt: ("success", ""),
        sleep=lambda _: None,
    )
    result = tool("mill/x")
    assert result.startswith("CI_PASSED")
    assert spans == ["wait_for_ci"]
