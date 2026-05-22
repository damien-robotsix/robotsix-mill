"""Tests for the epic breakdown agent."""

from robotsix_mill.agents.epic_breakdown import (
    SYSTEM_PROMPT,
    EpicBreakdownResult,
)


def test_epic_breakdown_prompt_covers_rules():
    """SYSTEM_PROMPT must include all documented breakdown rules so
    that prompt edits cannot silently drop a constraint."""
    p = SYSTEM_PROMPT.lower()

    # 2–8 children range.
    assert "2–8 children" in p or "2-8 children" in p or "2-8" in p

    # Self-contained tickets.
    assert "self-contained" in p

    # Union covers epic scope.
    assert "full scope" in p
    assert "union" in p

    # Verb-led titles.
    assert "verb" in p

    # No fabricated dependencies.
    assert "do not fabricate dependencies" in p or "do not fabricate" in p

    # No priorities or estimates.
    assert "priorities" in p
    assert "estimates" in p or "estimate effort" in p


def test_epic_breakdown_result_model():
    """EpicBreakdownResult has the expected fields."""
    result = EpicBreakdownResult(
        child_titles=["A", "B"],
        child_bodies=["Body A", "Body B"],
    )
    assert result.child_titles == ["A", "B"]
    assert result.child_bodies == ["Body A", "Body B"]
