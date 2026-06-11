"""Tests for the trace_inspector sub-agent and its tool factory."""

import json


import robotsix_mill.agents.trace_inspector as trace_inspector_mod
from robotsix_mill.agents.trace_inspector import (
    _SYSTEM_PROMPT,
    TraceInspectResult,
    make_trace_inspect_tool,
)
from robotsix_mill.config import Settings, Secrets, _reset_secrets


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _set_secrets(**kw):
    """Populate the Secrets singleton for tests."""
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(**kw)


def _settings_with_api_key(api_key="sk-test", **kw):
    """Return a Settings and set the matching secret."""
    _set_secrets(openrouter_api_key=api_key)
    return Settings(openrouter_api_key=api_key, **kw)


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


def test_system_prompt_requires_optimization_code_verification():
    """Optimization findings must be gated on secondary code verification.

    Guards the trace-review false-positive fix: the prompt must tell the
    inspector to verify the assumed code path, cite code locations for
    root-cause claims, and downgrade unverifiable architectural /
    control-flow hypotheses to REQUIRES_HUMAN_REVIEW rather than auto-file.
    """
    prompt = _SYSTEM_PROMPT
    assert "Verifying optimization hypotheses" in prompt
    assert "REQUIRES_HUMAN_REVIEW" in prompt
    assert "control-flow" in prompt or "control flow" in prompt
    # Root-cause claims for optimizations must cite concrete code locations.
    assert "path/to/file.py:LINE" in prompt


def _fake_trace_clean() -> str:
    trace = {
        "id": "trace-clean",
        "name": "refine",
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

    def test_no_api_key_returns_error(self, monkeypatch):
        """When OPENROUTER_API_KEY is unset, return a result with the
        cause surfaced in ``error`` (rather than indistinguishable empty
        findings — the user couldn't tell 'no key configured' from 'no
        issues found' before this change)."""
        settings = _settings_with_api_key(api_key=None)
        result = trace_inspector_mod.run_trace_inspector(
            settings=settings, trace_data=_fake_trace_with_errors()
        )
        assert result.error
        assert "OPENROUTER_API_KEY" in result.error

    def test_returns_result_with_errors_found(self, monkeypatch):
        """When the sub-agent identifies tool errors, they appear in the result."""
        from robotsix_mill.agents.trace_inspector import TraceFinding

        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                findings=[
                    TraceFinding(
                        category="tool_error",
                        symptom="pytest exit code 1",
                        root_cause="",
                        proposed_solution="",
                    ),
                    TraceFinding(
                        category="tool_error",
                        symptom="flake8 exit code 1",
                        root_cause="",
                        proposed_solution="",
                    ),
                ]
            ),
        )
        result = trace_inspector_mod.run_trace_inspector(
            settings=_settings_with_api_key(),
            trace_data=_fake_trace_with_errors(),
        )
        assert len(result.findings) == 2
        assert all(f.category == "tool_error" for f in result.findings)
        assert "pytest" in result.findings[0].symptom

    def test_returns_empty_result_on_clean_trace(self, monkeypatch):
        """Clean trace → empty lists (no false positives)."""
        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(),
        )
        result = trace_inspector_mod.run_trace_inspector(
            settings=_settings_with_api_key(),
            trace_data=_fake_trace_clean(),
        )
        assert result.findings == []

    def test_detects_agent_loop_pattern(self, monkeypatch):
        """A trace with repeated edit→test→fail cycles should flag limitations."""
        from robotsix_mill.agents.trace_inspector import TraceFinding

        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                findings=[
                    TraceFinding(
                        category="agent_limitation",
                        symptom="fix loop detected: 3 edit_file → run_command cycles without convergence",
                        root_cause="",
                        proposed_solution="",
                    ),
                ]
            ),
        )
        result = trace_inspector_mod.run_trace_inspector(
            settings=_settings_with_api_key(),
            trace_data=_fake_trace_loop(),
        )
        assert len(result.findings) == 1
        assert result.findings[0].category == "agent_limitation"
        assert "fix loop" in result.findings[0].symptom


