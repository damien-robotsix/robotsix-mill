"""Unit tests for the process-wide GitHub rate-limit tracker.

Covers the snapshot recorder, the ``should_skip`` reserve gate, the health
payload, and that a non-urgent runner skips its pass (naming the reset time)
and a stage caller returns a same-state re-poll without hitting the forge,
all without any real network I/O.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from robotsix_mill.forge import rate_limit


@pytest.fixture(autouse=True)
def _clean_snapshot():
    rate_limit._reset_for_tests()
    yield
    rate_limit._reset_for_tests()


def _headers(remaining, *, limit=5000, reset=None, auth="token ghs_abc"):
    reset = int(time.time()) + 3600 if reset is None else reset
    h = {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(reset),
    }
    if auth is not None:
        h["Authorization"] = auth
    return h


def _resp(remaining, **kw):
    return SimpleNamespace(status_code=200, headers=_headers(remaining, **kw))


# --- recording ------------------------------------------------------------


def test_record_populates_snapshot():
    rate_limit.record(_resp(4800).headers)
    snap = rate_limit.most_constrained()
    assert snap is not None
    assert snap.limit == 5000
    assert snap.remaining == 4800
    assert snap.used == 200


def test_record_ignores_non_github_response():
    # GitLab-style response: no x-ratelimit-limit header -> no snapshot.
    rate_limit.record({"RateLimit-Remaining": "0"})
    assert rate_limit.most_constrained() is None


def test_most_constrained_picks_lowest_remaining():
    rate_limit.record(_resp(4000, auth="token A").headers, key="A")
    rate_limit.record(_resp(50, auth="token B").headers, key="B")
    assert rate_limit.most_constrained().remaining == 50


# --- should_skip ----------------------------------------------------------


def test_should_skip_when_below_reserve():
    rate_limit.record(_resp(0).headers)
    settings = SimpleNamespace(github_rate_limit_reserve=200)
    snap = rate_limit.should_skip(settings)
    assert snap is not None
    assert snap.remaining == 0


def test_should_not_skip_when_above_reserve():
    rate_limit.record(_resp(4800).headers)
    settings = SimpleNamespace(github_rate_limit_reserve=200)
    assert rate_limit.should_skip(settings) is None


def test_should_not_skip_when_window_already_reset():
    rate_limit.record(_resp(0, reset=int(time.time()) - 10).headers)
    settings = SimpleNamespace(github_rate_limit_reserve=200)
    assert rate_limit.should_skip(settings) is None


def test_should_not_skip_without_snapshot():
    settings = SimpleNamespace(github_rate_limit_reserve=200)
    assert rate_limit.should_skip(settings) is None


# --- health payload -------------------------------------------------------


def test_health_payload_empty_then_populated():
    assert rate_limit.health_payload() == {}
    rate_limit.record(_resp(4900).headers)
    payload = rate_limit.health_payload()
    assert payload["limit"] == 5000
    assert payload["remaining"] == 4900
    assert payload["used"] == 100
    assert "reset" in payload and "reset_hhmm" in payload


# --- stage-caller throttled skip log -------------------------------------


def test_log_skip_throttled_once_per_window(caplog):
    rate_limit.record(_resp(0).headers)
    snap = rate_limit.most_constrained()
    log = logging.getLogger("test.ratelimit")
    with caplog.at_level(logging.INFO):
        rate_limit.log_skip(log, "ticket-1: merge", snap)
        rate_limit.log_skip(log, "ticket-1: merge", snap)
    skip_lines = [
        r for r in caplog.records if "rate-limit reserve reached" in r.message
    ]
    assert len(skip_lines) == 1
    assert snap.reset_hhmm() in caplog.text


# --- non-urgent runner skips its pass ------------------------------------


def test_upstream_ci_recovery_skips_when_rate_limited(caplog):
    from robotsix_mill.agents.runners import upstream_ci_recovery_runner

    rate_limit.record(_resp(0).headers)
    snap = rate_limit.most_constrained()
    settings = SimpleNamespace(github_rate_limit_reserve=200)
    with caplog.at_level(logging.INFO):
        result = upstream_ci_recovery_runner.run_upstream_ci_recovery(settings)
    assert result == {"resumed": 0, "still_parked": 0, "skipped": 0}
    assert "skipping pass" in caplog.text
    assert snap.reset_hhmm() in caplog.text


# --- stage caller returns a same-state re-poll without a forge call -------


def test_ci_fix_stage_skips_without_forge_call(monkeypatch):
    from robotsix_mill.core.states import State
    from robotsix_mill.stages import ci_fix as ci_fix_mod

    rate_limit.record(_resp(0).headers)

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("forge was called despite rate-limit skip")

    monkeypatch.setattr(ci_fix_mod, "get_forge", _boom)

    stage = ci_fix_mod.CIFixStage()
    ticket = SimpleNamespace(id="t1", state=State.FIXING_CI)
    ctx = SimpleNamespace(settings=SimpleNamespace(github_rate_limit_reserve=200))

    outcome = stage.run(ticket, ctx)
    assert outcome.next_state == State.FIXING_CI
