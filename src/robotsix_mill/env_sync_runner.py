"""Env-sync runner — orchestrates a single env-sync pass.

Mirrors the test-gap runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_env_sync_agent`` from agents.env_syncing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings, get_secrets
from .core.service import TicketService

log = logging.getLogger("robotsix_mill.env_sync")


@dataclass
class EnvSyncPassResult:
    """Result of running an env-sync pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this env-sync run


def run_env_sync_pass(root: str | None = None) -> EnvSyncPassResult:
    """Execute one full env-sync pass.

    Reads the memory ledger, invokes the env-sync agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    drift gaps.

    Args:
        root: repository root (unused directly — the agent uses
              forge_remote_url for repo context; kept for API
              compatibility).

    Returns:
        EnvSyncPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.env_sync_memory_file

    # Import here to allow monkeypatching in tests.
    from .agents import env_syncing
    from .runtime import tracing
    from .vcs import git_ops

    # Clone the repo locally so the env-sync agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if settings.forge_remote_url:
        import subprocess

        cand = settings.data_dir / "env_sync_workspace" / "repo"
        if (cand / ".git").exists():
            repo_dir = cand
        else:
            try:
                git_ops.clone(
                    settings.forge_remote_url, cand,
                    settings.forge_target_branch, get_secrets().forge_token,
                )
                repo_dir = cand
            except subprocess.CalledProcessError as e:
                log.warning(
                    "env-sync clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    # One Langfuse session per env-sync run, so its model calls are
    # attributed (no untagged traces). No-op if tracing isn't ready.
    from .runtime.tracing import make_session_id

    session_id = make_session_id("env-sync")
    log.info("env-sync pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id, "env-sync"):
            from functools import partial
            from .pass_runner import run_agent_pass

            agent_fn = partial(env_syncing.run_env_sync_agent, repo_dir=repo_dir)
            result = run_agent_pass(
                agent_fn=agent_fn,
                memory_file=memory_file,
                source_label="env_sync",
                service=service,
                settings=settings,
                origin_session=session_id,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("env-sync agent failed")
        raise RuntimeError(f"env-sync agent failed: {e}") from e

    return EnvSyncPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
