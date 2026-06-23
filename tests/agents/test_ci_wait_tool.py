"""Tests for build_ci_wait_tool — the wait_for_ci agent tool."""

import contextlib

from robotsix_mill.agents.ci_wait_tool import build_ci_wait_tool


def _no_sleep(_):  # never actually wait in tests
    pass


def test_returns_passed_when_ci_green():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda: ("success", ""),
        sleep=_no_sleep,
    )
    out = tool("mill/x")
    assert out.startswith("CI_PASSED")


def test_returns_failing_with_summary():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda: ("failure", "ruff format would reformat foo.py"),
        sleep=_no_sleep,
    )
    out = tool("mill/x")
    assert out.startswith("CI_FAILING")
    assert "attempt 1/5" in out
    assert "ruff format" in out


def test_returns_gone_when_pr_missing():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda: ("gone", ""),
        sleep=_no_sleep,
    )
    assert tool("mill/x").startswith("CI_GONE")


def test_branch_guardrail_rejects_foreign_branch():
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda: ("success", ""),
        sleep=_no_sleep,
    )
    out = tool("main")
    assert out.startswith("error:")
    assert "guardrailed" in out


def test_iteration_cap_reached_after_max_calls():
    # Always-failing CI: the agent keeps re-checking until the cap.
    tool = build_ci_wait_tool(
        branch="mill/x",
        ci_status_fn=lambda: ("failure", "still broken"),
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

    def status():
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
        ci_status_fn=lambda: next(seq),
        timeout_s=10_000.0,
        poll_interval_s=1.0,
        sleep=_no_sleep,
    )
    assert tool("mill/x").startswith("CI_PASSED")


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
        ci_status_fn=lambda: ("success", ""),
        sleep=lambda _: None,
    )
    result = tool("mill/x")
    assert result.startswith("CI_PASSED")
    assert spans == ["wait_for_ci"]
