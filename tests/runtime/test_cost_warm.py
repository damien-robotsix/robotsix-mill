"""On-demand cost warming (replaces the cost_warmer daemon)."""

from __future__ import annotations

from robotsix_mill.runtime import cost_warm


def test_warm_ticket_costs_warms_each(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, tid, repo_config=None: calls.append(tid),
    )
    cost_warm.warm_ticket_costs(object(), [("a", None), ("b", None)])
    assert sorted(calls) == ["a", "b"]


def test_warm_ticket_costs_empty_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda *a, **k: calls.append(1),
    )
    cost_warm.warm_ticket_costs(object(), [])
    assert calls == []


def test_warm_ticket_costs_skips_when_already_running(monkeypatch):
    """Non-blocking lock: a poke while a warm is in flight is a no-op
    (prevents overlapping board polls from piling up threads)."""
    calls = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda *a, **k: calls.append(1),
    )
    assert cost_warm._warm_lock.acquire(blocking=False)
    try:
        cost_warm.warm_ticket_costs(object(), [("a", None)])
        assert calls == []  # skipped because the lock is held
    finally:
        cost_warm._warm_lock.release()


def test_warm_ticket_costs_swallows_lookup_errors(monkeypatch):
    def _boom(settings, tid, repo_config=None):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr("robotsix_mill.langfuse.client.session_cost", _boom)
    # Must not raise — best-effort cache warm.
    cost_warm.warm_ticket_costs(object(), [("a", None)])
