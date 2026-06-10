"""A ``list_epic_children`` tool that lets an agent enumerate its sibling
epic children (the other children of its parent epic).

Agent tools run **in the mill agent process**, which has direct in-process
``TicketService``/board-DB access — they do NOT run inside the
network-isolated sandbox.  This is the read-only counterpart that closes
the "agent can't discover its sibling epic children" gap: the current
ticket id is bound at build time by the factory, so the closure takes no
argument.

Hard requirement: read-only.  The closure never calls ``create``,
``transition``, ``add_comment``, ``redraft``, or any other mutating
``TicketService`` method.
"""

from __future__ import annotations

from ..config import Settings

# Output budgets (chars). Each child's description excerpt is capped
# individually; the whole rendered Markdown is capped again at the end so
# a large epic can't blow the agent's context.
_DESC_CAP = 500
_RESULT_CAP = 6000
# Maximum number of children rendered before the rest are summarised.
_CHILD_ROWS = 40


def _render_children(service, parent_id: str, children, current_ticket_id: str) -> str:
    """Render the Markdown list of *children*, marking the current ticket
    and soft-capping the description excerpt per child and the whole output."""
    lines = [f"## Children of epic `{parent_id}` ({len(children)})", ""]
    for child in children[:_CHILD_ROWS]:
        marker = " (this ticket)" if child.id == current_ticket_id else ""
        lines.append(
            f"- `{child.id}`{marker} — **{child.title}** "
            f"[{child.state.value}] ({child.kind})"
        )
        desc = (service.workspace(child).read_description() or "").strip()
        if desc:
            excerpt = desc[:_DESC_CAP]
            if len(desc) > _DESC_CAP:
                excerpt += " ... [truncated]"
            lines.append(f"  - {excerpt}")
    if len(children) > _CHILD_ROWS:
        lines.append(f"\n... [{len(children) - _CHILD_ROWS} more children omitted]")

    rendered = "\n".join(lines)
    if len(rendered) > _RESULT_CAP:
        rendered = rendered[:_RESULT_CAP] + "\n\n... [truncated]"
    return rendered


def make_list_epic_children_tool(settings: Settings, current_ticket_id: str):
    """Return the ``list_epic_children`` closure bound to *settings* and
    *current_ticket_id*.

    Lazily constructs a ``TicketService`` per call so this stays cheap to
    attach to every agent and hermetic for tests.

    Args:
        settings: The application settings instance.
        current_ticket_id: The id of the ticket the agent is working on;
            its parent epic's children are the siblings to enumerate.
    """

    def list_epic_children() -> str:
        """List the sibling epic children of the current ticket.

        Read-only — cannot modify tickets in any way. Takes no argument:
        the current ticket id is bound at build time. Returns a Markdown
        list of the children of the current ticket's parent epic (id,
        title, state, kind, and a short description excerpt), marking the
        current ticket, or a clear message when there is no parent epic.
        """
        try:
            from ..core.service import TicketService

            service = TicketService(settings)
            ticket = service.get(current_ticket_id)
            if ticket is None:
                return (
                    f"list_epic_children: no ticket found with id '{current_ticket_id}'"
                )

            if not ticket.parent_id:
                return (
                    "list_epic_children: this ticket has no parent epic — no siblings"
                )

            children = service.list_children(ticket.parent_id)
            if not children:
                return (
                    f"list_epic_children: parent epic '{ticket.parent_id}' "
                    "has no children"
                )

            return _render_children(
                service, ticket.parent_id, children, current_ticket_id
            )

        except Exception as e:  # noqa: BLE001 — never abort the agent run
            return f"list_epic_children: error ({e!r})"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="list_epic_children",
            description=(
                "List the sibling epic children of the current ticket "
                "(children of its parent epic): id, title, state, kind, "
                "and a short description excerpt. Read-only; takes no argument."
            ),
            category="reporting",
            parameters={},
        )
    )

    return list_epic_children
