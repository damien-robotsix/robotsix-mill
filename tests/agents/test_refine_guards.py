from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    RefineResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core.states import State
from robotsix_mill.stages.refine import RefineStage
from tests.agents._refine_test_helpers import (
    _FakeRunResult,
    _FakeUsage,
    _simple_agent,
)
from tests.agents.conftest import (
    _single,
)

# ---------------------------------------------------------------------------
# Continuation guard tests (finish_reason == "tool_call")
# ---------------------------------------------------------------------------


def test_continuation_guard_fires_on_tool_calls(monkeypatch, settings):
    """When finish_reason == 'tool_call', the guard triggers a single
    continuation call with message_history=all_messages() and returns
    the continuation's RefineResult."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    first_messages = [{"role": "tool", "content": "tool result"}]
    expected_spec = RefineResult(split=False, spec_markdown="## Problem\nfixed\n")

    # Track run_sync calls so we can assert on the continuation prompt
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(
                {"user_prompt": user_prompt, "message_history": message_history}
            )
            if len(run_sync_calls) == 1:
                return _FakeRunResult(
                    output=RefineResult(split=False, spec_markdown=""),
                    finish_reason="tool_call",
                    all_messages=first_messages,
                )
            else:
                return _FakeRunResult(
                    output=expected_spec,
                    finish_reason="stop",
                    all_messages=[
                        *first_messages,
                        {"role": "assistant", "content": "done"},
                    ],
                )

        def close(self):
            pass

    mock_agent = _MockAgent()

    # run_agent: pass-through so the run executes directly on the handle
    def pass_through_retry(agent, make_run, *, what="model call", sleep=None):
        return make_run(agent)

    monkeypatch.setattr(retry_module, "run_agent", pass_through_retry)
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: mock_agent
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="original draft",
    )

    # Guard must have fired: exactly two run_sync calls
    assert len(run_sync_calls) == 2, (
        f"Expected 2 run_sync calls (original + continuation), got {len(run_sync_calls)}"
    )

    # Continuation must carry the first call's messages as history
    assert run_sync_calls[1]["message_history"] == first_messages, (
        "Continuation call must receive message_history=all_messages() from first result"
    )

    # Continuation prompt must contain the synthesis instruction
    assert "synthesise a final answer" in run_sync_calls[1]["user_prompt"].lower(), (
        "Continuation user prompt must instruct the model to synthesise a final answer"
    )

    # Final output must be the continuation's RefineResult
    assert output.spec_markdown == "## Problem\nfixed\n"


def test_continuation_guard_not_triggered_on_stop(monkeypatch, settings):
    """When finish_reason == 'stop', no continuation occurs — the
    original result passes through unchanged."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    expected_spec = RefineResult(split=False, spec_markdown="## Problem\nok\n")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=expected_spec,
                finish_reason="stop",
                all_messages=[{"role": "user", "content": "hi"}],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Only one call — no continuation
    assert len(run_sync_calls) == 1
    assert output.spec_markdown == "## Problem\nok\n"


def test_continuation_guard_skipped_when_response_missing(monkeypatch, settings):
    """When result.response is None (missing attribute), the guard is
    skipped entirely — no AttributeError, no continuation."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    expected_spec = RefineResult(split=False, spec_markdown="## Problem\nok\n")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=expected_spec,
                finish_reason=None,  # response will be None
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Guard skipped — exactly one call
    assert len(run_sync_calls) == 1
    assert output.spec_markdown == "## Problem\nok\n"


def test_continuation_guard_skipped_when_already_valid_output(monkeypatch, settings):
    """When finish_reason == 'tool_call' but the agent already produced a
    valid RefineResult in an earlier turn, skip the continuation to avoid
    burning quota on verification loops."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    # A RefineResult with real content — spec_markdown is non-empty.
    valid_output = RefineResult(split=False, spec_markdown="## Problem\ndone\n")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=valid_output,
                finish_reason="tool_call",
                all_messages=[{"role": "tool", "content": "verify"}],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Pre-output guard fires: continuation skipped, only one call
    assert len(run_sync_calls) == 1
    assert output.spec_markdown == "## Problem\ndone\n"


