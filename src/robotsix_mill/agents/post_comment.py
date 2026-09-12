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

The tool is idempotent on (ticket, author, body) so a retrying agent
doesn't spam duplicates — the guard checks against every comment this
agent has already posted on the ticket (surviving tool-closure rebuilds
within a run), not just the most recent one. The author is stamped with
the agent name (e.g. ``"implement"``) so the comment is traceable to the
originating agent's run.
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
    # Track posted (body) hashes within this tool's lifetime as a cheap
    # fast-path so a retried tool call short-circuits without a DB query.
    # The authoritative guard is the persisted-comment check in
    # ``post_comment`` below: it dedupes against EVERY comment this agent
    # has already posted on the ticket, so a duplicate is caught even when
    # a fresh tool closure (a rebuilt agent / retried pass) loses this
    # in-memory set — not just when the body matches the last one posted.
    _seen: set[int] = set()

    def post_comment(
        body: str | None = None,
        comment: str | None = None,
        ticket_id: str | None = None,
    ) -> str:
        """Post a top-level comment on the current ticket.

        Use when the ticket's deliverable is information rather than
        code — e.g. a spec that asks you to "post a comment with
        findings", "explain why no change is needed", or "summarise
        the investigation". The comment is filed as a fresh top-level
        thread (no parent). For replying inside an existing PR review
        thread, use ``reply_to_thread`` instead.

        Args:
            body: The comment body (Markdown is fine). ``comment`` is an
                accepted alias (three calls were schema-rejected for it on
                2026-09-08).
            comment: Alias of ``body``.
            ticket_id: Accepted and ignored — the comment always goes to
                the CURRENT ticket (the one this run implements).

        Returns:
            A short status string with the new comment's id, or an
            error message starting with ``post_comment:``.
        """
        body = (body or comment or "").strip()
        if not body:
            return "post_comment: empty body — refusing to post"

        # Dedupe fast-path within this tool's lifetime — a retried tool
        # call with the exact same body returns the prior status instead
        # of double-posting.
        h = hash(body)
        if h in _seen:
            return "post_comment: duplicate body in this run — skipped"

        from ._ticket_context import current_ticket_service

        result = current_ticket_service(settings)
        if result is None:
            return (
                "post_comment: no active ticket session — cannot "
                "determine current ticket."
            )
        svc, ticket_id = result

        # Idempotency beyond the in-memory set: an identical comment this
        # agent already posted on this ticket is a duplicate even if a
        # fresh tool closure (rebuilt agent / retried pass) lost the
        # seen-set. Checking the persisted comments makes the guard
        # survive closure rebuilds, so a double-post can't slip through.
        try:
            for existing in svc.list_comments(ticket_id):
                if existing.author == agent_name and existing.body == body:
                    return "post_comment: duplicate body in this run — skipped"
        except Exception as exc:
            # If listing comments fails, fall through — the in-memory set
            # still guards same-closure retries and add_comment reports
            # its own error if posting is impossible.
            log.debug(
                "post_comment: dedup check failed for %s (%r); proceeding",
                ticket_id,
                exc,
            )

        try:
            created = svc.add_comment(
                ticket_id,
                body,
                author=agent_name,
            )
        except Exception as exc:
            log.exception(
                "post_comment: add_comment failed for %s",
                ticket_id,
            )
            return f"post_comment: could not post ({exc!r})"

        _seen.add(h)
        return f"posted comment {created.id}"

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
