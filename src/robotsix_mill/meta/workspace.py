"""Build a multi-repo workspace for a meta-board ticket.

A meta ticket proposes cross-repo work, so its workspace holds a fresh
clone of each required repo (chosen by the triage agent — see
:mod:`robotsix_mill.meta.triage`). The first clone is the primary
``repo_dir``; all clones are passed as ``extra_roots`` so the refine /
implement agents (via ``explore`` + ``fs_tools``) can read and edit across
every required repository.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Settings, get_repos_config
from ..forge.auth import github_token
from ..vcs import git_ops

if TYPE_CHECKING:
    from ..core.models import Ticket
    from ..stages.base import Outcome, StageContext

log = logging.getLogger("robotsix_mill.meta.workspace")


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


def build_triaged_meta_workspace(
    ctx: "StageContext",
    ticket: "Ticket",
    ws,
    spec: str,
    *,
    author: str,
) -> tuple[Path | None, list[Path] | None, "Outcome | None"]:
    """Build the multi-repo workspace for a meta-board ticket.

    Runs the repo-triage agent over *spec* to pick the required
    registered repos, clones them fresh into ``ws.dir/repos/<id>``, and
    returns ``(repo_dir, extra_roots, None)``. On failure (triage error
    or no repo cloned) returns ``(None, None, Outcome(BLOCKED))``.

    The ``author`` argument is the comment author label used for the
    BLOCKED comments (e.g. ``"refine"`` or ``"implement"``) so the
    operator can see which stage hit the failure.
    """
    from .triage import required_repos_for
    from ..core.states import State
    from ..stages.base import Outcome

    try:
        repo_ids = required_repos_for(settings=ctx.settings, spec=spec)
    except Exception:
        log.warning("%s: meta repo-triage failed", ticket.id, exc_info=True)
        ctx.service.add_comment(
            ticket.id,
            "[BLOCKED] meta repo-triage failed — could not determine which "
            "repositories this cross-repo proposal requires.",
            author=author,
        )
        return None, None, Outcome(State.BLOCKED, "meta repo-triage failed")

    repo_dir, extra_roots = build_meta_workspace(ctx.settings, ws, repo_ids)
    if repo_dir is None:
        ctx.service.add_comment(
            ticket.id,
            "[BLOCKED] meta workspace: none of the required repos "
            f"({', '.join(repo_ids) or 'none'}) could be cloned.",
            author=author,
        )
        return (
            None,
            None,
            Outcome(State.BLOCKED, "meta workspace: no repos could be cloned"),
        )
    log.info(
        "%s: meta workspace built — %d repo(s): %s",
        ticket.id,
        len(extra_roots),
        ", ".join(p.name for p in extra_roots),
    )
    return repo_dir, extra_roots, None
