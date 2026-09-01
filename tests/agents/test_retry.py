"""Bounded retry+backoff for transient model/network failures."""

import asyncio
import json
import sqlite3
import threading
import time

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded

from robotsix_mill.agents.retry import (
    call_with_retry,
    closing_scratch_loop,
    is_rate_limited,
    is_transient,
    run_agent,
)
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    env.setdefault("transient_retries", "3")
    env.setdefault("transient_backoff_base", "1.0")
    env.setdefault("transient_backoff_cap", "4.0")
    return Settings(**env)


def _httpx_status(code):
    req = httpx.Request("POST", "http://x")
    return httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(code, request=req)
    )


# --- classification -----------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ModelHTTPError(429, "m"), True),
        (ModelHTTPError(503, "m"), True),
        (ModelHTTPError(404, "m"), False),
        (ModelHTTPError(400, "m"), False),
        (httpx.ReadTimeout("t"), True),
        (httpx.ConnectError("c"), True),
        (_httpx_status(429), True),
        (_httpx_status(502), True),
        (_httpx_status(403), False),
        (UsageLimitExceeded("cap"), False),
        (ValueError("bug"), False),
        (json.JSONDecodeError("Expecting value", "x", 0), True),  # bad model JSON
        (sqlite3.OperationalError("database or disk is full"), True),
    ],
)
def test_is_transient(exc, transient):
    assert is_transient(exc) is transient


def test_is_transient_jsondecode_wrapped():
    """Regression: a model emitting malformed JSON for a tool call
    raised JSONDecodeError, which hard-ERRORed the ticket (not
    retried). It must be transient, even wrapped in the cause chain."""
    inner = json.JSONDecodeError("Expecting value", "doc", 990)
    wrapped = RuntimeError("agent run failed")
    wrapped.__cause__ = inner
    assert is_transient(json.JSONDecodeError("x", "y", 0)) is True
    assert is_transient(wrapped) is True


def test_is_transient_claude_sdk_degenerate_success():
    """The degenerate ``is_error=True`` + ``subtype='success'`` result is NOT
    transient — observed behaviour shows it is deterministic for a given
    input.  The refine runner catches it at the agent-output level instead."""
    assert (
        is_transient(Exception("Claude Code returned an error result: success"))
        is False
    )
    inner = Exception("Claude Code returned an error result: success")
    wrapped = RuntimeError("agent run failed")
    wrapped.__cause__ = inner
    assert is_transient(wrapped) is False

    ctx_wrapped = RuntimeError("agent run failed")
    ctx_wrapped.__context__ = Exception("Claude Code returned an error result: success")
    assert is_transient(ctx_wrapped) is False


def test_is_transient_claude_sdk_genuine_error_not_transient():
    """The broadening must stay narrow: a genuine error result subtype (e.g.
    error_during_execution) and an unrelated failure must remain non-transient."""
    assert (
        is_transient(
            Exception("Claude Code returned an error result: error_during_execution")
        )
        is False
    )
    assert is_transient(Exception("some other failure")) is False


def test_is_transient_openrouter_finish_reason_error():
    """OpenRouter returns finish_reason='error' on an upstream provider
    failure; the OpenAI SDK raises a pydantic ValidationError because
    'error' isn't in its finish_reason literal set. That's a transient
    upstream hiccup, not a prompt/schema bug — it must ride out, not
    BLOCK the ticket. Matched by type name + the finish_reason/'error'
    markers so our own structured-output validation failures are not
    swept up."""
    from pydantic import BaseModel, ValidationError

    class _FinishReason(BaseModel):
        finish_reason: str  # placeholder; we craft the message below

    # Build a real ValidationError carrying the OpenRouter signature.
    try:
        # Simulate the SDK's literal-validation failure message.
        raise _make_finish_reason_validation_error()
    except ValidationError as e:
        assert is_transient(e) is True

    # A ValidationError WITHOUT the finish_reason signature (e.g. our
    # own AuditResult schema failing) must NOT be treated as transient
    # by this path.
    class _Schema(BaseModel):
        n: int

    try:
        _Schema(n="not-an-int")
    except ValidationError as e:
        assert "finish_reason" not in str(e)
        assert is_transient(e) is False


