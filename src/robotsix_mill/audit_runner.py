"""Audit runner — orchestrates a single audit pass.

Mirrors the retrospect stage pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_audit_agent`` from agents.auditing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .core.service import TicketService
from .core.states import State

log = logging.getLogger("robotsix_mill.audit")


@dataclass
class AuditPassResult:
    """Result of running an audit pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)


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

    # Read current memory — empty string if missing/unreadable.
    memory_text = ""
    try:
        if memory_file.exists():
            memory_text = memory_file.read_text(encoding="utf-8")
    except OSError:
        log.warning("could not read memory file %s", memory_file)

    # Import here to allow monkeypatching in tests.
    from .agents import auditing

    try:
        res = auditing.run_audit_agent(
            settings=settings,
            memory=memory_text,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("audit agent failed")
        raise RuntimeError(f"audit agent failed: {e}") from e

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
            ticket = service.create(title, body, source="audit")
            # ticket is already in DRAFT state after create()
            created.append({"id": ticket.id, "title": ticket.title})
            log.info("audit spawned draft %s: %s", ticket.id, title)
        except Exception:
            log.exception("failed to create draft ticket: %s", title)

    return AuditPassResult(
        updated_memory=res.updated_memory or memory_text,
        drafts_created=created,
    )
