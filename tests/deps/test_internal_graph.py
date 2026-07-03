"""Tests for ``robotsix_mill.deps.internal_graph``."""

from __future__ import annotations

import pytest

from robotsix_mill.config import load_repos_config
from robotsix_mill.deps.internal_graph import (
    INTERNAL_GIT_HOST,
    CyclicDependencyError,
    GitPin,
    build_internal_dep_graph,
    parse_internal_git_pins,
)


# ---------------------------------------------------------------------------
# test helpers
# ---------------------------------------------------------------------------


def _write(tmp_path, body: str) -> str:
    """Write *body* to ``repos.yaml`` under *tmp_path* and return the
    path as a string."""
    f = tmp_path / "repos.yaml"
    f.write_text(body, encoding="utf-8")
    return str(f)


def _repos_yaml(*repo_ids: str) -> str:
    """Minimal ``repos.yaml`` body for the given repo ids."""
    lines = ["repos:"]
    for rid in repo_ids:
        lines.append(f"  {rid}:")
        lines.append(f'    board_id: "{rid}"')
    return "\n".join(lines) + "\n"


def _internal_pin(pkg: str, rev: str = "abc123") -> str:
    """Return a TOML inline-table source entry for an internal pin.

    The ``pkg`` name is used for BOTH the git URL path segment AND as
    the package key, so that the normalised package name matches the
    repo id in the registry.
    """
    return '{ git = "https://' + INTERNAL_GIT_HOST + pkg + '", rev = "' + rev + '" }'


def _external_pin(pkg: str, rev: str = "abc123") -> str:
    """Return a TOML inline-table source entry for a non-internal git dep."""
    return (
        '{ git = "https://github.com/some-other-org/' + pkg + '", rev = "' + rev + '" }'
    )


