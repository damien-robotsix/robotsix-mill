"""Tests for the epic breakdown agent."""

from robotsix_mill.agents.epic_breakdown import (
    SYSTEM_PROMPT,
    EpicBreakdownResult,
)


def test_epic_breakdown_prompt_covers_rules():
    """SYSTEM_PROMPT must include all documented breakdown rules so
    that prompt edits cannot silently drop a constraint.

    Uses direct substring checks against the known rule formulations
    in the prompt rather than loose keyword matching, so that
    rewording is detected as a failure.
    """
    p = SYSTEM_PROMPT.lower()

    # Break the epic into 2–8 children.
    assert "2–8 children" in p or "2-8 children" in p

    # Self-contained tickets.
    assert "self-contained" in p

    # Union covers epic scope.
    assert "full scope" in p
    assert "union" in p

    # Verb-led titles (exact phrase from prompt).
    assert "start with a verb" in p

    # No fabricated dependencies (exact phrase from prompt).
    assert "do not fabricate dependencies" in p

    # No priorities or estimates (exact phrases from the prompt rule
    # "Do NOT assign priorities or estimate effort").
    assert "do not assign priorities" in p
    assert "estimate effort" in p


def test_epic_breakdown_result_model():
    """EpicBreakdownResult has the expected fields."""
    result = EpicBreakdownResult(
        child_titles=["A", "B"],
        child_bodies=["Body A", "Body B"],
    )
    assert result.child_titles == ["A", "B"]
    assert result.child_bodies == ["Body A", "Body B"]
