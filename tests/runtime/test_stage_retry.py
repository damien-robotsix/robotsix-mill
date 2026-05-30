"""Tests for ``runtime.stage_retry.compute_retry_delay``.

A trivial pure function but worth covering: the production retry loop
in ``runtime/worker.py`` schedules transient-error retries with this
delay, so getting the base / cap / jitter math wrong silently means
either an instant retry storm or a many-hours back-off — neither
shows up in any other test because the loop's other paths are mocked.
"""

from __future__ import annotations

import pytest

from robotsix_mill.runtime.stage_retry import compute_retry_delay


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    """Disable the random jitter so the deterministic formula is testable.
    Individual tests that need jitter override the patch."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.stage_retry.random.uniform",
        lambda _lo, _hi: 0.0,
    )


def test_first_attempt_equals_base():
    """attempt=1 → 2**0 = 1 → delay == base."""
    assert compute_retry_delay(1, base=2.0, cap=120.0) == pytest.approx(2.0)


def test_doubling_per_attempt():
    """Exponential: delay doubles each attempt (until clamped)."""
    assert compute_retry_delay(2, base=2.0, cap=120.0) == pytest.approx(4.0)
    assert compute_retry_delay(3, base=2.0, cap=120.0) == pytest.approx(8.0)
    assert compute_retry_delay(4, base=2.0, cap=120.0) == pytest.approx(16.0)


def test_cap_clamps_huge_attempt():
    """attempt=1000 must not return seconds-in-the-millions — the cap holds."""
    assert compute_retry_delay(1000, base=2.0, cap=120.0) == pytest.approx(120.0)


def test_cap_clamps_at_the_boundary():
    """Exactly at the cap boundary: 2*2^5 = 64 < 120; 2*2^7 = 256 > 120 → clamped."""
    assert compute_retry_delay(6, base=2.0, cap=120.0) == pytest.approx(64.0)
    assert compute_retry_delay(8, base=2.0, cap=120.0) == pytest.approx(120.0)


def test_jitter_adds_up_to_half_the_delay(monkeypatch):
    """When jitter is enabled the result lies in
    ``[base * 2**(attempt-1), 1.5 * base * 2**(attempt-1)]`` (post-cap)."""
    # Re-enable real jitter for this test.
    import random

    monkeypatch.setattr(
        "robotsix_mill.runtime.stage_retry.random.uniform",
        random.uniform,
    )
    for attempt in (1, 2, 3, 4, 5):
        floor = min(120.0, 2.0 * (2 ** (attempt - 1)))
        for _ in range(20):
            d = compute_retry_delay(attempt, base=2.0, cap=120.0)
            assert floor <= d <= 1.5 * floor + 1e-9, (
                f"attempt={attempt} delay={d} outside [{floor}, {1.5 * floor}]"
            )


def test_zero_base_collapses_to_zero():
    """Edge case: base=0 → every retry fires immediately with no jitter
    (jitter range is [0, 0/2) = empty)."""
    assert compute_retry_delay(5, base=0.0, cap=120.0) == 0.0