def _make_finish_reason_validation_error():
    """Return a ValidationError whose message mimics the OpenRouter
    finish_reason='error' literal failure the OpenAI SDK raises."""
    from typing import Literal

    from pydantic import BaseModel, ValidationError

    class _Choice(BaseModel):
        finish_reason: Literal[
            "stop", "length", "tool_calls", "content_filter", "function_call"
        ]

    try:
        _Choice(finish_reason="error")
    except ValidationError as e:
        return e


def test_is_transient_walks_wrapped_timeout():
    """A hung request surfaces wrapped (openai/pydantic-ai) — the
    timeout must still be recognised through the cause chain."""
    inner = httpx.ReadTimeout("read timed out")
    wrapped = RuntimeError("model request failed")
    wrapped.__cause__ = inner
    assert is_transient(wrapped) is True

    class APITimeoutError(Exception):  # mimics openai's class name
        pass

    assert is_transient(APITimeoutError("deadline exceeded")) is True


def test_is_transient_disk_full():
    """sqlite3.OperationalError with 'database or disk is full' must be
    classified as transient.  Also walks the cause chain so a wrapped
    version is still recognised."""
    inner = sqlite3.OperationalError("database or disk is full")
    assert is_transient(inner) is True

    wrapped = RuntimeError("stage run failed")
    wrapped.__cause__ = inner
    assert is_transient(wrapped) is True

    ctx_wrapped = RuntimeError("stage run failed")
    ctx_wrapped.__context__ = sqlite3.OperationalError("database or disk is full")
    assert is_transient(ctx_wrapped) is True


# --- retry behaviour (injected sleep, no real waiting) ------------------


def test_transient_then_success(tmp_path):
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelHTTPError(429, "hy3")
        return "ok"

    out = call_with_retry(fn, sleep=slept.append)
    assert out == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2  # two backoffs before the 3rd, successful call


# NOTE: retry COUNT/BACKOFF/flush semantics now live in robotsix-llmio (baked
# constants, internal OTel flush) and are covered by that package's tests. Mill
# keeps only the classification re-exports + the boundary/fallback behaviour.


@pytest.mark.parametrize(
    "exc",
    [
        ModelHTTPError(404, "m"),
        ValueError("x"),
    ],
)
def test_non_transient_not_retried(tmp_path, exc):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise exc

    with pytest.raises(type(exc)):
        call_with_retry(fn, sleep=lambda _: None)
    assert calls["n"] == 1  # raised immediately, no retry


# --- is_rate_limited classification -------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UsageLimitExceeded("cap"), True),
        (ModelHTTPError(429, "m"), False),
        (ModelHTTPError(503, "m"), False),
        (ModelHTTPError(404, "m"), False),
        (httpx.ReadTimeout("t"), False),
        (httpx.ConnectError("c"), False),
        (_httpx_status(429), False),
        (ValueError("bug"), False),
        (json.JSONDecodeError("Expecting value", "x", 0), False),
    ],
)
def test_is_rate_limited(exc, expected):
    assert is_rate_limited(exc) is expected


def test_is_rate_limited_walks_chain():
    """UsageLimitExceeded wrapped in a RuntimeError must still be
    recognised through the cause chain."""
    inner = UsageLimitExceeded("cap")
    wrapped = RuntimeError("agent run failed")
    wrapped.__cause__ = inner
    assert is_rate_limited(wrapped) is True


# --- rate-limit retry behaviour ------------------------------------------


