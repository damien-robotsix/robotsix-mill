"""Read-only internal-dependency graph model.

Parses ``[tool.uv.sources]`` git ``rev`` pins from supplied
``pyproject.toml`` contents, builds a directed dependency graph, and
returns it topologically sorted (leaves first).
"""

from __future__ import annotations

import graphlib
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_mill.config.repos import ReposRegistry

INTERNAL_GIT_HOST = "github.com/damien-robotsix/"


@dataclass(frozen=True)
class GitPin:
    """A single resolved git-source pin from ``[tool.uv.sources]``."""

    git_url: str  # full URL, e.g. https://github.com/damien-robotsix/robotsix-llmio
    rev: str  # commit SHA


@dataclass
class InternalDepGraph:
    """Parsed internal-dependency graph for a set of repos.

    *pins* maps each *repo_id* present in both *pyproject_map* and
    the registry to its resolved internal ``GitPin`` entries (which
    may include deps whose package name is NOT in the registry —
    those are metadata-only, they produce no topo edge).

    *topo_order* is a topological sort of the repos — leaves (those
    with no internal dependents) appear first.  If *A* pins *B*,
    *B* appears before *A*.
    """

    pins: dict[str, dict[str, GitPin]] = field(default_factory=dict)
    topo_order: list[str] = field(default_factory=list)


class CyclicDependencyError(ValueError):
    """Raised when the internal-dependency graph contains a cycle."""


def _normalise(name: str) -> str:
    """PEP 503 normalisation: lowercase and replace underscores with hyphens."""
    return name.lower().replace("_", "-")


def parse_internal_git_pins(
    pyproject_content: str,
    internal_repo_ids: frozenset[str],
) -> dict[str, GitPin]:
    """Parse ``[tool.uv.sources]`` git pins from *pyproject_content*.

    Returns ``{normalised_pkg_name: GitPin}`` for every
    ``[tool.uv.sources]`` entry whose git URL contains
    ``INTERNAL_GIT_HOST`` and carries a ``rev``.

    Entries whose normalised name is NOT in *internal_repo_ids* are
    still returned — the caller decides which pins become graph edges.
    """
    data = tomllib.loads(pyproject_content)
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})

    if not isinstance(sources, dict):
        return {}

    result: dict[str, GitPin] = {}
    for pkg_name, source in sources.items():
        if not isinstance(source, dict):
            continue

        git_url = source.get("git", "")
        if not isinstance(git_url, str) or INTERNAL_GIT_HOST not in git_url:
            continue

        rev = source.get("rev")
        if rev is None or not isinstance(rev, str):
            continue

        normalised = _normalise(pkg_name)
        result[normalised] = GitPin(git_url=git_url, rev=rev)

    return result


def build_internal_dep_graph(
    pyproject_map: dict[str, str],
    registry: ReposRegistry,
) -> InternalDepGraph:
    """Build an :class:`InternalDepGraph` from *pyproject_map* and *registry*.

    *pyproject_map* maps each *repo_id* to the full text of its
    ``pyproject.toml``.  Only repos present in BOTH *pyproject_map*
    and *registry* become *pins* keys and graph nodes.  Non-registry
    deps produce no graph edge but stay in *pins* as metadata.
    """
    from robotsix_mill.config.repos import ReposRegistry

    if not isinstance(registry, ReposRegistry):
        raise TypeError(
            f"registry must be a ReposRegistry instance, got {type(registry).__name__}"
        )

    active_ids = set(pyproject_map.keys()) & set(registry.repos.keys())
    all_registry_ids = frozenset(registry.repos.keys())

    pins: dict[str, dict[str, GitPin]] = {}
    for repo_id in active_ids:
        pins[repo_id] = parse_internal_git_pins(
            pyproject_map[repo_id], all_registry_ids
        )

    predecessors: dict[str, set[str]] = {rid: set() for rid in active_ids}
    for repo_id in active_ids:
        for dep_id in pins[repo_id]:
            if dep_id in active_ids:
                predecessors[repo_id].add(dep_id)

    ts = graphlib.TopologicalSorter(predecessors)
    try:
        topo_order: list[str] = list(ts.static_order())
    except graphlib.CycleError as exc:
        raise CyclicDependencyError(str(exc)) from exc

    return InternalDepGraph(pins=pins, topo_order=topo_order)
