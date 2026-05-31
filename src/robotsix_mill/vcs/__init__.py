"""Local version control helpers."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..config import get_repos_config
from ..forge.auth import github_token

from . import git_ops

log = logging.getLogger("robotsix_mill.vcs")


def clone_all_repos(settings) -> dict[str, Path]:
    """Clone every registered repo that has a ``forge_remote_url``.

    Best-effort: a clone failure for one repo is logged as a warning
    and the remaining repos are still processed.  Each call wipes any
    existing workspace and clones FRESH, so callers (periodic agents)
    always get the current ``origin`` tip — never a stale reused tree.

    Returns a ``dict`` mapping ``repo_id`` → clone destination path.
    """
    repos_config = get_repos_config()
    result: dict[str, Path] = {}

    for repo_id, repo_config in repos_config.repos.items():
        if not repo_config.forge_remote_url:
            continue

        token = None
        try:
            token = github_token(settings, repo_config=repo_config)
        except RuntimeError:
            pass  # token is None — clone will fail and be caught below

        dest = settings.data_dir / "meta" / "workspace" / repo_id / "repo"

        # Each run starts from a CLEAN, fresh clone: wipe any prior workspace
        # so periodic agents (meta, module-curator) never analyse a STALE tree
        # (a reused clone keeps whatever commit it was last left at).
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)

        try:
            git_ops.clone(
                repo_config.forge_remote_url,
                dest,
                settings.forge_target_branch,
                token,
            )
            result[repo_id] = dest
        except subprocess.CalledProcessError as e:
            log.warning(
                "clone_all_repos: clone failed for repo %r: %s",
                repo_id,
                (e.stderr or "")[:200],
            )

    return result
