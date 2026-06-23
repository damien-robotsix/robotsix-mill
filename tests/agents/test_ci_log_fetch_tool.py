"""Tests for build_ci_log_fetch_tool — the fetch_ci_logs agent tool."""

import contextlib

from robotsix_mill.agents.ci_log_fetch_tool import build_ci_log_fetch_tool


# --- trace_stage child-span test ----------------------------------------


def test_fetch_ci_logs_emits_span(monkeypatch):
    """fetch_ci_logs opens a child span named 'fetch_ci_logs' via trace_stage."""
    import robotsix_mill.agents.ci_log_fetch_tool as clf

    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(clf, "trace_stage", fake_trace_stage)

    def fetch_fn(run_id: int, full_log: bool) -> str:
        return "[mock log content]"

    tool = build_ci_log_fetch_tool(branch="mill/x", fetch_fn=fetch_fn)
    result = tool(run_id=42)
    assert result == "[mock log content]"
    assert spans == ["fetch_ci_logs"]


def test_fetch_ci_logs_unavailable_also_emits_span(monkeypatch):
    """Even when fetch_fn is None (CI_LOG_FETCH_UNAVAILABLE), the span is still emitted."""
    import robotsix_mill.agents.ci_log_fetch_tool as clf

    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(clf, "trace_stage", fake_trace_stage)

    tool = build_ci_log_fetch_tool(branch="mill/x", fetch_fn=None)
    result = tool(run_id=42)
    assert result.startswith("CI_LOG_FETCH_UNAVAILABLE")
    assert spans == ["fetch_ci_logs"]


def test_fetch_ci_logs_by_run_url_emits_span(monkeypatch):
    """Resolving by run_url also emits the span."""
    import robotsix_mill.agents.ci_log_fetch_tool as clf

    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(clf, "trace_stage", fake_trace_stage)

    def fetch_fn(run_id: int, full_log: bool) -> str:
        return f"[log for run {run_id}]"

    tool = build_ci_log_fetch_tool(branch="mill/x", fetch_fn=fetch_fn)
    result = tool(run_url="https://github.com/o/r/actions/runs/99")
    assert result == "[log for run 99]"
    assert spans == ["fetch_ci_logs"]
