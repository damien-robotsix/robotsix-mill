"""Pin-bump scheduled runner — detection + PR actuator.

A ``schedule_only`` periodic runner that scans every registered repo's
``pyproject.toml`` for internal git-source pins, computes the coherent
dependency graph (topological order + current SHAs), and opens PRs to
bump stale pins.  No LLM call — pure graph computation.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from robotsix_mill.config.repos import (
    RepoConfig,
    get_repos_config,
    target_branch_for,
)
from robotsix_mill.config.settings import Settings
from robotsix_mill.deps.bump_planner import (
    BumpPlan,
    plan_pin_bumps,
    resolve_latest_shas,
)
from robotsix_mill.deps.internal_graph import (
    CyclicDependencyError,
    build_internal_dep_graph,
)
from robotsix_mill.forge.auth import github_token
from robotsix_mill.vcs import git_ops

log = logging.getLogger(__name__)


def _fetch_one_pyproject(
    repo_id: str,
    rc: RepoConfig,
    settings: Settings,
) -> str | None:
    """Clone *rc* and return its ``pyproject.toml`` text, or ``None``.

    Returns ``None`` for any expected failure (no remote URL, no
    token, clone failure, missing file, read error) — each case is
    logged at warning level so the scheduler loop can see why a repo
    was skipped without crashing the pass.
    """
    repo_url = rc.forge_remote_url
    if not repo_url:
        log.warning("pin_bump: no forge_remote_url for %s — skipping", repo_id)
        return None

    try:
        token = github_token(settings, repo_config=rc)
    except RuntimeError as exc:
        log.warning("pin_bump: no forge token for %s — skipping (%s)", repo_id, exc)
        return None

    branch = target_branch_for(settings, rc)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        try:
            git_ops.clone(repo_url, tmp_path, branch, token)
        except Exception as exc:
            log.warning(
                "pin_bump: clone failed for %s (branch=%s) — skipping (%s)",
                repo_id,
                branch,
                exc,
            )
            return None

        toml_path = tmp_path / "pyproject.toml"
        if not toml_path.is_file():
            log.warning("pin_bump: no pyproject.toml in %s — skipping", repo_id)
            return None

        try:
            return toml_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                "pin_bump: cannot read pyproject.toml for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return None


def _log_plan(plan: BumpPlan) -> None:
    """Log the bump plan at INFO level."""
    if plan.actions:
        log.info("pin_bump: bump plan (%d actions):", len(plan.actions))
        for action in plan.actions:
            log.info(
                "pin_bump: bump %s  %s  %s -> %s",
                action.repo_id,
                action.dep_name,
                action.from_rev,
                action.to_rev,
            )
    else:
        log.info("pin_bump: all pins current — nothing to bump")


def run_pin_bump_pass(
    *,
    session_id: str,
    repo_config: RepoConfig | None = None,
) -> None:
    """Execute one pin-bump detection pass across all registered repos.

    Returns immediately when *repo_config* is ``None`` (the periodic
    supervisor fires per-repo, but the pin_bump pass is cross-repo —
    it needs the full registry, so the first invocation with a
    non-None *repo_config* does the work and subsequent invocations
    in the same pass are no-ops via the early return).

    Detection only — computes and logs the topological order and
    current pin SHAs.  Zero PRs are created.
    """
    if repo_config is None:
        return

    settings = Settings()
    registry = get_repos_config()

    # Gather pyproject.toml text from every reachable registered repo.
    pyproject_map: dict[str, str] = {}
    for repo_id, rc in registry.repos.items():
        content = _fetch_one_pyproject(repo_id, rc, settings)
        if content is not None:
            pyproject_map[repo_id] = content

    if not pyproject_map:
        log.info("pin_bump: no reachable repos with pyproject.toml — nothing to do")
        return

    # Build the dependency graph and report.
    try:
        graph = build_internal_dep_graph(pyproject_map, registry)
    except CyclicDependencyError as exc:
        log.warning(
            "pin_bump: cyclic dependency detected — cannot compute "
            "topological order (%s)",
            exc,
        )
        return

    log.info("pin_bump: topological order: %s", graph.topo_order)
    for repo_id in graph.topo_order:
        pins = graph.pins.get(repo_id, {})
        if pins:
            for dep_id, pin in sorted(pins.items()):
                log.info(
                    "pin_bump: pin  %s → %s  @ %s",
                    repo_id,
                    dep_id,
                    pin.rev,
                )
        else:
            log.info("pin_bump: pin  %s  (no internal deps)", repo_id)

    # Resolve latest upstream SHAs and compute a bump plan.
    latest_shas = resolve_latest_shas(graph, registry, settings)
    plan = plan_pin_bumps(graph, latest_shas)
    _log_plan(plan)
