"""Bounded retry+backoff for transient model/network failures."""

import json

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError

from robotsix_mill.agents.retry import call_with_retry, is_transient, is_rate_limited
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    env.setdefault("MILL_TRANSIENT_RETRIES", "3")
    env.setdefault("MILL_TRANSIENT_BACKOFF_BASE", "1.0")
    env.setdefault("MILL_TRANSIENT_BACKOFF_CAP", "4.0")
    return Settings(**env)


class _FakeUsageLimitExceeded(Exception):
    """Name matches the real pydantic-ai cap exception."""


_FakeUsageLimitExceeded.__name__ = "UsageLimitExceeded"


def _httpx_status(code):
    req = httpx.Request("POST", "http://x")
    return httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(code, request=req)
    )


# --- classification -----------------------------------------------------

@pytest.mark.parametrize("exc,transient", [
    (ModelHTTPError(429, "m"), True),
    (ModelHTTPError(503, "m"), True),
    (ModelHTTPError(404, "m"), False),
    (ModelHTTPError(400, "m"), False),
    (httpx.ReadTimeout("t"), True),
    (httpx.ConnectError("c"), True),
    (_httpx_status(429), True),
    (_httpx_status(502), True),
    (_httpx_status(403), False),
    (_FakeUsageLimitExceeded("cap"), False),
    (ValueError("bug"), False),
    (json.JSONDecodeError("Expecting value", "x", 0), True),  # bad model JSON
])
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


def test_timeout_http_client_uses_configured_timeout(tmp_path):
    from robotsix_mill.agents.base import timeout_http_client

    s = _settings(tmp_path, MILL_MODEL_REQUEST_TIMEOUT="42")
    c = timeout_http_client(s)
    assert c.timeout.read == 42.0  # hard per-request read timeout kills hangs
    assert c.timeout.connect == 15.0


# --- retry behaviour (injected sleep, no real waiting) ------------------

def test_transient_then_success(tmp_path):
    s = _settings(tmp_path)
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelHTTPError(429, "hy3")
        return "ok"

    out = call_with_retry(fn, settings=s, sleep=slept.append)
    assert out == "ok" and calls["n"] == 3
    assert len(slept) == 2  # two backoffs before the 3rd, successful call


def test_persistent_transient_exhausts_then_raises(tmp_path):
    s = _settings(tmp_path, MILL_TRANSIENT_RETRIES="3")
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        raise ModelHTTPError(429, "hy3")

    with pytest.raises(ModelHTTPError):
        call_with_retry(fn, settings=s, sleep=slept.append)
    assert calls["n"] == 4          # 1 try + 3 retries
    assert len(slept) == 3
    assert all(d <= s.transient_backoff_cap * 1.5 for d in slept)  # capped+jitter


@pytest.mark.parametrize("exc", [
    ModelHTTPError(404, "m"), ValueError("x"),
])
def test_non_transient_not_retried(tmp_path, exc):
    s = _settings(tmp_path)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise exc

    with pytest.raises(type(exc)):
        call_with_retry(fn, settings=s, sleep=lambda _: None)
    assert calls["n"] == 1  # raised immediately, no retry


def test_zero_retries_means_single_attempt(tmp_path):
    s = _settings(tmp_path, MILL_TRANSIENT_RETRIES="0")
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ModelHTTPError(429, "m")

    with pytest.raises(ModelHTTPError):
        call_with_retry(fn, settings=s, sleep=lambda _: None)
    assert calls["n"] == 1


# --- is_rate_limited classification -------------------------------------

@pytest.mark.parametrize("exc,expected", [
    (_FakeUsageLimitExceeded("cap"), True),
    (ModelHTTPError(429, "m"), False),
    (ModelHTTPError(503, "m"), False),
    (ModelHTTPError(404, "m"), False),
    (httpx.ReadTimeout("t"), False),
    (httpx.ConnectError("c"), False),
    (_httpx_status(429), False),
    (ValueError("bug"), False),
    (json.JSONDecodeError("Expecting value", "x", 0), False),
])
def test_is_rate_limited(exc, expected):
    assert is_rate_limited(exc) is expected


def test_is_rate_limited_walks_chain():
    """UsageLimitExceeded wrapped in a RuntimeError must still be
    recognised through the cause chain."""
    inner = _FakeUsageLimitExceeded("cap")
    wrapped = RuntimeError("agent run failed")
    wrapped.__cause__ = inner
    assert is_rate_limited(wrapped) is True


# --- rate-limit retry behaviour ------------------------------------------

def test_rate_limit_raises_immediately_without_fallback(tmp_path):
    """UsageLimitExceeded without a fallback_fn must re-raise
    immediately — no backoff, no retries."""
    s = _settings(tmp_path)
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        raise _FakeUsageLimitExceeded("cap")

    with pytest.raises(_FakeUsageLimitExceeded):
        call_with_retry(fn, settings=s, sleep=slept.append)
    assert calls["n"] == 1  # exactly one call, no retries
    assert len(slept) == 0   # no backoff delay


