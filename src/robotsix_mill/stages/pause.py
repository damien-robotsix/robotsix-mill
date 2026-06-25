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

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from ..core.workspace import Workspace

log = logging.getLogger(__name__)

_SENTINEL = "__ASK_USER_PAUSE__"


def check_for_pause(new_messages: bytes | None) -> bool:
    """Return ``True`` when *new_messages* contains an ``ask_user``
    tool-return carrying the pause sentinel.

    *new_messages* MUST be ``result.new_messages_json()`` — the bytes
    pydantic-ai produces for messages added during the current run
    only. Passing the full ``all_messages_json()`` re-triggers on the
    prior turn's ``ask_user`` after a resume (the saved transcript
    keeps the old sentinel forever) and was the source of the
    "ticket re-pauses without a new question" bug fixed in 61a9709.

    Scanning *every* tool-return in the new messages — not just the
    last — is necessary because ``ask_user`` does NOT actually halt
    the agent: pydantic-ai treats the sentinel as a normal tool
    return and the model keeps running, often producing a structured
    output before stopping. The final message is the model's
    response, not the sentinel; only an earlier tool-return carries
    the marker we look for.

    Args:
        new_messages: Raw JSON bytes from
            :meth:`pydantic_ai.agent.AgentRunResult.new_messages_json`,
            or ``None``.
    """
    if new_messages is None:
        return False
    try:
        messages = json.loads(new_messages)
    except json.JSONDecodeError, TypeError:
        log.warning("check_for_pause: invalid JSON, treating as no-pause")
        return False
    for msg in messages:
        for part in msg.get("parts", []):
            if part.get("part_kind") == "tool-return":
                content = part.get("content", "")
                if isinstance(content, str) and content == _SENTINEL:
                    return True
    return False


def _state_path(ws: Workspace, stage_name: str) -> Path:
    return ws.artifacts_dir / f"{stage_name}_conversation_state.json"


def save_conversation_state(
    ws: Workspace,
    conversation_state: bytes,
    stage_name: str,
) -> None:
    """Persist the full agent conversation to
    ``artifacts/{stage_name}_conversation_state.json``.

    *stage_name* (``"refine"`` / ``"implement"``) namespaces the file so
    a refine pause never gets loaded as an implement resume context — a
    cross-stage contamination that masqueraded as "implement BLOCKED
    with no changes produced" before 6f7f-2026-05-29.
    """
    _state_path(ws, stage_name).write_bytes(conversation_state)


def load_conversation_state(
    ws: Workspace,
    stage_name: str,
) -> bytes | None:
    """Read and return ``{stage_name}_conversation_state.json`` if it
    exists; return ``None`` otherwise."""
    path = _state_path(ws, stage_name)
    if not path.exists():
        return None
    return path.read_bytes()


def clear_conversation_state(ws: Workspace, stage_name: str) -> None:
    """Remove the saved state file for *stage_name* if present.

    Called when a stage exits the AWAITING_USER_REPLY loop in a way that
    won't lead to a resume — e.g. when the operator's reply has been
    consumed and the run completed normally, so the next stage shouldn't
    pick up the stale file.
    """
    path = _state_path(ws, stage_name)
    if path.exists():
        path.unlink()


def build_resume_message_history(
    conversation_state: bytes,
    reply_text: str,
) -> list:
    """Deserialize the saved message history, append a synthetic user
    message containing the operator's reply, and return the reconstructed
    ``list[ModelMessage]`` ready for ``message_history=``.

    Args:
        conversation_state: Raw JSON bytes from a prior
            ``all_messages_json()`` call.
        reply_text: The operator's answer text.
    """
    messages = ModelMessagesTypeAdapter.validate_json(conversation_state)
    messages.append(
        ModelRequest(
            parts=[
                UserPromptPart(content=f"[Operator reply]: {reply_text}"),
            ]
        ),
    )
    return messages


