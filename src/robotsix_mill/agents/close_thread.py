"""A ``close_thread`` tool for agents to close a comment thread.

The implement agent (and potentially others in the future) can use this
to mark a top-level comment thread as resolved after addressing the
feedback it contains.
"""

from __future__ import annotations

from ..config import Settings


def make_close_thread_tool(settings: Settings, agent_name: str):
    """Return the ``close_thread`` closure bound to *settings*.

    Args:
        settings: The application settings instance.
        agent_name: Included for future per-agent authorisation
            or logging (currently unused).
    """

    def close_thread(comment_id: int) -> str:
        """Close a top-level comment thread on the current ticket.

        Args:
            comment_id: The id of the top-level comment whose thread
                should be closed (resolved).
        """
        from ..runtime.tracing import current_session

        ticket_id = current_session()
        if ticket_id is None:
            return "Error: no active ticket session — cannot determine current ticket."

        from ..core.service import TicketService

        svc = TicketService(settings)
        try:
            svc.close_thread(comment_id)
            return f"Thread closed (id={comment_id})."
        except (ValueError, KeyError) as e:
            return f"Error: {e}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(ToolInfo(
        name="close_thread",
        description="Close a top-level comment thread on the current ticket (marks it resolved).",
        category="reporting",
        parameters={"comment_id": "int"},
    ))

    return close_thread