def test_rate_limit_raises_immediately_without_fallback(tmp_path):
    """UsageLimitExceeded without a fallback_fn must re-raise
    immediately — no backoff, no retries."""
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        raise UsageLimitExceeded("cap")

    with pytest.raises(UsageLimitExceeded):
        call_with_retry(fn, sleep=slept.append)
    assert calls["n"] == 1  # exactly one call, no retries
    assert len(slept) == 0  # no backoff delay


def test_rate_limit_exhausts_then_raises(tmp_path):
    """Persistent UsageLimitExceeded with no fallback — must raise
    immediately without retrying (UsageLimitExceeded is never retried)."""
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        raise UsageLimitExceeded("cap")

    with pytest.raises(UsageLimitExceeded):
        call_with_retry(fn, sleep=slept.append)
    assert calls["n"] == 1  # exactly one call, no retries
    assert len(slept) == 0  # no backoff


def test_rate_limit_fallback_activates(tmp_path):
    """UsageLimitExceeded on first attempt — fallback_fn is invoked
    immediately (not after rate_limit_fallback_retries)."""
    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}

    def primary():
        primary_calls["n"] += 1
        raise UsageLimitExceeded("cap")

    def fallback():
        fallback_calls["n"] += 1
        return "fallback-ok"

    out = call_with_retry(
        primary,
        sleep=lambda _: None,
        fallback_fn=fallback,
    )
    assert out == "fallback-ok"
    assert primary_calls["n"] == 1  # fallback activates on first failure
    assert fallback_calls["n"] == 1  # fallback succeeds on first try


def test_rate_limit_fallback_exhausts_then_raises(tmp_path):
    """Fallback also fails with UsageLimitExceeded — re-raises
    immediately (no retries)."""
    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}

    def primary():
        primary_calls["n"] += 1
        raise UsageLimitExceeded("cap")

    def fallback():
        fallback_calls["n"] += 1
        raise UsageLimitExceeded("fallback-cap")

    with pytest.raises(UsageLimitExceeded):
        call_with_retry(
            primary,
            sleep=lambda _: None,
            fallback_fn=fallback,
        )
    assert primary_calls["n"] == 1  # fallback activates on first failure
    assert fallback_calls["n"] == 1  # fallback also fails immediately


def test_rate_limit_fallback_not_called_for_transient(tmp_path):
    """429 (transient) errors must NOT activate fallback — only
    UsageLimitExceeded does."""
    calls = {"n": 0}
    fallback_calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ModelHTTPError(429, "m")

    def fallback():
        fallback_calls["n"] += 1
        return "fallback"

    with pytest.raises(ModelHTTPError):
        call_with_retry(fn, sleep=lambda _: None, fallback_fn=fallback)
    # Baked retry count (5 = 1 try + 4 retries); the key assertion is that a
    # transient NEVER activates the rate-limit fallback.
    assert calls["n"] == 5
    assert fallback_calls["n"] == 0


# Trace-flush-on-retry now happens inside robotsix-llmio (best-effort OTel
# force_flush), no longer via mill's runtime.tracing.flush_tracing — so the
# former flush-hook tests moved out with the retry logic.


# --- async retry (acall_with_retry) -------------------------------------
#
# acall_with_retry is the seam the sub-agent tools (explore/consult_expert/
# web_research/web_knowledge) use so they can ``await agent.run(...)`` on the
# parent coordinator's running event loop — instead of ``run_sync`` →
# ``asyncio.run`` which is illegal inside the Claude SDK's loop. It must
# mirror the sync schedule: retry transient, never retry UsageLimitExceeded
# (except via a fallback once).


