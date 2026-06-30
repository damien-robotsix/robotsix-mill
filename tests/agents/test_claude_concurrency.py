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
    bound_board_manager_handle,
    bound_claude_handle,
    get_board_manager_semaphore,
    get_claude_run_semaphore,
    reset_board_manager_for_tests,
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


# ============================================================================
# Board-manager semaphore — independent dedicated lane
# ============================================================================


@pytest.fixture(autouse=True)
def _fresh_board_manager_semaphore():
    """Isolate the board-manager semaphore between tests."""
    reset_board_manager_for_tests()
    yield
    reset_board_manager_for_tests()


# NOTE: the ``_fresh_semaphore`` fixture (defined above) also calls
# ``reset_for_tests()`` on the heavy-work semaphore.  The board-manager
# semaphore is a separate singleton; resetting one does not affect the other.
# The two autouse fixtures coexist safely (order is deterministic in pytest
# but immaterial — the fixtures touch different global state).


def test_get_board_manager_semaphore_singleton():
    """Calling twice with the same limit returns the same object."""
    s1 = get_board_manager_semaphore(3)
    s2 = get_board_manager_semaphore(3)
    assert s1 is s2


def test_get_board_manager_semaphore_ignores_later_limit():
    """Second call with a different limit is silently ignored; size stays at
    first-call value."""
    s1 = get_board_manager_semaphore(2)
    s2 = get_board_manager_semaphore(99)
    assert s1 is s2
    # Size was set on first call (2); second call must not resize.
    assert s1.acquire(blocking=False) is True
    assert s1.acquire(blocking=False) is True  # both permits taken
    assert s1.acquire(blocking=False) is False  # third blocks
    s1.release()
    s1.release()


def test_get_board_manager_semaphore_min_one():
    """``limit=0`` → semaphore sized to 1."""
    sem = get_board_manager_semaphore(0)
    assert sem.acquire(blocking=False) is True
    assert sem.acquire(blocking=False) is False
    sem.release()


def test_bound_board_manager_handle_delegates_run_sync():
    """Wraps a fake handle; ``run_sync`` is called through and the return value
    passes unchanged."""
    inner = _RecordingHandle()
    wrapped = bound_board_manager_handle(inner, limit=2)

    out = wrapped.run_sync("prompt", extra="val")

    assert out == "OUT"
    assert inner.calls == [(("prompt",), {"extra": "val"})]


def test_bound_board_manager_handle_blocks_when_full():
    """With ``limit=1``, a second concurrent ``run_sync`` blocks until the first
    releases."""
    limit = 1
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

    wrapped = bound_board_manager_handle(_SlowHandle(), limit=limit)

    threads = [
        threading.Thread(target=wrapped.run_sync, args=("go",)) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["peak"] == 1, f"board-manager runs overlapped (peak={state['peak']})"


def test_reset_board_manager_for_tests_allows_resize():
    """After reset, ``get_board_manager_semaphore`` accepts a new limit."""
    s1 = get_board_manager_semaphore(5)
    assert s1 is not None

    reset_board_manager_for_tests()

    s2 = get_board_manager_semaphore(3)
    assert s2 is not s1
    # The new semaphore is sized to 3, not 5.
    for _ in range(3):
        assert s2.acquire(blocking=False) is True
    assert s2.acquire(blocking=False) is False
    for _ in range(3):
        s2.release()


def test_board_manager_semaphore_independent_of_heavy_work():
    """The two semaphores are distinct objects; acquiring one does not affect
    the other."""
    bm = get_board_manager_semaphore(1)
    hw = get_claude_run_semaphore(4)
    assert bm is not hw

    # Acquire the board-manager's only permit — does NOT consume heavy-work.
    assert bm.acquire(blocking=False) is True

    # Heavy-work permits are still fully available.
    for _ in range(4):
        assert hw.acquire(blocking=False) is True
    assert hw.acquire(blocking=False) is False  # all 4 taken

    # Release board-manager; heavy-work stays saturated.
    bm.release()
    assert hw.acquire(blocking=False) is False  # still full
    for _ in range(4):
        hw.release()


# ============================================================================
# Fast-lane gate — handle_wrapper must be present on BoardManager.__init__
# ============================================================================


@pytest.mark.skip(reason="awaiting robotsix-board-agent release with handle_wrapper")
def test_handle_wrapper_present_in_board_manager():
    """Gate: lifespan.py's inject guard checks inspect.signature at startup.
    If handle_wrapper is absent the board-manager semaphore is silently bypassed.
    Failure here means child #2 (cross-repo) has not been merged and pinned."""
    import inspect

    from robotsix_board_agent.board_manager import BoardManager

    assert "handle_wrapper" in inspect.signature(BoardManager.__init__).parameters, (
        "handle_wrapper missing from BoardManager.__init__ — "
        "child #2 cross-repo change not yet merged and pinned in pyproject.toml"
    )


def test_board_manager_not_blocked_by_saturated_heavy_work():
    """Board-manager run_sync completes in < 0.5 s even when all heavy-work
    semaphore slots are permanently held by blocked threads."""
    gate = threading.Event()

    class _GatedHandle:
        def run_sync(self, *args, **kwargs):
            gate.wait()  # hold slot until released
            return "ok"

    # Saturate heavy-work semaphore (limit=4) with 4 blocked threads.
    hw_wrapped = bound_claude_handle(_GatedHandle(), limit=4)
    threads = [
        threading.Thread(target=hw_wrapped.run_sync, args=("go",)) for _ in range(4)
    ]
    for t in threads:
        t.start()
    # Give threads time to acquire their permits.
    time.sleep(0.05)

    # Board-manager run should complete immediately — independent lane.
    bm_wrapped = bound_board_manager_handle(_RecordingHandle(), limit=1)
    t_start = time.perf_counter()
    try:
        bm_wrapped.run_sync("probe")
        elapsed = time.perf_counter() - t_start
    finally:
        # Always release threads — even if the probe fails — to prevent
        # leaked threads accumulating across tests and triggering OOM/SIGKILL.
        gate.set()
        for t in threads:
            t.join(timeout=5)

    assert elapsed < 0.5, f"board-manager blocked by heavy-work semaphore ({elapsed:.3f}s)"
