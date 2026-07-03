"""Pin-bump scheduled runner — detection + PR actuator.

A ``schedule_only`` periodic runner that scans every registered repo's
``pyproject.toml`` for internal git-source pins, computes the coherent
dependency graph (topological order + current SHAs), and opens PRs to
bump stale pins.  No LLM call — pure graph computation.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from robotsix_mill.config.repos import (
    RepoConfig,
    ReposRegistry,
    get_repos_config,
    target_branch_for,
)
from robotsix_mill.config.settings import Settings
from robotsix_mill.deps.internal_graph import (
    CyclicDependencyError,
    GitPin,
    InternalDepGraph,
    build_internal_dep_graph,
)
from robotsix_mill.forge import Forge, get_forge
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

    # After detection, run the PR actuator to bump stale pins.
    run_pin_bump_pr_actuator(
        session_id=session_id,
        repo_config=repo_config,
        graph=graph,
    )


def _update_pin_rev(pyproject_text: str, dep_name: str, new_rev: str) -> str:
    """Update the ``rev`` value for *dep_name* in ``[tool.uv.sources]``.

    Returns *pyproject_text* with the ``rev`` string replaced in-place.
    Raises ``ValueError`` when the source entry or its ``rev`` key
    cannot be found.
    """
    pattern = re.compile(
        rf'({re.escape(dep_name)}\s*=\s*\{{[^}}]*?rev\s*=\s*")[^"]*(")',
    )
    if not pattern.search(pyproject_text):
        raise ValueError(f"Could not find rev for {dep_name} in [tool.uv.sources]")
    return pattern.sub(rf"\1{new_rev}\2", pyproject_text)


def _compute_actuator_graph(
    settings: Settings, registry: ReposRegistry
) -> InternalDepGraph | None:
    """Fetch pyproject.toml from every registered repo and build the graph.

    Returns ``None`` when no repos are reachable or a cycle is detected
    (logged and swallowed).
    """
    pyproject_map: dict[str, str] = {}
    for repo_id, rc in registry.repos.items():
        content = _fetch_one_pyproject(repo_id, rc, settings)
        if content is not None:
            pyproject_map[repo_id] = content

    if not pyproject_map:
        log.info("pin_bump: no reachable repos with pyproject.toml — nothing to do")
        return None

    try:
        return build_internal_dep_graph(pyproject_map, registry)
    except CyclicDependencyError as exc:
        log.warning(
            "pin_bump: cyclic dependency detected — cannot compute "
            "topological order (%s)",
            exc,
        )
        return None


def run_pin_bump_pr_actuator(
    *,
    session_id: str,
    repo_config: RepoConfig | None = None,
    graph: InternalDepGraph | None = None,
) -> None:
    """Create PRs to bump stale internal-dependency pins.

    For every :class:`GitPin` in *graph* whose ``rev`` does not match
    the latest commit SHA on the dependency's default branch, clones
    the consuming repo, edits its ``pyproject.toml``, regenerates
    ``uv.lock``, and opens a PR via the forge API.

    When *graph* is ``None`` the dependency graph is computed from
    scratch (re-fetches every ``pyproject.toml``).  Callers that
    already have the graph (e.g. :func:`run_pin_bump_pass`) should
    pass it to avoid duplicate I/O.

    Logs a summary of created PRs and skipped (already-latest) pins.
    Never raises — all expected failures are caught and logged.
    """
    if repo_config is None:
        return

    settings = Settings()
    registry = get_repos_config()

    if graph is None:
        graph = _compute_actuator_graph(settings, registry)
        if graph is None:
            return

    try:
        ls_token = github_token(settings, repo_config=repo_config)
    except RuntimeError as exc:
        log.warning("pin_bump: no forge token for ls-remote — skipping (%s)", exc)
        return

    prs_created, skipped = _bump_pins_for_repos(
        settings=settings,
        registry=registry,
        graph=graph,
        ls_token=ls_token,
    )

    if prs_created:
        log.info("pin_bump: PRs created: %s", prs_created)
    if skipped:
        log.info("pin_bump: skipped (already at latest): %s", skipped)
    if not prs_created and not skipped:
        log.info("pin_bump: no internal pins to bump")


def _bump_pins_for_repos(
    *,
    settings: Settings,
    registry: ReposRegistry,
    graph: InternalDepGraph,
    ls_token: str,
) -> tuple[list[str], list[str]]:
    """Iterate *graph* topo-order and bump every stale pin.

    Returns ``(prs_created, skipped)`` summary lists.
    """
    prs_created: list[str] = []
    skipped: list[str] = []

    for repo_id in graph.topo_order:
        pins = graph.pins.get(repo_id, {})
        if not pins:
            continue

        rc = registry.repos.get(repo_id)
        if rc is None:
            continue

        try:
            push_token = github_token(settings, repo_config=rc)
        except RuntimeError as exc:
            log.warning("pin_bump: no forge token for %s — skipping (%s)", repo_id, exc)
            continue

        target_branch = target_branch_for(settings, rc)
        forge = get_forge(settings, repo_config=rc)

        for dep_name, pin in sorted(pins.items()):
            _bump_one_pin(
                repo_id=repo_id,
                rc=rc,
                dep_name=dep_name,
                pin=pin,
                ls_token=ls_token,
                push_token=push_token,
                target_branch=target_branch,
                forge=forge,
                prs_created=prs_created,
                skipped=skipped,
            )

    return prs_created, skipped


def _bump_one_pin(
    *,
    repo_id: str,
    rc: RepoConfig,
    dep_name: str,
    pin: GitPin,
    ls_token: str,
    push_token: str,
    target_branch: str,
    forge: Forge,
    prs_created: list[str],
    skipped: list[str],
) -> None:
    """Resolve the latest SHA for *pin* and apply the bump if stale."""
    latest_sha = git_ops.ls_remote_sha(pin.git_url, ref="HEAD", token=ls_token)
    if latest_sha is None:
        log.warning(
            "pin_bump: cannot resolve latest SHA for %s → %s (%s)",
            repo_id,
            dep_name,
            pin.git_url,
        )
        return

    if latest_sha == pin.rev:
        skipped.append(f"{repo_id} → {dep_name} (already at {pin.rev[:7]})")
        return

    log.info(
        "pin_bump: bump  %s → %s  %s → %s",
        repo_id,
        dep_name,
        pin.rev[:7],
        latest_sha[:7],
    )

    _apply_one_bump(
        repo_id=repo_id,
        rc=rc,
        dep_name=dep_name,
        old_rev=pin.rev,
        new_rev=latest_sha,
        push_token=push_token,
        target_branch=target_branch,
        forge=forge,
        prs_created=prs_created,
    )


def _apply_one_bump(
    *,
    repo_id: str,
    rc: RepoConfig,
    dep_name: str,
    old_rev: str,
    new_rev: str,
    push_token: str,
    target_branch: str,
    forge: Forge,
    prs_created: list[str],
) -> None:
    """Clone *rc*, update the pin rev, regenerate uv.lock, push, and open a PR."""
    branch_name = f"mill/pin-bump/{dep_name}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        try:
            git_ops.clone(
                rc.forge_remote_url or "",
                tmp_path,
                target_branch,
                push_token,
            )
        except Exception as exc:
            log.warning(
                "pin_bump: clone failed for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return

        toml_path = tmp_path / "pyproject.toml"
        if not toml_path.is_file():
            log.warning(
                "pin_bump: no pyproject.toml in %s clone — skipping",
                repo_id,
            )
            return

        original_text = toml_path.read_text(encoding="utf-8")
        try:
            updated_text = _update_pin_rev(original_text, dep_name, new_rev)
        except ValueError as exc:
            log.warning(
                "pin_bump: cannot update rev for %s → %s — skipping (%s)",
                repo_id,
                dep_name,
                exc,
            )
            return

        toml_path.write_text(updated_text, encoding="utf-8")

        # Regenerate uv.lock (best-effort; warn-and-proceed on failure).
        try:
            result = subprocess.run(
                ["uv", "lock"],  # noqa: S607
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except Exception as exc:
            log.warning(
                "pin_bump: uv lock failed for %s — "
                "proceeding with existing uv.lock (%s)",
                repo_id,
                exc,
            )
        else:
            if result.returncode != 0:
                log.warning(
                    "pin_bump: uv lock failed for %s (exit %s) — "
                    "proceeding with existing uv.lock: %s",
                    repo_id,
                    result.returncode,
                    (result.stderr or "")[:500],
                )

        # Create branch, commit, push.
        try:
            git_ops.create_branch(tmp_path, branch_name)
            git_ops.commit_all(
                tmp_path,
                f"chore(deps): bump {dep_name} pin to {new_rev[:7]}",
            )
        except Exception as exc:
            log.warning(
                "pin_bump: commit failed for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return

        try:
            git_ops.push(
                tmp_path,
                branch_name,
                rc.forge_remote_url or "",
                push_token,
            )
        except Exception as exc:
            log.warning(
                "pin_bump: push failed for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return

        try:
            pr_url = forge.open_merge_request(
                source_branch=branch_name,
                title=f"chore(deps): bump {dep_name} pin to {new_rev[:7]}",
                body=(
                    f"Bump `{dep_name}` pin from `{old_rev[:7]}` "
                    f"to `{new_rev[:7]}`.\n\n"
                    f"*Detected by the pin-bump periodic pass.*"
                ),
            )
        except Exception as exc:
            log.warning(
                "pin_bump: PR creation failed for %s → %s — "
                "branch %s was pushed but no PR opened (%s)",
                repo_id,
                dep_name,
                branch_name,
                exc,
            )
            return

        prs_created.append(
            f"{repo_id} → {dep_name} ({old_rev[:7]} → {new_rev[:7]}) {pr_url}"
        )
