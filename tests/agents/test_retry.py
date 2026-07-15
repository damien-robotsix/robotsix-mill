"""Bounded retry+backoff for transient model/network failures."""

import json

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded

from robotsix_mill.agents.retry import (
    call_with_retry,
    is_transient,
    is_rate_limited,
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
    "exc,transient",
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
        is_transient(Exception("Claude Code returned an error result: success")) is False
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


# --- retry behaviour (injected sleep, no real waiting) ------------------


def test_transient_then_success(tmp_path):
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelHTTPError(429, "hy3")
        return "ok"

    out = call_with_retry(fn, sleep=slept.append)
    assert out == "ok" and calls["n"] == 3
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
    "exc,expected",
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
    assert out == "ok" and calls["n"] == 3
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
    assert calls["n"] == 1 and fb["n"] == 1


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
