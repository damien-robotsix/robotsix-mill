"""Tests for ``robotsix_mill.deps.bump_planner``."""

from __future__ import annotations

from robotsix_mill.deps.bump_planner import BumpAction, BumpPlan, plan_pin_bumps
from robotsix_mill.deps.internal_graph import (
    INTERNAL_GIT_HOST,
    GitPin,
    InternalDepGraph,
)


# ---------------------------------------------------------------------------
# plan_pin_bumps
# ---------------------------------------------------------------------------


class TestPlanPinBumps:
    def test_topo_ordering(self) -> None:
        """Actions are emitted in graph.topo_order (leaves first)."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old-lib"
                    )
                },
                "b": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old-lib"
                    )
                },
            },
            topo_order=["lib", "a", "b"],
        )
        latest_shas = {"lib": "new-lib"}

        plan = plan_pin_bumps(graph, latest_shas)

        # Both "a" and "b" pin lib.  Topo order is ["lib", "a", "b"].
        # "lib" is not in graph.pins (no pyproject parsed for it), so
        # only "a" and "b" produce actions, and "a" comes before "b".
        assert len(plan.actions) == 2
        assert plan.actions[0].repo_id == "a"
        assert plan.actions[1].repo_id == "b"

    def test_shared_dep_coherence(self) -> None:
        """A dep pinned by two repos resolves to ONE agreed target SHA
        in every action (verified via resolve_coherent_set)."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old1")
                },
                "b": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old2")
                },
            },
            topo_order=["lib", "a", "b"],
        )
        latest_shas = {"lib": "agreed-sha"}

        plan = plan_pin_bumps(graph, latest_shas)

        assert len(plan.actions) == 2
        # Both actions must target the same agreed SHA.
        assert plan.actions[0].to_rev == "agreed-sha"
        assert plan.actions[1].to_rev == "agreed-sha"
        assert plan.actions[0].dep_name == "lib"
        assert plan.actions[1].dep_name == "lib"

    def test_no_op_when_current(self) -> None:
        """When every pin already equals the coherent target, the plan
        is empty."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="current"
                    )
                },
                "b": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="current"
                    )
                },
            },
            topo_order=["lib", "a", "b"],
        )
        latest_shas = {"lib": "current"}  # same as current pins

        plan = plan_pin_bumps(graph, latest_shas)

        assert plan.actions == []
        assert isinstance(plan, BumpPlan)

    def test_unresolvable_sha_skip(self) -> None:
        """A dep not present in latest_shas produces no BumpAction
        (the coherent resolver keeps the current pin)."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old-lib"
                    )
                },
            },
            topo_order=["lib", "a"],
        )
        # "lib" is NOT in latest_shas — should keep current pin, no action.
        latest_shas: dict[str, str] = {}

        plan = plan_pin_bumps(graph, latest_shas)

        assert plan.actions == []

    def test_mixed_updates_and_current(self) -> None:
        """Some pins are stale, others already at target — only stale
        ones produce actions."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old-lib"
                    ),
                    "util": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}util", rev="current-util"
                    ),
                },
                "b": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old-lib"
                    ),
                },
            },
            topo_order=["lib", "util", "a", "b"],
        )
        latest_shas = {"lib": "new-lib", "util": "current-util"}

        plan = plan_pin_bumps(graph, latest_shas)

        # lib is stale for both a and b → 2 actions.
        # util is current for a → 0 actions.
        assert len(plan.actions) == 2
        assert {a.dep_name for a in plan.actions} == {"lib"}
        assert all(a.to_rev == "new-lib" for a in plan.actions)

    def test_repo_not_in_pins_produces_no_action(self) -> None:
        """A repo in topo_order but absent from graph.pins is skipped
        gracefully."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old-lib"
                    )
                },
            },
            topo_order=["lib", "a", "b"],  # "b" not in pins
        )
        latest_shas = {"lib": "new-lib"}

        plan = plan_pin_bumps(graph, latest_shas)

        assert len(plan.actions) == 1
        assert plan.actions[0].repo_id == "a"

    def test_empty_graph(self) -> None:
        """Empty graph → empty plan."""
        graph = InternalDepGraph()
        latest_shas: dict[str, str] = {}

        plan = plan_pin_bumps(graph, latest_shas)

        assert plan.actions == []

    def test_unshared_dep_keeps_latest_sha(self) -> None:
        """An unshared dep (pinned by only one repo) gets its
        latest_shas value — the coherent resolver forwards it."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "helper": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}helper", rev="old-helper"
                    )
                },
            },
            topo_order=["helper", "a"],
        )
        latest_shas = {"helper": "new-helper"}

        plan = plan_pin_bumps(graph, latest_shas)

        assert len(plan.actions) == 1
        assert plan.actions[0].dep_name == "helper"
        assert plan.actions[0].from_rev == "old-helper"
        assert plan.actions[0].to_rev == "new-helper"

    def test_bump_action_is_frozen(self) -> None:
        """BumpAction is frozen (immutable)."""
        action = BumpAction(repo_id="a", dep_name="lib", from_rev="old", to_rev="new")
        assert action.repo_id == "a"
        # Frozen dataclass: attempting mutation raises.
        try:
            action.repo_id = "b"  # type: ignore[misc]
        except Exception:
            pass  # expected — frozen is enforced
        assert action.repo_id == "a"
