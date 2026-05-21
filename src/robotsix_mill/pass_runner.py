"""Shared agent-pass runner.

Extracts the common boilerplate from audit_runner and scout_runner:
read memory, invoke agent, write memory, create draft tickets.
Agent modules are NOT imported here — the caller provides a callable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .core.service import TicketService
from .core.states import State
from .core.workspace import Workspace

log = logging.getLogger("robotsix_mill.pass_runner")

# Matches <!-- audit-gap-id: foo_bar --> style markers in ticket descriptions.
_GAP_ID_RE = re.compile(
    r'<!--\s*(audit|health|agent_check|retrospect)-gap-id:\s*(\S+)\s*-->'
)


def _verify_prior_proposals(
    service: TicketService,
    settings: Settings,
    source_label: str,
) -> dict[str, dict]:
    """Query the ticket store for drafts previously spawned by the
    agent identified by *source_label*, check their state, and return a
    mapping from ``gap_id`` → ``{ticket_id, state, resolution, branch}``.

    Only tickets whose description contains a ``<!-- {label}-gap-id:
    ... -->`` marker matching *source_label* are included.  Pre-rollout
    drafts without markers are silently skipped.
    """
    result: dict[str, dict] = {}

    # 1. List all tickets; filter client-side to matching source.
    try:
        all_tickets = service.list()
    except Exception:
        log.debug("_verify_prior_proposals: service.list() failed — "
                  "returning empty mapping (DB may not be initialised)")
        return result
    for ticket in all_tickets:
        if ticket.source != source_label:
            continue

        # 2. Read description and parse marker.
        desc = Workspace(settings.workspaces_dir, ticket.id).read_description()
        for m in _GAP_ID_RE.finditer(desc):
            marker_label, gap_id = m.group(1), m.group(2)
            if marker_label != source_label:
                continue

            # 3. Determine resolution.
            state_str = ticket.state.name if hasattr(ticket.state, 'name') else str(ticket.state)
            if ticket.state == State.CLOSED:
                history = service.history(ticket.id)
                if any(ev.state == State.DONE for ev in history):
                    resolution = "merged"
                else:
                    resolution = "declined"
            elif ticket.state == State.DONE:
                resolution = "merged"
            else:
                resolution = "in-flight"

            result[gap_id] = {
                "ticket_id": ticket.id,
                "state": state_str,
                "resolution": resolution,
                "branch": ticket.branch,
            }

    return result


def _render_verified_table(verified: dict[str, dict]) -> str:
    """Render a Markdown table from the verified mapping for agent input."""
    lines = [
        "## Prior proposals — verified state",
        "",
        "| gap_id | ticket_id | state | resolution |",
        "|--------|-----------|-------|------------|",
    ]
    for gap_id, info in verified.items():
        tid = info["ticket_id"]
        if info.get("branch"):
            tid = f"{tid} (branch: {info['branch']})"
        resolution = info["resolution"]
        if resolution == "merged":
            resolution_str = "merged (via DONE)"
        elif resolution == "declined":
            resolution_str = "declined (closed directly)"
        else:
            resolution_str = "in-flight"
        lines.append(
            f"| {gap_id} | {tid} | {info['state']} | {resolution_str} |"
        )
    return "\n".join(lines)


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

    # 2. Verify prior proposals and prepend verified-state table.
    verified = _verify_prior_proposals(service, settings, source_label)
    if verified:
        table = _render_verified_table(verified)
        memory_text = table + "\n\n" + memory_text

    # 3. Invoke the agent callable.
    res = agent_fn(settings=settings, memory=memory_text)

    # 4. Persist the agent's updated memory verbatim.
    if res.updated_memory:
        persist_memory(memory_file, res.updated_memory)

    # 5. Create draft tickets for each proposal.
    gap_ids = getattr(res, 'gap_ids', [])
    created: list[dict] = []
    for i in range(min(len(res.draft_titles), len(res.draft_bodies))):
        title = res.draft_titles[i]
        body = res.draft_bodies[i]
        if not title or not body:
            continue
        # Append gap-id marker if available.
        if i < len(gap_ids) and gap_ids[i]:
            body += f"\n\n<!-- {source_label}-gap-id: {gap_ids[i]} -->"
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
