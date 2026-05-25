"""Shared agent-pass runner.

Extracts the common boilerplate shared by the periodic-pass runners
(audit, health, agent-check): read memory, invoke agent, write memory,
create draft tickets.
Agent modules are NOT imported here — the caller provides a callable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .core.models import SourceKind, Ticket
from .core.service import TicketService
from .core.states import State
from .core.workspace import Workspace

log = logging.getLogger("robotsix_mill.pass_runner")

# Matches <!-- audit-gap-id: foo_bar --> style markers in ticket descriptions.
_GAP_ID_RE = re.compile(
    r'<!--\s*(audit|health|agent_check|retrospect|survey|test_gap|bc_check|env_sync|completeness_check)-gap-id:\s*(\S+)\s*-->'
)


def _verify_prior_proposals(
    service: TicketService,
    settings: Settings,
    source_label: SourceKind,
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


def _format_recent_proposals(tickets: list[Ticket]) -> str:
    """Format a ``<recent_proposals>`` block for agent prompt injection.

    One line per ticket: ``[STATE] short_id | title``, most recent first.
    """
    if not tickets:
        return "<recent_proposals>\n(no recent proposals)\n</recent_proposals>"
    lines = ["<recent_proposals>"]
    for t in tickets:
        short_id = t.id[:7]
        state_val = t.state.value
        lines.append(f"[{state_val}] {short_id} | {t.title}")
    lines.append("</recent_proposals>")
    return "\n".join(lines)


def load_memory(memory_file: Path, max_chars: int | None = None) -> str:
    """Read a memory ledger file; returns ``""`` if missing/unreadable.

    When *max_chars* is set and the file exceeds that limit, the oldest
    entries are dropped — only the last *max_chars* characters (most
    recent) are kept, adjusted to a newline boundary so entries aren't
    split mid-line.  A ``[... memory truncated: N chars omitted]`` note
    is prepended and a warning is logged.
    """
    try:
        if memory_file.exists():
            text = memory_file.read_text(encoding="utf-8")
            if max_chars is not None and len(text) > max_chars:
                original_size = len(text)
                # Find the cut point (keep the last max_chars), then
                # advance to the next newline so the first kept line is
                # a complete line.
                cut_point = original_size - max_chars
                nl_idx = text.find("\n", cut_point)
                if nl_idx != -1:
                    kept = text[nl_idx + 1:]  # start after the newline
                else:
                    kept = text[cut_point:]  # fallback (no newline found)
                omitted = original_size - len(kept)
                text = f"[... memory truncated: {omitted} chars omitted]\n\n{kept}"
                log.warning(
                    "memory file %s truncated: %d → %d chars",
                    memory_file, original_size, len(text),
                )
            return text
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
    source_label: SourceKind,
    service: TicketService,
    settings: Settings,
    origin_session: str | None = None,
    max_drafts: int | None = None,
) -> AgentPassResult:
    """Execute one agent pass with shared boilerplate.

    Args:
        agent_fn: Callable invoked as ``agent_fn(settings=settings,
                  memory=memory_text)``.  The caller pre-bakes extra
                  kwargs (e.g. ``repo_dir``) via ``functools.partial``.
        memory_file: Path to the memory/ledger file.
        source_label: Label for draft ticket ``source`` field (e.g.
                      ``SourceKind.AUDIT``, ``SourceKind.AGENT``).
        service: ``TicketService`` for creating draft tickets.
        settings: Mill settings (passed through to the agent callable).
        origin_session: Value for ``origin_session`` on created tickets.
        max_drafts: If set, limit the number of draft tickets created
                    (clips ``draft_titles``, ``draft_bodies``, and
                    ``gap_ids`` before the creation loop).  Defaults to
                    ``None`` (no limit).

    Returns:
        ``AgentPassResult`` with updated memory and created draft info.
    """
    # 1. Read current memory — empty string if missing/unreadable.
    memory_text = load_memory(memory_file, max_chars=settings.max_memory_chars)

    # 2. Verify prior proposals and prepend verified-state table.
    verified = _verify_prior_proposals(service, settings, source_label)
    if verified:
        table = _render_verified_table(verified)
        memory_text = table + "\n\n" + memory_text

    # 3. Build the recent-proposals block for prompt injection.
    recent = service.recent_proposals_for(source_label, limit=100)
    rp_block = _format_recent_proposals(recent)

    # 4. Invoke the agent callable.
    res = agent_fn(settings=settings, memory=memory_text, recent_proposals=rp_block)

    # 5. Persist the agent's updated memory verbatim.
    if res.updated_memory:
        persist_memory(memory_file, res.updated_memory)

    # 6. Create draft tickets for each proposal.
    gap_ids = getattr(res, 'gap_ids', [])
    created: list[dict] = []
    limit = min(len(res.draft_titles), len(res.draft_bodies))
    if max_drafts is not None:
        limit = min(limit, max_drafts)
    for i in range(limit):
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