def test_continuation_guard_skipped_when_low_remaining_quota(monkeypatch, settings):
    """When finish_reason == 'tool_call' but remaining requests ≤ 5,
    skip the continuation to avoid failing mid-turn."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    empty_output = RefineResult(split=False, spec_markdown="")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=empty_output,
                finish_reason="tool_call",
                all_messages=[{"role": "tool", "content": "verify"}],
                # Simulate all-but-3 requests already used → 3 remaining
                usage=_FakeUsage(requests=settings.refine_request_limit - 3),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Quota guard fires: continuation skipped, only one call
    assert len(run_sync_calls) == 1
    # Empty spec_markdown — the raw output (coerced to RefineResult)
    assert output.spec_markdown == ""


# ---------------------------------------------------------------------------
# --- run_refine_agent no longer short-circuits from memory ---
# The deterministic _check_memory_for_no_change function and its
# Jaccard-based short-circuit have been removed per review directive.
# Memory is still passed to the LLM as context, but no_change_needed
# can only originate from the LLM's structured output. The following
# test verifies that run_refine_agent does NOT skip the LLM call even
# when a matching memory entry exists.


def test_run_refine_agent_no_memory_short_circuit(
    monkeypatch,
    settings,
):
    """run_refine_agent does NOT return no_change_needed from memory
    alone — the LLM must be invoked to decide."""

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    agent_called = False

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            nonlocal agent_called
            agent_called = True
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    result = refining.run_refine_agent(
        settings=settings,
        title="some title",
        draft="some draft",
        memory="",
    )

    # The full refine agent WAS called (no memory short-circuit).
    assert agent_called is True
    # Must NOT short-circuit to no_change_needed on memory alone.
    assert result.no_change_needed is False
    assert result.spec_markdown == "## Problem\nok\n"


def test_run_refine_agent_no_match_proceeds_to_llm(monkeypatch, settings):
    """run_refine_agent always proceeds to the LLM path — there is no
    memory-based short-circuit."""

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    agent_called = False

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            nonlocal agent_called
            agent_called = True
            # Return a simple valid result to avoid continuation guard
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    result = refining.run_refine_agent(
        settings=settings,
        title="completely different ticket",
        draft="something else entirely",
        memory="",
    )

    assert agent_called is True
    assert result.no_change_needed is False
    assert result.spec_markdown == "## Problem\nok\n"


def test_run_refine_agent_passes_request_limit(monkeypatch, settings):
    """run_refine_agent bounds its tool loop with
    ``UsageLimits(request_limit=settings.refine_request_limit)`` on its
    run_sync call (mirrors the explore-agent capture pattern)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured: dict = {}

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            captured["usage_limits"] = usage_limits
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    monkeypatch.setattr(settings, "refine_request_limit", 23)
    refining.run_refine_agent(settings=settings, title="t", draft="d")

    assert captured["usage_limits"] is not None
    assert captured["usage_limits"].request_limit == 23


# ---------------------------------------------------------------------------
# deterministic <test-warnings> injection for warnings-hardening refines
# ---------------------------------------------------------------------------


def test_test_warnings_block_skips_non_warnings_ticket(tmp_path):
    s = Settings(data_dir=str(tmp_path))
    assert (
        refining._collect_test_warnings_block("Refactor the CLI parser", tmp_path, s)
        == ""
    )


def test_test_warnings_block_skips_when_no_repo(tmp_path):
    s = Settings(data_dir=str(tmp_path))
    assert (
        refining._collect_test_warnings_block(
            "Add filterwarnings = error to pytest", None, s
        )
        == ""
    )


