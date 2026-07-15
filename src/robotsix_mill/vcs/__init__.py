"""Local version control helpers."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import get_repos_config, target_branch_for
from ..forge.auth import github_token

from . import git_ops

log = logging.getLogger("robotsix_mill.vcs")


def _remote_has_branches(remote_url: str, token: str | None) -> bool:
    """Return ``True`` if *remote_url* has at least one branch (``git
    ls-remote --heads`` returns non-empty output).

    A ``CalledProcessError`` (network error, permission denied, …) is
    treated as "unknown" and returns ``True`` — the caller should not
    attempt a bootstrap when it cannot verify emptiness.
    """
    try:
        result = subprocess.run(  # noqa: S603 — remote_url from repo config, token from env
            [  # noqa: S607 — git is on PATH
                "git",
                "ls-remote",
                "--quiet",
                "--heads",
                git_ops._authed_url(remote_url, token),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        # Can't determine — assume non-empty to avoid dangerous bootstrap.
        return True
    return bool(result.stdout.strip())


def _bootstrap_empty_repo(
    remote_url: str,
    dest: Path,
    branch: str,
    token: str | None,
    repo_id: str,
) -> None:
    """Bootstrap an empty remote repo by pushing an initial commit.

    Creates a temporary local repo with a minimal README, force-pushes
    it to *remote_url*, and then moves the repo to *dest* so it behaves
    like a fresh clone.  Raises :class:`subprocess.CalledProcessError`
    (or :class:`OSError`) on failure — callers must catch and log.
    """
    tmp = Path(tempfile.mkdtemp(prefix="bootstrap_"))
    try:
        git_ops.init_repo(tmp, branch)
        (tmp / "README.md").write_text(
            f"# {repo_id}\n\nMill-managed repository — bootstrapped automatically.\n",
            encoding="utf-8",
        )
        git_ops.commit_all(tmp, "Initial bootstrap commit")
        git_ops.push(tmp, branch, remote_url, token)

        # Ensure destination is clean (clone may have left partial state).
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


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
                target_branch_for(settings, repo_config),
                token,
            )
            result[repo_id] = dest
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            if "Remote branch" in stderr and "not found" in stderr:
                # The requested branch doesn't exist on the remote.
                # Only bootstrap if the remote is *truly empty* (no
                # branches at all).  Otherwise the repo already has a
                # different default branch (e.g. "lyrical" instead of
                # "main") and we must not silently overwrite it.
                if not _remote_has_branches(repo_config.forge_remote_url, token):
                    log.info(
                        "clone_all_repos: remote has no branches for %r "
                        "— attempting bootstrap",
                        repo_id,
                    )
                    try:
                        _bootstrap_empty_repo(
                            repo_config.forge_remote_url,
                            dest,
                            target_branch_for(settings, repo_config),
                            token,
                            repo_id,
                        )
                        result[repo_id] = dest
                        log.info(
                            "clone_all_repos: bootstrapped empty repo %r",
                            repo_id,
                        )
                    except Exception as bootstrap_err:
                        log.error(
                            "clone_all_repos: failed to bootstrap empty repo %r: %s",
                            repo_id,
                            bootstrap_err,
                        )
                else:
                    log.warning(
                        "clone_all_repos: remote branch not found for %r "
                        "(remote has branches, but not the target one)",
                        repo_id,
                    )
            else:
                log.warning(
                    "clone_all_repos: clone failed for repo %r: %s",
                    repo_id,
                    stderr[:200],
                )

    return result
