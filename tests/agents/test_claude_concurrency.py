"""Global semaphore that bounds concurrent Claude Agent SDK runs."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time

import pytest
from pydantic import ValidationError

from robotsix_mill.agents import base
from robotsix_mill.agents.claude_concurrency import (
    _BoundedClaudeHandle,
    bound_claude_handle,
    get_claude_run_semaphore,
    reset_for_tests,
)
from robotsix_mill.config import Settings


@pytest.fixture(autouse=True)
def _fresh_semaphore():
    """Isolate the process-wide singleton between tests."""
    reset_for_tests()
    yield
    reset_for_tests()


def _settings(**kw) -> Settings:
    return Settings(data_dir=tempfile.mkdtemp(), **kw)


# --- wrapper delegation ----------------------------------------------------


class _RecordingHandle:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.closed = False

    def run_sync(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "OUT"

    def close(self) -> None:
        self.closed = True

    @property
    def marker(self) -> str:
        return "delegated"


def test_run_sync_delegates_args_and_returns_result():
    inner = _RecordingHandle()
    wrapped = bound_claude_handle(inner, limit=2)

    out = wrapped.run_sync("prompt", message_history=[1], usage_limits="L")

    assert out == "OUT"
    assert inner.calls == [(("prompt",), {"message_history": [1], "usage_limits": "L"})]


def test_non_run_sync_attrs_delegate_to_inner():
    inner = _RecordingHandle()
    wrapped = bound_claude_handle(inner, limit=2)

    assert isinstance(wrapped, _BoundedClaudeHandle)
    assert wrapped.marker == "delegated"  # property delegated via __getattr__
    wrapped.close()
    assert inner.closed is True


# --- singleton sizing ------------------------------------------------------


def test_semaphore_is_a_reused_singleton():
    s1 = get_claude_run_semaphore(3)
    s2 = get_claude_run_semaphore(99)  # later differing limit ignored
    assert s1 is s2


def test_limit_below_one_is_clamped_to_one():
    sem = get_claude_run_semaphore(0)
    assert sem.acquire(blocking=False) is True
    # Sized to 1: a second non-blocking acquire must fail.
    assert sem.acquire(blocking=False) is False
    sem.release()


# --- the actual concurrency bound ------------------------------------------


def test_semaphore_bounds_concurrent_run_sync():
    """With limit=2, no more than 2 wrapped run_sync calls overlap, even with
    many threads contending — proving the permit is held across the whole run."""
    limit = 2
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    class _SlowHandle:
        def run_sync(self, *args, **kwargs):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            return "ok"

    wrapped = bound_claude_handle(_SlowHandle(), limit=limit)

    threads = [
        threading.Thread(target=wrapped.run_sync, args=("go",)) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["peak"] <= limit
    assert state["peak"] >= 1


# --- config + build_agent wiring -------------------------------------------


def test_config_default_and_validation():
    assert _settings().claude_max_concurrency == 4
    assert _settings(claude_max_concurrency=1).claude_max_concurrency == 1
    with pytest.raises(ValidationError, match="claude_max_concurrency"):
        _settings(claude_max_concurrency=0)


def test_build_agent_wraps_claude_handle_with_the_bound(monkeypatch):
    """build_agent routes a Claude-SDK agent (level 3) through the concurrency
    wrapper, sized from settings.claude_max_concurrency."""
    from unittest.mock import MagicMock, patch

    # An agent runs on the Claude SDK iff its level is 3 — there is no separate
    # backend toggle. build_agent returns the bound handle directly.
    s = _settings(claude_max_concurrency=3)

    provider = MagicMock()
    provider.build_agent.side_effect = lambda **kw: "RAW_HANDLE"

    with patch(
        "robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider",
        return_value=provider,
    ):
        handle = base.build_agent(
            s,
            system_prompt="sys",
            name="refine",
            level=3,
            report_issue=False,
            reply_to_thread=False,
            close_thread=False,
            ask_user=False,
        )

    assert isinstance(handle, _BoundedClaudeHandle)
    assert handle._handle == "RAW_HANDLE"
    # Sized to the configured cap (3 permits available, 4th blocks).
    assert handle._semaphore is get_claude_run_semaphore(3)


# --- live: the bound serializes real CLI runs ------------------------------


@pytest.mark.skipif(
    not os.environ.get("MILL_RUN_LIVE") or shutil.which("claude") is None,
    reason="needs MILL_RUN_LIVE=1 and a logged-in `claude` CLI",
)
def test_live_bound_serializes_real_claude_runs():
    """Live: with claude_max_concurrency=1, the *real* CLI run executes entirely
    inside the semaphore's critical section — two concurrently-launched runs
    never overlap. We instrument the inner (real) ``run_sync`` to record peak
    concurrency of the actual work; the permit-wait happens in the wrapper
    around it, so peak must stay at 1."""
    s = _settings(claude_max_concurrency=1)

    handle = base.build_agent(
        s,
        system_prompt="You are terse. Answer with a single word.",
        name="livecheck",
        level=3,  # → Claude SDK transport
        report_issue=False,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
    )

    real_inner = handle._handle
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    class _Probe:
        def run_sync(self, *args, **kwargs):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            try:
                return real_inner.run_sync(*args, **kwargs)
            finally:
                with lock:
                    state["current"] -= 1

    handle._handle = _Probe()

    threads = [
        threading.Thread(target=handle.run_sync, args=("Reply with the word: ok",))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The real run holds the only permit for its whole duration → no overlap.
    assert state["peak"] == 1, f"real runs overlapped (peak={state['peak']})"