def test_rate_limit_exhausts_then_raises(tmp_path):
    """Persistent UsageLimitExceeded with no fallback — must raise
    immediately without retrying (UsageLimitExceeded is never retried)."""
    s = _settings(tmp_path, MILL_TRANSIENT_RETRIES="2")
    slept, calls = [], {"n": 0}

    def fn():
        calls["n"] += 1
        raise _FakeUsageLimitExceeded("cap")

    with pytest.raises(_FakeUsageLimitExceeded):
        call_with_retry(fn, settings=s, sleep=slept.append)
    assert calls["n"] == 1  # exactly one call, no retries
    assert len(slept) == 0   # no backoff


def test_rate_limit_fallback_activates(tmp_path):
    """UsageLimitExceeded on first attempt — fallback_fn is invoked
    immediately (not after rate_limit_fallback_retries)."""
    s = _settings(
        tmp_path,
        MILL_TRANSIENT_RETRIES="4",
        MILL_RATE_LIMIT_FALLBACK_RETRIES="3",
    )
    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}

    def primary():
        primary_calls["n"] += 1
        raise _FakeUsageLimitExceeded("cap")

    def fallback():
        fallback_calls["n"] += 1
        return "fallback-ok"

    out = call_with_retry(
        primary, settings=s, sleep=lambda _: None, fallback_fn=fallback,
    )
    assert out == "fallback-ok"
    assert primary_calls["n"] == 1   # fallback activates on first failure
    assert fallback_calls["n"] == 1  # fallback succeeds on first try


def test_rate_limit_fallback_exhausts_then_raises(tmp_path):
    """Fallback also fails with UsageLimitExceeded — re-raises
    immediately (no retries)."""
    s = _settings(
        tmp_path,
        MILL_TRANSIENT_RETRIES="4",
        MILL_RATE_LIMIT_FALLBACK_RETRIES="3",
    )
    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}

    def primary():
        primary_calls["n"] += 1
        raise _FakeUsageLimitExceeded("cap")

    def fallback():
        fallback_calls["n"] += 1
        raise _FakeUsageLimitExceeded("fallback-cap")

    with pytest.raises(_FakeUsageLimitExceeded):
        call_with_retry(
            primary, settings=s, sleep=lambda _: None, fallback_fn=fallback,
        )
    assert primary_calls["n"] == 1  # fallback activates on first failure
    assert fallback_calls["n"] == 1  # fallback also fails immediately


def test_rate_limit_fallback_not_called_for_transient(tmp_path):
    """429 (transient) errors must NOT activate fallback — only
    UsageLimitExceeded does."""
    s = _settings(
        tmp_path,
        MILL_TRANSIENT_RETRIES="2",
        MILL_RATE_LIMIT_FALLBACK_RETRIES="1",
    )
    calls = {"n": 0}
    fallback_calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ModelHTTPError(429, "m")

    def fallback():
        fallback_calls["n"] += 1
        return "fallback"

    with pytest.raises(ModelHTTPError):
        call_with_retry(fn, settings=s, sleep=lambda _: None, fallback_fn=fallback)
    assert calls["n"] == 3  # 1 try + 2 retries (transient, not rate-limit)
    assert fallback_calls["n"] == 0


# --- flush_tracing on failure / retry -----------------------------------

def test_non_retryable_flushes_before_raise(tmp_path, monkeypatch):
    """Non-retryable error: flush_tracing must be called before the
    exception propagates."""
    flush_calls = []

    def fake_flush():
        flush_calls.append(1)

    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.flush_tracing", fake_flush,
    )
    s = _settings(tmp_path)

    def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        call_with_retry(fn, settings=s, sleep=lambda _: None)
    assert len(flush_calls) == 1


def test_persistent_transient_flushes_per_attempt(tmp_path, monkeypatch):
    """Transient failures that exhaust retries: flush_tracing must be
    called once per failed attempt."""
    flush_calls = []

    def fake_flush():
        flush_calls.append(1)

    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.flush_tracing", fake_flush,
    )
    s = _settings(tmp_path, MILL_TRANSIENT_RETRIES="3")
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ModelHTTPError(429, "hy3")

    with pytest.raises(ModelHTTPError):
        call_with_retry(fn, settings=s, sleep=lambda _: None)
    assert calls["n"] == 4          # 1 try + 3 retries
    assert len(flush_calls) == 4    # flush after each of the 4 failed attempts


def test_success_does_not_flush(tmp_path, monkeypatch):
    """A successful call must NOT call flush_tracing."""
    flush_calls = []

    def fake_flush():
        flush_calls.append(1)

    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.flush_tracing", fake_flush,
    )
    s = _settings(tmp_path)

    def fn():
        return "ok"

    out = call_with_retry(fn, settings=s)
    assert out == "ok"
    assert len(flush_calls) == 0


def test_flush_tracing_error_does_not_swallow_agent_exception(
    tmp_path, monkeypatch,
):
    """If flush_tracing itself raises, the original agent exception
    must still propagate (not be swallowed by the flush guard)."""

    def fake_flush():
        raise RuntimeError("trace export failed")

    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.flush_tracing", fake_flush,
    )
    s = _settings(tmp_path)

    def fn():
        raise ValueError("agent boom")

    with pytest.raises(ValueError):
        call_with_retry(fn, settings=s, sleep=lambda _: None)
