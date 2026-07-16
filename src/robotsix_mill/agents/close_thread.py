"""A ``close_thread`` tool for agents to close a comment thread.

The implement agent (and potentially others in the future) can use this
to mark a top-level comment thread as resolved after addressing the
feedback it contains.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:
    from ..core.service import TicketService


def _resolve_service(settings: Settings) -> tuple["TicketService", str] | None:
    """Resolve the ticket service and current ticket id.

    Returns ``(svc, ticket_id)`` or ``None`` when no active session.
    """
    from ._ticket_context import current_ticket_service

    return current_ticket_service(settings)


def _close_thread_impl(settings: Settings, comment_id: int) -> str:
    """Close a single thread (standalone helper to keep factory lean)."""
    result = _resolve_service(settings)
    if result is None:
        return "Error: no active ticket session — cannot determine current ticket."
    svc, ticket_id = result
    try:
        svc.close_thread(comment_id, ticket_id=ticket_id)
        return f"Thread closed (id={comment_id})."
    except (ValueError, KeyError) as e:
        if "already closed" in str(e):
            return f"Thread already closed (id={comment_id} is already resolved)."
        return f"Error: {e}"


def _close_threads_impl(settings: Settings, comment_ids: list[int]) -> str:
    """Batch-close multiple threads (standalone helper to keep factory lean)."""
    result = _resolve_service(settings)
    if result is None:
        return "Error: no active ticket session — cannot determine current ticket."
    svc, ticket_id = result

    closed: list[int] = []
    already: list[int] = []
    errors: list[str] = []

    for cid in comment_ids:
        try:
            svc.close_thread(cid, ticket_id=ticket_id)
            closed.append(cid)
        except (ValueError, KeyError) as e:
            if "already closed" in str(e):
                already.append(cid)
            else:
                errors.append(f"id {cid}: {e}")

    parts: list[str] = []
    if closed:
        plural = "s" if len(closed) != 1 else ""
        parts.append(
            f"Closed {len(closed)} thread{plural}: ids {', '.join(str(c) for c in closed)}."
        )
    if already:
        parts.append(
            f"Already closed (idempotent success): ids {', '.join(str(c) for c in already)}."
        )
    if errors:
        parts.append(f"Errors: {'; '.join(errors)}.")
    if not parts:
        return "No comment ids provided."
    return " ".join(parts)


def make_close_thread_tool(
    settings: Settings, agent_name: str
) -> tuple[Callable[[int], str], Callable[[list[int]], str]]:
    """Return ``(close_thread, close_threads)`` closures bound to *settings*.

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
        return _close_thread_impl(settings, comment_id)

    def close_threads(comment_ids: list[int]) -> str:
        """Close multiple top-level comment threads in a single call.

        Call ``list_threads`` first to discover valid ``comment_id``
        values — do not guess or hardcode IDs.  Idempotent:
        re-closing an already-resolved thread is treated as success
        and reported in the summary.

        Args:
            comment_ids: A list of comment ids whose threads should
                be closed (resolved).
        """
        return _close_threads_impl(settings, comment_ids)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="close_thread",
            description='Close a top-level comment thread on the current ticket (marks it resolved). Call ``list_threads`` first to discover valid ``comment_id`` values — do not guess or hardcode IDs. Idempotent: re-closing an already-resolved thread returns an "already closed" success message — do not retry.',
            category="reporting",
            parameters={"comment_id": "int"},
        )
    )

    return close_thread, close_threads
