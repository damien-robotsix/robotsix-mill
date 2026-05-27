"""Shared pause-detection helpers for refine and implement stage runners.

These detect ``ask_user`` invocations in the agent's message history,
persist/load the conversation state for cheap resume, and reconstruct
the message history with the operator's reply appended so the agent can
continue from exactly where it paused.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..core.workspace import Workspace

log = logging.getLogger(__name__)

_SENTINEL = "__ASK_USER_PAUSE__"


def check_for_pause(conversation_state: bytes | None) -> bool:
    """Return ``True`` when the run ended with an ``ask_user`` call.

    Only the LAST message is inspected — the run halts immediately
    after ``ask_user`` returns the sentinel, so the sentinel must be
    in the most recent ``ToolReturn`` part. Scanning earlier messages
    (the previous behaviour) re-triggered on the prior turn's
    ``ask_user`` after a resume: the saved ``all_messages_json``
    still contained the old sentinel, ``check_for_pause`` returned
    ``True``, and the ticket was put right back into
    ``AWAITING_USER_REPLY`` without the agent actually asking
    anything new.

    Args:
        conversation_state: Raw JSON bytes from
            :meth:`pydantic_ai.agent.Agent.all_messages_json`, or
            ``None``.
    """
    if conversation_state is None:
        return False
    try:
        messages = json.loads(conversation_state)
    except (json.JSONDecodeError, TypeError):
        log.warning("check_for_pause: invalid JSON, treating as no-pause")
        return False
    if not messages:
        return False
    last = messages[-1]
    for part in last.get("parts", []):
        if part.get("part_kind") == "tool-return":
            content = part.get("content", "")
            if isinstance(content, str) and content == _SENTINEL:
                return True
    return False


def save_conversation_state(ws: Workspace, conversation_state: bytes) -> None:
    """Persist the full agent conversation to
    ``artifacts/conversation_state.json``."""
    path = ws.artifacts_dir / "conversation_state.json"
    path.write_bytes(conversation_state)


def load_conversation_state(ws: Workspace) -> bytes | None:
    """Read and return ``conversation_state.json`` if it exists;
    return ``None`` otherwise."""
    path = ws.artifacts_dir / "conversation_state.json"
    if not path.exists():
        return None
    return path.read_bytes()


def build_resume_message_history(
    conversation_state: bytes, reply_text: str,
) -> list:
    """Deserialize the saved message history, append a synthetic user
    message containing the operator's reply, and return the reconstructed
    ``list[ModelMessage]`` ready for ``message_history=``.

    Args:
        conversation_state: Raw JSON bytes from a prior
            ``all_messages_json()`` call.
        reply_text: The operator's answer text.
    """
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter, ModelRequest, UserPromptPart,
    )

    messages = ModelMessagesTypeAdapter.validate_json(conversation_state)
    messages.append(
        ModelRequest(parts=[
            UserPromptPart(content=f"[Operator reply]: {reply_text}"),
        ]),
    )
    return messages


def _collect_ask_user_replies(ctx, ticket) -> str:
    """Collect operator replies from every closed ``[ASK_USER]`` thread
    on *ticket*.

    For each top-level comment starting with ``[ASK_USER]`` that has
    ``closed_at IS NOT NULL``, collects all child replies (ordered by
    ``created_at``).  Returns a single formatted string suitable for
    feeding into ``build_resume_message_history``.

    When ``list_comments`` raises, returns ``"(no operator reply found)"``
    and logs a warning — this preserves the existing defensive fallback.
    """
    try:
        comments = ctx.service.list_comments(ticket.id)
    except Exception:
        log.warning(
            "%s: list_comments failed during resume, "
            "proceeding without operator reply",
            ticket.id,
        )
        return "(no operator reply found)"

    # Partition comments by parent_id for O(1) child lookup.
    children_by_parent: dict[int, list] = {}
    ask_threads = []
    for c in comments:
        if c.parent_id is None and (c.body or "").startswith("[ASK_USER]"):
            ask_threads.append(c)
        else:
            pid = c.parent_id
            if pid is not None:
                children_by_parent.setdefault(pid, []).append(c)

    # Only care about answered threads (closed ASK_USER).
    answered = [t for t in ask_threads if t.closed_at is not None]
    if not answered:
        return "(no operator reply found)"

    parts: list[str] = []
    for t in answered:
        question_snippet = (t.body or "[ASK_USER]")[9:].strip()[:80]
        replies = children_by_parent.get(t.id, [])
        if replies:
            reply_text = "; ".join(r.body for r in replies if r.body)
        else:
            reply_text = "(closed without reply)"
        parts.append(f'[Q: "{question_snippet}"]: {reply_text}')

    return "\n".join(parts)
