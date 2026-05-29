"""Tests for the cross-trace analyzer sub-agent."""

from robotsix_mill.agents import cross_trace_analyzer as cta
from robotsix_mill.agents.cross_trace_analyzer import (
    CrossTraceFinding,
    CrossTraceResult,
    make_cross_trace_analyze_tool,
)
from robotsix_mill.config import Settings


def test_no_api_key_returns_error(monkeypatch):
    """When OPENROUTER_API_KEY is unset, returns error string — no raise."""
    monkeypatch.setattr(
        "robotsix_mill.agents.cross_trace_analyzer.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": None})(),
    )
    result = cta.run_cross_trace_analyzer(
        settings=Settings(),
        per_trace_summaries="--- trace-1 (implement) ---\nfound tool error",
    )
    assert result.error
    assert "OPENROUTER_API_KEY" in result.error


def test_detects_redundant_exploration(monkeypatch):
    """Per-trace summaries showing review re-reading implement's files
    → redundant_exploration finding."""
    monkeypatch.setattr(
        cta,
        "run_cross_trace_analyzer",
        lambda **kwargs: CrossTraceResult(
            findings=[
                CrossTraceFinding(
                    category="redundant_exploration",
                    symptom=(
                        "Review agent re-read 15 files the implement "
                        "agent already explored."
                    ),
                    root_cause=(
                        "No handoff summary from implement to review; "
                        "the review agent starts cold."
                    ),
                    proposed_solution=(
                        "Add a handoff summary in stages/review.py that "
                        "passes explored-file list from implement."
                    ),
                    confidence="high",
                )
            ]
        ),
    )
    result = cta.run_cross_trace_analyzer(
        settings=Settings(),
        per_trace_summaries=(
            "--- trace-1 (implement) ---\nexplored 15 files\n\n"
            "--- trace-2 (review) ---\nexplored 15 files again"
        ),
    )
    assert len(result.findings) == 1
    assert result.findings[0].category == "redundant_exploration"


def test_detects_retry_cascade(monkeypatch):
    """Per-trace summaries showing a test gate failure amplifying into
    later-stage retries → retry_cascade finding."""
    monkeypatch.setattr(
        cta,
        "run_cross_trace_analyzer",
        lambda **kwargs: CrossTraceResult(
            findings=[
                CrossTraceFinding(
                    category="retry_cascade",
                    symptom=(
                        "Implement stage failed 3x at test gate; "
                        "review stage retried 2x re-running all tests."
                    ),
                    root_cause=(
                        "Flaky test in the test gate caused implement "
                        "retries; review inherits no knowledge of which "
                        "tests are flaky."
                    ),
                    proposed_solution=(
                        "Record flaky-tests in a session artifact so "
                        "later stages can skip known-flaky tests."
                    ),
                    confidence="medium",
                )
            ]
        ),
    )
    result = cta.run_cross_trace_analyzer(
        settings=Settings(),
        per_trace_summaries="--- trace-1 (implement) ---\n3 retries\n\n"
        "--- trace-2 (review) ---\n2 retries",
    )
    assert len(result.findings) == 1
    assert result.findings[0].category == "retry_cascade"


def test_clean_multitrace_returns_empty(monkeypatch):
    """No cross-cutting patterns → empty findings, no error."""
    monkeypatch.setattr(
        cta,
        "run_cross_trace_analyzer",
        lambda **kwargs: CrossTraceResult(findings=[]),
    )
    result = cta.run_cross_trace_analyzer(
        settings=Settings(),
        per_trace_summaries=(
            "--- trace-1 (implement) ---\nclean\n\n--- trace-2 (review) ---\nclean"
        ),
    )
    assert result.findings == []
    assert result.error == ""


def test_make_tool_formats_output(monkeypatch):
    """Tool wrapper produces grouped Markdown sections."""
    monkeypatch.setattr(
        cta,
        "run_cross_trace_analyzer",
        lambda **kwargs: CrossTraceResult(
            findings=[
                CrossTraceFinding(
                    category="redundant_exploration",
                    symptom="Review re-explored 10 files.",
                    root_cause="No handoff.",
                    proposed_solution="Add handoff summary.",
                    confidence="high",
                ),
                CrossTraceFinding(
                    category="retry_cascade",
                    symptom="Implement retries cascaded to review.",
                    root_cause="Flaky test gate.",
                    proposed_solution="Cache flaky test results.",
                    confidence="medium",
                ),
            ]
        ),
    )
    tool = make_cross_trace_analyze_tool(Settings())
    output = tool("dummy summaries")
    assert "## cross-trace analysis" in output
    assert "### Redundant Exploration" in output
    assert "### Retry Cascades" in output
    assert "re-explored 10 files" in output
    assert "retries cascaded" in output


def test_make_tool_empty_findings(monkeypatch):
    """Tool wrapper renders a clean 'no patterns' message for empty findings."""
    monkeypatch.setattr(
        cta,
        "run_cross_trace_analyzer",
        lambda **kwargs: CrossTraceResult(findings=[]),
    )
    tool = make_cross_trace_analyze_tool(Settings())
    output = tool("dummy summaries")
    assert "(no cross-trace patterns found)" in output


def test_make_tool_degradation_on_error(monkeypatch):
    """When run_cross_trace_analyzer returns an error, the tool renders
    a degradation message."""
    monkeypatch.setattr(
        cta,
        "run_cross_trace_analyzer",
        lambda **kwargs: CrossTraceResult(error="context overflow: 500K chars"),
    )
    tool = make_cross_trace_analyze_tool(Settings())
    output = tool("huge summaries")
    assert "_analyzer error:" in output
    assert "context overflow" in output
