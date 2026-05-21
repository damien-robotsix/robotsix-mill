"""Shared agent-pass runner.

Extracts the common boilerplate from audit_runner and scout_runner:
read memory, invoke agent, write memory, create draft tickets.
Agent modules are NOT imported here — the caller provides a callable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .core.service import TicketService

log = logging.getLogger("robotsix_mill.pass_runner")


def load_memory(memory_file: Path) -> str:
    """Read a memory ledger file; returns ``""`` if missing/unreadable."""
    try:
        if memory_file.exists():
            return memory_file.read_text(encoding="utf-8")
    except OSError:
        log.warning("could not read memory file %s", memory_file)
    return ""


def persist_memory(memory_file: Path, text: str) -> None:
    """Write *text* to *memory_file*, creating parent dirs as needed."""
    if text or not memory_file.exists():
        try:
            memory_file.parent.mkdir(parents=True, exist_ok=True)
            memory_file.write_text(text, encoding="utf-8")
        except OSError:
            log.warning("could not write memory file %s", memory_file)


@dataclass
class AgentPassResult:
    """Internal result of running an agent pass."""

    updated_memory: str
    drafts_created: list[dict]  # [{"id": ..., "title": ...}, ...]
    session_id: str = ""


def run_agent_pass(
    agent_fn: Callable[..., Any],
    *,
    memory_file: Path,
    source_label: str,
    service: TicketService,
    settings: Settings,
    origin_session: str | None = None,
) -> AgentPassResult:
    """Execute one agent pass with shared boilerplate.

    Args:
        agent_fn: Callable invoked as ``agent_fn(settings=settings,
                  memory=memory_text)``.  The caller pre-bakes extra
                  kwargs (e.g. ``repo_dir``) via ``functools.partial``.
        memory_file: Path to the memory/ledger file.
        source_label: Label for draft ticket ``source`` field (e.g.
                      ``"audit"``, ``"scout"``).
        service: ``TicketService`` for creating draft tickets.
        settings: Mill settings (passed through to the agent callable).
        origin_session: Value for ``origin_session`` on created tickets.

    Returns:
        ``AgentPassResult`` with updated memory and created draft info.
    """
    # 1. Read current memory — empty string if missing/unreadable.
    memory_text = load_memory(memory_file)

    # 2. Invoke the agent callable.
    res = agent_fn(settings=settings, memory=memory_text)

    # 3. Persist the agent's updated memory verbatim.
    if res.updated_memory:
        persist_memory(memory_file, res.updated_memory)

    # 4. Create draft tickets for each proposal.
    created: list[dict] = []
    for i in range(min(len(res.draft_titles), len(res.draft_bodies))):
        title = res.draft_titles[i]
        body = res.draft_bodies[i]
        if not title or not body:
            continue
        try:
            ticket = service.create(
                title,
                body,
                source=source_label,
                origin_session=origin_session,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            log.info(
                "%s spawned draft %s: %s", source_label, ticket.id, title,
            )
        except Exception:
            log.exception("failed to create draft ticket: %s", title)

    return AgentPassResult(
        updated_memory=res.updated_memory or memory_text,
        drafts_created=created,
        session_id=origin_session or "",
    )
