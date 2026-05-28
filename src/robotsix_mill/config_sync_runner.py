"""Config-sync runner — orchestrates a single config-sync pass.

Mirrors the test-gap runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_config_sync_agent`` from agents.config_syncing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import RepoConfig, Settings, get_secrets
from .core.models import SourceKind
from .core.service import TicketService

log = logging.getLogger("robotsix_mill.config_sync")


@dataclass
class ConfigSyncPassResult:
    """Result of running a config-sync pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this config-sync run


def run_config_sync_pass(session_id: str, repo_config: RepoConfig | None = None) -> ConfigSyncPassResult:
    """Execute one full config-sync pass.

    Reads the memory ledger, invokes the config-sync agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    drift gaps.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Optional per-repo configuration for multi-repo
            serve. When provided, ticket creation and memory files
            are scoped to this repo.

    Returns:
        ConfigSyncPassResult with updated memory and created draft info.
    """
    settings = Settings()
    memory_file = settings.memory_file_for('config_sync', repo_config.board_id if repo_config else '')
    clone_dir: Path | None = None
    forge_remote_url = settings.forge_remote_url

    if repo_config is not None:
        service = TicketService(settings, board_id=repo_config.board_id)
        repo_data_dir = settings.data_dir / repo_config.repo_id
        repo_data_dir.mkdir(parents=True, exist_ok=True)
        memory_file = repo_data_dir / "config_sync_memory.md"
        if repo_config.forge_remote_url:
            forge_remote_url = repo_config.forge_remote_url
            clone_dir = repo_data_dir / "config_sync_workspace" / "repo"
    else:
        service = TicketService(settings)

    # Import here to allow monkeypatching in tests.
    from .agents import config_syncing
    from .vcs import git_ops

    # Clone the repo locally so the config-sync agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if forge_remote_url:
        import subprocess

        cand = clone_dir or (settings.data_dir / "config_sync_workspace" / "repo")
        if (cand / ".git").exists():
            repo_dir = cand
        else:
            try:
                git_ops.clone(
                    forge_remote_url, cand,
                    settings.forge_target_branch, get_secrets().forge_token,
                )
                repo_dir = cand
            except subprocess.CalledProcessError as e:
                log.warning(
                    "config-sync clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    log.info("config-sync pass starting (session %s)", session_id)
    from functools import partial
    from .pass_runner import run_agent_pass

    agent_fn = partial(config_syncing.run_config_sync_agent, repo_dir=repo_dir)
    result = run_agent_pass(
        agent_fn=agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.CONFIG_SYNC,
        service=service,
        settings=settings,
        origin_session=session_id,
    )

    return ConfigSyncPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
