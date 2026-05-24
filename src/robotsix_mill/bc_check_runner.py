"""BC-check runner — orchestrates a single backward-compatibility
inspection pass.

Mirrors the agent-check runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_bc_check_agent`` from agents.bc_check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from .config import Settings, get_secrets
from .core.models import SourceKind
from .core.service import TicketService
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.bc_check")


@dataclass
class BcCheckPassResult:
    """Result of running a bc-check pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this run


def run_bc_check_pass(root: str | None = None) -> BcCheckPassResult:
    """Execute one full backward-compatibility inspection pass.

    Reads the memory ledger, invokes the bc-check agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    backward-compat code that is ripe for removal.

    Args:
        root: repository root (unused directly — the agent uses
              forge_remote_url for repo context; kept for API
              compatibility).

    Returns:
        BcCheckPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.bc_check_memory_file

    # Import here to allow monkeypatching in tests.
    from .agents import bc_check
    from .runtime import tracing
    from .vcs import git_ops

    # Clone the repo locally so the agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files. Idempotent; best-effort.
    repo_dir = None
    if settings.forge_remote_url:
        import subprocess

        cand = settings.data_dir / "bc_check_workspace" / "repo"
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
                    "bc_check clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    # One Langfuse session per bc-check run.
    from .runtime.tracing import make_session_id

    session_id = make_session_id("bc-check")
    log.info("bc-check pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id, "bc-check"):
            agent_fn = partial(
                bc_check.run_bc_check_agent, repo_dir=repo_dir
            )
            result = run_agent_pass(
                agent_fn=agent_fn,
                memory_file=memory_file,
                source_label=SourceKind.BC_CHECK,
                service=service,
                settings=settings,
                origin_session=session_id,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("bc-check agent failed")
        raise RuntimeError(f"bc-check agent failed: {e}") from e

    return BcCheckPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
