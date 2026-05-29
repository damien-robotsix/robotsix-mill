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

from ..config import Settings


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
        ticket_id: the full ticket ID string (e.g. '20250331T142030Z-fix-auth-timeout-a3f2').
        Returns a formatted Markdown summary or a clear error message.
        """
        ticket_id = (ticket_id or "").strip()
        if not ticket_id:
            return "read_ticket: a non-empty ticket_id is required"

        try:
            from ..core.service import TicketService

            service = TicketService(settings)
            ticket = service.get(ticket_id)

            if ticket is None:
                return f"read_ticket: no ticket found with id '{ticket_id}'"

            # --- Build the Markdown output ---
            lines: list[str] = []

            # Header
            lines.append(f"## {ticket.title}")
            lines.append("")
            lines.append(f"**ID:** `{ticket.id}`")
            lines.append(f"**State:** {ticket.state.value}")
            lines.append(f"**Kind:** {ticket.kind}")
            lines.append(f"**Source:** {ticket.source}")
            lines.append(f"**Created:** {ticket.created_at}")
            lines.append(f"**Updated:** {ticket.updated_at}")
            lines.append("")

            # Description (soft-cap at 3000 chars)
            lines.append("### Description")
            lines.append("")
            desc = service.workspace(ticket).read_description() or ""
            desc = desc.strip()
            if not desc:
                lines.append("(no description)")
            else:
                if len(desc) > 3000:
                    # Prefer a paragraph boundary, then a line boundary
                    cutoff = 3000
                    for marker in ("\n\n", "\n"):
                        pos = desc.rfind(marker, 0, 3000)
                        if pos != -1 and pos > 2700:
                            cutoff = pos
                            break
                    desc = desc[:cutoff] + "\n\n... [truncated]"
                lines.append(desc)
            lines.append("")

            # History — last 30 events, most recent first
            history = service.history(ticket_id)
            n_history = len(history)
            shown_history = history[-30:] if n_history > 30 else history
            shown_history = list(reversed(shown_history))
            lines.append(f"### History ({n_history} events)")
            lines.append("")
            if not history:
                lines.append("(no history)")
            else:
                if n_history > 30:
                    lines.append(f"... [{n_history - 30} earlier events omitted]")
                    lines.append("")
                for ev in shown_history:
                    note_str = ev.note or "(no note)"
                    lines.append(f"- [{ev.state.value}] {ev.at} — {note_str}")
            lines.append("")

            # Comments — last 15, most recent first
            comments = service.list_comments(ticket_id)
            n_comments = len(comments)
            shown_comments = comments[-15:] if n_comments > 15 else comments
            shown_comments = list(reversed(shown_comments))
            lines.append(f"### Comments ({n_comments})")
            lines.append("")
            if not comments:
                lines.append("(no comments)")
            else:
                if n_comments > 15:
                    lines.append(f"... [{n_comments - 15} earlier comments omitted]")
                    lines.append("")
                for c in shown_comments:
                    lines.append(f"**{c.author}** ({c.created_at}):")
                    lines.append(c.body)
                    lines.append("")

            result = "\n".join(lines)

            # Soft overall cap at ~6000 characters.
            # Truncate at a section/paragraph boundary when possible
            # to avoid cutting mid-word or mid-heading.
            if len(result) > 6000:
                cutoff = 6000
                # Prefer cutting before a section heading, then a
                # paragraph break, then any newline.
                for marker in ("\n### ", "\n## ", "\n\n", "\n"):
                    pos = result.rfind(marker, 0, 6000)
                    if pos != -1 and pos > 5400:
                        cutoff = pos
                        break
                result = result[:cutoff] + "\n\n... [truncated]"

            return result

        except Exception as e:  # noqa: BLE001 — never abort the agent run
            return f"read_ticket: error reading ticket '{ticket_id}' ({e!r})"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="read_ticket",
            description="Return the full details of a ticket: description, history, and comments.",
            category="reporting",
            parameters={"ticket_id": "str"},
        )
    )

    return read_ticket
