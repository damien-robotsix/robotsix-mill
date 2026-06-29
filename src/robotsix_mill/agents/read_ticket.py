"""A ``read_ticket`` tool injected into periodic agents via ``build_agent``.

Periodic agents (audit, health, survey, test-gap, bc-check, agent-check,
config-sync, retrospect) receive a ``<recent_proposals>`` block listing their
past proposals with one-line summaries.  When an agent needs the full
context of a past proposal — its description, history, and comments — this
tool provides it.  It is the read-only counterpart to ``report_issue``.

Hard requirement: read-only.  The tool closure never calls ``create``,
``transition``, ``add_comment``, ``set_branch``, ``redraft``, or any other
mutating ``TicketService`` method.  This is structurally enforced — the
closure has no access to write methods.
"""

from __future__ import annotations

import re

from ..config import Settings

# Ticket ID format: YYYYMMDDTHHMMSSZ-slug-hex4
# timestamp (16 chars), dash, slug body, dash, 4 hex chars. The slug body may
# contain consecutive dashes for legacy IDs minted before _slug stripped
# dashes after truncation (e.g. '...split-worker--f2d4').
_TICKET_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[a-z0-9-]+-[a-f0-9]{4}$")

# Output budgets (chars). The description is capped individually; the whole
# rendered Markdown is capped again at the end so a long history/comment run
# can't blow the agent's context.
_DESC_CAP = 3000
_RESULT_CAP = 6000


def _truncate_at_boundary(text: str, cap: int, markers: tuple[str, ...]) -> str:
    """Return *text* trimmed to roughly *cap* chars, preferring to cut at the
    last occurrence of one of *markers* (a heading / paragraph / line break)
    in the final ~10% of the budget so we never cut mid-word or mid-heading.
    Appends a ``... [truncated]`` sentinel. No-op when already within *cap*."""
    if len(text) <= cap:
        return text
    cutoff = cap
    floor = int(cap * 0.9)
    for marker in markers:
        pos = text.rfind(marker, 0, cap)
        if pos != -1 and pos > floor:
            cutoff = pos
            break
    return text[:cutoff] + "\n\n... [truncated]"


def _header_lines(ticket) -> list[str]:
    """Title + metadata block."""
    return [
        f"## {ticket.title}",
        "",
        f"**ID:** `{ticket.id}`",
        f"**State:** {ticket.state.value}",
        f"**Kind:** {ticket.kind}",
        f"**Source:** {ticket.source}",
        f"**Created:** {ticket.created_at}",
        f"**Updated:** {ticket.updated_at}",
        "",
    ]


def _description_section(desc: str) -> list[str]:
    """``### Description`` block, soft-capped at ``_DESC_CAP`` chars."""
    desc = (desc or "").strip()
    if not desc:
        body = "(no description)"
    else:
        body = _truncate_at_boundary(desc, _DESC_CAP, ("\n\n", "\n"))
    return ["### Description", "", body, ""]


def _history_section(history) -> list[str]:
    """``### History`` block — all events, most recent first."""
    n = len(history)
    lines = [f"### History ({n} events)", ""]
    if not history:
        lines.append("(no history)")
        lines.append("")
        return lines
    for ev in reversed(history):
        lines.append(f"- [{ev.state.value}] {ev.at} — {ev.note or '(no note)'}")
    lines.append("")
    return lines


def _comments_section(comments) -> list[str]:
    """``### Comments`` block — all comments, most recent first."""
    n = len(comments)
    lines = [f"### Comments ({n})", ""]
    if not comments:
        lines.append("(no comments)")
        return lines
    for c in reversed(comments):
        lines.append(f"**{c.author}** ({c.created_at}, id={c.id}):")
        lines.append(c.body)
        lines.append("")
    return lines


def make_read_ticket_tool(settings: Settings):
    """Return the ``read_ticket`` closure bound to *settings*.

    Lazily constructs a ``TicketService`` per call so this stays cheap
    to attach to every agent and hermetic for tests.

    Args:
        settings: The application settings instance.
    """

    def read_ticket(ticket_id: str) -> str:
        """Return the full details of a ticket: description, history, and comments.

        Read-only — cannot modify tickets in any way.
        ticket_id: the full ticket ID string, formatted as 'YYYYMMDDTHHMMSSZ-slug-4hex'
                   (e.g. '20250331T142030Z-fix-auth-timeout-a3f2').
                   The ID must include the slug and trailing 4-hex-digit suffix —
                   a bare timestamp like '20250331T142030Z' is NOT valid.
        Returns a formatted Markdown summary or a clear error message.
        """
        ticket_id = (ticket_id or "").strip()
        if not ticket_id:
            return "read_ticket: a non-empty ticket_id is required"

        if _TICKET_ID_RE.match(ticket_id) is None:
            return (
                "read_ticket: invalid ticket_id format — ID may be truncated; "
                "full IDs look like '20250331T142315Z-add-billing-endpoint-3a1f'"
            )

        try:
            from ..core.service import TicketService

            service = TicketService(settings)
            ticket = service.get(ticket_id)
            if ticket is None:
                return f"read_ticket: no ticket found with id '{ticket_id}'"

            lines = _header_lines(ticket)
            lines += _description_section(service.workspace(ticket).read_description())
            lines += _history_section(service.history(ticket_id))
            lines += _comments_section(service.list_comments(ticket_id))

            return _truncate_at_boundary(
                "\n".join(lines),
                _RESULT_CAP,
                ("\n### ", "\n## ", "\n\n", "\n"),
            )

        except Exception as e:  # noqa: BLE001 — never abort the agent run
            return f"read_ticket: error reading ticket '{ticket_id}' ({e!r})"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="read_ticket",
            description="Return the full details of a ticket (requires ID in 'YYYYMMDDTHHMMSSZ-slug-4hex' format).",
            category="reporting",
            parameters={"ticket_id": "str"},
        )
    )

    return read_ticket
