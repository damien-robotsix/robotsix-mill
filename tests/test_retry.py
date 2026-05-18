"""Bounded retry+backoff for transient model/network failures."""

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError

from robotsix_mill.agents.retry import call_with_retry, is_transient
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
])
def test_is_transient(exc, transient):
    assert is_transient(exc) is transient


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
    ModelHTTPError(404, "m"), _FakeUsageLimitExceeded("cap"), ValueError("x"),
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
