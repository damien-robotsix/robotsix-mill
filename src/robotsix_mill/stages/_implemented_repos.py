"""Resolve the implemented repo clone(s) for post-implement stages.

The review and document stages run after implement and need the
clone(s) it produced. Two workspace layouts exist:

* **Single-repo** (the common case): implement clones the one target
  repo into ``ws.dir/"repo"`` and leaves it checked out on the ticket
  branch. No manifest is written.
* **Meta multi-repo** (the d776 epic): implement clones each target
  repo into ``ws.dir/"repos"/<repo_id>`` and records a
  ``touched_repos.json`` manifest under ``artifacts/`` — the same
  artifact the deliver stage consumes.

Deliver already branches on the manifest; review and document
historically only knew the single-repo path and so hard-BLOCKED every
meta multi-repo ticket with "no repository clone (re-run implement)"
even though the clones were present under ``repos/``. This helper hides
the difference so each stage gets a uniform list of implemented repos.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..core.models import Ticket
from ..forge.auth import _resolve_remote_url, github_token
from ..vcs import git_ops


@dataclass(frozen=True)
class ImplementedRepo:
    """One repository clone produced by implement.

    ``repo_id`` is the empty string for the legacy single-repo layout
    (the caller falls back to ``ctx.repo_config`` for forge auth).
    """

    repo_id: str
    branch: str
    repo_dir: Path


def implemented_repos(ws, settings: Settings, ticket: Ticket) -> list[ImplementedRepo]:
    """Return the implemented clones for *ticket* across both layouts.

    Reconstructs each multi-repo path from ``ws.dir`` rather than the
    ``repo_path`` recorded in the manifest: the manifest stores the
    container-internal path (``/data/...``) which need not match the
    path this process sees. Only clones that actually exist on disk
    (``.git`` present) are returned; an empty list means implement left
    nothing reviewable and the caller should BLOCK.
    """
    artifacts = ws.artifacts_dir
    manifest = artifacts / "touched_repos.json"

    if manifest.exists():
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError, ValueError:
            entries = []
        out: list[ImplementedRepo] = []
        for entry in entries:
            repo_id = entry.get("repo_id", "")
            branch = entry.get("branch", "")
            repo_dir = ws.dir / "repos" / repo_id
            if (repo_dir / ".git").exists():
                out.append(ImplementedRepo(repo_id, branch, repo_dir))
        if out:
            return out
        # Manifest present but NONE of its clones exist on disk. This happens
        # when a ticket was a meta multi-repo ticket (which wrote the manifest)
        # and was later retargeted to a single-repo board: implement re-cloned
        # into ws.dir/"repo", but the stale manifest still points at the gone
        # ws.dir/"repos"/<id> paths. On-disk reality wins — fall through to the
        # single-repo layout rather than spuriously BLOCK with "no repository
        # clone to review".

    # Single-repo / legacy layout: one clone at ws.dir/"repo".
    repo_dir = ws.dir / "repo"
    if (repo_dir / ".git").exists():
        branch = ticket.branch or f"{settings.branch_prefix}{ticket.id}"
        return [ImplementedRepo("", branch, repo_dir)]
    return []


def combined_diff(
    settings: Settings,
    repo_config,
    repos: list[ImplementedRepo],
    target_branch: str,
) -> str:
    """Return the union diff across every implemented clone.

    Each repo is fetched through a freshly-minted token for its own
    forge (resolved per ``repo_id``; the legacy single-repo entry falls
    back to *repo_config*, the stage's ``ctx.repo_config``). The minted
    token matters because the GitHub App installation token baked into a
    clone's ``origin`` URL expires ~1h after clone, so a stale clone's
    later fetch would 401 with exit 128. When more than one repo was
    touched, each repo's diff is prefixed with a
    ``# ===== repo: <id> =====`` header so the reviewer / doc agent can
    attribute hunks to the right repository.
    """
    from ..config import get_repo_config
    from ..config import ConfigError

    multi = len(repos) > 1
    parts: list[str] = []
    for r in repos:
        rc = repo_config
        if r.repo_id:
            try:
                rc = get_repo_config(r.repo_id)
            except ConfigError:
                rc = repo_config
        remote_url = _resolve_remote_url(settings, rc)
        try:
            token = github_token(settings, repo_config=rc)
        except RuntimeError:
            token = None
        d = git_ops.diff_base(
            r.repo_dir,
            target_branch,
            remote_url=remote_url,
            token=token,
        )
        if not d.strip():
            continue
        parts.append(f"# ===== repo: {r.repo_id} =====\n{d}" if multi else d)
    return "\n\n".join(parts)
