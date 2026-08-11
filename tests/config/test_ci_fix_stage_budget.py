"""The ci_fix stage must never be killed inside its own agent's budget.

Regression guard for the dominant stage-timeout class: 25 of the 31
``stage X timed out after Ns`` events this mill had ever recorded were
``ci_fix``. The ci-fix agent owns a fix->push->wait_for_ci->verify loop
whose sanctioned budget is ``ci_fix_max_iterations * ci_fix_wait_timeout_s``
plus one coordinator pass, but the stage wrapper fell back to the generic
``stage_timeout_seconds`` (2400 s) because ``ci_fix`` had no override. With
the values pinned in production (5 x 1500 s) the wrapper killed the agent at
26% of its budget, mid-verify-loop, discarding fixes it had already pushed.
"""

import pytest

from robotsix_mill.config import Settings

BASE = {
    "forge_remote_url": "https://github.com/damien-robotsix/x.git",
    "forge_auth": "token",
}


def _settings(**kw):
    return Settings(**BASE, **kw)


def test_ci_fix_stage_covers_the_agent_verify_loop():
    """The floor is the agent's own budget, not the generic default."""
    s = _settings(
        ci_fix_max_iterations=3,
        ci_fix_wait_timeout_s=900.0,
        coordinator_timeout_seconds=1800,
        stage_timeout_seconds=2400,
        stage_timeout_overrides={},
    )

    assert s.ci_fix_agent_budget_seconds == 3 * 900 + 1800
    assert s.stage_timeout_for("ci_fix") > s.ci_fix_agent_budget_seconds
    # The bug: the generic default would have cut the loop short.
    assert s.stage_timeout_for("ci_fix") > 2400


def test_production_pinned_values_are_also_protected():
    """Operator config that pins the old loop budget still gets a safe stage.

    This is the case that actually mattered: the live config.json sets
    ci_fix_max_iterations=5 and ci_fix_wait_timeout_s=1500, so changing the
    code defaults alone would NOT have fixed production.
    """
    s = _settings(
        ci_fix_max_iterations=5,
        ci_fix_wait_timeout_s=1500.0,
        coordinator_timeout_seconds=1800,
        stage_timeout_seconds=2400,
        stage_timeout_overrides={"implement": 7200, "refine": 3600},
    )

    assert s.stage_timeout_for("ci_fix") >= 5 * 1500 + 1800


# --- the same drift, one level down: agent timeout vs agent budget ------
#
# stage_timeout_for closed the gap between the stage wrapper and the
# agent's budget, but ci_fix_agent_timeout_seconds remained an
# independent constant wrapping the very same agent call. Shipped at
# 1800 s against a 4500 s budget it reproduced the original bug exactly:
# six live tickets on 2026-08-11 blocked at "timed out after 1800s",
# none of which had been allowed to finish two of its three sanctioned
# verify iterations.


def test_agent_timeout_covers_its_own_sanctioned_budget():
    s = _settings(
        ci_fix_max_iterations=3,
        ci_fix_wait_timeout_s=900.0,
        coordinator_timeout_seconds=1800,
        ci_fix_agent_timeout_seconds=1800,
    )

    assert s.ci_fix_agent_budget_seconds == 4500
    assert s.ci_fix_agent_timeout_effective == 4500


def test_a_larger_configured_agent_timeout_still_wins():
    s = _settings(
        ci_fix_max_iterations=3,
        ci_fix_wait_timeout_s=900.0,
        coordinator_timeout_seconds=1800,
        ci_fix_agent_timeout_seconds=9_999,
    )

    assert s.ci_fix_agent_timeout_effective == 9_999


def test_a_zero_agent_timeout_stays_disabled():
    """0 means "no agent timeout"; the floor must not resurrect one."""
    s = _settings(ci_fix_agent_timeout_seconds=0)

    assert s.ci_fix_agent_timeout_effective == 0


@pytest.mark.parametrize(
    "iterations,wait_s,agent_timeout",
    [(3, 900.0, 1800), (5, 1500.0, 1800), (1, 300.0, 7200), (4, 600.0, 0)],
)
def test_the_stage_always_outlives_its_agent(iterations, wait_s, agent_timeout):
    """The ordering invariant, across configurations.

    Whatever the knobs say, the agent's own timeout must fire first so
    the ticket gets the diagnostic block note (which failing check, last
    known state) rather than the wrapper's anonymous stage kill.
    """
    s = _settings(
        ci_fix_max_iterations=iterations,
        ci_fix_wait_timeout_s=wait_s,
        coordinator_timeout_seconds=1800,
        ci_fix_agent_timeout_seconds=agent_timeout,
        stage_timeout_seconds=2400,
        stage_timeout_overrides={},
    )

    stage = s.stage_timeout_for("ci_fix")
    agent = s.ci_fix_agent_timeout_effective
    if agent == 0:
        # Agent timeout disabled: the stage still covers the loop budget.
        assert stage >= s.ci_fix_agent_budget_seconds
    else:
        assert stage > agent


def test_a_larger_explicit_override_still_wins():
    s = _settings(stage_timeout_overrides={"ci_fix": 99_999})

    assert s.stage_timeout_for("ci_fix") == 99_999


def test_an_explicit_zero_disables_the_timeout():
    """0 means "no timeout"; the floor must not resurrect one."""
    s = _settings(stage_timeout_overrides={"ci_fix": 0})

    assert s.stage_timeout_for("ci_fix") == 0


@pytest.mark.parametrize(
    "stage,expected",
    [("implement", 7200), ("refine", 3600), ("review", 2400), ("merge", 2400)],
)
def test_other_stages_are_untouched(stage, expected):
    """The floor is ci_fix-specific — no other stage's budget changes."""
    s = _settings(
        stage_timeout_seconds=2400,
        stage_timeout_overrides={"implement": 7200, "refine": 3600},
    )

    assert s.stage_timeout_for(stage) == expected
