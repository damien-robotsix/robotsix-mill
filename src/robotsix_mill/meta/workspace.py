"""Build a multi-repo workspace for a meta-board ticket.

A meta ticket proposes cross-repo work, so its workspace holds a fresh
clone of each required repo (chosen by the triage agent — see
:mod:`robotsix_mill.meta.triage`). The first clone is the primary
``repo_dir``; all clones are passed as ``extra_roots`` so the refine /
implement agents (via ``explore`` + ``fs_tools``) can read and edit across
every required repository.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Settings, get_repos_config, target_branch_for
from ..forge.auth import github_token
from ..vcs import git_ops

if TYPE_CHECKING:
    from ..core.models import Ticket
    from ..core.workspace import Workspace
    from ..stages.base import Outcome, StageContext

log = logging.getLogger("robotsix_mill.meta.workspace")


def _write_meta_triage(ws: "Workspace", repo_ids: list[str], fallback: bool) -> None:
    """Persist the repo-triage decision to ``artifacts/meta_triage.json``.

    Schema::

        {"repo_ids": [str, ...], "fallback": bool}

    ``fallback`` is ``True`` only when triage could not match a target
    repo and cloned every clonable repo — the deliver-time guard reads
    this to refuse silently misrouting brand-new top-level files. Best
    effort: a write failure is logged, not raised.
    """
    try:
        (ws.artifacts_dir / "meta_triage.json").write_text(
            json.dumps(
                {"repo_ids": list(repo_ids), "fallback": bool(fallback)}, indent=2
            ),
            encoding="utf-8",
        )
    except OSError:
        log.warning("could not write meta_triage.json", exc_info=True)


def build_meta_workspace(
    settings: Settings, ws: "Workspace", repo_ids: list[str]
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
    last_clone_error: subprocess.CalledProcessError | None = None

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
            # No creds resolved — a public repo still clones, but a private
            # one will fail below (and be caught by the partial-clone guard
            # in build_triaged_meta_workspace). Log so it is diagnosable.
            token = None
            log.warning(
                "build_meta_workspace: no credentials for %r — clone will "
                "fail if the repo is private",
                repo_id,
            )

        try:
            git_ops.clone(
                rc.forge_remote_url,
                dest,
                target_branch_for(settings, rc),
                token,
                repo_id=repo_id,
            )
            clones.append(dest)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            if "Remote branch" in stderr and "not found" in stderr:
                # clone() already checked _remote_has_branches — if we
                # get here the remote has branches but not the target
                # one (configuration mismatch, not an empty repo).
                last_clone_error = e
                log.warning(
                    "build_meta_workspace: remote branch not found for %r "
                    "(remote has branches, but not the target one)",
                    repo_id,
                )
            else:
                last_clone_error = e
                log.warning(
                    "build_meta_workspace: clone failed for %r: %s",
                    repo_id,
                    stderr[:200],
                )

    # Every clone failed: when the failure is transient (forge 5xx, DNS
    # outage) raise it so the worker retries / outage-parks instead of
    # the caller hard-blocking on "no repo cloned".
    if not clones and last_clone_error is not None:
        from ..runtime.transient_errors import reraise_if_transient

        reraise_if_transient(last_clone_error)

    repo_dir = clones[0] if clones else None
    return repo_dir, clones


def build_triaged_meta_workspace(
    ctx: "StageContext",
    ticket: "Ticket",
    ws: "Workspace",
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
        fallback = bool(getattr(repo_ids, "fallback", False))
    except Exception as e:
        from ..runtime.transient_errors import reraise_if_transient

        reraise_if_transient(e)
        log.warning("%s: meta repo-triage failed", ticket.id, exc_info=True)
        ctx.service.add_comment(
            ticket.id,
            "[BLOCKED] meta repo-triage failed — could not determine which "
            "repositories this cross-repo proposal requires.",
            author=author,
        )
        return None, None, Outcome(State.BLOCKED, "meta repo-triage failed")

    # Persist the triage decision so the deliver-time guard can tell a
    # confident/all-repos match (proceed) from a "no match → clone
    # everything" fallback (block brand-new top-level files).
    _write_meta_triage(ws, list(repo_ids), fallback)

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

    # Partial clone guard: when triage *confidently* named the required
    # repos (not the clone-everything fallback), every one of them must be
    # present — otherwise the implement agent is handed a workspace missing
    # a target repo and, unable to find/modify it, pauses with a confused
    # "repo not present, cannot clone" question instead of doing the work.
    # Block clearly and actionably rather than proceed with a partial tree.
    if not fallback:
        cloned = {p.name for p in extra_roots}
        missing = [r for r in repo_ids if r not in cloned]
        if missing:
            ctx.service.add_comment(
                ticket.id,
                "[BLOCKED] meta workspace: required repo(s) "
                f"{', '.join(missing)} could not be cloned (private repo "
                "without usable credentials, or a clone error). A meta ticket "
                "must have every target repo checked out so the agent can "
                "modify it — proceeding with a partial workspace would force a "
                "spurious clarifying question. Fix repo access/credentials and "
                "resume.",
                author=author,
            )
            return (
                None,
                None,
                Outcome(
                    State.BLOCKED,
                    f"meta workspace: required repos not cloned: {', '.join(missing)}",
                ),
            )

    log.info(
        "%s: meta workspace built — %d repo(s): %s",
        ticket.id,
        len(extra_roots),
        ", ".join(p.name for p in extra_roots),
    )
    return repo_dir, extra_roots, None
