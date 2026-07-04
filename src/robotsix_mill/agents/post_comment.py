"""A ``post_comment`` tool for agents to post a top-level comment on
the current ticket — no thread_id required.

Use case: a ticket whose deliverable is *information* rather than
code. The implement agent reaches this when a spec says "post a
comment summarising the findings" and the agent has nothing to edit.
Without this tool the only comment-shaped surfaces are
``reply_to_thread`` (needs a parent thread) and ``close_thread``
(needs a comment id) — both useless for a fresh top-level comment.
That mismatch produced ticket d129's bogus
"what's the thread_id?" ASK_USER question.

The tool is idempotent on (ticket, body) so a retrying agent doesn't
spam duplicates. The author is stamped with the agent name (e.g.
``"implement"``) so the comment is traceable to the originating
agent's run.
"""

from __future__ import annotations

import logging

from ..config import Settings

log = logging.getLogger(__name__)


def make_post_comment_tool(settings: Settings, agent_name: str):
    """Return the ``post_comment`` closure bound to *settings*.

    Args:
        settings: The application settings instance.
        agent_name: Stamped as the comment author so the originating
            agent is identifiable (e.g. ``"implement"``).
    """
    # Track posted (body) hashes within this tool's lifetime so an
    # accidental double-call from a retried agent step doesn't pile up
    # duplicate comments. Per-call dedupe is sufficient — a genuinely
    # NEW comment on the same ticket from a later run gets a fresh tool
    # closure with an empty seen-set.
    _seen: set[int] = set()

    def post_comment(body: str) -> str:
        """Post a top-level comment on the current ticket.

        Use when the ticket's deliverable is information rather than
        code — e.g. a spec that asks you to "post a comment with
        findings", "explain why no change is needed", or "summarise
        the investigation". The comment is filed as a fresh top-level
        thread (no parent). For replying inside an existing PR review
        thread, use ``reply_to_thread`` instead.

        Args:
            body: The comment body (Markdown is fine).

        Returns:
            A short status string with the new comment's id, or an
            error message starting with ``post_comment:``.
        """
        body = (body or "").strip()
        if not body:
            return "post_comment: empty body — refusing to post"

        # Dedupe within this tool's lifetime — a retried tool call
        # with the exact same body returns the prior status instead
        # of double-posting.
        h = hash(body)
        if h in _seen:
            return "post_comment: duplicate body in this run — skipped"

        from ._ticket_context import _resolve_current_ticket

        resolved = _resolve_current_ticket(settings, error_prefix="post_comment")
        if isinstance(resolved, str):
            return resolved
        svc, ticket_id = resolved
        try:
            comment = svc.add_comment(
                ticket_id,
                body,
                author=agent_name,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the agent loop
            log.exception(
                "post_comment: add_comment failed for %s",
                ticket_id,
            )
            return f"post_comment: could not post ({exc!r})"

        _seen.add(h)
        return f"posted comment {comment.id}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="post_comment",
            description=(
                "Post a top-level comment on the current ticket. Use for "
                "tickets whose deliverable is information rather than "
                "code — e.g. a spec that asks you to 'post a comment with "
                "findings'. The comment is a fresh top-level thread; for "
                "replies inside an existing PR review thread, use "
                "``reply_to_thread`` instead."
            ),
            category="reporting",
            parameters={"body": "str (Markdown comment body)"},
        )
    )

    return post_comment
