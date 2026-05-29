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
from unittest.mock import MagicMock, call, patch

import pytest

from robotsix_mill.core.workspace import Workspace
from robotsix_mill.stages.pause import (
    _SENTINEL, acknowledge_unanswered_threads, check_for_pause,
    clear_conversation_state, load_conversation_state,
    save_conversation_state,
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


# ---------------------------------------------------------------------------
# acknowledge_unanswered_threads
# ---------------------------------------------------------------------------

def _make_comment(
    id: int,
    closed_at=None,
    parent_id=None,
    body="review comment",
) -> MagicMock:
    c = MagicMock()
    c.id = id
    c.closed_at = closed_at
    c.parent_id = parent_id
    c.body = body
    return c


def test_acknowledge_empty_thread_ids_is_noop():
    ctx = MagicMock()
    ticket = MagicMock()
    acknowledge_unanswered_threads(ctx, ticket, set())
    ctx.service.list_comments.assert_not_called()


def test_acknowledge_already_closed_thread_is_noop():
    ctx = MagicMock()
    ticket = MagicMock()
    closed_thread = _make_comment(1, closed_at="2025-01-01")
    ctx.service.list_comments.return_value = [closed_thread]

    acknowledge_unanswered_threads(ctx, ticket, {1})

    ctx.service.close_thread.assert_not_called()
    ctx.service.add_comment.assert_not_called()


def test_acknowledge_open_thread_with_child_reply_closes_it():
    ctx = MagicMock()
    ticket = MagicMock()
    top = _make_comment(1, closed_at=None, parent_id=None)
    child = _make_comment(2, parent_id=1)
    ctx.service.list_comments.return_value = [top, child]

    acknowledge_unanswered_threads(ctx, ticket, {1})

    ctx.service.close_thread.assert_called_once_with(1)
    ctx.service.add_comment.assert_not_called()


def test_acknowledge_open_thread_no_child_reply_adds_ack_and_closes():
    ctx = MagicMock()
    ticket = MagicMock()
    top = _make_comment(1, closed_at=None, parent_id=None)
    ctx.service.list_comments.return_value = [top]

    acknowledge_unanswered_threads(ctx, ticket, {1})

    ctx.service.add_comment.assert_called_once_with(
        ticket.id, "Addressed.", parent_id=1,
    )
    ctx.service.close_thread.assert_called_once_with(1)


def test_acknowledge_list_comments_raises_logs_warning():
    ctx = MagicMock()
    ticket = MagicMock()
    ctx.service.list_comments.side_effect = RuntimeError("boom")

    with patch("logging.Logger.warning") as mock_warn:
        acknowledge_unanswered_threads(ctx, ticket, {1})

    mock_warn.assert_called_once()
    ctx.service.close_thread.assert_not_called()
    ctx.service.add_comment.assert_not_called()


def test_acknowledge_mixed_threads():
    """Thread 1: already closed. Thread 2: open with reply. Thread 3: open no reply."""
    ctx = MagicMock()
    ticket = MagicMock()
    t1 = _make_comment(1, closed_at="2025-01-01", parent_id=None)
    t2 = _make_comment(2, closed_at=None, parent_id=None)
    t3 = _make_comment(3, closed_at=None, parent_id=None)
    child2 = _make_comment(10, parent_id=2)
    ctx.service.list_comments.return_value = [t1, t2, t3, child2]

    acknowledge_unanswered_threads(ctx, ticket, {1, 2, 3})

    # Thread 1: already closed — no touch.
    # Thread 2: open with reply → close only.
    ctx.service.close_thread.assert_has_calls([call(2), call(3)], any_order=True)
    # Thread 3: open no reply → add_comment + close.
    ctx.service.add_comment.assert_called_once_with(
        ticket.id, "Addressed.", parent_id=3,
    )


def test_acknowledge_thread_not_in_list_comments_skipped():
    """thread_ids references an id that list_comments doesn't return."""
    ctx = MagicMock()
    ticket = MagicMock()
    ctx.service.list_comments.return_value = []

    acknowledge_unanswered_threads(ctx, ticket, {999})

    ctx.service.close_thread.assert_not_called()
    ctx.service.add_comment.assert_not_called()


def test_save_load_namespaced_per_stage(tmp_path):
    """Refine's saved state must NOT be visible under the implement
    namespace and vice versa — the cross-stage contamination is the
    bug this namespace exists to prevent."""
    ws = Workspace(tmp_path / "workspaces", "t-state")
    save_conversation_state(ws, b"refine-bytes", "refine")
    assert load_conversation_state(ws, "refine") == b"refine-bytes"
    assert load_conversation_state(ws, "implement") is None

    save_conversation_state(ws, b"implement-bytes", "implement")
    assert load_conversation_state(ws, "implement") == b"implement-bytes"
    assert load_conversation_state(ws, "refine") == b"refine-bytes"


def test_clear_conversation_state_removes_file(tmp_path):
    ws = Workspace(tmp_path / "workspaces", "t-clear")
    save_conversation_state(ws, b"x", "refine")
    assert (ws.artifacts_dir / "refine_conversation_state.json").exists()
    clear_conversation_state(ws, "refine")
    assert not (ws.artifacts_dir / "refine_conversation_state.json").exists()
    # Clearing again is a no-op.
    clear_conversation_state(ws, "refine")


def test_clear_does_not_touch_other_stage(tmp_path):
    ws = Workspace(tmp_path / "workspaces", "t-clear-iso")
    save_conversation_state(ws, b"r", "refine")
    save_conversation_state(ws, b"i", "implement")
    clear_conversation_state(ws, "refine")
    assert load_conversation_state(ws, "refine") is None
    assert load_conversation_state(ws, "implement") == b"i"


def test_acknowledge_thread_ids_subset_only_touches_specified():
    """Only threads in thread_ids are touched, not others that happen to be open."""
    ctx = MagicMock()
    ticket = MagicMock()
    t1 = _make_comment(1, closed_at=None, parent_id=None)
    t2 = _make_comment(2, closed_at=None, parent_id=None)  # not in thread_ids
    ctx.service.list_comments.return_value = [t1, t2]

    acknowledge_unanswered_threads(ctx, ticket, {1})

    ctx.service.close_thread.assert_called_once_with(1)
    ctx.service.add_comment.assert_called_once_with(
        ticket.id, "Addressed.", parent_id=1,
    )
    # t2 was NOT touched.
    assert call(2) not in ctx.service.close_thread.call_args_list
