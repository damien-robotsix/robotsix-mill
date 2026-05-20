"""Audit runner — orchestrates a single audit pass.

Mirrors the retrospect stage pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_audit_agent`` from agents.auditing.
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

log = logging.getLogger("robotsix_mill.audit")


@dataclass
class AuditPassResult:
    """Result of running an audit pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this audit run


def run_audit_pass(root: str | None = None) -> AuditPassResult:
    """Execute one full audit pass.

    Reads the memory ledger, invokes the audit agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    gaps.

    Args:
        root: repository root (unused directly — the agent uses
              forge_remote_url for repo context; kept for API
              compatibility with the spec).

    Returns:
        AuditPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.audit_memory_file

    # Import here to allow monkeypatching in tests.
    from .agents import auditing
    from .runtime import tracing
    from .vcs import git_ops

    # Clone the repo locally so the audit agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if settings.forge_remote_url:
        import subprocess

        cand = settings.data_dir / "audit_workspace" / "repo"
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
                    "audit clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    # One Langfuse session per audit run, so its model calls are
    # attributed (no untagged traces). No-op if tracing isn't ready.
    session_id = (
        f"audit-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    log.info("audit pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id), \
                tracing.trace_stage("audit"):
            agent_fn = partial(auditing.run_audit_agent, repo_dir=repo_dir)
            result = run_agent_pass(
                agent_fn=agent_fn,
                memory_file=memory_file,
                source_label="audit",
                service=service,
                settings=settings,
                origin_session=session_id,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("audit agent failed")
        raise RuntimeError(f"audit agent failed: {e}") from e

    return AuditPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
