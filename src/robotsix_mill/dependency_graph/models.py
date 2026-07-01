"""Data models for the dependency-graph pin-bump system.

Key types
---------
- :class:`GitPin` — one ``[tool.uv.sources]`` entry parsed from a
  ``pyproject.toml``, capturing the package name, git URL, and
  current pinned rev.
- :class:`RepoNode` — one repo in the dependency graph: its identity,
  local clone path, current pins, and upstream/downstream edges.
- :class:`DependencyGraph` — the full directed graph across all
  registered repos, with topological ordering.
- :class:`PinBump` — a proposed update: repo + package → old rev → new
  rev, plus the resolved target SHA and whether it was already current.

Utilities
---------
- :func:`normalize_git_url` — strip protocol, ``.git`` suffix,
  and trailing slash from a git URL so two URLs pointing at the same
  repo compare equal.
- :func:`url_matches_repo` — check whether a ``[tool.uv.sources]``
  git URL points at a given :class:`RepoNode`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class GitPin:
    """A single ``[tool.uv.sources]`` entry pointing at a git dependency."""

    package: str
    """The package name (key in ``[tool.uv.sources]``). E.g. ``robotsix-mill``."""

    git_url: str
    """The ``git`` URL. E.g. ``https://github.com/robotsix/mill.git``."""

    rev: str | None = None
    """The pinned revision (``rev``, ``tag``, or ``branch``). ``None`` when
    the pin floats (no rev/tag/branch — uv resolves to HEAD)."""

    subdirectory: str | None = None
    """Optional ``subdirectory`` for monorepo packages."""

    @property
    def is_pinned(self) -> bool:
        """True when this source carries an explicit revision pin."""
        return self.rev is not None and len(self.rev) >= 7  # short SHA floor


@dataclass
class RepoNode:
    """One repository in the dependency graph."""

    repo_id: str
    """The mill-internal repo id (key in ``config/repos.yaml``)."""

    clone_path: Path | None = None
    """Local clone path (``None`` when this repo wasn't cloned this pass)."""

    forge_remote_url: str = ""
    """The forge remote URL for cloning / PR creation."""

    target_branch: str = "main"
    """The branch to read pins from and open PRs against."""

    pins: list[GitPin] = field(default_factory=list)
    """Git pins declared by THIS repo (its ``[tool.uv.sources]`` entries)."""

    upstream: list[str] = field(default_factory=list)
    """Repo IDs that THIS repo depends on (its pins point to them)."""

    downstream: list[str] = field(default_factory=list)
    """Repo IDs that depend on THIS repo."""

    latest_sha: str | None = None
    """The latest commit SHA on *target_branch* at scan time."""

    current_pins: dict[str, str] = field(default_factory=dict)
    """Snapshot of current pin revs: ``{package: rev}`` before bumping."""

    @property
    def is_leaf(self) -> bool:
        """True when this repo has no internal dependencies (no upstream)."""
        return len(self.upstream) == 0

    @property
    def is_root(self) -> bool:
        """True when no other repo depends on this one (no downstream)."""
        return len(self.downstream) == 0


@dataclass
class DependencyGraph:
    """The full directed dependency graph across registered repos.

    Built by :func:`~.parser.build_graph` and consumed by
    :func:`~.resolver.resolve_coherent_pins`.
    """

    nodes: dict[str, RepoNode] = field(default_factory=dict)
    """All nodes keyed by repo_id."""

    topo_order: list[str] = field(default_factory=list)
    """Repo IDs in topological order (dependencies before dependents)."""

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def node(self, repo_id: str) -> RepoNode | None:
        return self.nodes.get(repo_id)


@dataclass
class PinBump:
    """A single proposed pin update for one repo."""

    repo_id: str
    """Which repo's pyproject.toml gets the bump."""

    package: str
    """The source package name to update."""

    old_rev: str | None
    """Current pinned rev (``None`` when unpinned)."""

    new_rev: str
    """Proposed new rev (the dependency's latest main-branch SHA)."""

    git_url: str = ""
    """The git URL this pin points to."""

    already_current: bool = False
    """True when *old_rev* == *new_rev* — no bump needed."""

    @property
    def description(self) -> str:
        if self.already_current:
            return f"{self.repo_id}: {self.package} already at {self.new_rev[:8]}"
        old = self.old_rev[:8] if self.old_rev else "unpinned"
        return f"{self.repo_id}: {self.package} {old} → {self.new_rev[:8]}"


# ---------------------------------------------------------------------------
# URL normalization utilities
# ---------------------------------------------------------------------------


def normalize_git_url(url: str) -> str:
    """Strip protocol, userinfo, ``.git`` suffix, and trailing slash so
    two URLs that point at the same repo compare equal.

    ``https://github.com/robotsix/mill.git`` and
    ``git@github.com:robotsix/mill`` both normalize to
    ``github.com/robotsix/mill``.
    """
    u = url.strip()
    # SCP-style: git@host:owner/repo → https://host/owner/repo
    if u.startswith("git@"):
        parts = u.split("@", 1)[1]
        host, _, path = parts.partition(":")
        u = f"https://{host}/{path}"
    parsed = urlparse(u)
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/")
    # Drop the trailing ``.git``.
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}{path}".lower()


def url_matches_repo(git_url: str, node: RepoNode) -> bool:
    """Check whether *git_url* points at *node*'s forge remote."""
    if not node.forge_remote_url:
        return False
    return normalize_git_url(git_url) == normalize_git_url(node.forge_remote_url)