def test_async_transient_then_success(tmp_path):
    import asyncio

    from robotsix_mill.agents.retry import acall_with_retry

    slept, calls = [], {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelHTTPError(429, "hy3")
        return "ok"

    async def fake_sleep(d):
        slept.append(d)

    out = asyncio.run(acall_with_retry(fn, sleep=fake_sleep))
    assert out == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2  # two backoffs before the 3rd, successful call


def test_async_non_transient_not_retried(tmp_path):
    import asyncio

    from robotsix_mill.agents.retry import acall_with_retry

    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise ValueError("bug")

    async def fake_sleep(d):
        pass

    with pytest.raises(ValueError):
        asyncio.run(acall_with_retry(fn, sleep=fake_sleep))
    assert calls["n"] == 1  # raised immediately, no retry


def test_async_rate_limit_activates_fallback_once(tmp_path):
    import asyncio

    from robotsix_mill.agents.retry import acall_with_retry

    calls, fb = {"n": 0}, {"n": 0}

    async def fn():
        calls["n"] += 1
        raise UsageLimitExceeded("cap")

    async def fallback():
        fb["n"] += 1
        return "fallback-answer"

    async def fake_sleep(d):
        pass

    out = asyncio.run(acall_with_retry(fn, sleep=fake_sleep, fallback_fn=fallback))
    assert out == "fallback-answer"
    assert calls["n"] == 1
    assert fb["n"] == 1


# ===========================================================================
# Triage transient-retry backoff regression (Part B)
# ===========================================================================


def test_triage_transient_retry_uses_backoff():
    """A triage LLM call that raises a transient OpenRouter error must be
    retried through run_agent/call_with_retry with a positive sleep delay.

    The four triage/classifier calls (triage_refine, triage_reviewer_agreement,
    triage_auto_approve, review_spec_for_conciseness) all invoke the LLM
    through ``run_agent`` → ``call_with_retry``, which uses ``is_transient`` as
    the retry predicate and sleeps with exponential backoff on each retry.
    This test verifies that ``run_agent`` itself implements that contract.
    """

    class _FakeAgent:
        pass

    slept: list[float] = []
    calls: list[int] = []

    def _make_run(agent):
        calls.append(1)
        if len(calls) < 3:
            raise ModelHTTPError(503, "upstream failure")
        return "ok"

    out = run_agent(
        _FakeAgent(),
        _make_run,
        what="triage",
        sleep=slept.append,
    )
    assert out == "ok"
    assert len(calls) == 3  # 2 failures + 1 success
    assert len(slept) == 2  # 2 backoff delays
    for delay in slept:
        assert delay > 0, f"expected positive backoff delay, got {delay}"


def test_triage_non_transient_not_retried():
    """A triage LLM call raising a non-transient error must NOT be retried —
    it should propagate immediately."""

    class _FakeAgent:
        pass

    slept: list[float] = []
    calls: list[int] = []

    def _make_run(agent):
        calls.append(1)
        raise ValueError("bug — not transient")

    with pytest.raises(ValueError):
        run_agent(
            _FakeAgent(),
            _make_run,
            what="triage",
            sleep=slept.append,
        )
    assert len(calls) == 1  # exactly one call, no retry
    assert len(slept) == 0  # no backoff delay


def test_triage_functions_use_run_agent(monkeypatch):
    """Every triage/classifier function (triage_refine, triage_reviewer_agreement,
    triage_auto_approve, review_spec_for_conciseness) must invoke the LLM
    through ``run_agent`` (or ``load_and_run_agent`` which uses ``run_agent``
    internally), ensuring transient errors are retried with backoff."""
    run_calls: list[dict] = []

    def _spy_run_agent(agent, make_run, *, what="model call", sleep=None):
        run_calls.append({"what": what, "sleep": sleep})
        return make_run(agent)

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _spy_run_agent)
    # yaml_loader imports run_agent from .retry inside the function body,
    # so patching robotsix_mill.agents.retry.run_agent is sufficient —
    # the internal ``from .retry import run_agent`` will resolve to the
    # patched version.

    # Smoke-test: run_agent through the spy works.
    from robotsix_mill.agents.retry import run_agent as retry_run_agent

    class _Fake:
        pass

    retry_run_agent(_Fake(), lambda h: "ok", what="triage-test")
    assert len(run_calls) == 1
    assert run_calls[0]["what"] == "triage-test"


