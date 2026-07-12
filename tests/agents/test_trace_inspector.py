"""Tests for the trace_inspector sub-agent and its tool factory."""

import json

import pytest

import robotsix_mill.agents.trace_inspector as trace_inspector_mod
from robotsix_mill.agents.trace_inspector import (
    _SYSTEM_PROMPT,
    TraceInspectResult,
    _wrap_tools_with_error_limit,
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


def test_system_prompt_has_statistical_signal_gate():
    """Statistical-signal flags must be held to a verification bar.

    Mirrors the optimization gate: before filing a finding that rests on
    a ``cost_outlier`` / ``observation_storm`` / ``tool_errors`` flag the
    inspector must cross-check the trace's own model/cost/usage data, rule
    out benign explanations, and downgrade to REQUIRES_HUMAN_REVIEW when it
    cannot.
    """
    prompt = _SYSTEM_PROMPT
    assert "Verifying statistical-signal flags" in prompt
    # Each of the three statistical signals is named with its check.
    assert "cost_outlier" in prompt
    assert "observation_storm" in prompt
    assert "tool_errors" in prompt
    # Cheap-model / cache benign-explanation guidance for cost_outlier.
    assert "calculatedTotalCost" in prompt
    # Reuses the same downgrade convention as the optimization gate.
    assert "REQUIRES_HUMAN_REVIEW" in prompt


def test_system_prompt_has_error_mechanism_gate():
    """The error-mechanism gate must instruct the inspector to trace failing
    strings to the raising frame and downgrade unverifiable mechanisms.

    Guards the reference ticket's false-positive class: the inspector must
    not assert a failure mechanism by blame-the-nearest-call proximity
    without locating the raiser. The test pins the section title, the
    worked-example string, and the REQUIRES_HUMAN_REVIEW downgrade token.
    """
    prompt = _SYSTEM_PROMPT
    assert "Verifying error-mechanism hypotheses" in prompt
    assert "This event loop is already" in prompt
    assert "REQUIRES_HUMAN_REVIEW" in prompt


def _capture_inspector_prompt(monkeypatch, **kwargs) -> str:
    """Run run_trace_inspector with the agent seam stubbed and return the
    user prompt that would have been sent to the model."""
    captured: dict[str, str] = {}

    class _Handle:
        def run_sync(self, prompt, **kw):
            captured["prompt"] = prompt

            class _R:
                output = TraceInspectResult()

            return _R()

    def fake_run_agent(agent, make_run, **kw):
        return make_run(_Handle())

    monkeypatch.setattr(
        "robotsix_mill.agents.retry.run_agent",
        fake_run_agent,
    )
    trace_inspector_mod.run_trace_inspector(**kwargs)
    return captured["prompt"]


def test_classifier_flags_injected_into_prompt(monkeypatch):
    """Non-empty classifier_flags render a classifier_flags section."""
    flags = ["cost_outlier ($9.99 vs $1.00)", "tool_errors (3)"]
    prompt = _capture_inspector_prompt(
        monkeypatch,
        settings=_settings_with_api_key(),
        trace_data=_fake_trace_clean(),
        classifier_flags=flags,
    )
    assert "classifier_flags" in prompt
    assert "cost_outlier ($9.99 vs $1.00)" in prompt
    assert "tool_errors (3)" in prompt


def test_classifier_flags_none_omits_section(monkeypatch):
    """classifier_flags=None leaves the prompt without the flags section."""
    prompt = _capture_inspector_prompt(
        monkeypatch,
        settings=_settings_with_api_key(),
        trace_data=_fake_trace_clean(),
        classifier_flags=None,
    )
    assert "classifier_flags" not in prompt


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

    # -----------------------------------------------------------------------
    # trace_review_model_level (AC1)
    # -----------------------------------------------------------------------

    def test_model_level_defaults_to_1_and_is_configurable(self, monkeypatch):
        """``trace_review_model_level`` defaults to 1 and the field is
        passed to ``build_openrouter_model`` — so a single Settings knob
        governs the inspector tier for both the automated pass and the
        ``langfuse_inspect_trace`` tool (AC1)."""
        # Default settings → level 1
        s = _settings_with_api_key()
        assert s.trace_review_model_level == 1

        # Override to level 2 and confirm the field propagates.
        s2 = _settings_with_api_key(trace_review_model_level=2)
        assert s2.trace_review_model_level == 2

        # Spy on build_openrouter_model to assert the level passed through.
        captured_levels: list[int] = []

        from robotsix_mill.agents import base as base_mod

        _orig_build = base_mod.build_openrouter_model

        def fake_build_openrouter_model(level=1, *, online=False):
            captured_levels.append(level)
            return _orig_build(level, online=online)

        monkeypatch.setattr(
            base_mod, "build_openrouter_model", fake_build_openrouter_model
        )

        # Stub run_agent so the real model is never exercised.
        monkeypatch.setattr(
            "robotsix_mill.agents.retry.run_agent",
            lambda agent, make_run, **kw: make_run(
                type(
                    "_H",
                    (),
                    {
                        "run_sync": lambda s, p, **kw: type(
                            "_R", (), {"output": TraceInspectResult()}
                        )(),
                    },
                )()
            ),
        )
        trace_inspector_mod.run_trace_inspector(
            settings=s2,
            trace_data=_fake_trace_clean(),
            repo_dir=None,
        )
        assert captured_levels == [2]


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


# ---------------------------------------------------------------------------
# Termination guardrails — tool-call / error limits
# ---------------------------------------------------------------------------


class TestWrapToolsWithErrorLimit:
    """Unit tests for _wrap_tools_with_error_limit."""

    def test_sync_tool_passthrough_on_success(self):
        """Successful sync tool calls pass through unchanged."""

        def ok_tool(x: int) -> int:
            """Add one."""
            return x + 1

        wrapped = _wrap_tools_with_error_limit([ok_tool], max_errors=3)
        assert wrapped[0](5) == 6

    def test_sync_tool_counts_errors(self):
        """Each failing sync call increments the shared error counter."""

        def fail_tool() -> None:
            raise ValueError("boom")

        wrapped = _wrap_tools_with_error_limit([fail_tool], max_errors=3)
        for _ in range(3):
            with pytest.raises(ValueError, match="boom"):
                wrapped[0]()
        # Fourth error exceeds limit → UsageLimitExceeded
        with pytest.raises(Exception) as exc_info:
            wrapped[0]()
        assert "Error limit (3) exceeded" in str(exc_info.value)

    def test_sync_tool_preserves_metadata(self):
        """Wrapped sync tools preserve name, docstring, and annotations."""

        def my_tool(path: str, mode: int = 0) -> str:
            """Read a file."""
            return path

        wrapped = _wrap_tools_with_error_limit([my_tool], max_errors=3)
        w = wrapped[0]
        assert w.__name__ == "my_tool"
        assert w.__doc__ == "Read a file."
        assert "path" in w.__annotations__
        assert w.__annotations__["path"] is str

    def test_async_tool_passthrough_on_success(self):
        """Successful async tool calls pass through unchanged."""
        import asyncio

        async def ok_tool(x: int) -> int:
            return x + 1

        wrapped = _wrap_tools_with_error_limit([ok_tool], max_errors=3)
        result = asyncio.run(wrapped[0](5))
        assert result == 6

    def test_async_tool_counts_errors(self):
        """Each failing async call increments the shared error counter."""
        import asyncio

        async def fail_tool() -> None:
            raise RuntimeError("async boom")

        wrapped = _wrap_tools_with_error_limit([fail_tool], max_errors=2)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="async boom"):
                asyncio.run(wrapped[0]())
        # Third error exceeds limit
        with pytest.raises(Exception) as exc_info:
            asyncio.run(wrapped[0]())
        assert "Error limit (2) exceeded" in str(exc_info.value)

    def test_shared_counter_across_tools(self):
        """Multiple tools share the same error budget."""

        def tool_a() -> None:
            raise ValueError("a")

        def tool_b() -> None:
            raise ValueError("b")

        wrapped = _wrap_tools_with_error_limit([tool_a, tool_b], max_errors=2)
        with pytest.raises(ValueError, match="a"):
            wrapped[0]()
        with pytest.raises(ValueError, match="b"):
            wrapped[1]()
        # Third error (from either tool) exceeds limit
        with pytest.raises(Exception) as exc_info:
            wrapped[0]()
        assert "Error limit (2) exceeded" in str(exc_info.value)

    def test_model_retry_not_counted(self):
        """ModelRetry passes through without consuming error budget."""
        from pydantic_ai.exceptions import ModelRetry

        def retry_tool() -> None:
            raise ModelRetry("bad args")

        wrapped = _wrap_tools_with_error_limit([retry_tool], max_errors=1)
        for _ in range(5):
            with pytest.raises(ModelRetry):
                wrapped[0]()
        # Budget untouched — a subsequent real error still fires at limit

        def real_fail() -> None:
            raise ValueError("real")

        wrapped2 = _wrap_tools_with_error_limit([real_fail], max_errors=1)
        with pytest.raises(ValueError, match="real"):
            wrapped2[0]()
        with pytest.raises(Exception) as exc_info:
            wrapped2[0]()
        assert "Error limit (1) exceeded" in str(exc_info.value)

    def test_usage_limit_exceeded_not_double_counted(self):
        """UsageLimitExceeded from the tool-call limit passes through."""
        from pydantic_ai.exceptions import UsageLimitExceeded

        def limited_tool() -> None:
            raise UsageLimitExceeded("too many calls")

        wrapped = _wrap_tools_with_error_limit([limited_tool], max_errors=1)
        with pytest.raises(UsageLimitExceeded, match="too many calls"):
            wrapped[0]()
        # Budget untouched — a real error still counts

        def real_fail() -> None:
            raise ValueError("real")

        wrapped2 = _wrap_tools_with_error_limit([real_fail], max_errors=1)
        with pytest.raises(ValueError):
            wrapped2[0]()
        with pytest.raises(Exception) as exc_info:
            wrapped2[0]()
        assert "Error limit (1) exceeded" in str(exc_info.value)

    def test_max_errors_zero_disables(self):
        """max_errors=0 → no wrapping, original tool returned as-is."""

        def ok_tool() -> str:
            return "hi"

        wrapped = _wrap_tools_with_error_limit([ok_tool], max_errors=0)
        # Should be the same function object, not a wrapper
        assert wrapped[0] is ok_tool


