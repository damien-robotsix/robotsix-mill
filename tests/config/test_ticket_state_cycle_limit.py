"""The loop guard must not fire on the review budget the pipeline grants.

``ticket_state_cycle_limit`` caps how many times one LLM-bearing stage
may be re-dispatched within a processing pass, to catch unbounded
implement<->review bounce-loops. But a review round that requests
changes re-dispatches ``implement`` by design, so a ticket spending its
full ``review_max_rounds`` budget dispatches implement
``review_max_rounds + 1`` times — and with both shipped at 3 it tripped
the guard on its last *sanctioned* round. Four live tickets were BLOCKED
this way on 2026-08-11 with "'implement' re-ran 4 times this pass
(limit 3)", each for doing exactly what review had asked of it.
"""

import pytest

from robotsix_mill.config import Settings

BASE = {
    "forge_remote_url": "https://github.com/damien-robotsix/x.git",
    "forge_auth": "token",
}


def _settings(**kw):
    return Settings(**BASE, **kw)


def test_a_full_review_budget_does_not_trip_the_guard():
    """The regression: 3 review rounds need a 4th implement dispatch."""
    s = _settings(review_max_rounds=3, ticket_state_cycle_limit=3)

    assert s.ticket_state_cycle_limit_effective == 4
    # One initial implement plus one per review round must all fit.
    assert s.ticket_state_cycle_limit_effective >= s.review_max_rounds + 1


def test_a_larger_configured_limit_still_wins():
    s = _settings(review_max_rounds=3, ticket_state_cycle_limit=10)

    assert s.ticket_state_cycle_limit_effective == 10


def test_zero_stays_disabled():
    """0 means "no ceiling"; the floor must not resurrect one."""
    s = _settings(review_max_rounds=3, ticket_state_cycle_limit=0)

    assert s.ticket_state_cycle_limit_effective == 0


@pytest.mark.parametrize("rounds", [0, 1, 2, 3, 5, 8])
def test_the_floor_tracks_review_max_rounds(rounds):
    """Raising the review budget raises the ceiling with it.

    The two settings drifting apart is what produced the bug; deriving
    one from the other means they cannot disagree again.
    """
    s = _settings(review_max_rounds=rounds, ticket_state_cycle_limit=3)

    assert s.ticket_state_cycle_limit_effective >= rounds + 1