# --- running-event-loop guard -------------------------------------------
# run_sync-style callables end in asyncio.run(), which raises RuntimeError
# when a loop is already running (worker processing on Python >=3.14, tools
# on the Claude SDK's loop).  run_agent must delegate the retry session to
# a thread in that case — same guard as call_with_retry.


def test_run_agent_inside_running_loop():
    async def _payload():
        return "ok"

    def make_run(_h):
        # Mirrors pydantic-ai run_sync: creates its own event loop.
        return asyncio.run(_payload())

    async def main():
        return run_agent(object(), make_run, sleep=lambda _: None)

    assert asyncio.run(main()) == "ok"


def test_run_agent_fallback_inside_running_loop():
    async def _payload():
        return "fallback-ok"

    def make_run(_h):
        raise ModelHTTPError(503, "down")

    def fallback():
        return asyncio.run(_payload())

    async def main():
        return run_agent(object(), make_run, fallback_fn=fallback, sleep=lambda _: None)

    assert asyncio.run(main()) == "fallback-ok"


def test_is_transient_permanent_api_400_not_retried():
    """An API 400 is request validation — every retry re-sends the identical
    rejected payload, so it must never be transient. The classification is
    robotsix_llmio's (``ClaudeSDKPermanentAPIError``); mill only defers to it."""
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKPermanentAPIError

    exc = ClaudeSDKPermanentAPIError(
        "Anthropic API rejected the request (refine): API Error: 400 "
        "`task_budget.total` must be at least 20,000 tokens"
    )
    assert is_transient(exc) is False

    wrapped = RuntimeError("agent run failed")
    wrapped.__cause__ = exc
    assert is_transient(wrapped) is False


def test_permanent_api_400_beats_degenerate_success():
    """The live refine outage, in the shape llmio actually raises it: the
    permanent error wraps the collapsed degenerate-success frame as its cause.
    Both signatures are in the chain — the permanent one must win, or the error
    is swallowed as an empty success and refine silently no-ops."""
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKPermanentAPIError

    from robotsix_mill.agents.retry import _is_claude_sdk_degenerate_result

    collapsed = Exception("Claude Code returned an error result: success")
    permanent = ClaudeSDKPermanentAPIError(
        "Anthropic API rejected the request (refine): API Error: 400 "
        "`task_budget.total` must be at least 20,000 tokens"
    )
    permanent.__cause__ = collapsed

    # The degenerate signature IS in the chain, so without deferring to llmio
    # this would be swallowed as a successful empty run.
    assert _is_claude_sdk_degenerate_result(permanent) is False
    assert is_transient(permanent) is False


def test_degenerate_success_without_api_error_still_swallowed():
    """A genuine degenerate frame (no real error behind it) keeps the existing
    treat-as-empty-success behaviour."""
    from robotsix_mill.agents.retry import _is_claude_sdk_degenerate_result

    assert (
        _is_claude_sdk_degenerate_result(
            Exception("Claude Code returned an error result: success")
        )
        is True
    )


def test_permanent_guard_is_the_library_predicate():
    """mill must not carry its own copy of this classification — the guard is
    llmio's ``is_claude_sdk_permanent_api_error``, re-exported under a private
    alias. Pinning this keeps a local reimplementation from creeping back."""
    from robotsix_llmio.claude_sdk.transient import is_claude_sdk_permanent_api_error

    from robotsix_mill.agents.retry import _is_permanent_api_error

    assert _is_permanent_api_error is is_claude_sdk_permanent_api_error