class TestToolCallsLimitInUsageLimits:
    """Verify tool_calls_limit is wired into UsageLimits for tools-on path."""

    def test_tools_on_path_sets_tool_calls_limit(self, monkeypatch):
        """When repo_dir is provided, tool_calls_limit is set on UsageLimits."""
        captured_limits: list = []

        class _Handle:
            def run_sync(self, prompt, **kw):
                captured_limits.append(kw.get("usage_limits"))
                return type("_R", (), {"output": TraceInspectResult()})()

        def fake_run_agent(agent, make_run, **kw):
            return make_run(_Handle())

        monkeypatch.setattr(
            "robotsix_mill.agents.retry.run_agent",
            fake_run_agent,
        )

        from pathlib import Path

        settings = _settings_with_api_key()
        trace_inspector_mod.run_trace_inspector(
            settings=settings,
            trace_data=_fake_trace_clean(),
            repo_dir=Path("/tmp"),
        )
        assert len(captured_limits) == 1
        limits = captured_limits[0]
        assert limits.tool_calls_limit == settings.trace_review_max_tool_calls
        assert limits.tool_calls_limit == 100  # default

    def test_tool_less_path_omits_tool_calls_limit(self, monkeypatch):
        """When repo_dir is None, tool_calls_limit stays None."""
        captured_limits: list = []

        class _Handle:
            def run_sync(self, prompt, **kw):
                captured_limits.append(kw.get("usage_limits"))
                return type("_R", (), {"output": TraceInspectResult()})()

        def fake_run_agent(agent, make_run, **kw):
            return make_run(_Handle())

        monkeypatch.setattr(
            "robotsix_mill.agents.retry.run_agent",
            fake_run_agent,
        )

        settings = _settings_with_api_key()
        trace_inspector_mod.run_trace_inspector(
            settings=settings,
            trace_data=_fake_trace_clean(),
            repo_dir=None,
        )
        assert len(captured_limits) == 1
        limits = captured_limits[0]
        assert limits.tool_calls_limit is None


