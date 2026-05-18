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
from .core.states import State

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

    # Read current memory — empty string if missing/unreadable.
    memory_text = ""
    try:
        if memory_file.exists():
            memory_text = memory_file.read_text(encoding="utf-8")
    except OSError:
        log.warning("could not read memory file %s", memory_file)

    # Import here to allow monkeypatching in tests.
    from .agents import scouting

    try:
        res = scouting.run_scout_agent(
            settings=settings,
            memory=memory_text,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("scout agent failed")
        raise RuntimeError(f"scout agent failed: {e}") from e

    # Persist the agent's updated memory verbatim.
    if res.updated_memory:
        try:
            memory_file.parent.mkdir(parents=True, exist_ok=True)
            memory_file.write_text(res.updated_memory, encoding="utf-8")
        except OSError:
            log.warning("could not write memory file %s", memory_file)

    # Create draft tickets for each proposal.
    created = []
    for i in range(min(len(res.draft_titles), len(res.draft_bodies))):
        title = res.draft_titles[i]
        body = res.draft_bodies[i]
        if not title or not body:
            continue
        try:
            ticket = service.create(title, body, source="scout")
            created.append({"id": ticket.id, "title": ticket.title})
            log.info("scout spawned draft %s: %s", ticket.id, title)
        except Exception:
            log.exception("failed to create draft ticket: %s", title)

    return ScoutPassResult(
        updated_memory=res.updated_memory or memory_text,
        drafts_created=created,
    )
