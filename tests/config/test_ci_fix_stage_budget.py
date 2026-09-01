"""The ci_fix stage must never be killed inside its own agent's budget.

Regression guard for the dominant stage-timeout class.  The ci-fix agent is
now ONE-SHOT (fix + push, no CI waiting), so the budget is just the
coordinator pass.  The stage wrapper must still let the agent's own timeout
fire first so the ticket gets the diagnostic block note rather than the
wrapper's anonymous stage kill.
"""

import pytest

from robotsix_mill.config import Settings

BASE = {
    "forge_remote_url": "https://github.com/damien-robotsix/x.git",
    "forge_auth": "token",
}


def _settings(**kw):
    return Settings(**BASE, **kw)


def test_ci_fix_stage_covers_the_agent_budget():
    """The floor is the agent's own budget, not the generic default."""
    s = _settings(
        coordinator_timeout_seconds=1800,
        stage_timeout_seconds=2400,
        stage_timeout_overrides={},
    )

    assert s.ci_fix_agent_budget_seconds == 1800
    assert s.stage_timeout_for("ci_fix") > s.ci_fix_agent_budget_seconds
    # The stage timeout must be above the generic default when the agent
    # budget exceeds it.
    assert s.stage_timeout_for("ci_fix") >= 2400


def test_agent_timeout_covers_its_own_budget():
    s = _settings(
        coordinator_timeout_seconds=1800,
        ci_fix_agent_timeout_seconds=1800,
    )

    assert s.ci_fix_agent_budget_seconds == 1800
    assert s.ci_fix_agent_timeout_effective == 1800


def test_a_larger_configured_agent_timeout_still_wins():
    s = _settings(
        coordinator_timeout_seconds=1800,
        ci_fix_agent_timeout_seconds=9_999,
    )

    assert s.ci_fix_agent_timeout_effective == 9_999


def test_a_zero_agent_timeout_stays_disabled():
    """0 means "no agent timeout"; the floor must not resurrect one."""
    s = _settings(ci_fix_agent_timeout_seconds=0)

    assert s.ci_fix_agent_timeout_effective == 0


@pytest.mark.parametrize(
    "coordinator_timeout,agent_timeout",
    [(1800, 1800), (600, 1800), (3600, 7200), (600, 0)],
)
def test_the_stage_always_outlives_its_agent(coordinator_timeout, agent_timeout):
    """The ordering invariant, across configurations.

    Whatever the knobs say, the agent's own timeout must fire first so
    the ticket gets the diagnostic block note (which failing check, last
    known state) rather than the wrapper's anonymous stage kill.
    """
    s = _settings(
        coordinator_timeout_seconds=coordinator_timeout,
        ci_fix_agent_timeout_seconds=agent_timeout,
        stage_timeout_seconds=2400,
        stage_timeout_overrides={},
    )

    stage = s.stage_timeout_for("ci_fix")
    agent = s.ci_fix_agent_timeout_effective
    if agent == 0:
        # Agent timeout disabled: the stage still covers the budget.
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