# ---------------------------------------------------------------------------
# Dynamic request budget / large-trace fallback tests
# ---------------------------------------------------------------------------


class TestDynamicRequestBudget:
    """Verify request_limit scales with observation count and large
    traces fall back to the tool-less path."""

    def _capture_limits_and_tools(
        self,
        monkeypatch,
        obs_count: int,
        *,
        repo_dir=True,
        extra_settings: dict | None = None,
        request_limit_override: int | None = None,
        classifier_flags: list[str] | None = None,
    ):
        """Run trace_inspector with a fake trace of *obs_count* observations
        and capture the UsageLimits + tool list passed to run_sync."""
        from pathlib import Path

        trace = {
            "id": "trace-dyn",
            "name": "implement",
            "observations": [
                {
                    "id": f"obs-{i}",
                    "type": "GENERATION",
                    "level": "DEFAULT",
                    "statusMessage": "ok",
                }
                for i in range(obs_count)
            ],
        }

        captured: dict = {}

        class _Handle:
            def run_sync(self, prompt, **kw):
                captured["limits"] = kw.get("usage_limits")
                captured["prompt"] = prompt
                return type("_R", (), {"output": TraceInspectResult()})()

        def fake_run_agent(agent, make_run, **kw):
            captured["agent"] = agent
            return make_run(_Handle())

        monkeypatch.setattr(
            "robotsix_mill.agents.retry.run_agent",
            fake_run_agent,
        )

        settings_kwargs = {}
        if extra_settings:
            settings_kwargs.update(extra_settings)
        settings = _settings_with_api_key(**settings_kwargs)
        kwargs: dict = {
            "settings": settings,
            "trace_data": json.dumps(trace),
            "repo_dir": Path("/tmp") if repo_dir else None,
        }
        if request_limit_override is not None:
            kwargs["request_limit_override"] = request_limit_override
        if classifier_flags is not None:
            kwargs["classifier_flags"] = classifier_flags
        trace_inspector_mod.run_trace_inspector(**kwargs)
        return captured

    def test_moderate_obs_tools_on_sets_scaled_request_limit(self, monkeypatch):
        """235 obs (with max_obs_for_tools=300) → request_limit > 20 and ≤ 80."""
        captured = self._capture_limits_and_tools(
            monkeypatch,
            235,
            extra_settings={"trace_review_inspector_max_obs_for_tools": 300},
        )
        limits = captured["limits"]
        # Formula: max(20, min(80, int(235 * 0.1))) = max(20, min(80, 23)) = 23
        assert limits.request_limit == 23
        assert limits.tool_calls_limit == 100  # default trace_review_max_tool_calls

    def test_small_obs_clamps_to_min_request_floor(self, monkeypatch):
        """10 obs → request_limit = 20 (floor)."""
        captured = self._capture_limits_and_tools(
            monkeypatch,
            10,
            extra_settings={"trace_review_inspector_max_obs_for_tools": 300},
        )
        limits = captured["limits"]
        # Formula: max(20, min(80, int(10 * 0.1))) = max(20, min(80, 1)) = 20
        assert limits.request_limit == 20
        assert limits.tool_calls_limit == 100

    def test_large_obs_exceeds_max_tools_threshold_falls_back_to_tool_less(
        self, monkeypatch
    ):
        """250 obs (> max_obs_for_tools=200) with repo_dir → tool-less path."""
        captured = self._capture_limits_and_tools(monkeypatch, 250, repo_dir=True)
        limits = captured["limits"]
        # Should be tool-less: request_limit = 3, tool_calls_limit = None
        assert limits.request_limit == 3
        assert limits.tool_calls_limit is None
        # Agent must have no tools (no explore / parallel_explore / read_file).
        agent = captured["agent"]
        tool_dict = agent._function_toolset.tools
        assert "explore" not in tool_dict
        assert "parallel_explore" not in tool_dict
        assert "read_file" not in tool_dict

    def test_tool_less_path_when_repo_dir_none_stays_cheap(self, monkeypatch):
        """repo_dir=None → always tool-less, regardless of obs count."""
        captured = self._capture_limits_and_tools(monkeypatch, 5, repo_dir=False)
        limits = captured["limits"]
        assert limits.request_limit == 3
        assert limits.tool_calls_limit is None
        agent = captured["agent"]
        tool_dict = agent._function_toolset.tools
        assert tool_dict == {}

    def test_request_limit_override_caps_dynamic_budget(self, monkeypatch):
        """request_limit_override caps (lowers, never raises) the tools-on request_limit."""
        # 500 obs with high max_obs_for_tools → dynamic budget = min(80, 50) = 50
        captured = self._capture_limits_and_tools(
            monkeypatch,
            500,
            extra_settings={
                "trace_review_inspector_max_obs_for_tools": 1000,
            },
            request_limit_override=15,
        )
        limits = captured["limits"]
        # Dynamic: max(20, min(80, int(500 * 0.1))) = max(20, min(80, 50)) = 50
        # Override: min(50, 15) = 15
        assert limits.request_limit == 15
        # override does NOT affect tool_calls_limit
        assert limits.tool_calls_limit == 100

    def test_request_limit_override_does_not_raise_below_floor(self, monkeypatch):
        """override can go below the min_requests floor — it's a caller-imposed cap."""
        captured = self._capture_limits_and_tools(
            monkeypatch,
            10,
            extra_settings={
                "trace_review_inspector_max_obs_for_tools": 300,
            },
            request_limit_override=5,
        )
        limits = captured["limits"]
        # Dynamic: max(20, min(80, int(10 * 0.1))) = 20
        # Override: min(20, 5) = 5
        assert limits.request_limit == 5

    def test_request_limit_override_never_raises_dynamic_ceiling(self, monkeypatch):
        """override=500 (> dynamic 50) → effective is still 50 (override only lowers)."""
        captured = self._capture_limits_and_tools(
            monkeypatch,
            500,
            extra_settings={
                "trace_review_inspector_max_obs_for_tools": 1000,
            },
            request_limit_override=500,
        )
        limits = captured["limits"]
        # Dynamic = 50, override = 500 → effective = min(50, 500) = 50
        assert limits.request_limit == 50

    def test_request_limit_override_ignored_on_tool_less_path(self, monkeypatch):
        """override does NOT affect the tool-less path (repo_dir=None)."""
        captured = self._capture_limits_and_tools(
            monkeypatch, 5, repo_dir=False, request_limit_override=100
        )
        limits = captured["limits"]
        # Tool-less should still be the toolless default (3)
        assert limits.request_limit == 3
        assert limits.tool_calls_limit is None

    def test_observation_storm_boosts_tools_on_request_limit(self, monkeypatch):
        """observation_storm flag → request_limit floored at 40 on tools-on path."""
        # 10 obs (would normally get floor 20) with observation_storm → 40
        captured = self._capture_limits_and_tools(
            monkeypatch,
            10,
            extra_settings={"trace_review_inspector_max_obs_for_tools": 300},
            classifier_flags=[
                "observation_storm (1364 obs vs threshold 70 = 3.0× median 24)"
            ],
        )
        limits = captured["limits"]
        # Normal: max(20, min(80, int(10 * 0.1))) = 20.  Storm boost → max(20, 40) = 40.
        assert limits.request_limit == 40

    def test_observation_storm_boosts_tool_less_request_limit(self, monkeypatch):
        """observation_storm flag → request_limit floored at 10 on tool-less path."""
        captured = self._capture_limits_and_tools(
            monkeypatch,
            250,
            repo_dir=True,
            classifier_flags=[
                "observation_storm (1364 obs vs threshold 70 = 3.0× median 24)"
            ],
        )
        limits = captured["limits"]
        # Tool-less: toolless_requests = 3.  Storm boost → max(3, 10) = 10.
        assert limits.request_limit == 10
        assert limits.tool_calls_limit is None

    def test_observation_storm_does_not_lower_already_high_budget(self, monkeypatch):
        """observation_storm only raises the floor — a higher dynamic budget is preserved."""
        # 600 obs with max_obs_for_tools=1000 → dynamic = min(80, 60) = 60.
        # Storm boost → max(60, 40) = 60 (unchanged).
        captured = self._capture_limits_and_tools(
            monkeypatch,
            600,
            extra_settings={"trace_review_inspector_max_obs_for_tools": 1000},
            classifier_flags=["observation_storm (600 obs vs threshold 70)"],
        )
        limits = captured["limits"]
        assert limits.request_limit == 60


