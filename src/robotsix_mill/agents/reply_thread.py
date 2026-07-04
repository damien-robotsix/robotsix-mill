"""A ``reply_to_thread`` tool for agents to reply to a comment thread.

The implement agent (and potentially others in the future) can use this
to answer questions, acknowledge feedback, or provide status updates
directly on the ticket's comment thread.
"""

from __future__ import annotations

from ..config import Settings


def make_reply_to_thread_tool(settings: Settings, agent_name: str):
    """Return the ``reply_to_thread`` closure bound to *settings*.

    Args:
        settings: The application settings instance.
        agent_name: Stamped as the comment author so the originating
            agent is identifiable (e.g. ``"implement"``).
    """

    def reply_to_thread(thread_id: int, body: str) -> str:
        """Reply to a comment thread on the current ticket.

        Args:
            thread_id: The id of the top-level comment to reply to.
            body: The reply text.
        """
        from ._ticket_context import _resolve_current_ticket

        resolved = _resolve_current_ticket(settings)
        if isinstance(resolved, str):
            return resolved
        svc, ticket_id = resolved
        try:
            comment = svc.add_comment(
                ticket_id, body, author=agent_name, parent_id=thread_id
            )
            return f"Reply posted (id={comment.id})."
        except ValueError as e:
            return f"Error: {e}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="reply_to_thread",
            description="Reply to a comment thread on the current ticket.",
            category="reporting",
            parameters={"thread_id": "int", "body": "string"},
        )
    )

    return reply_to_thread
