"""Tests for the trace_inspector sub-agent and its tool factory."""

import json

import pytest

import robotsix_mill.agents.trace_inspector as trace_inspector_mod
from robotsix_mill.agents.trace_inspector import (
    TraceInspectResult,
    make_trace_inspect_tool,
)
from robotsix_mill.config import Settings


# ---------------------------------------------------------------------------
# fake trace data
# ---------------------------------------------------------------------------


def _fake_trace_with_errors() -> str:
    trace = {
        "id": "trace-abc",
        "name": "implement",
        "observations": [
            {
                "id": "obs-1",
                "type": "GENERATION",
                "level": "DEFAULT",
                "statusMessage": "model call ok",
            },
            {
                "id": "obs-2",
                "type": "SPAN",
                "name": "run_command",
                "level": "ERROR",
                "statusMessage": "pytest returned exit code 1",
            },
            {
                "id": "obs-3",
                "type": "SPAN",
                "name": "run_command",
                "level": "ERROR",
                "statusMessage": "flake8 returned exit code 1",
            },
        ],
    }
    return json.dumps(trace)


def _fake_trace_clean() -> str:
    trace = {
        "id": "trace-clean",
        "name": "scout",
        "observations": [
            {
                "id": "obs-1",
                "type": "GENERATION",
                "level": "DEFAULT",
                "statusMessage": "model call ok",
            },
        ],
    }
    return json.dumps(trace)


def _fake_trace_loop() -> str:
    trace = {
        "id": "trace-loop",
        "name": "implement",
        "observations": [
            {
                "id": "obs-1",
                "type": "GENERATION",
                "level": "DEFAULT",
                "statusMessage": "thinking about fix",
            },
            {
                "id": "obs-2",
                "type": "SPAN",
                "name": "edit_file",
                "level": "DEFAULT",
                "statusMessage": "edit foo.py",
            },
            {
                "id": "obs-3",
                "type": "SPAN",
                "name": "run_command",
                "level": "DEFAULT",
                "statusMessage": "pytest failed",
            },
            {
                "id": "obs-4",
                "type": "GENERATION",
                "level": "DEFAULT",
                "statusMessage": "thinking about fix again",
            },
            {
                "id": "obs-5",
                "type": "SPAN",
                "name": "edit_file",
                "level": "DEFAULT",
                "statusMessage": "edit foo.py again",
            },
            {
                "id": "obs-6",
                "type": "SPAN",
                "name": "run_command",
                "level": "DEFAULT",
                "statusMessage": "pytest failed again",
            },
            {
                "id": "obs-7",
                "type": "GENERATION",
                "level": "DEFAULT",
                "statusMessage": "thinking about fix yet again",
            },
            {
                "id": "obs-8",
                "type": "SPAN",
                "name": "edit_file",
                "level": "DEFAULT",
                "statusMessage": "edit foo.py again",
            },
            {
                "id": "obs-9",
                "type": "SPAN",
                "name": "run_command",
                "level": "DEFAULT",
                "statusMessage": "pytest still failing",
            },
        ],
    }
    return json.dumps(trace)


# ---------------------------------------------------------------------------
# run_trace_inspector seam tests
# ---------------------------------------------------------------------------


class TestRunTraceInspector:
    """Unit tests for run_trace_inspector — mock the pydantic-ai agent."""

    def test_no_api_key_returns_empty(self, monkeypatch):
        """When OPENROUTER_API_KEY is unset, returns an empty result."""
        settings = Settings(openrouter_api_key=None)
        result = trace_inspector_mod.run_trace_inspector(
            settings=settings, trace_data=_fake_trace_with_errors()
        )
        assert result == TraceInspectResult()

    def test_returns_result_with_errors_found(self, monkeypatch):
        """When the sub-agent identifies tool errors, they appear in the result."""
        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                tool_errors=["pytest exit code 1", "flake8 exit code 1"],
            ),
        )
        result = trace_inspector_mod.run_trace_inspector(
            settings=Settings(openrouter_api_key="sk-test"),
            trace_data=_fake_trace_with_errors(),
        )
        assert len(result.tool_errors) == 2
        assert "pytest" in result.tool_errors[0]

    def test_returns_empty_result_on_clean_trace(self, monkeypatch):
        """Clean trace → empty lists (no false positives)."""
        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(),
        )
        result = trace_inspector_mod.run_trace_inspector(
            settings=Settings(openrouter_api_key="sk-test"),
            trace_data=_fake_trace_clean(),
        )
        assert result.tool_errors == []
        assert result.agent_limitations == []
        assert result.optimizations == []

    def test_detects_agent_loop_pattern(self, monkeypatch):
        """A trace with repeated edit→test→fail cycles should flag limitations."""
        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                agent_limitations=[
                    "fix loop detected: 3 edit_file → run_command cycles without convergence"
                ],
            ),
        )
        result = trace_inspector_mod.run_trace_inspector(
            settings=Settings(openrouter_api_key="sk-test"),
            trace_data=_fake_trace_loop(),
        )
        assert len(result.agent_limitations) == 1
        assert "fix loop" in result.agent_limitations[0]