def _pyproject(sources: dict[str, str] | None = None) -> str:
    """Build a minimal ``pyproject.toml`` string with an optional
    ``[tool.uv.sources]`` section."""
    if not sources:
        return '[project]\nname = "test"\n'
    lines = ["[project]", 'name = "test"', "", "[tool.uv.sources]"]
    for pkg, entry in sources.items():
        lines.append(f"{pkg} = {entry}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# parse_internal_git_pins
# ---------------------------------------------------------------------------


class TestParseInternalGitPins:
    def test_no_uv_sources_section(self) -> None:
        content = '[project]\nname = "test"\n'
        pins = parse_internal_git_pins(content, frozenset({"a"}))
        assert pins == {}

    def test_non_internal_source_ignored(self) -> None:
        content = _pyproject({"robotsix-a": _external_pin("robotsix-a")})
        pins = parse_internal_git_pins(content, frozenset({"robotsix-a"}))
        assert pins == {}

    def test_single_internal_pin(self) -> None:
        content = _pyproject({"b": _internal_pin("b", "deadbeef")})
        pins = parse_internal_git_pins(content, frozenset({"b"}))
        assert "b" in pins
        assert pins["b"].git_url == f"https://{INTERNAL_GIT_HOST}b"
        assert pins["b"].rev == "deadbeef"

    def test_normalisation_underscore_to_hyphen(self) -> None:
        content = _pyproject({"robotsix_llmio": _internal_pin("robotsix_llmio")})
        pins = parse_internal_git_pins(content, frozenset({"robotsix-llmio"}))
        assert "robotsix-llmio" in pins
        assert "robotsix_llmio" not in pins

    def test_missing_rev_skipped(self) -> None:
        """A source dict without a ``rev`` key is skipped."""
        content = (
            '[project]\nname = "test"\n\n'
            "[tool.uv.sources]\n"
            'robotsix-a = { git = "https://' + INTERNAL_GIT_HOST + 'robotsix-a" }\n'
        )
        pins = parse_internal_git_pins(content, frozenset({"robotsix-a"}))
        assert pins == {}

    def test_non_dict_source_skipped(self) -> None:
        """``[[tool.uv.sources]]`` (array-of-tables, parsed as list) →
        empty result."""
        content = (
            '[project]\nname = "test"\n\n'
            "[[tool.uv.sources]]\n"
            'name = "robotsix-a"\n'
            'git = "https://' + INTERNAL_GIT_HOST + 'robotsix-a"\n'
            'rev = "abc123"\n'
        )
        pins = parse_internal_git_pins(content, frozenset({"robotsix-a"}))
        assert pins == {}

    def test_gitpin_fields(self) -> None:
        content = _pyproject({"my-pkg": _internal_pin("my-pkg", "sha123")})
        pins = parse_internal_git_pins(content, frozenset({"my-pkg"}))
        pin = pins["my-pkg"]
        assert isinstance(pin, GitPin)
        assert pin.git_url == f"https://{INTERNAL_GIT_HOST}my-pkg"
        assert pin.rev == "sha123"

    def test_dep_not_in_registry_still_recorded(self) -> None:
        """A dep whose normalised name is NOT in the registry is still
        recorded — the caller decides whether to use it as a graph edge."""
        content = _pyproject({"unknown": _internal_pin("unknown")})
        pins = parse_internal_git_pins(content, frozenset())
        assert "unknown" in pins
        assert pins["unknown"].rev == "abc123"


# ---------------------------------------------------------------------------
# build_internal_dep_graph
# ---------------------------------------------------------------------------


class TestBuildInternalDepGraph:
    def test_no_uv_sources_section(self, tmp_path) -> None:
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b")))
        graph = build_internal_dep_graph({"a": _pyproject(), "b": _pyproject()}, reg)
        assert graph.pins == {"a": {}, "b": {}}
        assert set(graph.topo_order) == {"a", "b"}

    def test_non_internal_source_ignored(self, tmp_path) -> None:
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b")))
        graph = build_internal_dep_graph(
            {
                "a": _pyproject({"b": _external_pin("b")}),
                "b": _pyproject(),
            },
            reg,
        )
        assert "b" not in graph.pins.get("a", {})

    def test_single_internal_pin(self, tmp_path) -> None:
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b")))
        graph = build_internal_dep_graph(
            {
                "a": _pyproject({"b": _internal_pin("b")}),
                "b": _pyproject(),
            },
            reg,
        )
        assert "b" in graph.pins["a"]
        # B (leaf) before A
        assert graph.topo_order.index("b") < graph.topo_order.index("a")

    def test_linear_chain(self, tmp_path) -> None:
        """A → B → C  (C is leaf, exact order [c, b, a])."""
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b", "c")))
        graph = build_internal_dep_graph(
            {
                "a": _pyproject({"b": _internal_pin("b")}),
                "b": _pyproject({"c": _internal_pin("c")}),
                "c": _pyproject(),
            },
            reg,
        )
        assert graph.topo_order == ["c", "b", "a"]

    def test_diamond(self, tmp_path) -> None:
        """Diamond: A→B, A→C, B→D, C→D.  D before B/C, B/C before A."""
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b", "c", "d")))
        graph = build_internal_dep_graph(
            {
                "a": _pyproject({"b": _internal_pin("b"), "c": _internal_pin("c")}),
                "b": _pyproject({"d": _internal_pin("d")}),
                "c": _pyproject({"d": _internal_pin("d")}),
                "d": _pyproject(),
            },
            reg,
        )
        order = graph.topo_order
        # leaf first
        assert order[0] == "d"
        # B and C before A
        assert order.index("b") < order.index("a")
        assert order.index("c") < order.index("a")

    def test_package_name_normalisation(self, tmp_path) -> None:
        """Registry key uses hyphen; dep listed with underscore →
        normalised to match."""
        reg = load_repos_config(_write(tmp_path, _repos_yaml("my-lib", "my-app")))
        graph = build_internal_dep_graph(
            {
                "my-app": _pyproject({"my_lib": _internal_pin("my_lib")}),
                "my-lib": _pyproject(),
            },
            reg,
        )
        assert "my-lib" in graph.pins["my-app"]
        assert graph.topo_order.index("my-lib") < graph.topo_order.index("my-app")

    def test_cycle_raises(self, tmp_path) -> None:
        """A → B, B → A  → CyclicDependencyError."""
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b")))
        with pytest.raises(CyclicDependencyError):
            build_internal_dep_graph(
                {
                    "a": _pyproject({"b": _internal_pin("b")}),
                    "b": _pyproject({"a": _internal_pin("a")}),
                },
                reg,
            )

    def test_cycle_is_value_error(self) -> None:
        assert issubclass(CyclicDependencyError, ValueError)

    def test_repo_absent_from_pyproject_map(self, tmp_path) -> None:
        """Repo in registry but not in pyproject_map → not in graph."""
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b", "c")))
        graph = build_internal_dep_graph(
            {
                "a": _pyproject({"b": _internal_pin("b")}),
                "b": _pyproject(),
            },
            reg,
        )
        # 'c' is in registry but not pyproject_map → not in graph
        assert "c" not in graph.pins
        assert "c" not in graph.topo_order

    def test_dep_not_in_registry_ignored_from_graph(self, tmp_path) -> None:
        """Dep not in registry → recorded in pins but no graph edge."""
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b")))
        graph = build_internal_dep_graph(
            {
                "a": _pyproject(
                    {
                        "b": _internal_pin("b"),
                        "unknown": _internal_pin("unknown"),
                    }
                ),
                "b": _pyproject(),
            },
            reg,
        )
        # 'unknown' is in pins metadata
        assert "unknown" in graph.pins["a"]
        # But 'unknown' is NOT in active_ids → no edge, so 'b' and 'a'
        # still ordered correctly
        assert graph.topo_order.index("b") < graph.topo_order.index("a")
        # 'unknown' does not appear in topo_order (not a node)
        assert "unknown" not in graph.topo_order

    def test_gitpin_fields(self, tmp_path) -> None:
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b")))
        graph = build_internal_dep_graph(
            {
                "a": _pyproject({"b": _internal_pin("b", "sha999")}),
                "b": _pyproject(),
            },
            reg,
        )
        pin = graph.pins["a"]["b"]
        assert isinstance(pin, GitPin)
        assert pin.git_url == f"https://{INTERNAL_GIT_HOST}b"
        assert pin.rev == "sha999"

    def test_isolated_nodes_in_topo_order(self, tmp_path) -> None:
        """Disconnected repos all appear in topo_order."""
        reg = load_repos_config(_write(tmp_path, _repos_yaml("a", "b", "c")))
        graph = build_internal_dep_graph(
            {"a": _pyproject(), "b": _pyproject(), "c": _pyproject()},
            reg,
        )
        assert set(graph.topo_order) == {"a", "b", "c"}

    def test_three_node_chain_normalisation_mixed(self, tmp_path) -> None:
        """Three-node chain with mixed underscore/hyphen names."""
        reg = load_repos_config(
            _write(tmp_path, _repos_yaml("alpha", "beta-lib", "gamma"))
        )
        graph = build_internal_dep_graph(
            {
                "alpha": _pyproject({"beta_lib": _internal_pin("beta_lib")}),
                "beta-lib": _pyproject({"gamma": _internal_pin("gamma")}),
                "gamma": _pyproject(),
            },
            reg,
        )
        # beta-lib normalises to match registry
        assert "beta-lib" in graph.pins["alpha"]
        assert graph.topo_order.index("gamma") < graph.topo_order.index("beta-lib")
        assert graph.topo_order.index("beta-lib") < graph.topo_order.index("alpha")