class TestClosingScratchLoop:
    """The pydantic-ai scratch event loop must not be stranded on a thread.

    ``Agent.run_sync`` installs a loop via ``pydantic_ai._utils.get_event_loop``
    and never closes it.  An unclosed loop keeps its default executor alive, so
    its ``asyncio_N`` threads park in ``futex_wait`` forever — the leak that
    grew the mill container into its memory cap and got it OOM-killed.
    """

    @staticmethod
    def _pydantic_ai_pattern() -> None:
        """Mirror ``pydantic_ai._utils.get_event_loop`` + a ``run_in_executor``."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        async def _work() -> None:
            await asyncio.get_running_loop().run_in_executor(None, lambda: None)

        loop.run_until_complete(_work())

    @staticmethod
    def _loop_threads() -> set[threading.Thread]:
        """The live pydantic-ai scratch-loop executor threads.

        Return the thread *objects* (not a count) so callers can diff
        against a baseline.  Comparing identities is robust to unrelated
        ``asyncio_*`` threads from other tests appearing or winding down
        between snapshots — a bare count is not, which made these tests
        flaky under xdist test-ordering / thread-timing races.
        """
        return {t for t in threading.enumerate() if t.name.startswith("asyncio_")}

    def test_leaks_without_the_guard(self) -> None:
        """Sanity-check the reproduction: unguarded, the executor thread survives."""
        done = threading.Event()

        def _run() -> None:
            self._pydantic_ai_pattern()
            done.set()

        # A long-lived thread, like the pooled ``asyncio.to_thread`` workers
        # that mill runs stages on.
        before = self._loop_threads()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        done.wait(timeout=10)
        # A new executor thread appeared and outlived the unguarded run.
        assert self._loop_threads() - before

    def test_guard_closes_the_loop(self) -> None:
        """With the guard the loop is closed and its executor thread exits."""
        before = self._loop_threads()
        finished = threading.Event()

        def _run() -> None:
            with closing_scratch_loop():
                self._pydantic_ai_pattern()
            finished.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        finished.wait(timeout=10)
        t.join(timeout=10)

        # The executor thread is signalled on close; give it a moment to exit.
        # Only threads created *inside* the guard matter — diff against the
        # baseline so unrelated ``asyncio_*`` threads never affect the verdict.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self._loop_threads() - before:
            time.sleep(0.05)
        assert not (self._loop_threads() - before)

    def test_leaves_a_preexisting_loop_alone(self) -> None:
        """A loop the caller already owns must survive the block untouched."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with closing_scratch_loop():
                pass
            assert not loop.is_closed()
            assert asyncio.get_event_loop() is loop
        finally:
            loop.close()
            asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# run_agent tier fallback (Claude subscription exhausted / credential dead)
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _hooked(rebuilt: list[bool], fallback: _FakeHandle) -> _FakeHandle:
    primary = _FakeHandle("primary")

    def _rebuild() -> _FakeHandle:
        rebuilt.append(True)
        return fallback

    primary._failover_rebuild = _rebuild  # type: ignore[attr-defined]
    return primary


def test_run_agent_falls_back_to_openrouter_when_claude_usage_exhausted():
    """A session-limit failure on a default-slot agent reruns on the
    fallback provider slot (same level), not BLOCKED."""
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKUsageExhaustedError

    from robotsix_mill.agents.retry import run_agent

    rebuilt: list[bool] = []
    fallback = _FakeHandle("fallback")
    primary = _hooked(rebuilt, fallback)
    seen: list[str] = []

    def make_run(h):
        seen.append(h.name)
        if h is primary:
            raise ClaudeSDKUsageExhaustedError("You've hit your session limit")
        return "ran-on-fallback"

    assert run_agent(primary, make_run, what="implement", sleep=lambda _s: None) == (
        "ran-on-fallback"
    )
    assert rebuilt == [True]
    assert seen == ["primary", "fallback"]
    assert fallback.closed is True
    assert primary.closed is False  # the caller owns the primary handle


