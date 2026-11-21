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

The tool is for *substance only* — never for progress/completion
announcements. Post the deliverable summary exactly once; never post
self-referential meta-comments ("Done.", "Delivered — see diff/summary
thread X", "Ignore my previous comment", "(Stop-noise marker)"), and
never append a "correction" of a comment already posted (there is no
edit-in-place API — corrected content belongs in the agent's final
structured summary, not a follow-up comment).

Prompt/docstring guidance proved unenforceable (ticket
20261121T042200Z: the implement agent still posted a deliverable
summary plus 5-6 noise comments despite the written rules), so the tool
enforces both constraints deterministically:

- **Per-(ticket, author) comment cap** — the tool refuses to post once
  this agent has already posted *max_comments* comments on the ticket
  in the run (default 1; multi-finding info tickets pass 2-3). The
  count is taken from the *persisted* comments (``author ==
  agent_name``), so the cap survives tool-closure rebuilds and retried
  passes, not just the in-memory seen-set.
- **Content guard** — bodies that are pure self-referential meta-noise
  (bare "Done.", "Stand down — no further action.", "(no further
  replies)", "Delivered — see diff and summary thread X",
  "Ignore my previous comment", ...) are refused regardless of how the
  wording is varied, because the dedupe's byte-identical check cannot
  catch semantically-similar but textually-distinct noise.

A refusal is logged as a warning and recorded as a step event (history,
never a comment) so the recurring violation stays observable; the
refused content belongs in the agent's final structured summary — there
is no edit-in-place API.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:
    from ..core.models import Comment
    from ..core.service import TicketService

log = logging.getLogger(__name__)

# Vocabulary of process/meta words. A body whose EVERY content token is
# drawn from this set only talks about the act of commenting / stopping /
# delivering — it carries no deliverable substance, so it is refused.
# The vocabulary (not an exact-string match) is deliberate: the noise the
# implement agent posts is semantically-similar but textually-distinct
# each time ("Stand down — no further action.", "Stopping here
# deliberately: this is the last comment.", "(no further replies)"), so
# a byte-identical dedupe can never catch it.
_META_NOISE_WORDS: frozenset[str] = frozenset(
    {
        # completion / delivery markers
        "done",
        "finish",
        "finished",
        "deliver",
        "delivered",
        "delivery",
        "complete",
        "completed",
        "completion",
        "see",
        "view",
        # references to the ticket's own comments / threads / diff
        "diff",
        "diffs",
        "summary",
        "thread",
        "threads",
        "comment",
        "comments",
        "ignore",
        "my",
        "previous",
        "prior",
        "above",
        "below",
        "earlier",
        "aforementioned",
        "noted",
        "already",
        "posted",
        "posting",
        "said",
        "correction",
        "corrected",
        "corrections",
        # stop / stand-down markers
        "stand",
        "down",
        "stopping",
        "stop",
        "stopped",
        "here",
        "deliberately",
        "last",
        "final",
        "record",
        "noise",
        "marker",
        "note",
        "reply",
        "replies",
        "responding",
        "further",
        "action",
        "actions",
        "nothing",
        "left",
        "remaining",
        # generic connectives / filler that carry no substance
        "the",
        "this",
        "these",
        "those",
        "that",
        "it",
        "its",
        "and",
        "or",
        "of",
        "to",
        "for",
        "with",
        "by",
        "on",
        "in",
        "at",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "an",
        "no",
        "but",
        "so",
        "then",
        "now",
        "just",
        "simply",
    }
)

# Single letters / bare digits (e.g. the "X" in "thread X", a numbered
# list marker) never make a body substantive.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _is_meta_noise(body: str) -> bool:
    """True when *body* is self-referential meta-noise with no substance.

    Normalises to lowercase tokens and refuses when EVERY token is drawn
    from the process vocabulary — i.e. the comment only references the
    act of commenting, the ticket's own threads/diff, or a stop marker.
    A single substantive token (a finding, a reason, a package name)
    makes the body deliverable content and lets it through.
    """
    tokens = {
        t for t in _TOKEN_RE.findall(body.lower()) if len(t) >= 2 and not t.isdigit()
    }
    return bool(tokens) and tokens <= _META_NOISE_WORDS


def _sanitize_log(value: str) -> str:
    """Flatten whitespace so an agent-controlled body can't forge log lines."""
    return " ".join(value.split())


def _record_refusal(svc: TicketService, ticket_id: str, note: str) -> None:
    """Record a refusal as a step event so it is observable in history.

    Step events are history entries, never comments — they must not
    pollute the comment stream the guards are protecting. Failures here
    must not crash the agent loop, so they are logged and swallowed.
    """
    try:
        svc.add_step_event(ticket_id, note)
    except Exception:
        log.debug(
            "post_comment: could not record refusal step event for %s",
            ticket_id,
            exc_info=True,
        )


