"""Bump-plan computation for the pin-bump pipeline.

Resolves latest upstream SHAs for every internal dependency, feeds the
existing coherent resolver, and produces an ordered, cycle-safe plan of
concrete bump actions.  Still dry-run — no pins are rewritten and no
PRs are created.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from robotsix_mill.deps.coherent_resolver import resolve_coherent_set

if TYPE_CHECKING:
    from robotsix_mill.config.repos import ReposRegistry
    from robotsix_mill.config.settings import Settings
    from robotsix_mill.deps.internal_graph import InternalDepGraph

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BumpAction:
    """A single pin-bump from one revision to another."""

    repo_id: str
    dep_name: str
    from_rev: str
    to_rev: str


@dataclass
class BumpPlan:
    """Ordered list of bump actions (leaves / upstream repos first)."""

    actions: list[BumpAction] = field(default_factory=list)


# ---------------------------------------------------------------------------
# latest-SHA resolution
# ---------------------------------------------------------------------------


def _authed_url(url: str, token: str) -> str:
    """Inject an oauth2 token into an https remote URL.

    Other schemes are returned unchanged.  Never log the result — it
    contains the credential.
    """
    if url.startswith("https://"):
        return f"https://oauth2:{token}@{url.removeprefix('https://')}"
    return url


def _resolve_latest_sha(git_url: str, branch: str, token: str) -> str | None:
    """Return the head commit SHA of *branch* on *git_url* via ``git ls-remote``.

    Returns ``None`` on any failure (timeout, non-zero exit, empty
    output, unexpected output format).
    """
    authed = _authed_url(git_url, token)
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "ls-remote", authed, f"refs/heads/{branch}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning(
            "pin_bump: git ls-remote failed for %s (branch=%s): %s",
            git_url,
            branch,
            exc,
        )
        return None

    if result.returncode != 0:
        log.warning(
            "pin_bump: git ls-remote non-zero exit for %s (branch=%s): %s",
            git_url,
            branch,
            result.stderr.strip(),
        )
        return None

    if not result.stdout.strip():
        log.warning(
            "pin_bump: git ls-remote empty output for %s (branch=%s)",
            git_url,
            branch,
        )
        return None

    # Output format: <sha>\t<refname> — take the first matching line.
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            return parts[0]

    log.warning(
        "pin_bump: git ls-remote unexpected output for %s (branch=%s): %.200s",
        git_url,
        branch,
        result.stdout.strip(),
    )
    return None


def resolve_latest_shas(
    dep_graph: InternalDepGraph,
    registry: ReposRegistry,
    settings: Settings,
) -> dict[str, str]:
    """Resolve the latest commit SHA for every internal dep in *dep_graph*.

    For each dep name that appears in any repo's pins, looks up the
    corresponding :class:`~robotsix_mill.config.repos.RepoConfig` in
    *registry* to determine the git URL and target branch, then resolves
    the head SHA of that branch via ``git ls-remote``.

    Returns ``{normalised_dep_name: latest_sha}``, keyed to match the
    same name space used by ``graph.pins`` and
    :func:`~robotsix_mill.deps.coherent_resolver.resolve_coherent_set`.
    Dependencies whose latest SHA cannot be resolved (no registry entry,
    no forge URL, no token, ``ls-remote`` failure) are logged at
    ``WARNING`` and omitted from the result.
    """
    from robotsix_mill.config.repos import target_branch_for
    from robotsix_mill.forge.auth import github_token

    # Collect every unique dep name across all repos.
    all_dep_names: set[str] = set()
    for repo_pins in dep_graph.pins.values():
        all_dep_names.update(repo_pins.keys())

    latest: dict[str, str] = {}
    for dep_name in sorted(all_dep_names):
        dep_rc = registry.repos.get(dep_name)
        if dep_rc is None:
            log.warning(
                "pin_bump: dep %s not in registry — cannot resolve latest SHA",
                dep_name,
            )
            continue

        git_url = dep_rc.forge_remote_url
        if not git_url:
            log.warning(
                "pin_bump: dep %s has no forge_remote_url — cannot resolve latest SHA",
                dep_name,
            )
            continue

        try:
            token = github_token(settings, repo_config=dep_rc)
        except RuntimeError as exc:
            log.warning(
                "pin_bump: no forge token for dep %s — cannot resolve latest SHA (%s)",
                dep_name,
                exc,
            )
            continue

        branch = target_branch_for(settings, dep_rc)
        sha = _resolve_latest_sha(git_url, branch, token)
        if sha is not None:
            latest[dep_name] = sha
        else:
            log.warning(
                "pin_bump: could not resolve latest SHA for dep %s (branch=%s)",
                dep_name,
                branch,
            )

    return latest


# ---------------------------------------------------------------------------
# plan computation
# ---------------------------------------------------------------------------


def plan_pin_bumps(
    graph: InternalDepGraph,
    latest_shas: dict[str, str],
) -> BumpPlan:
    """Compute a bump plan from *graph* and pre-resolved *latest_shas*.

    Calls :func:`~robotsix_mill.deps.coherent_resolver.resolve_coherent_set`
    to ensure every shared transitive dependency agrees on ONE target
    commit SHA, then compares each current pin against its resolved
    target.

    Returns a :class:`BumpPlan` whose actions are ordered by
    ``graph.topo_order`` (leaves / upstream repos first — an upstream
    bump is planned before the downstream repos that depend on it).
    Repos absent from ``graph.pins`` and deps whose current pin already
    matches the resolved target produce no action.

    *latest_shas* must be keyed by **normalised dep name** (the same
    key space used by ``graph.pins`` and the coherent resolver).
    """
    resolution = resolve_coherent_set(graph, latest_shas)

    actions: list[BumpAction] = []
    for repo_id in graph.topo_order:
        repo_pins = graph.pins.get(repo_id)
        if repo_pins is None:
            continue
        for dep_name, current_pin in sorted(repo_pins.items()):
            # For shared deps, use the coherent target (agreed SHA).
            # For non-shared deps, use latest_shas directly (the
            # coherent resolver keeps the current pin for those).
            if dep_name in resolution.shared_deps:
                target_rev = resolution.per_repo_pins.get(repo_id, {}).get(dep_name)
            else:
                target_rev = latest_shas.get(dep_name, current_pin.rev)

            if target_rev is None:
                continue
            if current_pin.rev != target_rev:
                actions.append(
                    BumpAction(
                        repo_id=repo_id,
                        dep_name=dep_name,
                        from_rev=current_pin.rev,
                        to_rev=target_rev,
                    )
                )

    return BumpPlan(actions=actions)
