"""Member-sync runner — deterministic workspace-member discovery pass.

Unlike the LLM-backed periodic passes, member-sync is a **deterministic**
pass: clone the managed repo → :func:`detect_workspace_members` →
:func:`sync_workspace_members`. It therefore needs no model, no memory
ledger, and no agent engine — it reuses only the periodic
scheduling/registration plumbing and this standalone runner.

The :class:`~robotsix_mill.workspace_member_sync.MemberSyncResult` is
reused directly as the pass-result shape (carrying ``added`` / ``updated``
/ ``flagged_for_removal`` / ``filed_tickets`` / ``skipped``).
"""

from __future__ import annotations

import logging

from ..config import RepoConfig, Settings, target_branch_for
from ..workspace_member_sync import MemberSyncResult, sync_workspace_members
from ..workspace_members import detect_workspace_members
from .periodic_runner import _forge_token

log = logging.getLogger("robotsix_mill.member_sync_runner")


def run_member_sync_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> MemberSyncResult:
    """Execute one workspace member-sync pass for *repo_config*.

    Clones the managed repo, detects its vcs2l workspace members, and
    upserts them into ``config/repos.yaml`` (registering new members,
    refreshing existing ones, flagging vanished ones for removal). A repo
    without a ``repos.yaml`` manifest is a silent no-op returning an empty
    :class:`MemberSyncResult`.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Per-repo configuration. Required — the pass must run
            against a registered master repo.

    Returns:
        The :class:`MemberSyncResult` from :func:`sync_workspace_members`,
        or an empty result when the repo has no manifest / the clone failed.
    """
    settings = Settings()
    if repo_config is None:
        raise ValueError(
            "run_member_sync_pass: repo_config is required — configure at "
            "least one repo in config/repos.yaml and pass its RepoConfig in."
        )

    forge_remote_url = repo_config.forge_remote_url or settings.forge_remote_url
    clone_dir = None
    if forge_remote_url:
        import shutil
        import subprocess

        from ..vcs import git_ops

        repo_data_dir = settings.data_dir / repo_config.repo_id
        cand = repo_data_dir / "member_sync_workspace" / "repo"
        # Each run starts from a CLEAN, fresh clone (mirrors run_periodic_pass).
        if cand.exists():
            shutil.rmtree(cand, ignore_errors=True)
        try:
            git_ops.clone(
                forge_remote_url,
                cand,
                target_branch_for(settings, repo_config),
                _forge_token(settings, repo_config),
            )
            clone_dir = cand
        except subprocess.CalledProcessError as e:
            log.warning(
                "member_sync clone failed, skipping: %s",
                (e.stderr or "")[:200],
            )

    members = detect_workspace_members(clone_dir)
    if not members:
        # No vcs2l manifest (the common case) — cheap silent no-op.
        return MemberSyncResult()

    result = sync_workspace_members(
        settings,
        master_repo_id=repo_config.repo_id,
        members=members,
    )
    log.info(
        "member_sync pass for %s: +%d added, %d updated, %d flagged",
        repo_config.repo_id,
        len(result.added),
        len(result.updated),
        len(result.flagged_for_removal),
    )
    return result