def _check_post_guard(
    svc: TicketService,
    ticket_id: str,
    body: str,
    agent_name: str,
    max_comments: int,
) -> str | None:
    """Return a status string when *body* must NOT be posted, else None.

    The three deterministic guards of the tool:

    - **Content guard** — pure self-referential meta-noise is never
      posted; there is no edit-in-place API, so a refused body belongs
      in the agent's final structured summary, not the comment stream.
    - **Persisted dedupe** — an identical comment this agent already
      posted on the ticket is a duplicate even if a fresh tool closure
      (rebuilt agent / retried pass) lost the in-memory seen-set.
    - **Per-(ticket, author) comment cap** — counted against the
      PERSISTED comments (author == agent_name) so it survives closure
      rebuilds, not just the in-memory seen-set.

    Guard refusals log a warning and a step event so the recurring
    violation stays observable and the prompt can be tightened further.
    If listing comments fails, fall through (return None) — the
    in-memory set still guards same-closure retries and ``add_comment``
    reports its own error if posting is impossible.
    """
    if _is_meta_noise(body):
        log.warning(
            "post_comment: refusing self-referential meta-noise "
            "body from agent %r on %s: %r",
            agent_name,
            ticket_id,
            _sanitize_log(body),
        )
        _record_refusal(
            svc,
            ticket_id,
            f"post_comment: refused a self-referential meta-noise "
            f"body from the {agent_name} agent",
        )
        return (
            "post_comment: refused — the body is self-referential "
            "meta-noise, not deliverable content. Put it in your final "
            "structured summary instead (there is no edit-in-place API)."
        )

    try:
        existing_comments: list[Comment] = svc.list_comments(ticket_id)
    except Exception as exc:
        log.debug(
            "post_comment: dedup check failed for %s (%r); proceeding",
            ticket_id,
            exc,
        )
        return None

    for existing in existing_comments:
        if existing.author == agent_name and existing.body == body:
            return "post_comment: duplicate body in this run — skipped"

    mine = [c for c in existing_comments if c.author == agent_name]
    if len(mine) >= max_comments:
        log.warning(
            "post_comment: refusing comment %d from agent %r on "
            "%s (cap %d per (ticket, agent))",
            len(mine) + 1,
            agent_name,
            ticket_id,
            max_comments,
        )
        _record_refusal(
            svc,
            ticket_id,
            f"post_comment: the {agent_name} agent already posted "
            f"{len(mine)} comment(s); cap is {max_comments} per "
            f"(ticket, agent) — further comments refused",
        )
        return (
            f"post_comment: refused — the {agent_name} agent has "
            f"already posted {len(mine)} comment(s) on this ticket "
            f"(cap {max_comments}). Put further content in your final "
            "structured summary instead."
        )
    return None


def make_post_comment_tool(
    settings: Settings,
    agent_name: str,
    max_comments: int = 1,
):
    """Return the ``post_comment`` closure bound to *settings*.

    Args:
        settings: The application settings instance.
        agent_name: Stamped as the comment author so the originating
            agent is identifiable (e.g. ``"implement"``).
        max_comments: Per-(ticket, author) comment cap — the tool
            refuses to post once this agent has already posted this
            many comments on the ticket in the current run. Counted
            against the PERSISTED comments so the cap survives
            tool-closure rebuilds and retried passes. Default 1;
            multi-finding info tickets pass 2-3.
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

        Post the deliverable at most once — both limits are enforced,
        not just advised: the tool refuses once you have already posted
        *max_comments* comment(s) on this ticket in this run, and it
        refuses bodies that are pure self-referential meta-noise
        ("Done.", "Delivered — see diff / summary thread X", "ignore my
        previous comment", "(stop-noise marker)") or a "correction" of a
        comment you already posted — there is no edit-in-place API; put
        the corrected content in your final structured summary instead.

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

        # Deterministic guards (content, persisted dedupe, per-(ticket,
        # author) comment cap) — see ``_check_post_guard``. A non-None
        # return is the refusal/duplicate status to surface to the agent.
        refused = _check_post_guard(svc, ticket_id, body, agent_name, max_comments)
        if refused is not None:
            return refused

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
                "``reply_to_thread`` instead. Post the deliverable at "
                "most once — the tool refuses further comments once you "
                "have already posted one on this ticket, and it refuses "
                "self-referential meta-comments ('Done.', 'see "
                "diff/summary thread X', 'ignore the previous comment', "
                "'stand down') or a 'correction' of an earlier comment; "
                "corrected content goes in your final summary."
            ),
            category="reporting",
            parameters={"body": "str (Markdown comment body)"},
        )
    )

    return post_comment
