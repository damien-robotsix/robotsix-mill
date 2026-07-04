"""A ``list_threads`` tool for agents to discover top-level comment
threads on the current ticket.

Without ``read_ticket`` access, agents that need to reply to threads
(``reply_to_thread``) have no way to discover valid ``thread_id``
values.  This lightweight tool fills that gap.
"""

from __future__ import annotations

from ..config import Settings


def make_list_threads_tool(settings: Settings, agent_name: str):
    """Return the ``list_threads`` closure bound to *settings*.

    Args:
        settings: The application settings instance.
        agent_name: Included for future per-agent authorisation
            or logging (currently unused).
    """

    def list_threads() -> str:
        """List top-level comment threads on the current ticket.

        Returns a formatted list of thread IDs with open/closed status
        and the first line of each comment body, or "(no threads)" if
        none exist.
        """
        from ._ticket_context import _resolve_current_ticket

        resolved = _resolve_current_ticket(settings)
        if isinstance(resolved, str):
            return resolved
        svc, ticket_id = resolved
        try:
            comments = svc.list_comments(ticket_id)
        except KeyError:
            return f"Error: ticket {ticket_id} not found."

        threads = [c for c in comments if c.parent_id is None]
        if not threads:
            return "(no threads — do not call reply_to_thread as there is nothing to reply to)"

        lines: list[str] = []
        for c in threads:
            status = "[closed]" if c.closed_at is not None else "[open]"
            # Take the first line of the body, truncated to ~80 chars.
            first_line = (c.body or "").split("\n", 1)[0].strip()
            if len(first_line) > 80:
                first_line = first_line[:77] + "..."
            lines.append(f'id={c.id:<4} {status:<8} "{first_line}"')

        return "\n".join(lines)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="list_threads",
            description="List top-level comment threads on the current ticket with their IDs and open/closed status.",
            category="reporting",
            parameters={},
        )
    )

    return list_threads
