"""Graph builder: parse ``pyproject.toml`` files across registered repos.

Walks every repo in :func:`~robotsix_mill.config.get_repos_config`,
clones (when a local path is provided), parses ``[tool.uv.sources]``
for git dependencies, matches each git URL against the known repo set,
and wires the directed edges.  The resulting :class:`DependencyGraph`
is topologically sorted so dependents are processed after their
dependencies.
"""

from __future__ import annotations

import logging
import subprocess
import tomllib
from pathlib import Path

from ..config import get_repos_config, target_branch_for
from ..config.settings import Settings
from ..vcs import git_ops
from .models import (
    DependencyGraph,
    GitPin,
    RepoNode,
    url_matches_repo,
)

log = logging.getLogger("robotsix_mill.dependency_graph")


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def _parse_sources(pyproject_path: Path) -> list[GitPin]:
    """Parse ``[tool.uv.sources]`` from *pyproject_path* into
    :class:`GitPin` objects.

    Returns an empty list when the file is absent, unparseable, or
    carries no ``[tool.uv.sources]`` table.
    """
    if not pyproject_path.is_file():
        return []
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("Failed to parse %s: %s", pyproject_path, exc)
        return []

    sources = data.get("tool", {}).get("uv", {}).get("sources")
    if not isinstance(sources, dict):
        return []

    pins: list[GitPin] = []
    for pkg, spec in sources.items():
        if not isinstance(spec, dict):
            continue
        git_url = spec.get("git")
        if not git_url:
            # Not a git dependency — skip (path, url, workspace members).
            continue
        rev = spec.get("rev") or spec.get("tag") or spec.get("branch")
        subdirectory = spec.get("subdirectory")
        pins.append(
            GitPin(
                package=pkg,
                git_url=str(git_url),
                rev=str(rev) if rev else None,
                subdirectory=str(subdirectory) if subdirectory else None,
            )
        )
    return pins


# ---------------------------------------------------------------------------
# Latest SHA resolution
# ---------------------------------------------------------------------------


def _resolve_latest_sha(clone_path: Path, target_branch: str) -> str | None:
    """Return the latest commit SHA on *target_branch* in *clone_path*.

    Requires a cloned repo; returns ``None`` when the clone doesn't
    exist or ``git`` fails.
    """
    if not clone_path or not (clone_path / ".git").exists():
        return None
    try:
        result = git_ops._git(
            clone_path,
            "rev-parse",
            f"origin/{target_branch}",
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        try:
            # Fall back to local branch when origin ref isn't fetched.
            result = git_ops._git(clone_path, "rev-parse", target_branch)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph(
    repo_paths: dict[str, Path] | None = None,
    *,
    settings: Settings | None = None,
) -> DependencyGraph:
    """Scan every registered repo and return a :class:`DependencyGraph`.

    Args:
        repo_paths: Optional ``{repo_id: clone_path}`` for repos the
            caller has already cloned.  Missing repos are parsed from
            their ``forge_remote_url`` by cloning fresh.
        settings: Pre-resolved settings; loaded fresh when ``None``.
    """
    if settings is None:
        settings = Settings()

    repos_config = get_repos_config()
    graph = DependencyGraph()

    # Phase 1: create a node for every registered repo.
    for repo_id, rc in repos_config.repos.items():
        remote = rc.forge_remote_url or settings.forge_remote_url
        target = target_branch_for(settings, rc)
        node = RepoNode(
            repo_id=repo_id,
            forge_remote_url=remote,
            target_branch=target,
        )
        graph.nodes[repo_id] = node

    # Phase 2: for each repo, read its pyproject.toml and extract pins.
    for repo_id, node in graph.nodes.items():
        clone_path = (repo_paths or {}).get(repo_id)

        # If no clone is provided, attempt a best-effort clone so we can
        # read pyproject.toml.  Pin-bump without a clone is safe to skip
        # — the repo will be picked up next week.
        if clone_path is None and node.forge_remote_url:
            try:
                from ..forge.auth import github_token

                rc = repos_config.repos.get(repo_id)
                clone_dest = settings.data_dir / repo_id / "pin_bump_workspace" / "repo"
                if clone_dest.exists():
                    import shutil

                    shutil.rmtree(clone_dest, ignore_errors=True)
                clone_dest.parent.mkdir(parents=True, exist_ok=True)
                token = None
                try:
                    token = github_token(settings, repo_config=rc)
                except RuntimeError:
                    pass
                git_ops.clone(
                    node.forge_remote_url,
                    clone_dest,
                    node.target_branch,
                    token,
                )
                clone_path = clone_dest
            except subprocess.CalledProcessError as exc:
                log.warning(
                    "pin_bump: clone failed for %s: %s",
                    repo_id,
                    (exc.stderr or "")[:200],
                )
                continue

        node.clone_path = clone_path

        if clone_path:
            pyproject = clone_path / "pyproject.toml"
            pins = _parse_sources(pyproject)
            node.pins = pins
            node.latest_sha = _resolve_latest_sha(clone_path, node.target_branch)

    # Phase 3: match pins to repo nodes (wire edges).
    for repo_id, node in graph.nodes.items():
        for pin in node.pins:
            for other_id, other_node in graph.nodes.items():
                if other_id == repo_id:
                    continue
                if url_matches_repo(pin.git_url, other_node):
                    node.upstream.append(other_id)
                    other_node.downstream.append(repo_id)
                    break

    # Phase 4: topological sort (Kahn's algorithm).
    in_degree: dict[str, int] = {
        nid: len(graph.nodes[nid].upstream) for nid in graph.nodes
    }
    queue: list[str] = [nid for nid, deg in in_degree.items() if deg == 0]
    topo: list[str] = []

    while queue:
        nid = queue.pop(0)
        topo.append(nid)
        for downstream_id in graph.nodes[nid].downstream:
            in_degree[downstream_id] -= 1
            if in_degree[downstream_id] == 0:
                queue.append(downstream_id)

    # Any remaining nodes are part of a cycle — append them at the end
    # so the resolver can still attempt bumps (with a warning).
    for nid in sorted(graph.nodes):
        if nid not in topo:
            log.warning("pin_bump: cycle detected involving %s", nid)
            topo.append(nid)

    graph.topo_order = topo
    return graph
