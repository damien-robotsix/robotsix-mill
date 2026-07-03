"""Tests for ``robotsix_mill.deps.coherent_resolver``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from _pytest.monkeypatch import MonkeyPatch

from robotsix_mill.deps.coherent_resolver import (
    resolve_coherent_set,
    run_coherence_check,
)
from robotsix_mill.deps.internal_graph import (
    INTERNAL_GIT_HOST,
    GitPin,
    InternalDepGraph,
)


# ---------------------------------------------------------------------------
# resolve_coherent_set
# ---------------------------------------------------------------------------


class TestResolveCoherentSet:
    def test_no_shared_deps_keeps_current_pins(self) -> None:
        """When no dep is pinned by more than one repo, every pin stays
        at its current revision."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "b": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}b", rev="sha-b")
                },
                "c": {
                    "d": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}d", rev="sha-d")
                },
            },
            topo_order=["b", "d", "a", "c"],
        )
        main_heads = {"b": "head-b", "d": "head-d"}
        result = resolve_coherent_set(graph, main_heads)

        assert result.shared_deps == frozenset()
        assert result.per_repo_pins == {
            "a": {"b": "sha-b"},
            "c": {"d": "sha-d"},
        }

    def test_shared_dep_agreed_commit_is_main_head(self) -> None:
        """When two repos pin the same dep, both get the dep's main HEAD."""
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
        main_heads = {"lib": "new-head"}
        result = resolve_coherent_set(graph, main_heads)

        assert result.shared_deps == frozenset({"lib"})
        assert result.per_repo_pins == {
            "a": {"lib": "new-head"},
            "b": {"lib": "new-head"},
        }

    def test_shared_dep_main_head_unknown_keeps_current(self) -> None:
        """When main HEAD for a shared dep is unknown, keep current pins."""
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
        main_heads: dict[str, str] = {}
        result = resolve_coherent_set(graph, main_heads)

        assert result.shared_deps == frozenset({"lib"})
        # Both keep their current pins because main HEAD is unknown
        assert result.per_repo_pins == {
            "a": {"lib": "old1"},
            "b": {"lib": "old2"},
        }

    def test_mixed_shared_and_unshared(self) -> None:
        """Repos A and B both pin lib (shared), A also pins helper
        (unshared)."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old"),
                    "helper": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}helper", rev="h1"
                    ),
                },
                "b": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="old"),
                },
            },
            topo_order=["lib", "helper", "a", "b"],
        )
        main_heads = {"lib": "new-head", "helper": "h2"}
        result = resolve_coherent_set(graph, main_heads)

        assert result.shared_deps == frozenset({"lib"})
        assert result.per_repo_pins == {
            "a": {"lib": "new-head", "helper": "h1"},
            "b": {"lib": "new-head"},
        }

    def test_three_repos_share_same_dep(self) -> None:
        """Three repos all pin the same dep → all get the agreed commit."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="a1")
                },
                "b": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="b1")
                },
                "c": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="c1")
                },
            },
            topo_order=["lib", "a", "b", "c"],
        )
        main_heads = {"lib": "agreed-sha"}
        result = resolve_coherent_set(graph, main_heads)

        assert result.shared_deps == frozenset({"lib"})
        assert result.per_repo_pins == {
            "a": {"lib": "agreed-sha"},
            "b": {"lib": "agreed-sha"},
            "c": {"lib": "agreed-sha"},
        }

    def test_multiple_shared_deps(self) -> None:
        """A→lib, B→lib, B→util, C→util (lib shared by A,B; util shared by B,C)."""
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="l1")
                },
                "b": {
                    "lib": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}lib", rev="l2"),
                    "util": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}util", rev="u1"
                    ),
                },
                "c": {
                    "util": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}util", rev="u2")
                },
            },
            topo_order=["lib", "util", "a", "b", "c"],
        )
        main_heads = {"lib": "lib-head", "util": "util-head"}
        result = resolve_coherent_set(graph, main_heads)

        assert result.shared_deps == frozenset({"lib", "util"})
        assert result.per_repo_pins == {
            "a": {"lib": "lib-head"},
            "b": {"lib": "lib-head", "util": "util-head"},
            "c": {"util": "util-head"},
        }

    def test_empty_graph(self) -> None:
        graph = InternalDepGraph()
        main_heads: dict[str, str] = {}
        result = resolve_coherent_set(graph, main_heads)
        assert result.shared_deps == frozenset()
        assert result.per_repo_pins == {}


# ---------------------------------------------------------------------------
# run_coherence_check
# ---------------------------------------------------------------------------


class TestRunCoherenceCheck:
    def test_no_conflicts(self, monkeypatch: MonkeyPatch) -> None:
        """uv lock succeeds with no conflicting-URLs output."""
        completed = subprocess.CompletedProcess(
            args=["uv", "lock"],
            returncode=0,
            stdout="Resolved 42 packages\n",
            stderr="",
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["uv", "lock"]:
                return completed
            return subprocess.run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        conflicts = run_coherence_check(Path("/fake/repo"))
        assert conflicts == []

    def test_single_conflict(self, monkeypatch: MonkeyPatch) -> None:
        """uv lock fails with one conflicting-URLs package."""
        completed = subprocess.CompletedProcess(
            args=["uv", "lock"],
            returncode=1,
            stdout="",
            stderr=(
                "  × No solution found when resolving dependencies:\n"
                "  ╰─▶ Requirements contain conflicting URLs for package "
                "`robotsix-llmio`:\n"
                "      …\n"
            ),
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["uv", "lock"]:
                return completed
            return subprocess.run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        conflicts = run_coherence_check(Path("/fake/repo"))
        assert len(conflicts) == 1
        assert "robotsix-llmio" in conflicts[0]

    def test_multiple_conflicts(self, monkeypatch: MonkeyPatch) -> None:
        """uv lock fails with multiple conflicting-URLs packages."""
        completed = subprocess.CompletedProcess(
            args=["uv", "lock"],
            returncode=1,
            stdout="",
            stderr=(
                "  ╰─▶ Requirements contain conflicting URLs for package "
                "`robotsix-llmio`:\n"
                "      …\n"
                "  ╰─▶ Requirements contain conflicting URLs for package "
                "`robotsix-board-agent`:\n"
                "      …\n"
            ),
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["uv", "lock"]:
                return completed
            return subprocess.run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        conflicts = run_coherence_check(Path("/fake/repo"))
        assert len(conflicts) == 2
        assert any("robotsix-llmio" in c for c in conflicts)
        assert any("robotsix-board-agent" in c for c in conflicts)

    def test_uv_lock_success_with_warnings(self, monkeypatch: MonkeyPatch) -> None:
        """uv lock exits 0 with warnings in stderr — not a conflict."""
        completed = subprocess.CompletedProcess(
            args=["uv", "lock"],
            returncode=0,
            stdout="Resolved 42 packages\n",
            stderr="warning: something\n",
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["uv", "lock"]:
                return completed
            return subprocess.run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        conflicts = run_coherence_check(Path("/fake/repo"))
        assert conflicts == []

    def test_uv_lock_nonzero_no_conflict_pattern(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """uv lock fails for some other reason → no conflicts detected."""
        completed = subprocess.CompletedProcess(
            args=["uv", "lock"],
            returncode=1,
            stdout="",
            stderr="error: Failed to parse `pyproject.toml`\n",
        )

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["uv", "lock"]:
                return completed
            return subprocess.run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        conflicts = run_coherence_check(Path("/fake/repo"))
        assert conflicts == []
