"""Dependency-graph model for coordinated internal git-rev pin bumps.

Parses ``[tool.uv.sources]`` from every registered repo's
``pyproject.toml``, builds a directed acyclic graph of internal
dependencies, topologically sorts it, and computes coherent target
SHAs so that ``uv lock`` succeeds across the stack.

Public API
----------
- :func:`build_graph` — scan all registered repos, return a
  :class:`DependencyGraph`.
- :func:`resolve_coherent_pins` — compute a consistent set of target
  SHAs for every repo in the graph.
- :class:`DependencyGraph` — the directed graph of internal deps.
- :class:`RepoNode` — one repo in the graph.
- :class:`GitPin` — a single ``[tool.uv.sources]`` git dependency.
"""

from __future__ import annotations

from .models import (
    DependencyGraph,
    GitPin,
    PinBump,
    RepoNode,
    normalize_git_url,
    url_matches_repo,
)
from .parser import build_graph
from .resolver import resolve_coherent_pins

__all__ = [
    "DependencyGraph",
    "GitPin",
    "PinBump",
    "RepoNode",
    "build_graph",
    "normalize_git_url",
    "resolve_coherent_pins",
    "url_matches_repo",
]
