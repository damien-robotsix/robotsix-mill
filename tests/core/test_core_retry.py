"""Core retry/transient classification — generic, provider-agnostic."""

from __future__ import annotations

import httpx

from robotsix_llmio.core.retry import (
    _status,
    call_with_retry,
    is_rate_limited,
    is_transient,
)


class _HTTPErr(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


class UsageLimitExceeded(Exception):
    """Name must match pydantic-ai's class — the detector keys on __name__."""


def test_status_from_attr():
    assert _status(_HTTPErr(503)) == 503
    assert _status(ValueError("x")) is None


def test_transient_429_and_5xx():
    assert is_transient(_HTTPErr(429)) is True
    assert is_transient(_HTTPErr(503)) is True


def test_fatal_4xx_and_other():
    assert is_transient(_HTTPErr(400)) is False
    assert is_transient(_HTTPErr(404)) is False
    assert is_transient(ValueError("boom")) is False


def test_transient_httpx_timeout_and_transport():
    assert is_transient(httpx.ReadTimeout("slow")) is True
    assert is_transient(httpx.ConnectError("refused")) is True


def test_transient_walks_cause_chain():
    outer = ValueError("outer")
    outer.__cause__ = httpx.ReadTimeout("slow")
    assert is_transient(outer) is True


def test_usage_limit_is_rate_limited_not_transient():
    e = UsageLimitExceeded("cap")
    assert is_rate_limited(e) is True
    assert is_transient(e) is False


def test_call_with_retry_retries_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HTTPErr(503)
        return "ok"

    out = call_with_retry(fn, sleep=lambda d: None)
    assert out == "ok"
    assert calls["n"] == 3


def test_call_with_retry_reraises_fatal_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _HTTPErr(400)

    try:
        call_with_retry(fn, sleep=lambda d: None)
    except _HTTPErr:
        pass
    else:
        raise AssertionError("expected fatal to re-raise")
    assert calls["n"] == 1  # no retries on fatal


def test_call_with_retry_uses_provider_transient_predicate():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("provider-specific transient")
        return "ok"

    # Generic is_transient would NOT retry a ValueError; the custom predicate does.
    out = call_with_retry(
        fn, sleep=lambda d: None, is_transient_fn=lambda e: isinstance(e, ValueError)
    )
    assert out == "ok"
    assert calls["n"] == 2
