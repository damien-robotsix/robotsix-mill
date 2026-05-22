"""Health runner — orchestrates a single health pass.

Mirrors the audit runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_health_agent`` from agents.health.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .core.service import TicketService

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
    from .runtime.tracing import make_session_id

    session_id = make_session_id("health")
    log.info("health pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id, "health"):
            from functools import partial
            from .pass_runner import run_agent_pass

            agent_fn = partial(health.run_health_agent, repo_dir=repo_dir)
            result = run_agent_pass(
                agent_fn=agent_fn,
                memory_file=memory_file,
                source_label="health",
                service=service,
                settings=settings,
                origin_session=session_id,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("health agent failed")
        raise RuntimeError(f"health agent failed: {e}") from e

    return HealthPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