def build_compact_resume_message_history(
    saved_state: bytes,
    reply_text: str,
    *,
    git_stat: str | None = None,
) -> list:
    """Build a compact 3-message resume history from the prior session.

    Unlike :func:`build_resume_message_history` — which replays the
    entire prior transcript — this helper extracts only the last
    assistant text summary and constructs a fresh synthetic
    ``message_history`` that is small and safe to re-upload.

    Args:
        saved_state: Raw JSON bytes from a prior
            ``all_messages_json()`` call.
        reply_text: The operator's answer text.
        git_stat: Optional ``git diff --stat HEAD`` output from the
            workspace repo, injected into the synthetic assistant
            acknowledgment.

    Returns:
        A ``list[ModelMessage]`` of exactly 3 messages.
    """
    # 1. Deserialize the full prior transcript.
    messages: list = ModelMessagesTypeAdapter.validate_json(saved_state)

    # 2. Find the last ModelResponse with at least one TextPart.
    prior_summary = "(prior session contained no text summary)"
    for m in reversed(messages):
        if getattr(m, "kind", None) == "response":
            text_parts = [
                p
                for p in getattr(m, "parts", [])
                if getattr(p, "part_kind", None) == "text"
            ]
            if text_parts:
                prior_summary = "\n".join(
                    p.content for p in text_parts if hasattr(p, "content")
                )
                break

    # 3. Build exactly three messages.
    return [
        ModelRequest(
            parts=[
                UserPromptPart(content="Context from previous session:"),
            ]
        ),
        ModelResponse(
            parts=[
                TextPart(
                    content=(
                        f"Prior session summary:\n{prior_summary}\n\n"
                        f"Modified files during that session:\n"
                        f"{git_stat or '(unavailable)'}"
                    )
                ),
            ]
        ),
        ModelRequest(
            parts=[
                UserPromptPart(content=f"[Operator reply]: {reply_text}"),
            ]
        ),
    ]


def acknowledge_unanswered_threads(ctx, ticket, thread_ids: set[int]) -> None:
    """Post-agent verification: ensure every reviewer comment thread is
    either replied to or closed.

    This is a deterministic safety net — the refine/implement agent
    system prompts instruct them to reply-to and close threads, but if
    the LLM forgets, this function catches it so no comment thread
    remains open indefinitely.

    For each *thread_id*:

    - Already closed → no-op.
    - Open with child replies → ``close_thread`` (agent replied but
      forgot to close).
    - Open with no child replies → ``add_comment("Addressed.", parent_id=...)``
      then ``close_thread`` (agent neither replied nor closed).

    Only threads in *thread_ids* are touched — threads already closed
    before the agent ran or created by the agent during its run are
    never auto-closed.

    If ``list_comments`` raises, logs a warning and returns (defensive
    fallback — never block the pipeline).
    """
    if not thread_ids:
        return

    try:
        comments = ctx.service.list_comments(ticket.id)
    except Exception:
        log.warning(
            "%s: list_comments failed during thread acknowledgment, "
            "skipping — threads may remain open",
            ticket.id,
        )
        return

    # Index by id for O(1) lookups.
    comments_by_id: dict[int, object] = {c.id: c for c in comments}

    # Index children by parent_id.
    children_by_parent: dict[int, list] = {}
    for c in comments:
        if c.parent_id is not None:
            children_by_parent.setdefault(c.parent_id, []).append(c)

    already_closed = 0
    closed_with_reply = 0
    default_ack = 0

    for tid in thread_ids:
        top = comments_by_id.get(tid)
        if top is None:
            continue  # vanished — shouldn't happen, but safe
        if top.closed_at is not None:
            already_closed += 1
            continue
        if children_by_parent.get(tid):
            # Agent replied → just close.
            try:
                ctx.service.close_thread(tid)
                closed_with_reply += 1
            except Exception:
                log.warning(
                    "%s: close_thread(%d) failed during acknowledgment",
                    ticket.id,
                    tid,
                )
        else:
            # No reply → post "Addressed." and close.
            try:
                ctx.service.add_comment(
                    ticket.id,
                    "Addressed.",
                    parent_id=tid,
                )
            except Exception:
                log.warning(
                    "%s: add_comment for thread %d failed during acknowledgment",
                    ticket.id,
                    tid,
                )
            try:
                ctx.service.close_thread(tid)
                default_ack += 1
            except Exception:
                log.warning(
                    "%s: close_thread(%d) failed during acknowledgment",
                    ticket.id,
                    tid,
                )

    if already_closed or closed_with_reply or default_ack:
        log.info(
            "%s: thread acknowledgment — %d already closed, "
            "%d closed (agent replied), %d auto-acknowledged",
            ticket.id,
            already_closed,
            closed_with_reply,
            default_ack,
        )
    else:
        log.debug(
            "%s: thread acknowledgment — no threads to handle",
            ticket.id,
        )


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
            "%s: list_comments failed during resume, proceeding without operator reply",
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
