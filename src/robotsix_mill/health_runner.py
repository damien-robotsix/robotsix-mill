"""Health runner — orchestrates a single health pass.

Mirrors the audit runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_health_agent`` from agents.health.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .core.service import TicketService
from .core.states import State

log = logging.getLogger("robotsix_mill.health")


@dataclass
class HealthPassResult:
    """Result of running a health pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this health run


def run_health_pass(root: str | None = None) -> HealthPassResult:
    """Execute one full health pass.

    Reads the memory ledger, invokes the health agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    gaps.

    Args:
        root: repository root (unused directly — the agent uses
              forge_remote_url for repo context; kept for API
              compatibility).

    Returns:
        HealthPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.health_memory_file

    # Read current memory — empty string if missing/unreadable.
    memory_text = ""
    try:
        if memory_file.exists():
            memory_text = memory_file.read_text(encoding="utf-8")
    except OSError:
        log.warning("could not read memory file %s", memory_file)

    # Import here to allow monkeypatching in tests.
    from .agents import health
    from .runtime import tracing
    from .vcs import git_ops

    # Clone the repo locally so the health agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if settings.forge_remote_url:
        import subprocess

        cand = settings.data_dir / "health_workspace" / "repo"
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
                    "health clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    # One Langfuse session per health run, so its model calls are
    # attributed (no untagged traces). No-op if tracing isn't ready.
    session_id = (
        f"health-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    log.info("health pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id), \
                tracing.trace_stage("health"):
            res = health.run_health_agent(
                settings=settings,
                memory=memory_text,
                repo_dir=repo_dir,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("health agent failed")
        raise RuntimeError(f"health agent failed: {e}") from e

    # Persist the agent's updated memory verbatim.
    if res.updated_memory:
        try:
            memory_file.parent.mkdir(parents=True, exist_ok=True)
            memory_file.write_text(res.updated_memory, encoding="utf-8")
        except OSError:
            log.warning("could not write memory file %s", memory_file)

    # Create draft tickets for each proposed gap.
    created = []
    for i in range(min(len(res.draft_titles), len(res.draft_bodies))):
        title = res.draft_titles[i]
        body = res.draft_bodies[i]
        if not title or not body:
            continue
        try:
            ticket = service.create(title, body, source="health")
            # ticket is already in DRAFT state after create()
            created.append({"id": ticket.id, "title": ticket.title})
            log.info("health spawned draft %s: %s", ticket.id, title)
        except Exception:
            log.exception("failed to create draft ticket: %s", title)

    return HealthPassResult(
        updated_memory=res.updated_memory or memory_text,
        drafts_created=created,
        session_id=session_id,
    )
