"""File-operations mixin: clone/branch, repo-change and gitignore checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ...config import ConfigError, Settings, get_repo_config, target_branch_for
from ...core.states import State
from ...forge.auth import _resolve_remote_url, github_token
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome
from ._base import _ImplementStageBase
from ._shared import (
    log,
)


class FileOperationsMixin(_ImplementStageBase):
    """Clone/branch and repo-change inspection for :class:`ImplementStage`."""

    @classmethod
    def _any_repo_has_changes(
        cls,
        repo_dir: Path,
        extra_roots: list[Path] | None,
        target_branch: str = "main",
        settings: Settings | None = None,
    ) -> bool:
        """Return True if any repo has uncommitted changes or is ahead of main.

        Used by the two exit-path guards so multi-repo tickets don't
        misroute to DONE/BLOCKED when only the primary repo was checked.

        When *settings* is provided, each ``extra_roots`` repo resolves
        its own target branch via :func:`target_branch_for` —
        ``target_branch`` is used only for the primary repo (or as a
        fallback when *settings* is ``None``, keeping the single-repo
        callers working).
        """
        if git_ops.has_changes(repo_dir) or git_ops.branch_is_ahead_of_main(
            repo_dir, target_branch
        ):
            return True
        if extra_roots:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                repo_target = target_branch
                if settings is not None:
                    try:
                        rc = get_repo_config(repo_path.name)
                    except ConfigError:
                        rc = None
                    repo_target = target_branch_for(settings, rc)
                if git_ops.has_changes(repo_path) or git_ops.branch_is_ahead_of_main(
                    repo_path, repo_target
                ):
                    return True
        return False

    @classmethod
    def _claimed_gitignored_edits(
        cls, repo_dir: Path, new_messages: bytes | str | None
    ) -> list[str]:
        """Repo-relative paths this run's edit tool-calls targeted that exist
        on disk but are gitignored (so the diff stays empty).

        Normalizes Claude-SDK absolute paths to repo-relative; paths outside
        the clone are skipped (a different failure mode with its own guard).
        Fail-open: errors yield ``[]`` — this only ENRICHES the blocked note,
        it never decides the outcome.
        """
        try:
            raw_paths = short_circuit_verify.run_claimed_edited_rawpaths(new_messages)
            rels: list[str] = []
            seen: set[str] = set()
            for raw in raw_paths:
                p = Path(raw)
                if p.is_absolute():
                    try:
                        p = p.relative_to(repo_dir)
                    except ValueError:
                        continue  # outside the clone
                rel = str(p)
                # Dedupe AFTER normalization — the same file can be claimed
                # both repo-relative (mill tools) and absolute (Claude SDK).
                if rel not in seen:
                    seen.add(rel)
                    rels.append(rel)
            return git_ops.ignored_existing_paths(repo_dir, rels)
        except Exception:  # noqa: BLE001 — diagnostic enrichment only
            log.warning(
                "gitignored-edit detection failed; emitting plain note",
                exc_info=True,
            )
            return []

    @classmethod
    def _clone_and_branch(cls, ctx, ticket, settings):
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = f"{settings.branch_prefix}{ticket.id}"
        remote_url = _resolve_remote_url(settings, ctx.repo_config)
        target = target_branch_for(settings, ctx.repo_config)

        # Resume iff a prior run left this ticket's clone + branch behind.
        resuming = (repo_dir / ".git").exists() and git_ops.branch_exists(
            repo_dir, branch
        )
        if resuming:
            git_ops.checkout(repo_dir, branch)
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            try:
                try:
                    token = github_token(settings, repo_config=ctx.repo_config)
                except RuntimeError:
                    token = None
                git_ops.clone(
                    remote_url,
                    repo_dir,
                    target,
                    token,
                )
            except subprocess.CalledProcessError as e:
                from ...runtime.transient_errors import reraise_if_transient

                reraise_if_transient(e)
                return Outcome(
                    State.BLOCKED,
                    "clone failed: " + git_ops.redact_credentials(e.stderr or "")[:300],
                )
            git_ops.create_branch(repo_dir, branch)

        # Refresh against current origin/<target> so the agent never
        # edits stale source — a branch based on even slightly outdated
        # origin/<target> can silently revert newer commits.
        # Pass a freshly minted token so try_rebase_onto's fetch
        # doesn't fall back to origin's stored (and likely expired)
        # GitHub App token — see git_ops.try_rebase_onto for the full
        # rationale. Token resolution can raise when the forge is
        # unconfigured (tests, file:// remotes); fall back to no token
        # and let try_rebase_onto use origin as-is.
        try:
            _rebase_token = github_token(
                settings,
                repo_config=ctx.repo_config,
            )
        except Exception:
            _rebase_token = None
        if not git_ops.try_rebase_onto(
            repo_dir,
            target,
            remote_url=_resolve_remote_url(settings, ctx.repo_config),
            token=_rebase_token,
        ):
            return Outcome(
                State.REBASING,
                f"rebase onto origin/{target} failed — handing to rebase agent",
            )

        # Hard invariant: NEVER run the agent / sandbox without a
        # materialized clone.
        if not (repo_dir / ".git").exists():
            log.warning(
                "%s: clone missing before agent run — re-cloning",
                ticket.id,
            )
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            try:
                try:
                    token = github_token(settings, repo_config=ctx.repo_config)
                except RuntimeError:
                    token = None
                git_ops.clone(
                    remote_url,
                    repo_dir,
                    target,
                    token,
                )
                git_ops.create_branch(repo_dir, branch)
            except subprocess.CalledProcessError as e:
                from ...runtime.transient_errors import reraise_if_transient

                reraise_if_transient(e)
                return Outcome(
                    State.BLOCKED,
                    "repo clone missing and re-clone failed — resumable: "
                    + git_ops.redact_credentials(e.stderr or "")[:200],
                )
        ctx.service.set_branch(ticket.id, branch)
        return (repo_dir, branch, resuming)