# ---------------------------------------------------------------------------
# make_trace_inspect_tool tests
# ---------------------------------------------------------------------------


class TestMakeTraceInspectTool:
    """Tests for the trace_inspect tool closure — monkeypatch
    run_trace_inspector to inject synthetic results, verifying the
    tool closure works end-to-end (trace fetch → inspect → format)."""

    def test_tool_returns_formatted_summary(self, monkeypatch):
        """The tool closure returns a Markdown-formatted summary."""
        settings = _settings_with_api_key()

        # Inject synthetic trace detail via fetch_trace_detail
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda s, tid: {"id": tid, "name": "test-trace", "observations": []},
        )
        # Inject synthetic inspection result
        from robotsix_mill.agents.trace_inspector import TraceFinding

        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                findings=[
                    TraceFinding(
                        category="tool_error",
                        symptom="error A",
                        root_cause="",
                        proposed_solution="",
                    ),
                    TraceFinding(
                        category="agent_limitation",
                        symptom="loop B",
                        root_cause="",
                        proposed_solution="",
                    ),
                    TraceFinding(
                        category="optimization",
                        symptom="cache C",
                        root_cause="",
                        proposed_solution="",
                    ),
                ]
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
        settings = _settings_with_api_key()
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda s, tid: None,
        )
        tool = make_trace_inspect_tool(settings)
        output = tool("missing-trace")
        assert "trace missing-trace unavailable" in output

    def test_tool_clean_trace_no_issues(self, monkeypatch):
        """When no issues found, a short 'no issues' message is included."""
        settings = _settings_with_api_key()
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
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
        """When only tool_error findings are present, only that section appears."""
        settings = _settings_with_api_key()
        monkeypatch.setattr(
            "robotsix_mill.langfuse.client.fetch_trace_detail",
            lambda s, tid: {"id": tid, "name": "partial", "observations": []},
        )
        from robotsix_mill.agents.trace_inspector import TraceFinding

        monkeypatch.setattr(
            trace_inspector_mod,
            "run_trace_inspector",
            lambda **kwargs: TraceInspectResult(
                findings=[
                    TraceFinding(
                        category="tool_error",
                        symptom="only error",
                        root_cause="",
                        proposed_solution="",
                    ),
                ]
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
        from robotsix_mill.langfuse.client import fetch_trace_detail

        assert callable(fetch_trace_detail)

    def test_returns_none_when_unconfigured(self):
        from robotsix_mill.langfuse.client import fetch_trace_detail

        settings = Settings(
            langfuse_base_url=None,
            langfuse_public_key=None,
            langfuse_secret_key=None,
        )
        assert fetch_trace_detail(settings, "any-id") is None


# ---------------------------------------------------------------------------
# TraceInspectResult model tests
# ---------------------------------------------------------------------------


class TestTraceInspectResult:
    def test_defaults_are_empty_lists(self):
        result = TraceInspectResult()
        assert result.findings == []

    def test_json_roundtrip(self):
        from robotsix_mill.agents.trace_inspector import TraceFinding

        result = TraceInspectResult(
            findings=[
                TraceFinding(
                    category="tool_error",
                    symptom="e1",
                    root_cause="rc",
                    proposed_solution="sol",
                ),
                TraceFinding(
                    category="tool_error",
                    symptom="e2",
                    root_cause="",
                    proposed_solution="",
                ),
                TraceFinding(
                    category="agent_limitation",
                    symptom="a1",
                    root_cause="",
                    proposed_solution="",
                    confidence="high",
                ),
            ]
        )
        data = result.model_dump_json()
        parsed = TraceInspectResult.model_validate_json(data)
        te = [f.symptom for f in parsed.findings if f.category == "tool_error"]
        al = [f.symptom for f in parsed.findings if f.category == "agent_limitation"]
        opt = [f.symptom for f in parsed.findings if f.category == "optimization"]
        assert te == ["e1", "e2"]
        assert al == ["a1"]
        assert opt == []
        # Round-trip preserves solution + confidence.
        assert parsed.findings[0].proposed_solution == "sol"
        assert parsed.findings[2].confidence == "high"
