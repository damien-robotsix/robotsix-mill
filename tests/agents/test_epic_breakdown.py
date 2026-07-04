"""Tests for the epic breakdown agent."""

from robotsix_mill.agents.epic_breakdown import (
    SYSTEM_PROMPT,
    EpicBreakdownResult,
    _detect_cross_repo_deps,
    _is_init_repo_child,
    _parse_prereq_packages,
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


def test_epic_breakdown_result_accepts_natural_keys():
    """The PromptedOutput parse must accept the natural ``titles`` /
    ``bodies`` keys that models (haiku AND opus, observed live) emit
    instead of ``child_titles`` / ``child_bodies`` — otherwise the
    children silently parse to empty and the epic spawns zero
    children (the live 23bd regression)."""
    result = EpicBreakdownResult.model_validate(
        {
            "titles": ["A", "B"],
            "bodies": ["Body A", "Body B"],
            "epic_body": "revised",
        }
    )
    assert result.child_titles == ["A", "B"]
    assert result.child_bodies == ["Body A", "Body B"]
    assert result.epic_body == "revised"


def test_epic_breakdown_result_accepts_canonical_keys():
    """The canonical ``child_titles`` / ``child_bodies`` keys still
    parse (alias must not break the documented schema)."""
    result = EpicBreakdownResult.model_validate(
        {"child_titles": ["A"], "child_bodies": ["Body A"]}
    )
    assert result.child_titles == ["A"]
    assert result.child_bodies == ["Body A"]


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


# -- cross-repo dependency tests -------------------------------------------


def _make_prereq_body(packages: list[str]) -> str:
    """Build a child body with a ``## Prerequisites`` section."""
    lines = [
        "## Prerequisites",
        "",
        "```prereq",
    ]
    for pkg in packages:
        lines.append(f"import {pkg}")
    lines.append("```")
    return "\n".join(lines)


def _make_repos_double():
    """Return a fake ReposRegistry with two repos whose ids follow the
    hyphen→underscore convention for Python package names."""
    from robotsix_mill.config.repos import RepoConfig, ReposRegistry

    return ReposRegistry(
        repos={
            "robotsix-llmio": RepoConfig(
                repo_id="robotsix-llmio",
                langfuse_project_name="llmio",
                langfuse_public_key="pk-llmio",
                langfuse_secret_key="sk-llmio",
            ),
            "robotsix-calendar-agent": RepoConfig(
                repo_id="robotsix-calendar-agent",
                langfuse_project_name="calendar",
                langfuse_public_key="pk-cal",
                langfuse_secret_key="sk-cal",
            ),
        }
    )


def _make_repos_single():
    """Return a fake ReposRegistry with a single repo."""
    from robotsix_mill.config.repos import RepoConfig, ReposRegistry

    return ReposRegistry(
        repos={
            "robotsix-llmio": RepoConfig(
                repo_id="robotsix-llmio",
                langfuse_project_name="llmio",
                langfuse_public_key="pk-llmio",
                langfuse_secret_key="sk-llmio",
            ),
        }
    )


class TestParsePrereqPackages:
    def test_empty_body(self):
        assert _parse_prereq_packages("") == set()

    def test_no_prereq_section(self):
        assert _parse_prereq_packages("## Scope\n\nDo stuff.") == set()

    def test_import_directive(self):
        body = _make_prereq_body(["robotsix_llmio.config.tier"])
        assert _parse_prereq_packages(body) == {"robotsix_llmio"}

    def test_symbol_directive(self):
        body = """## Prerequisites

```prereq
symbol TierConfig from robotsix_llmio.config.tier
```
"""
        assert _parse_prereq_packages(body) == {"robotsix_llmio"}

    def test_multiple_directives(self):
        body = """## Prerequisites

```prereq
import robotsix_llmio.config.tier
symbol load_tier_config from robotsix_calendar_agent.loader
```
"""
        assert _parse_prereq_packages(body) == {
            "robotsix_llmio",
            "robotsix_calendar_agent",
        }

    def test_stdlib_import(self):
        body = _make_prereq_body(["os.path"])
        assert _parse_prereq_packages(body) == {"os"}

    def test_ignores_non_directive_lines(self):
        body = """## Prerequisites

```prereq
import robotsix_llmio.config.tier
This is a comment line.
# This is also a comment.
symbol TierConfig from robotsix_llmio.config.tier
```
"""
        assert _parse_prereq_packages(body) == {"robotsix_llmio"}


class TestDetectCrossRepoDeps:
    def test_single_repo_no_cross_deps(self):
        """When all children are in the same repo, no cross-repo edges."""
        children = [
            ("c0", "Add TierConfig", _make_prereq_body([])),
            ("c1", "Use TierConfig", _make_prereq_body(["robotsix_llmio"])),
        ]
        repos = _make_repos_single()
        extra_edges, bump_ids = _detect_cross_repo_deps(
            children,
            child_board_id=lambda cid: "board-llmio",
            create_child=lambda t, b: "bump-0",
            repos=repos,
        )
        assert extra_edges == {}
        assert bump_ids == []

    def test_cross_repo_producer_consumer(self):
        """Consumer in repo-b references a package from repo-a; producer
        children exist in repo-a → bump child created + wired."""
        children = [
            ("prod-0", "Add TierConfig to llmio", _make_prereq_body([])),
            (
                "cons-1",
                "Use TierConfig in calendar-agent",
                _make_prereq_body(["robotsix_llmio.config.tier"]),
            ),
        ]
        repos = _make_repos_double()

        created_children: list[tuple[str, str]] = []

        def create_child(title: str, body: str) -> str:
            cid = f"bump-{len(created_children)}"
            created_children.append((title, body))
            return cid

        extra_edges, bump_ids = _detect_cross_repo_deps(
            children,
            child_board_id=lambda cid: (
                "board-llmio" if cid == "prod-0" else "board-calendar"
            ),
            create_child=create_child,
            repos=repos,
        )

        # One bump child created.
        assert len(bump_ids) == 1
        bump_id = bump_ids[0]
        assert bump_id.startswith("bump-")

        # Bump child depends on producer child.
        assert extra_edges[bump_id] == ["prod-0"]

        # Consumer depends on bump child.
        assert "cons-1" in extra_edges
        assert bump_id in extra_edges["cons-1"]

        # Bump child title references both repos.
        assert len(created_children) == 1
        title, body = created_children[0]
        assert "robotsix-calendar-agent" in title
        assert "robotsix-llmio" in title
        assert "robotsix_llmio" in title

    def test_bump_depends_on_all_producers(self):
        """Bump child depends on ALL producer children in the supplier repo."""
        children = [
            ("prod-0", "Add TierConfig", _make_prereq_body([])),
            ("prod-1", "Add load_tier_config", _make_prereq_body([])),
            (
                "cons-2",
                "Use TierConfig",
                _make_prereq_body(["robotsix_llmio.config.tier"]),
            ),
        ]
        repos = _make_repos_double()

        def create_child(title: str, body: str) -> str:
            return "bump-0"

        extra_edges, bump_ids = _detect_cross_repo_deps(
            children,
            child_board_id=lambda cid: (
                "board-llmio" if cid.startswith("prod") else "board-calendar"
            ),
            create_child=create_child,
            repos=repos,
        )

        assert len(bump_ids) == 1
        # Bump depends on both producers (order may vary).
        assert set(extra_edges["bump-0"]) == {"prod-0", "prod-1"}

    def test_multiple_consumers_one_bump(self):
        """Multiple consumers in the same consumer repo referencing the
        same producer repo get one shared bump child."""
        children = [
            ("prod-0", "Add TierConfig", _make_prereq_body([])),
            (
                "cons-1",
                "Use TierConfig",
                _make_prereq_body(["robotsix_llmio.config.tier"]),
            ),
            (
                "cons-2",
                "Use load_tier_config",
                _make_prereq_body(["robotsix_llmio.config.loader"]),
            ),
        ]
        repos = _make_repos_double()

        created: list[str] = []

        def create_child(title: str, body: str) -> str:
            cid = f"bump-{len(created)}"
            created.append(cid)
            return cid

        extra_edges, bump_ids = _detect_cross_repo_deps(
            children,
            child_board_id=lambda cid: (
                "board-llmio" if cid == "prod-0" else "board-calendar"
            ),
            create_child=create_child,
            repos=repos,
        )

        # Only one bump child for the (repo-b, repo-a) pair.
        assert len(bump_ids) == 1
        bump_id = bump_ids[0]

        # Both consumers depend on the same bump child.
        assert bump_id in extra_edges.get("cons-1", [])
        assert bump_id in extra_edges.get("cons-2", [])

    def test_no_producer_children_no_bump(self):
        """When a prerequisite references a different repo but that repo
        has NO producer children among siblings, no bump is created."""
        children = [
            (
                "cons-0",
                "Use TierConfig",
                _make_prereq_body(["robotsix_llmio.config.tier"]),
            ),
        ]
        repos = _make_repos_double()

        extra_edges, bump_ids = _detect_cross_repo_deps(
            children,
            child_board_id=lambda cid: "board-calendar",
            create_child=lambda t, b: "should-not-be-called",
            repos=repos,
        )
        assert extra_edges == {}
        assert bump_ids == []

    def test_same_repo_prereq_no_cross_dep(self):
        """A prerequisite referencing the child's own repo is not cross-repo."""
        children = [
            ("prod-0", "Add TierConfig", _make_prereq_body([])),
            ("cons-1", "Use TierConfig", _make_prereq_body(["robotsix_llmio.core"])),
        ]
        repos = _make_repos_double()

        extra_edges, bump_ids = _detect_cross_repo_deps(
            children,
            child_board_id=lambda cid: "board-llmio",
            create_child=lambda t, b: "should-not-be-called",
            repos=repos,
        )
        assert extra_edges == {}
        assert bump_ids == []


class TestPlanChildDependenciesCrossRepo:
    def test_no_cross_repo_preserves_linear_chain(self):
        """When child_board_id/create_child are not passed, behavior is
        unchanged (backward compat)."""
        children = [
            ("c0", "Design API", "body"),
            ("c1", "Implement API", "body"),
        ]
        edges = plan_child_dependencies(children)
        assert edges == {"c1": ["c0"]}

    def test_cross_repo_integration(self):
        """Full integration: plan_child_dependencies creates bump child
        and wires edges correctly."""
        children = [
            ("prod-0", "Add TierConfig to llmio", _make_prereq_body([])),
            (
                "cons-1",
                "Use TierConfig in calendar-agent",
                _make_prereq_body(["robotsix_llmio.config.tier"]),
            ),
        ]

        created_children: list[tuple[str, str]] = []
        next_bump = 0

        def create_child(title: str, body: str) -> str:
            nonlocal next_bump
            cid = f"bump-{next_bump}"
            next_bump += 1
            created_children.append((title, body))
            return cid

        # Patch get_repos_config to return our fake repos.
        import robotsix_mill.config as _config_mod

        _config_mod._repos_config = _make_repos_double()

        try:
            edges = plan_child_dependencies(
                children,
                child_board_id=lambda cid: (
                    "board-llmio" if cid == "prod-0" else "board-calendar"
                ),
                create_child=create_child,
            )
        finally:
            _config_mod._repos_config = None

        # Bump child created.
        assert len(created_children) == 1
        bump_id = "bump-0"

        # Consumer depends on bump child AND linear predecessor (prod-0).
        assert "cons-1" in edges
        assert bump_id in edges["cons-1"]
        assert "prod-0" in edges["cons-1"]

        # Bump child depends on producer.
        assert bump_id in edges
        assert edges[bump_id] == ["prod-0"]

        # Producer (first child) has no dependency.
        assert "prod-0" not in edges

    def test_cross_repo_with_init_repo_child(self):
        """Cross-repo edges are additive with init-repo wiring.

        The init-repo child and producer child are both in the llmio
        repo, so the bump child conservatively depends on both (it
        waits for all producer-repo children to close).
        """
        children = [
            ("init", "Initialize communication system repository", "create repo"),
            ("prod-0", "Add TierConfig to llmio", _make_prereq_body([])),
            (
                "cons-1",
                "Use TierConfig",
                _make_prereq_body(["robotsix_llmio.config.tier"]),
            ),
        ]

        import robotsix_mill.config as _config_mod

        _config_mod._repos_config = _make_repos_double()

        try:
            edges = plan_child_dependencies(
                children,
                child_board_id=lambda cid: (
                    "board-llmio" if cid in ("init", "prod-0") else "board-calendar"
                ),
                create_child=lambda t, b: "bump-0",
            )
        finally:
            _config_mod._repos_config = None

        # Init-repo wiring: populating children depend on init.
        assert "prod-0" in edges
        assert "init" in edges["prod-0"]
        # Consumer also depends on init (init-repo rule).
        assert "init" in edges["cons-1"]
        # Consumer also depends on bump child (cross-repo rule).
        assert "bump-0" in edges["cons-1"]
        # Bump child depends on all children in the producer repo
        # (init + prod-0), since it must wait for those repos' work.
        assert set(edges["bump-0"]) == {"init", "prod-0"}


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


# -- child_repo_ids field tests --------------------------------------------


def test_epic_breakdown_result_parses_repo_ids_alias():
    """The model may emit ``repo_ids`` instead of ``child_repo_ids``."""
    result = EpicBreakdownResult.model_validate(
        {
            "child_titles": ["A", "B"],
            "child_bodies": ["Body A", "Body B"],
            "repo_ids": ["repo-a", "repo-b"],
        }
    )
    assert result.child_repo_ids == ["repo-a", "repo-b"]


def test_epic_breakdown_result_parses_child_repo_ids():
    """The canonical ``child_repo_ids`` key parses correctly."""
    result = EpicBreakdownResult.model_validate(
        {
            "child_titles": ["A"],
            "child_bodies": ["Body A"],
            "child_repo_ids": ["robotsix-mill"],
        }
    )
    assert result.child_repo_ids == ["robotsix-mill"]


def test_epic_breakdown_result_tolerates_missing_repo_ids():
    """A result with no ``child_repo_ids`` / ``repo_ids`` field yields an
    empty list (all-fallback)."""
    result = EpicBreakdownResult.model_validate(
        {"child_titles": ["A"], "child_bodies": ["Body A"]}
    )
    assert result.child_repo_ids == []


def test_epic_breakdown_result_tolerates_empty_repo_ids():
    """An explicit empty ``repo_ids`` list parses to empty list."""
    result = EpicBreakdownResult.model_validate(
        {
            "child_titles": ["A"],
            "child_bodies": ["Body A"],
            "repo_ids": [],
        }
    )
    assert result.child_repo_ids == []


def test_epic_breakdown_result_defaults_to_empty_list():
    """Construction without any repo_ids field defaults to empty list."""
    result = EpicBreakdownResult(
        child_titles=["A"],
        child_bodies=["Body A"],
    )
    assert result.child_repo_ids == []


def test_run_epic_breakdown_agent_injects_available_repos(monkeypatch):
    """When *available_repos* is provided, an ``available-repos`` section
    is injected into the prompt listing the valid repo IDs."""
    captured_prompt: str | None = None

    class FakeAgent:
        def run_sync(self, prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            return type("R", (), {"output": EpicBreakdownResult()})()

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

    # Without available_repos — no available-repos block.
    run_epic_breakdown_agent(
        settings=settings,
        epic_title="Test Epic",
        epic_description="Build the thing.",
    )
    assert "````available-repos" not in captured_prompt

    # With available_repos — block present with listed repo IDs.
    run_epic_breakdown_agent(
        settings=settings,
        epic_title="Test Epic",
        epic_description="Build the thing.",
        available_repos=[
            ("robotsix-mill", "board-mill"),
            ("robotsix-auto-mail", "board-auto-mail"),
        ],
        epic_repo_id="robotsix-mill",
    )
    assert "````available-repos" in captured_prompt
    assert "``robotsix-mill``" in captured_prompt
    assert "``robotsix-auto-mail``" in captured_prompt
    # The epic's repo is named.
    assert "robotsix-mill" in captured_prompt
    # Must appear after the epic-description block.
    assert captured_prompt.index("````epic-description") < captured_prompt.index(
        "````available-repos"
    )
