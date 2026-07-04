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

        Call ``list_threads`` first to discover valid ``comment_id``
        values — do not guess or hardcode IDs.  Idempotent:
        re-closing an already-resolved thread returns an
        \"already closed\" success message — do not retry.

        Args:
            comment_id: The id of the top-level comment whose thread
                should be closed (resolved).
        """
        from ._ticket_context import _resolve_current_ticket

        resolved = _resolve_current_ticket(settings)
        if isinstance(resolved, str):
            return resolved
        svc, ticket_id = resolved
        try:
            # Pass ticket_id so the service resolves the correct
            # per-board DB — comment ids are per-board, not globally
            # unique.
            svc.close_thread(comment_id, ticket_id=ticket_id)
            return f"Thread closed (id={comment_id})."
        except (ValueError, KeyError) as e:
            # Idempotency: already-closed threads are a success, not an error.
            if "already closed" in str(e):
                return f"Thread already closed (id={comment_id} is already resolved)."
            return f"Error: {e}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="close_thread",
            description='Close a top-level comment thread on the current ticket (marks it resolved). Call ``list_threads`` first to discover valid ``comment_id`` values — do not guess or hardcode IDs. Idempotent: re-closing an already-resolved thread returns an "already closed" success message — do not retry.',
            category="reporting",
            parameters={"comment_id": "int"},
        )
    )

    return close_thread
