"""Tests for stages.pause helpers.

Contract: ``check_for_pause`` receives the raw bytes of pydantic-ai's
``new_messages_json()`` — only messages added during the current run.
ANY tool-return carrying the ask_user sentinel in those bytes means
the agent paused. Scanning the FULL transcript (``all_messages_json``)
was the source of the "ticket re-pauses after resume" bug; scanning
only the last message was the source of the "ticket lands in
HUMAN_ISSUE_APPROVAL instead of AWAITING_USER_REPLY" bug
(ask_user doesn't actually halt the agent, so the model usually emits
a structured-output reply afterwards and the sentinel sits in an
earlier tool-return).
"""

from __future__ import annotations

import json

from robotsix_mill.stages.pause import (
    _SENTINEL, check_for_pause,
)


def _msg(role: str, parts: list[dict]) -> dict:
    """Minimal serialized pydantic-ai message."""
    return {"role": role, "kind": "request" if role == "request" else "response", "parts": parts}


def _tool_return_part(content: str, tool_call_id: str = "x") -> dict:
    return {
        "part_kind": "tool-return",
        "tool_name": "ask_user",
        "content": content,
        "tool_call_id": tool_call_id,
    }


def _text_part(content: str) -> dict:
    return {"part_kind": "text", "content": content}


def test_no_state_returns_false():
    assert check_for_pause(None) is False
    assert check_for_pause(b"") is False


def test_invalid_json_returns_false():
    assert check_for_pause(b"not json") is False


def test_sentinel_in_last_message_returns_true():
    """Last message contains the sentinel → fresh pause → True."""
    msgs = [
        _msg("response", [_text_part("planning")]),
        _msg("request", [_tool_return_part(_SENTINEL, "ask-1")]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is True


def test_sentinel_followed_by_text_response_still_triggers_pause():
    """ask_user does NOT actually halt the agent — pydantic-ai treats
    the sentinel as a normal tool return, so the model typically
    emits a text/structured-output response after. The sentinel ends
    up in an *earlier* tool-return, not the last message. Since the
    caller scopes the bytes to THIS run's new_messages_json(), any
    sentinel in those bytes is a legitimate pause."""
    msgs = [
        _msg("response", [_text_part("planning")]),
        # Tool call + sentinel return from this run.
        _msg("request", [_tool_return_part(_SENTINEL, "ask-1")]),
        # Model's text reply after the sentinel — not a halt.
        _msg("response", [_text_part("Awaiting reply.")]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is True


def test_non_sentinel_tool_return_in_last_message_returns_false():
    """A non-ask_user tool return in the last message → no pause."""
    msgs = [
        _msg("request", [{
            "part_kind": "tool-return",
            "tool_name": "read_file",
            "content": "file content",
            "tool_call_id": "x",
        }]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is False


def test_empty_messages_returns_false():
    assert check_for_pause(b"[]") is False


def test_sentinel_in_buried_tool_return_with_trailing_response():
    """Regression for the HUMAN_ISSUE_APPROVAL-instead-of-AWAITING_USER_REPLY
    bug. pydantic-ai's ask_user tool returns the sentinel string but
    does NOT halt the agent — the model then emits a structured-output
    response. The sentinel is in an earlier tool-return; the last
    message is the model's text reply. Scanning ALL tool returns in
    this run's new_messages still catches it."""
    msgs = [
        _msg("response", [
            {"part_kind": "tool-call", "tool_name": "ask_user",
             "args": {"question": "..."}, "tool_call_id": "ask-1"},
        ]),
        _msg("request", [_tool_return_part(_SENTINEL, "ask-1")]),
        _msg("response", [_text_part(
            '{"summary": "asked the user", "updated_memory": ""}'
        )]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is True


def test_sentinel_in_both_old_and_new_returns_true():
    """If the run re-paused with ANOTHER ask_user, the last
    message also carries the sentinel — that's a legitimate pause
    and must trigger."""
    msgs = [
        _msg("request", [_tool_return_part(_SENTINEL, "ask-1")]),
        _msg("request", [{"part_kind": "user-prompt", "content": "reply"}]),
        _msg("response", [_text_part("ok one more")]),
        _msg("request", [_tool_return_part(_SENTINEL, "ask-2")]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is True
