"""Core retry/transient classification — generic, provider-agnostic."""

from __future__ import annotations

import httpx

import pytest

from robotsix_llmio.core.retry import (
    _status,
    call_with_retry,
    call_with_retry_and_fallback,
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


# --- retry-then-fallback ----------------------------------------------------


def _noop_sleep(_d: float) -> None:
    return None


def test_fallback_not_used_when_primary_succeeds():
    calls = {"primary": 0, "fallback": 0}

    def primary():
        calls["primary"] += 1
        return "primary-ok"

    def fallback():
        calls["fallback"] += 1
        return "fallback-ok"

    out = call_with_retry_and_fallback(
        primary, fallback, sleep=_noop_sleep
    )
    assert out == "primary-ok"
    assert calls == {"primary": 1, "fallback": 0}


def test_fallback_only_after_primary_local_retries_exhausted():
    """The primary must burn its FULL transient-retry budget before the fallback
    is tried — retry locally first, fall back only when that failed."""
    calls = {"primary": 0, "fallback": 0}

    def primary():
        calls["primary"] += 1
        raise _HTTPErr(503)  # always transient → exhausts retries

    def fallback():
        calls["fallback"] += 1
        return "fallback-ok"

    out = call_with_retry_and_fallback(primary, fallback, sleep=_noop_sleep)
    assert out == "fallback-ok"
    # 1 initial + TRANSIENT_RETRIES attempts, all on the primary, THEN fallback.
    assert calls["primary"] > 1
    assert calls["fallback"] == 1


def test_fallback_on_non_transient_terminal_error():
    def primary():
        raise _HTTPErr(400)  # non-transient → terminal immediately

    out = call_with_retry_and_fallback(
        primary, lambda: "fallback-ok", sleep=_noop_sleep
    )
    assert out == "fallback-ok"


def test_no_fallback_reraises_primary():
    def primary():
        raise _HTTPErr(400)

    with pytest.raises(_HTTPErr):
        call_with_retry_and_fallback(primary, None, sleep=_noop_sleep)


def test_should_fallback_false_reraises_primary_without_fallback():
    calls = {"fallback": 0}

    def primary():
        raise ValueError("nope")

    def fallback():
        calls["fallback"] += 1
        return "fallback-ok"

    with pytest.raises(ValueError):
        call_with_retry_and_fallback(
            primary, fallback, sleep=_noop_sleep, should_fallback=lambda _e: False
        )
    assert calls["fallback"] == 0


def test_both_fail_raises_fallback_chained_to_primary():
    class PrimaryErr(Exception):
        pass

    class FallbackErr(Exception):
        pass

    def primary():
        raise PrimaryErr("primary")

    def fallback():
        raise FallbackErr("fallback")

    with pytest.raises(FallbackErr) as ei:
        call_with_retry_and_fallback(primary, fallback, sleep=_noop_sleep)
    # The primary cause is chained so the original failure isn't lost.
    assert isinstance(ei.value.__cause__, PrimaryErr)


def test_each_side_uses_its_own_transient_predicate():
    """Primary retries on ITS classifier; the fallback retries on its own."""
    calls = {"primary": 0, "fallback": 0}

    class PrimaryTransient(Exception):
        pass

    def primary():
        calls["primary"] += 1
        raise PrimaryTransient("only primary classifier knows this")

    def fallback():
        calls["fallback"] += 1
        if calls["fallback"] < 2:
            raise _HTTPErr(503)  # generic transient
        return "fallback-ok"

    out = call_with_retry_and_fallback(
        primary,
        fallback,
        sleep=_noop_sleep,
        is_transient_primary=lambda e: isinstance(e, PrimaryTransient),
        is_transient_fallback=is_transient,
    )
    assert out == "fallback-ok"
    assert calls["primary"] > 1  # primary retried on its own predicate
    assert calls["fallback"] == 2  # fallback retried on the generic predicate
