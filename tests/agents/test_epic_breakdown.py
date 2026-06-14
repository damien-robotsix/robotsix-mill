"""Tests for the epic breakdown agent."""

from robotsix_mill.agents.epic_breakdown import (
    SYSTEM_PROMPT,
    EpicBreakdownResult,
    _is_init_repo_child,
    plan_child_dependencies,
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


def test_is_init_repo_child_detection():
    """Create/initialize-repository children are recognised by title or body."""
    assert _is_init_repo_child("Create repo robotsix-agent-comm", "")
    assert _is_init_repo_child("Create repository for X", "")
    # The live-incident title — keyword split across the phrase.
    assert _is_init_repo_child("Initialize communication system repository", "")
    assert _is_init_repo_child("Bootstrap the agent-comm repository", "")
    assert _is_init_repo_child("Set up the new repository", "")
    # Detected from the body too.
    assert _is_init_repo_child(
        "Foundation work", "First, initialize the repository skeleton."
    )
    # Populating / unrelated children are NOT init-repo actions.
    assert not _is_init_repo_child("Design the architecture", "Write the design doc.")
    assert not _is_init_repo_child(
        "Add repository pattern to the data layer", "refactor"
    )


def test_plan_child_dependencies_linear_chain_without_init_repo():
    """No init-repo child → preserve the existing linear chain."""
    children = [
        ("c0", "Design API", "body"),
        ("c1", "Implement API", "body"),
        ("c2", "Document API", "body"),
    ]
    edges = plan_child_dependencies(children)
    assert edges == {"c1": ["c0"], "c2": ["c1"]}


def test_plan_child_dependencies_linear_chain_with_predecessor():
    children = [("c0", "A", "b"), ("c1", "B", "b")]
    edges = plan_child_dependencies(children, predecessor_id="existing-9")
    assert edges == {"c0": ["existing-9"], "c1": ["c0"]}


def test_plan_child_dependencies_wires_populating_children_to_init_repo():
    """An epic with an init-repo child plus repo-populating children:
    every populating child depends on the init-repo child so it stays
    blocked until the repo exists — regardless of agent order."""
    # The init-repo child comes AFTER a populating sibling in agent
    # order (mirrors the live incident where the design child ran first).
    children = [
        ("design", "Design agent communication architecture", "write design doc"),
        ("init", "Initialize communication system repository", "create the repo"),
        ("transport", "Implement the transport layer", "code"),
    ]
    edges = plan_child_dependencies(children)
    # Populating children depend on the init-repo child.
    assert edges["design"] == ["init"]
    assert edges["transport"] == ["init"]
    # The init-repo child gains no dependency on its populating siblings.
    assert "init" not in edges


def test_plan_child_dependencies_init_repo_anchored_to_predecessor():
    """With a predecessor, the init-repo child anchors to it and the
    populating children depend on the init-repo child."""
    children = [
        ("init", "Create repo robotsix-agent-comm", "scaffold"),
        ("pop", "Populate the new repo", "files"),
    ]
    edges = plan_child_dependencies(children, predecessor_id="prev-1")
    assert edges["init"] == ["prev-1"]
    assert edges["pop"] == ["init"]


def test_plan_child_dependencies_empty():
    assert plan_child_dependencies([]) == {}


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
