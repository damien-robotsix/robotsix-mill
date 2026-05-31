"""Build a multi-repo workspace for a meta-board ticket.

A meta ticket proposes cross-repo work, so its workspace holds a fresh
clone of each required repo (chosen by the triage agent — see
:mod:`robotsix_mill.agents.meta_triage`). The first clone is the primary
``repo_dir``; all clones are passed as ``extra_roots`` so the refine /
implement agents (via ``explore`` + ``fs_tools``) can read and edit across
every required repository.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import Settings, get_repos_config
from .forge.auth import github_token
from .vcs import git_ops

log = logging.getLogger("robotsix_mill.meta_workspace")


def build_meta_workspace(
    settings: Settings, ws, repo_ids: list[str]
) -> tuple[Path | None, list[Path]]:
    """Clone each repo in *repo_ids* fresh under ``ws.dir / "repos"``.

    Mirrors the fresh-clone policy of ``vcs.clone_all_repos`` (wipe any
    prior clone so the agent never analyses a stale tree). Best-effort: a
    repo whose clone fails is skipped with a warning.

    Returns ``(repo_dir, extra_roots)`` where ``repo_dir`` is the first
    successful clone (or ``None`` if none succeeded) and ``extra_roots`` is
    the list of all successful clone paths.
    """
    repos_config = get_repos_config()
    clones: list[Path] = []

    for repo_id in repo_ids:
        rc = repos_config.repos.get(repo_id)
        if rc is None or not rc.forge_remote_url:
            log.warning(
                "build_meta_workspace: %r is not a clonable registered repo — skipping",
                repo_id,
            )
            continue

        dest = ws.dir / "repos" / repo_id
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)

        try:
            token = github_token(settings, repo_config=rc)
        except RuntimeError:
            token = None  # no creds — clone may still work for public repos

        try:
            git_ops.clone(
                rc.forge_remote_url,
                dest,
                settings.forge_target_branch,
                token,
            )
            clones.append(dest)
        except subprocess.CalledProcessError as e:
            log.warning(
                "build_meta_workspace: clone failed for %r: %s",
                repo_id,
                (e.stderr or "")[:200],
            )

    repo_dir = clones[0] if clones else None
    return repo_dir, clones
