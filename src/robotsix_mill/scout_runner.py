"""Scout runner — orchestrates a single scout pass.

Mirrors the audit runner pattern: read memory, invoke scout agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_scout_agent`` from agents.scouting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .core.service import TicketService
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.scout")


@dataclass
class ScoutPassResult:
    """Result of running a scout pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)


def run_scout_pass(root: str | None = None) -> ScoutPassResult:
    """Execute one full scout pass.

    Reads the memory ledger, invokes the scout agent, writes the
    returned memory verbatim, and creates draft tickets for model
    improvement proposals.

    Args:
        root: repository root (unused directly; kept for API
              compatibility).

    Returns:
        ScoutPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.scout_memory_file

    # Import here to allow monkeypatching in tests.
    from .agents import scouting
    from .runtime.tracing import current_session

    try:
        result = run_agent_pass(
            agent_fn=scouting.run_scout_agent,
            memory_file=memory_file,
            source_label="scout",
            service=service,
            settings=settings,
            origin_session=current_session(),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("scout agent failed")
        raise RuntimeError(f"scout agent failed: {e}") from e

    return ScoutPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
    )
