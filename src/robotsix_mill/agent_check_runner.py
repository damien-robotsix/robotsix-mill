"""Agent-check runner — orchestrates a single agent-check pass.

Mirrors the audit runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_agent_check_agent`` from agents.agent_check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from .config import RepoConfig, Settings, get_secrets
from .core.models import SourceKind
from .core.service import TicketService
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.agent_check")


@dataclass
class AgentCheckPassResult:
    """Result of running an agent-check pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this run


def run_agent_check_pass(session_id: str, repo_config: RepoConfig | None = None) -> AgentCheckPassResult:
    """Execute one full agent-check pass.

    Reads the memory ledger, invokes the agent-check agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    gaps.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Optional per-repo configuration for multi-repo
            serve. When provided, ticket creation and memory files
            are scoped to this repo.

    Returns:
        AgentCheckPassResult with updated memory and created draft info.
    """
    settings = Settings()
    memory_file = settings.agent_check_memory_file
    clone_dir: Path | None = None
    forge_remote_url = settings.forge_remote_url

    if repo_config is not None:
        service = TicketService(settings, board_id=repo_config.board_id)
        repo_data_dir = settings.data_dir / repo_config.repo_id
        repo_data_dir.mkdir(parents=True, exist_ok=True)
        memory_file = repo_data_dir / "agent_check_memory.md"
        if repo_config.forge_remote_url:
            forge_remote_url = repo_config.forge_remote_url
            clone_dir = repo_data_dir / "agent_check_workspace" / "repo"
    else:
        service = TicketService(settings)

    # Import here to allow monkeypatching in tests.
    from .agents import agent_check
    from .vcs import git_ops

    # Clone the repo locally so the agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if forge_remote_url:
        import subprocess

        cand = clone_dir or (settings.data_dir / "agent_check_workspace" / "repo")
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
                    "agent_check clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    log.info("agent-check pass starting (session %s)", session_id)
    agent_fn = partial(
        agent_check.run_agent_check_agent,
        repo_dir=repo_dir,
        memory_dir=settings.data_dir,
    )
    result = run_agent_pass(
        agent_fn=agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AGENT_CHECK,
        service=service,
        settings=settings,
        origin_session=session_id,
    )

    return AgentCheckPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