def test_test_warnings_block_injects_summary(tmp_path, monkeypatch):
    """A warnings-hardening draft triggers ONE sandbox run and injects the
    summary as a <test-warnings> block telling the agent not to re-run."""
    from robotsix_mill import sandbox

    s = Settings(data_dir=str(tmp_path))
    calls = {}

    def fake_run(cmd, *, repo_dir, settings, install_project=False):
        calls["cmd"] = cmd
        calls["install"] = install_project
        return 0, "=== warnings summary ===\nsrc/x.py:1: DeprecationWarning: old\n===="

    monkeypatch.setattr(sandbox, "run", fake_run)
    out = refining._collect_test_warnings_block(
        "Add filterwarnings = error to pytest config with documented ignores",
        tmp_path,
        s,
    )
    assert "test-warnings" in out
    assert "DeprecationWarning" in out
    assert "do not run the test suite" in out.lower()
    assert calls["install"] is True  # deps installed so warnings are real
    assert "pytest" in calls["cmd"]


def test_test_warnings_block_best_effort_on_sandbox_failure(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = Settings(data_dir=str(tmp_path))

    def boom(*a, **k):
        raise sandbox.SandboxError("docker unavailable")

    monkeypatch.setattr(sandbox, "run", boom)
    assert (
        refining._collect_test_warnings_block("filterwarnings hardening", tmp_path, s)
        == ""
    )


def test_test_warnings_block_empty_output(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(sandbox, "run", lambda *a, **k: (0, "   "))
    assert (
        refining._collect_test_warnings_block("make warnings strict", tmp_path, s) == ""
    )


class TestIsInternalToolchainFailure:
    """Unit tests for ``is_internal_toolchain_failure`` — the predicate
    that gates ``ask_web_knowledge`` during refine when the draft carries
    CI/type/lint/test failure signals."""

    def test_traceback_positive(self):
        """Draft containing a Python traceback → True."""
        assert refining.is_internal_toolchain_failure(
            "Traceback (most recent call last):\n  File ..."
        )

    def test_pytest_failed_summary_positive(self):
        """Draft with a pytest ``FAILED`` summary line → True."""
        assert refining.is_internal_toolchain_failure(
            "FAILED tests/foo.py::test_bar - AssertionError"
        )

    def test_pytest_failures_section_positive(self):
        """Draft with ``= FAILURES =`` → True."""
        assert refining.is_internal_toolchain_failure(
            "= FAILURES =\n something went wrong"
        )

    def test_pytest_short_test_summary_positive(self):
        """Draft with ``short test summary`` → True."""
        assert refining.is_internal_toolchain_failure("short test summary info")

    def test_mypy_arg_type_positive(self):
        """Draft with a mypy ``error: ... [arg-type]`` line → True."""
        assert refining.is_internal_toolchain_failure(
            'src/foo.py:10: error: Argument 1 to "f" has incompatible type '
            '"int"; expected "str"  [arg-type]'
        )

    def test_ruff_code_positive(self):
        """Draft explicitly mentioning ``ruff`` → True."""
        assert refining.is_internal_toolchain_failure(
            "ruff check failed: E501 line too long"
        )

    def test_flake8_code_positive(self):
        """Draft with an ``F401`` lint code → True."""
        assert refining.is_internal_toolchain_failure("unused import: F401")

    def test_exit_code_positive(self):
        """Draft with ``exit code`` → True."""
        assert refining.is_internal_toolchain_failure("mypy exited with exit code 1")

    def test_feature_draft_negative(self):
        """Ordinary feature draft with no failure markers → False."""
        assert not refining.is_internal_toolchain_failure(
            "Add a new endpoint to the API that returns user preferences."
        )

    def test_bare_test_word_negative(self):
        """Draft that mentions 'test' but no failure markers → False
        (predicate is conservative)."""
        assert not refining.is_internal_toolchain_failure(
            "We should test the new feature thoroughly."
        )

    def test_case_insensitive(self):
        """Markers are matched case-insensitively."""
        assert refining.is_internal_toolchain_failure(
            "TRACEBACK (most recent call last):"
        )

    # -- bare tool NAMES must NOT trip the predicate (false-positive fix) --
    # A normal feature spec routinely mentions tooling; matching the bare
    # tool name replaced the real spec with a generic "fix the failing
    # check" template across many tickets.

    def test_feature_spec_mentioning_pytest_negative(self):
        """A feature spec that says it will ship pytest tests → False."""
        assert not refining.is_internal_toolchain_failure(
            "Implement the runner and ship pytest async tests for both the "
            "success and failure paths."
        )

    def test_feature_spec_mentioning_mypy_hook_negative(self):
        """A spec about fixing a mypy pre-commit hook → False (no output)."""
        assert not refining.is_internal_toolchain_failure(
            "Fix the pre-commit mypy hook: use a local system hook so it runs "
            "against the project environment."
        )

    def test_feature_spec_mentioning_ruff_config_negative(self):
        """A spec about adding ruff/vulture/bandit config → False."""
        assert not refining.is_internal_toolchain_failure(
            "Add a ruff config and a bandit + vulture pass to the dev tooling."
        )

    def test_prose_failed_word_negative(self):
        """Lowercase 'failed'/'failure' in prose must NOT match the pytest
        ``FAILED `` marker (it is case-sensitive)."""
        assert not refining.is_internal_toolchain_failure(
            "On a failed connection, retry; cover the success and failure paths."
        )

    def test_pytest_uppercase_failed_positive(self):
        """Real pytest ``FAILED `` output (uppercase) still matches."""
        assert refining.is_internal_toolchain_failure(
            "FAILED tests/test_x.py::test_y - AssertionError: nope"
        )


class TestRefineTraceWebBudgetDefaults:
    """The refine stage reuses the proven per-trace web budget helpers
    (``reset_trace_web_fetch_budget`` / ``reset_trace_web_search_budget``)
    that ``run_refine_agent`` resets at the start of each run. Mirrors the
    survey trace-budget mechanism — once the cap is set with refine's
    defaults, the 6th+ fetch/search is refused with the budget-exhausted
    sentinel instead of executing."""

    def test_refine_web_fetch_default_cap(self, tmp_path, monkeypatch):
        """With refine's default cap (5), the 6th cache-miss web_fetch in
        one trace returns the budget-exhausted sentinel without fetching."""
        from robotsix_mill import sandbox
        from robotsix_mill.agents.web_tools import (
            _cache,
            make_web_fetch,
            reset_trace_web_fetch_budget,
            reset_web_fetch_budget,
        )

        s = Settings(data_dir=str(tmp_path))
        assert s.refine_web_fetch_max_calls == 5
        assert s.refine_web_fetch_max_total_bytes == 500_000

        _cache.clear()
        reset_web_fetch_budget()
        # Exactly what run_refine_agent does at the start of a trace.
        reset_trace_web_fetch_budget(
            s.refine_web_fetch_max_calls,
            s.refine_web_fetch_max_total_bytes,
        )

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, f"body for {url}"

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        # First 5 distinct URLs succeed (consume the trace budget).
        for i in range(5):
            assert wf(f"https://x.test/{i}") == f"body for https://x.test/{i}"
        assert len(calls) == 5

        # Per-consult reset does NOT clear the trace counters.
        reset_web_fetch_budget()

        # 6th distinct URL is refused by the trace budget.
        out = wf("https://x.test/6th")
        assert "trace budget exhausted" in out.lower()
        assert len(calls) == 5  # no new fetch

    def test_refine_web_search_default_cap(self, tmp_path, monkeypatch):
        """With refine's default cap (5), the 6th web_search in one trace
        returns the budget-exhausted sentinel."""
        import asyncio

        from robotsix_mill.agents.web_knowledge import (
            _make_tools,
            reset_trace_web_search_budget,
        )

        s = Settings(data_dir=str(tmp_path))
        assert s.refine_web_search_max_calls == 5

        async def fake_run_web_research(*, settings, query):
            return f"conclusion for: {query}"

        import robotsix_mill.agents.web_research as wr_mod

        monkeypatch.setattr(wr_mod, "run_web_research", fake_run_web_research)

        reset_trace_web_search_budget(s.refine_web_search_max_calls)
        tools = _make_tools(s)
        web_search = tools[-1]  # web_search is the last tool

        # First 5 searches succeed.
        for i in range(5):
            assert asyncio.run(web_search(f"query {i}")) == f"conclusion for: query {i}"

        # 6th search hits the trace budget cap.
        r6 = asyncio.run(web_search("query 6"))
        assert "web_search trace budget exhausted" in r6


class TestRefineRunawayLoopGuard:
    """The refine run caps total tool calls (``tool_calls_limit``) and
    wraps the assembled tools with the shared error-counter, mirroring
    test_gap / trace_inspector. Only the pathological runaway tail is
    terminated; the normal path is unchanged."""

    def test_refine_usage_limits_and_error_wrapper(self, tmp_path, monkeypatch):
        from robotsix_mill.agents import base, retry, trace_inspector

        s = Settings(data_dir=str(tmp_path))

        captured: dict = {}

        real_wrap = trace_inspector._wrap_tools_with_error_limit

        def spy_wrap(tools, max_errors):
            captured["max_errors"] = max_errors
            return real_wrap(tools, max_errors)

        monkeypatch.setattr(trace_inspector, "_wrap_tools_with_error_limit", spy_wrap)

        class FakeResult:
            output = RefineResult(spec_markdown="ok")
            response = type("R", (), {"finish_reason": "stop"})()

            def all_messages_json(self):
                return b"[]"

            def new_messages_json(self):
                return b"[]"

        class FakeAgent:
            def run_sync(self, prompt, *, message_history=None, usage_limits=None):
                captured["usage_limits"] = usage_limits
                return FakeResult()

        monkeypatch.setattr(
            base, "build_agent_from_definition", lambda *a, **k: FakeAgent()
        )
        monkeypatch.setattr(base, "_safe_close", lambda *a, **k: None)
        monkeypatch.setattr(retry, "run_agent", lambda agent, fn, what: fn(agent))

        out = refining.run_refine_agent(settings=s, title="t", draft="d", repo_dir=None)
        assert out.spec_markdown == "ok"

        limits = captured["usage_limits"]
        assert limits.tool_calls_limit == s.refine_max_tool_calls
        assert limits.request_limit == s.refine_request_limit
        assert captured["max_errors"] == s.refine_max_errors


# ---------------------------------------------------------------------------
# Exploration gating tests (cost-saving: skip explore/parallel_explore
# for simple tickets)
# ---------------------------------------------------------------------------


def test_max_refine_explore_calls_default():
    """Default max_refine_explore_calls is 4."""
    s = Settings(data_dir="/tmp")
    assert s.max_refine_explore_calls == 4


def test_max_refine_explore_calls_zero_disables():
    """max_refine_explore_calls=0 disables exploration cap enforcement."""
    from robotsix_mill.config import Settings as S

    s = S(data_dir="/tmp", max_refine_explore_calls="0")
    assert s.max_refine_explore_calls == 0


def test_build_repo_tools_include_explore_false(tmp_path):
    """With include_explore=False, the explore tool is NOT in the returned list."""
    from robotsix_mill.agents._repo_tools import _build_repo_tools
    from robotsix_mill.config import Settings as S

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    s = S(data_dir=str(tmp_path))

    tools = _build_repo_tools(
        repo_dir, s, include_explore=False, include_parallel_explore=False
    )
    tool_names = {t.__name__ for t in tools}
    assert "explore" not in tool_names, (
        "explore should be absent when include_explore=False"
    )
    assert "parallel_explore" not in tool_names, (
        "parallel_explore should be absent when include_parallel_explore=False"
    )
    assert "read_file" in tool_names, "read_file should still be present"


def test_build_repo_tools_include_explore_true_default(tmp_path):
    """With include_explore=True (default), explore IS in the returned list."""
    from robotsix_mill.agents._repo_tools import _build_repo_tools
    from robotsix_mill.config import Settings as S

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    s = S(data_dir=str(tmp_path))

    tools = _build_repo_tools(
        repo_dir, s, include_explore=True, include_parallel_explore=True
    )
    tool_names = {t.__name__ for t in tools}
    assert "explore" in tool_names
    assert "parallel_explore" in tool_names


def test_run_refine_agent_respects_include_explore_false(
    monkeypatch, settings, tmp_path
):
    """When include_explore=False, run_refine_agent builds tools without
    explore/parallel_explore, and the log records 'skipped'."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Simple ticket",
        draft="Fix typo in README.md",
        repo_dir=repo_dir,
        include_explore=False,
        include_parallel_explore=False,
    )

    tool_names = {t.__name__ for t in captured_tools if hasattr(t, "__name__")}
    assert "explore" not in tool_names
    assert "parallel_explore" not in tool_names
    assert "read_file" in tool_names
    assert "list_dir" in tool_names
    assert "run_command" in tool_names


def test_sendback_agent_has_no_filesystem_tools(monkeypatch, settings, tmp_path):
    """When reviewer_comments is truthy, the agent built for refine
    cannot call explore, parallel_explore, read_file, list_dir, or
    run_command — but can still call reply_to_thread, close_thread,
    and report_issue."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Sendback ticket",
        draft="Fix the scope per feedback",
        repo_dir=repo_dir,
        reviewer_comments="Please tighten the acceptance criteria",
    )

    tool_names = {t.__name__ for t in captured_tools if hasattr(t, "__name__")}
    # Filesystem/exploration tools must be absent.
    for forbidden in (
        "explore",
        "parallel_explore",
        "read_file",
        "list_dir",
        "run_command",
    ):
        assert forbidden not in tool_names, (
            f"sendback agent must not have '{forbidden}' tool, "
            f"got tools: {sorted(tool_names)}"
        )
    # Thread-management tools must be present (they're auto-injected by
    # build_agent based on reply_to_thread/close_thread/report_issue flags,
    # not via the explicit tools list — so they won't show up here).
    # The sendback overrides set reply_to_thread=True and close_thread=True,
    # and report_issue remains True from the YAML definition.  We verify
    # those flags are in the build_agent kwargs (tested separately in
    # test_sendback_enables_reply_and_close_thread_tools).


def test_first_refine_has_full_tool_set(monkeypatch, settings, tmp_path):
    """Without reviewer_comments, the full tool set is available
    (explore, parallel_explore, read_file, list_dir, run_command)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="First refine",
        draft="Add a new feature",
        repo_dir=repo_dir,
        # no reviewer_comments
    )

    tool_names = {t.__name__ for t in captured_tools if hasattr(t, "__name__")}
    for required in ("explore", "read_file", "list_dir", "run_command"):
        assert required in tool_names, (
            f"first-refine agent must have '{required}' tool, "
            f"got tools: {sorted(tool_names)}"
        )


def test_run_refine_agent_explore_cap_enforcement(monkeypatch, settings, tmp_path):
    """When max_refine_explore_calls=1, the 2nd explore call is rejected
    with a cap-exhausted message."""
    import asyncio

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    # Cap exploration at 1 call.
    capped = Settings(data_dir=str(tmp_path), max_refine_explore_calls="1")

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=capped,
        title="Test",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=True,
        include_parallel_explore=True,
    )

    explore_tool = None
    for t in captured_tools:
        if getattr(t, "__name__", "") == "explore":
            explore_tool = t
            break
    assert explore_tool is not None, "explore tool must be present"

    # First call should succeed.
    r1 = asyncio.run(explore_tool("question one"))
    assert "cap reached" not in r1.lower()

    # Second call should be rejected (cap = 1).
    r2 = asyncio.run(explore_tool("question two"))
    assert "exploration cap reached" in r2.lower()
    assert "1/1" in r2


def test_run_refine_agent_explore_cap_zero_disables_enforcement(
    monkeypatch, settings, tmp_path
):
    """When max_refine_explore_calls=0, exploration is never capped."""
    import asyncio

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    capped = Settings(data_dir=str(tmp_path), max_refine_explore_calls="0")

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=capped,
        title="Test",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=True,
        include_parallel_explore=True,
    )

    explore_tool = next(
        t for t in captured_tools if getattr(t, "__name__", "") == "explore"
    )

    # Multiple calls should all succeed (cap=0 disables enforcement).
    for i in range(10):
        r = asyncio.run(explore_tool(f"question {i}"))
        assert "cap reached" not in r.lower()


def test_run_refine_agent_logs_exploration_skipped(
    caplog, monkeypatch, settings, tmp_path
):
    """When include_explore=False, a structured log line records 'skipped'."""
    import logging

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    caplog.set_level(logging.INFO, logger="robotsix_mill.agents.refining")

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Simple",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=False,
        include_parallel_explore=False,
    )

    assert any("refine exploration: skipped" in r.message for r in caplog.records), (
        "Must log exploration skipped"
    )


def test_run_refine_agent_logs_exploration_invoked(
    caplog, monkeypatch, settings, tmp_path
):
    """When include_explore=True, a structured log line records 'invoked' + cap."""
    import logging

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    caplog.set_level(logging.INFO, logger="robotsix_mill.agents.refining")

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Complex",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=True,
        include_parallel_explore=True,
    )

    assert any("refine exploration: invoked" in r.message for r in caplog.records), (
        "Must log exploration invoked"
    )
    assert any("cap=4" in r.message for r in caplog.records), "Must log the cap value"


def test_triage_complexity_simple_suppresses_exploration(ctx, service, monkeypatch):
    """When triage returns REFINE with complexity='simple', run_refine_agent
    is called with include_explore=False and include_parallel_explore=False."""

    refine_kwargs: dict = {}

    from robotsix_mill.agents import pre_refine_classifier
    from robotsix_mill.agents.pre_refine_classifier import PreRefineClassifierResult

    monkeypatch.setattr(
        pre_refine_classifier,
        "run_pre_refine_classifier",
        lambda **kw: PreRefineClassifierResult(
            triage_decision="REFINE",
            triage_reason="single-file change",
            complexity="simple",
        ),
    )

    def spy_refine(**kwargs):
        refine_kwargs.update(kwargs)
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Fix typo in README", "Change one word in README.md line 3")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_kwargs.get("include_explore") is False
    assert refine_kwargs.get("include_parallel_explore") is False


def test_triage_complexity_needs_exploration_provides_exploration(
    ctx, service, monkeypatch
):
    """When triage returns REFINE with complexity='needs-exploration',
    run_refine_agent is called with include_explore=True and
    include_parallel_explore=True."""
    from robotsix_mill.agents.refining import TriageResult

    refine_kwargs: dict = {}

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE",
            reason="multi-file change, needs codebase search",
            complexity="needs-exploration",
        )

    def spy_refine(**kwargs):
        refine_kwargs.update(kwargs)
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Refactor auth", "multi-file auth refactor")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_kwargs.get("include_explore") is True
    assert refine_kwargs.get("include_parallel_explore") is True


def test_triage_complexity_none_defaults_to_needs_exploration(
    ctx, service, monkeypatch
):
    """When triage returns complexity=None, the default is 'needs-exploration'
    and run_refine_agent receives full exploration tools (conservative)."""
    from robotsix_mill.agents.refining import TriageResult

    refine_kwargs: dict = {}

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE",
            reason="ambiguous scope",
            complexity=None,  # backward-compat: no complexity field
        )

    def spy_refine(**kwargs):
        refine_kwargs.update(kwargs)
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Add feature", "ambiguous feature draft")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_kwargs.get("include_explore") is True
    assert refine_kwargs.get("include_parallel_explore") is True