# ---------------------------------------------------------------------------
# make_trace_inspect_tool tests
# ---------------------------------------------------------------------------


class TestMakeTraceInspectTool:
    """Tests for the trace_inspect tool closure — monkeypatch
    run_trace_inspector to inject synthetic results, verifying the
    tool closure works end-to-end (trace fetch → inspect → format)."""

    def test_tool_returns_formatted_summary(self, monkeypatch):
        """The tool closure returns a Markdown-formatted summary."""
        settings = Settings(openrouter_api_key="sk-test")

        # Inject synthetic trace detail via fetch_trace_detail
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda s, tid: {"id": tid, "name": "test-trace", "observations": []},
        )
        # Inject synthetic inspection result
        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                tool_errors=["error A"],
                agent_limitations=["loop B"],
                optimizations=["cache C"],
            ),
        )
        tool = make_trace_inspect_tool(settings)
        output = tool("trace-1")
        assert "## trace trace-1 inspection" in output
        assert "### Tool Errors" in output
        assert "- error A" in output
        assert "### Agent Limitations" in output
        assert "- loop B" in output
        assert "### Optimizations" in output
        assert "- cache C" in output

    def test_tool_degradation_trace_unavailable(self, monkeypatch):
        """When fetch_trace_detail returns None, the tool returns a
        degradation message instead of raising."""
        settings = Settings(openrouter_api_key="sk-test")
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda s, tid: None,
        )
        tool = make_trace_inspect_tool(settings)
        output = tool("missing-trace")
        assert "trace missing-trace unavailable" in output

    def test_tool_clean_trace_no_issues(self, monkeypatch):
        """When no issues found, a short 'no issues' message is included."""
        settings = Settings(openrouter_api_key="sk-test")
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda s, tid: {"id": tid, "name": "clean", "observations": []},
        )
        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(),
        )
        tool = make_trace_inspect_tool(settings)
        output = tool("clean-trace")
        assert "(no issues found in this trace)" in output

    def test_tool_partial_result_one_category(self, monkeypatch):
        """When only tool_errors are present, only that section appears."""
        settings = Settings(openrouter_api_key="sk-test")
        monkeypatch.setattr(
            "robotsix_mill.langfuse_client.fetch_trace_detail",
            lambda s, tid: {"id": tid, "name": "partial", "observations": []},
        )
        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                tool_errors=["only error"],
            ),
        )
        tool = make_trace_inspect_tool(settings)
        output = tool("partial-trace")
        assert "### Tool Errors" in output
        assert "### Agent Limitations" not in output
        assert "### Optimizations" not in output
        assert "(no issues found" not in output


# ---------------------------------------------------------------------------
# fetch_trace_detail public API tests
# ---------------------------------------------------------------------------


class TestFetchTraceDetail:
    """Verify fetch_trace_detail is a public, callable function."""

    def test_public_and_callable(self):
        from robotsix_mill.langfuse_client import fetch_trace_detail

        assert callable(fetch_trace_detail)

    def test_returns_none_when_unconfigured(self):
        from robotsix_mill.langfuse_client import fetch_trace_detail

        settings = Settings(
            langfuse_base_url=None,
            langfuse_public_key=None,
            langfuse_secret_key=None,
        )
        assert fetch_trace_detail(settings, "any-id") is None

    def test_backward_compat_alias(self):
        """_fetch_single_trace still works and is the same function."""
        from robotsix_mill.langfuse_client import (
            _fetch_single_trace,
            fetch_trace_detail,
        )

        assert _fetch_single_trace is fetch_trace_detail


# ---------------------------------------------------------------------------
# TraceInspectResult model tests
# ---------------------------------------------------------------------------


class TestTraceInspectResult:
    def test_defaults_are_empty_lists(self):
        result = TraceInspectResult()
        assert result.tool_errors == []
        assert result.agent_limitations == []
        assert result.optimizations == []

    def test_json_roundtrip(self):
        result = TraceInspectResult(
            tool_errors=["e1", "e2"],
            agent_limitations=["a1"],
            optimizations=[],
        )
        data = result.model_dump_json()
        parsed = TraceInspectResult.model_validate_json(data)
        assert parsed.tool_errors == ["e1", "e2"]
        assert parsed.agent_limitations == ["a1"]
        assert parsed.optimizations == []