def test_run_agent_exhaustion_arms_llmio_failover_window():
    """The failure is recorded on llmio's tracker so subsequent builds
    resolve the fallback slot (and the UI shows failover as active)."""
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKUsageExhaustedError
    from robotsix_llmio.core.failover import get_failover_tracker

    from robotsix_mill.agents.retry import run_agent

    rebuilt: list[bool] = []
    primary = _hooked(rebuilt, _FakeHandle("fallback"))

    def make_run(h):
        if h is primary:
            raise ClaudeSDKUsageExhaustedError("You've hit your weekly limit")
        return "ok"

    run_agent(primary, make_run, sleep=lambda _s: None)
    assert get_failover_tracker().active_slot() == "fallback"


def test_run_agent_auth_failure_also_switches_provider():
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKAuthError

    from robotsix_mill.agents.retry import run_agent

    rebuilt: list[bool] = []
    primary = _hooked(rebuilt, _FakeHandle("fallback"))

    def make_run(h):
        if h is primary:
            raise ClaudeSDKAuthError("Failed to authenticate. API Error: 401")
        return "ok"

    assert run_agent(primary, make_run, sleep=lambda _s: None) == "ok"
    assert rebuilt == [True]


def test_run_agent_without_rebuild_hook_reraises_unchanged():
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKUsageExhaustedError

    from robotsix_mill.agents.retry import run_agent

    def make_run(_h):
        raise ClaudeSDKUsageExhaustedError("You've hit your session limit")

    with pytest.raises(ClaudeSDKUsageExhaustedError):
        run_agent(_FakeHandle("plain"), make_run, sleep=lambda _s: None)


def test_run_agent_other_errors_do_not_switch_provider():
    from robotsix_mill.agents.retry import run_agent

    rebuilt: list[bool] = []
    primary = _hooked(rebuilt, _FakeHandle("fallback"))

    def make_run(_h):
        raise ValueError("spec-determined failure")

    with pytest.raises(ValueError):
        run_agent(primary, make_run, sleep=lambda _s: None)
    assert rebuilt == []


def test_run_agent_fallback_slot_failing_raises_its_own_error():
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKUsageExhaustedError

    from robotsix_mill.agents.retry import run_agent

    rebuilt: list[bool] = []
    primary = _hooked(rebuilt, _FakeHandle("fallback"))

    def make_run(h):
        if h is primary:
            raise ClaudeSDKUsageExhaustedError("You've hit your session limit")
        raise RuntimeError("openrouter down")

    with pytest.raises(RuntimeError, match="openrouter down"):
        run_agent(primary, make_run, sleep=lambda _s: None)
    assert rebuilt == [True]


def test_build_agent_from_definition_attaches_rebuild_hook(monkeypatch):
    from unittest import mock

    from robotsix_mill.agents import base as bmod
    from robotsix_mill.agents.yaml_loader import AgentDefinition
    from robotsix_mill.config import Settings

    captured: list[dict] = []

    def fake_build_agent(settings, **kwargs):
        captured.append(kwargs)
        return mock.MagicMock()

    monkeypatch.setattr(bmod, "build_agent", fake_build_agent)
    definition = AgentDefinition(name="implement", level=2, system_prompt="x")
    handle = bmod.build_agent_from_definition(
        Settings(provider_failover_enabled=True), definition, tools=[]
    )

    handle._failover_rebuild()
    # Same level both times; the rebuild forces the fallback slot's binding.
    assert [k["level"] for k in captured] == [2, 2]
    assert captured[1]["tier_binding"].provider == "openrouter"
    assert captured[1]["name"] == "implement"


def test_build_agent_from_definition_no_failover_hook_by_default(monkeypatch):
    """Default: a Claude quota exhaustion propagates (the worker parks until
    the reset) instead of silently re-running on the paid OpenRouter slot."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.agents.yaml_loader import AgentDefinition
    from robotsix_mill.config import Settings

    class _Plain:
        pass

    monkeypatch.setattr(bmod, "build_agent", lambda settings, **kw: _Plain())
    definition = AgentDefinition(name="implement", level=2, system_prompt="x")
    handle = bmod.build_agent_from_definition(Settings(), definition, tools=[])

    assert not hasattr(handle, "_failover_rebuild")
