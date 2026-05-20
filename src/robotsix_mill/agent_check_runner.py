"""Agent-check runner — orchestrates a single agent-check pass.

Mirrors the audit runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_agent_check_agent`` from agents.agent_check.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from .config import Settings
from .core.service import TicketService
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.agent_check")


@dataclass
class AgentCheckPassResult:
    """Result of running an agent-check pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this run


def run_agent_check_pass(root: str | None = None) -> AgentCheckPassResult:
    """Execute one full agent-check pass.

    Reads the memory ledger, invokes the agent-check agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    gaps.

    Args:
        root: repository root (unused directly — the agent uses
              forge_remote_url for repo context; kept for API
              compatibility).

    Returns:
        AgentCheckPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.agent_check_memory_file

    # Import here to allow monkeypatching in tests.
    from .agents import agent_check
    from .runtime import tracing
    from .vcs import git_ops

    # Clone the repo locally so the agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if settings.forge_remote_url:
        import subprocess

        cand = settings.data_dir / "agent_check_workspace" / "repo"
        if (cand / ".git").exists():
            repo_dir = cand
        else:
            try:
                git_ops.clone(
                    settings.forge_remote_url, cand,
                    settings.forge_target_branch, settings.forge_token,
                )
                repo_dir = cand
            except subprocess.CalledProcessError as e:
                log.warning(
                    "agent_check clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    # One Langfuse session per agent-check run, so its model calls are
    # attributed (no untagged traces). No-op if tracing isn't ready.
    session_id = (
        f"agent-check-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    log.info("agent-check pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id), \
                tracing.trace_stage("agent-check"):
            agent_fn = partial(
                agent_check.run_agent_check_agent, repo_dir=repo_dir
            )
            result = run_agent_pass(
                agent_fn=agent_fn,
                memory_file=memory_file,
                source_label="agent_check",
                service=service,
                settings=settings,
                origin_session=session_id,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("agent-check agent failed")
        raise RuntimeError(f"agent-check agent failed: {e}") from e

    return AgentCheckPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
