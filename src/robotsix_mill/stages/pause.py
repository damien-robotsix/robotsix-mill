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
    """Return ``True`` if the serialised message history contains a
    ``ToolReturn`` part whose content is the ``ask_user`` sentinel.

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
    for msg in messages:
        for part in msg.get("parts", []):
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
