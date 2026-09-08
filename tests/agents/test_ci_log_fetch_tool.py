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


# --- output cap (2026-09-08 8a6b: 4 MB full_log blew the 1M-token context) ---


def _stub_trace(monkeypatch):
    import robotsix_mill.agents.ci_log_fetch_tool as clf

    @contextlib.contextmanager
    def fake_trace_stage(name):
        yield

    monkeypatch.setattr(clf, "trace_stage", fake_trace_stage)


def test_full_log_result_is_capped_per_job(monkeypatch):
    """A multi-MB full_log result is capped, keeping head+tail of EVERY job."""
    import robotsix_mill.agents.ci_log_fetch_tool as clf

    _stub_trace(monkeypatch)
    # Real forge shape: one ``### Job:`` section per failed job, joined by
    # newlines, the big one first (a 3.9 MB pytest job) then a small one.
    big = (
        "### Job: Tests (id=1)\n"
        + "".join(
            f"line {i} of a very long pytest log with progress bars\n"
            for i in range(80_000)
        )
        + "FAILED tests/test_x.py::test_y - AssertionError\n"
    )
    small = "### Job: Pyright type checker (id=2)\nerror: 1 problem\nPYRIGHT FAILED\n"
    logs = f"{big}\n{small}\n"
    assert len(logs) > clf._FULL_LOG_MAX_CHARS

    tool = build_ci_log_fetch_tool(branch="mill/x", fetch_fn=lambda rid, full: logs)
    result = tool(run_id=34222100609, full_log=True)

    # Bounded (cap + per-job markers + trailer), and both jobs survive.
    assert len(result) < clf._FULL_LOG_MAX_CHARS + 2_000
    assert "### Job: Tests (id=1)" in result
    assert "### Job: Pyright type checker (id=2)" in result
    assert "PYRIGHT FAILED" in result
    # The failure line at the tail of the big job is what the agent needs.
    assert "FAILED tests/test_x.py::test_y" in result
    assert "fetch_ci_logs truncated" in result
    assert "capped to ~" in result


def test_small_logs_pass_through_untouched(monkeypatch):
    """Logs under the ceiling are returned verbatim — no markers, no trailer."""
    _stub_trace(monkeypatch)
    logs = "### Job: lint (id=3)\nruff failed\n"
    tool = build_ci_log_fetch_tool(branch="mill/x", fetch_fn=lambda rid, full: logs)
    assert tool(run_id=1, full_log=True) == logs
    assert tool(run_id=1) == logs


def test_cap_log_text_single_section_without_header():
    """A headerless blob (GitLab path) is still head+tail capped."""
    import robotsix_mill.agents.ci_log_fetch_tool as clf

    blob = "START\n" + ("x" * 999 + "\n") * 400 + "END-OF-LOG\n"
    out = clf._cap_log_text(blob, max_chars=10_000)
    assert out.startswith("START")
    assert "END-OF-LOG" in out
    assert "truncated" in out
    assert len(out) < 12_000
