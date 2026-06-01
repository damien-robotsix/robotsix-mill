"""Tests for the epic breakdown agent."""

from robotsix_mill.agents.epic_breakdown import (
    SYSTEM_PROMPT,
    EpicBreakdownResult,
    run_epic_breakdown_agent,
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

    # Operator comments guidance (added with epic-reprocess feature).
    assert "operator comments" in p
    assert "authoritative direction" in p


def test_epic_breakdown_result_model():
    """EpicBreakdownResult has the expected fields."""
    result = EpicBreakdownResult(
        child_titles=["A", "B"],
        child_bodies=["Body A", "Body B"],
    )
    assert result.child_titles == ["A", "B"]
    assert result.child_bodies == ["Body A", "Body B"]


def test_comments_parameter_appended_to_prompt(monkeypatch):
    """When *comments* is non-empty, the operator_comments block is
    appended to the prompt after </epic_description>."""
    captured_prompt: str | None = None

    class FakeAgent:
        def run_sync(self, prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            return type("R", (), {"output": EpicBreakdownResult()})()

    # build_agent and call_with_retry are imported locally inside
    # run_epic_breakdown_agent from .base and .retry respectively.
    # Patch the source modules so the local imports pick up the fakes.
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent",
        lambda *args, **kw: FakeAgent(),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.retry.run_agent",
        lambda agent, make_run, **kw: make_run(agent),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.base._safe_close",
        lambda agent: None,
    )

    from robotsix_mill.config import Settings

    settings = Settings()

    # Without comments — no operator_comments block.
    run_epic_breakdown_agent(
        settings=settings,
        epic_title="Test Epic",
        epic_description="Build the thing.",
    )
    assert "````operator-comments" not in captured_prompt

    # With comments — operator_comments block present and placed
    # after </epic_description>.
    run_epic_breakdown_agent(
        settings=settings,
        epic_title="Test Epic",
        epic_description="Build the thing.",
        comments="Focus on backend only.",
    )
    assert "````operator-comments" in captured_prompt
    assert "Focus on backend only." in captured_prompt
    # Must appear after the epic-description block.
    assert captured_prompt.index("````epic-description") < captured_prompt.index(
        "````operator-comments"
    )