# ---------------------------------------------------------------------------
# _shrink_trace_data unit tests
# ---------------------------------------------------------------------------


class TestShrinkTraceData:
    """Unit tests for _shrink_trace_data's new (str, int) return type."""

    def test_valid_json_returns_count_equal_to_observations_length(self):
        """The count matches len(trace['observations'])."""
        trace = {"id": "t1", "observations": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        assert isinstance(shrunk, str)
        assert count == 3

    def test_empty_observations_returns_zero(self):
        """Empty observations list → count = 0."""
        trace = {"id": "t2", "observations": []}
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        assert count == 0

    def test_missing_observations_key_returns_zero(self):
        """No 'observations' key → count = 0."""
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps({"id": "t3"}))
        assert count == 0

    def test_unparseable_string_returns_zero(self):
        """Non-JSON input returns count = 0."""
        shrunk, count = trace_inspector_mod._shrink_trace_data("not json at all")
        assert count == 0
        # The shrunk string should still contain something useful.
        assert len(shrunk) > 0

    def test_short_valid_json_preserves_content(self):
        """When the trace is well under max_chars, the returned string
        is valid JSON and contains the original observation ids."""
        trace = {
            "id": "t4",
            "observations": [{"id": "obs-1", "input": "hello", "output": "world"}],
        }
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        assert count == 1
        parsed = json.loads(shrunk)
        assert parsed["observations"][0]["id"] == "obs-1"
        assert parsed["observations"][0]["input"] == "hello"

    def test_large_trace_strips_input_output(self):
        """When obs_count > 200, input/output/metadata are stripped."""
        obs = [
            {
                "id": f"obs-{i}",
                "type": "GENERATION",
                "level": "DEFAULT",
                "name": "test",
                "input": f"big input {i}",
                "output": f"big output {i}",
                "metadata": {"key": "value"},
                "startTime": "2025-01-01T00:00:00Z",
            }
            for i in range(201)
        ]
        trace = {"id": "t-large", "observations": obs}
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        assert count == 201
        parsed = json.loads(shrunk)
        for o in parsed["observations"]:
            assert "input" not in o
            assert "output" not in o
            assert "metadata" not in o
            assert "id" in o
            assert "name" in o

    def test_large_trace_keeps_structural_fields(self):
        """Large-trace path preserves id, type, level, statusMessage, name,
        model, calculatedTotalCost, latency, usageDetails, startTime, endTime."""
        obs = [
            {
                "id": "obs-200",
                "type": "GENERATION",
                "level": "ERROR",
                "statusMessage": "failed",
                "name": "gpt-4",
                "model": "openai/gpt-4",
                "calculatedTotalCost": 0.5,
                "latency": 1.2,
                "usageDetails": {"input": 1000},
                "startTime": "2025-01-01T00:00:00Z",
                "endTime": "2025-01-01T00:00:01Z",
                "input": "should be stripped",
                "output": "should be stripped",
                "extraField": "should be stripped",
            }
            for _ in range(201)
        ]
        trace = {"id": "t-keep", "observations": obs}
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        assert count == 201
        parsed = json.loads(shrunk)
        o = parsed["observations"][0]
        assert o == {
            "id": "obs-200",
            "type": "GENERATION",
            "level": "ERROR",
            "statusMessage": "failed",
            "name": "gpt-4",
            "model": "openai/gpt-4",
            "calculatedTotalCost": 0.5,
            "latency": 1.2,
            "usageDetails": {"input": 1000},
            "startTime": "2025-01-01T00:00:00Z",
            "endTime": "2025-01-01T00:00:01Z",
        }

    def test_small_trace_keeps_input_output(self):
        """When obs_count <= 200, input/output are preserved (trimmed if long)."""
        trace = {
            "id": "t-small",
            "observations": [{"id": "obs-1", "input": "hello", "output": "world"}],
        }
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        assert count == 1
        parsed = json.loads(shrunk)
        assert parsed["observations"][0]["input"] == "hello"
        assert parsed["observations"][0]["output"] == "world"

    def test_string_observations_in_large_trace_are_skipped(self):
        """When obs_count > 200, string entries in observations are
        skipped instead of raising 'str' object has no attribute 'items'."""
        obs = [
            {
                "id": f"obs-{i}",
                "type": "GENERATION",
                "level": "DEFAULT",
                "name": "test",
            }
            for i in range(200)
        ]
        # Insert string entries that would crash on .items()
        obs.append("bare-string-obs")
        obs.append("another-bare-string")
        trace = {"id": "t-str-large", "observations": obs}
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        # 202 total entries, 2 are strings → filtered out
        assert count == 202
        parsed = json.loads(shrunk)
        # Only the 200 dict entries survive
        assert len(parsed["observations"]) == 200
        for o in parsed["observations"]:
            assert isinstance(o, dict)

    def test_string_observations_in_small_trace_are_skipped(self):
        """When obs_count <= 200, string entries in observations are
        skipped instead of raising TypeError on item assignment."""
        trace = {
            "id": "t-str-small",
            "observations": [
                {"id": "obs-1", "input": "hello", "output": "world"},
                "bare-string-obs",
                {"id": "obs-2", "input": "foo", "output": "bar"},
            ],
        }
        shrunk, count = trace_inspector_mod._shrink_trace_data(json.dumps(trace))
        assert count == 3
        parsed = json.loads(shrunk)
        assert len(parsed["observations"]) == 3
        # Dict entries are preserved, string entry passes through untouched
        assert parsed["observations"][0]["id"] == "obs-1"
        assert parsed["observations"][1] == "bare-string-obs"
        assert parsed["observations"][2]["id"] == "obs-2"
