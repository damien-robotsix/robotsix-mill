"""Tests for ``compress_history`` in ``coordinating.py``.

Covers all five acceptance criteria from the parent ticket
``20260528T071800Z-add-token-aware-observation-compression--a636``:

(a) under-budget histories returned unchanged (identity fast-path)
(b) messages dropped from the front when over budget
(c) last N messages preserved unconditionally via ``keep_last``
(d) surviving messages never mutated (identity + field equality)
(e) char/4 estimation includes ``ToolReturnPart.content`` and
    ``ToolCallPart.args``
"""

from __future__ import annotations

import dataclasses
import json
import types
from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from robotsix_mill.agents.coordinating import compress_history

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_FIXED_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _add_json(msg):
    """Monkey-patch ``.json()`` onto a dataclass message instance.

    ``ModelRequest`` / ``ModelResponse`` are plain typed dataclasses,
    not Pydantic BaseModels — they lack ``.json()`` at the type level.
    At runtime the objects that ``Agent.run_sync`` returns *do* carry a
    ``.json()``, so we simulate that here via ``dataclasses.asdict`` +
    ``json.dumps``.
    """

    def _json(self):
        return json.dumps(dataclasses.asdict(self), default=str)

    msg.json = types.MethodType(_json, msg)
    return msg


def _mk_request(*parts):
    """Create a ``ModelRequest`` carrying *parts* with ``.json()`` attached."""
    return _add_json(ModelRequest(parts=list(parts), timestamp=_FIXED_TS))


def _mk_response(*parts):
    """Create a ``ModelResponse`` carrying *parts* with ``.json()`` attached."""
    return _add_json(ModelResponse(parts=list(parts), timestamp=_FIXED_TS))


def _mk_user_prompt(content="Hello"):
    return UserPromptPart(content=content, timestamp=_FIXED_TS)


def _mk_tool_return(
    tool_name="cat",
    content="meow",
    tool_call_id="call_1",
):
    return ToolReturnPart(
        tool_name=tool_name,
        content=content,
        tool_call_id=tool_call_id,
        timestamp=_FIXED_TS,
    )


def _mk_tool_call(
    tool_name="cat",
    args=None,
    tool_call_id="call_1",
):
    if args is None:
        args = {"path": "a.txt"}
    return ToolCallPart(
        tool_name=tool_name,
        args=args,
        tool_call_id=tool_call_id,
    )


def _est(msg):
    """Return the char/4 estimate for a single message (for assertions)."""
    return len(msg.json()) // 4


def _total_est(*msgs):
    """Return the char/4 estimate for a sequence of messages."""
    return sum(_est(m) for m in msgs)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestCompressHistory:
    # -- (a) under-budget fast paths -----------------------------------

    def test_empty_history_returns_empty_list(self):
        assert compress_history([], history_max_tokens=100, history_keep_last=2) == []

    def test_max_tokens_zero_returns_unchanged(self):
        msg = _mk_request(_mk_user_prompt("hi"))
        history = [msg]
        result = compress_history(history, history_max_tokens=0, history_keep_last=0)
        assert result is history

    def test_max_tokens_negative_returns_unchanged(self):
        msg = _mk_request(_mk_user_prompt("hi"))
        history = [msg]
        result = compress_history(history, history_max_tokens=-1, history_keep_last=0)
        assert result is history

    def test_under_budget_returns_same_list(self):
        """When total estimate ≤ budget the *same list object* is returned."""
        msgs = [
            _mk_request(_mk_user_prompt("short")),
            _mk_response(_mk_tool_call(args={"path": "x"})),
        ]
        budget = _total_est(*msgs) + 10  # well above
        result = compress_history(msgs, history_max_tokens=budget, history_keep_last=2)
        assert result is msgs

    def test_exactly_at_budget_returns_unchanged(self):
        """Exactly at budget (total_est == budget) returns unchanged."""
        msgs = [
            _mk_request(_mk_user_prompt("a")),
        ]
        budget = _total_est(*msgs)
        result = compress_history(msgs, history_max_tokens=budget, history_keep_last=0)
        assert result is msgs

    # -- (b) over-budget drops from front -----------------------------

    def test_over_budget_drops_from_front(self):
        """Messages are dropped from the front (oldest first)."""
        msgs = [
            _mk_request(_mk_user_prompt("X" * 200)),  # large
            _mk_response(_mk_tool_call(args={"path": "x"})),
            _mk_request(_mk_tool_return(content="Y" * 200)),  # large
            _mk_response(_mk_tool_call(args={"path": "y"})),
        ]
        # Budget: only enough for the last 2 messages.
        budget = _total_est(msgs[2], msgs[3])
        result = compress_history(msgs, history_max_tokens=budget, history_keep_last=0)
        assert len(result) == 2
        assert result[0] is msgs[2]
        assert result[1] is msgs[3]

    def test_over_budget_drops_minimal_messages(self):
        """Drops the *minimum* number of messages to get under budget."""
        small = _mk_request(_mk_user_prompt("s"))
        large = _mk_request(_mk_user_prompt("L" * 500))
        msgs = [large, small, small, small]
        # Budget: enough for the last 3 small messages but NOT the large one.
        budget = _total_est(small, small, small)
        result = compress_history(msgs, history_max_tokens=budget, history_keep_last=0)
        assert len(result) == 3
        assert result[0] is msgs[1]
        assert result[1] is msgs[2]
        assert result[2] is msgs[3]

    # -- (c) last N messages preserved ---------------------------------

    def test_keep_last_zero_allows_all_dropped(self):
        """With keep_last=0, the loop may drop every message if that
        finally satisfies the budget.  The fallback
        ``message_history[-1:]`` only fires when the budget is *still*
        exceeded after dropping everything except the keep-last tail."""
        msgs = [
            _mk_request(_mk_user_prompt("X" * 200)),
            _mk_request(_mk_user_prompt("Y" * 200)),
        ]
        # Dropping both messages brings estimate to 0, which is ≤ 1.
        result = compress_history(msgs, history_max_tokens=1, history_keep_last=0)
        assert result == []

    def test_keep_last_preserves_tail_unconditionally(self):
        """The last ``keep_last`` messages survive even when way over budget."""
        msgs = [
            _mk_request(_mk_user_prompt("A" * 500)),
            _mk_request(_mk_user_prompt("B" * 500)),
            _mk_request(_mk_user_prompt("C" * 500)),
            _mk_request(_mk_user_prompt("keeper1")),
            _mk_request(_mk_user_prompt("keeper2")),
            _mk_request(_mk_user_prompt("keeper3")),
        ]
        # Budget is tiny — far below even one large message.
        result = compress_history(msgs, history_max_tokens=1, history_keep_last=3)
        assert len(result) == 3
        assert result[0] is msgs[3]
        assert result[1] is msgs[4]
        assert result[2] is msgs[5]

    def test_keep_last_greater_than_history(self):
        """When keep_last > len(history), all messages survive."""
        msgs = [
            _mk_request(_mk_user_prompt("hi")),
            _mk_request(_mk_user_prompt("there")),
        ]
        result = compress_history(msgs, history_max_tokens=1, history_keep_last=10)
        # The loop: for i in range(2 - 2) = range(0) → no iterations
        # Falls through to: return message_history[-10:] → all 2
        assert len(result) == 2
        assert result == msgs

    # -- (d) surviving messages never modified -------------------------

    def test_surviving_messages_are_same_objects(self):
        """Messages that survive compression are the *exact same objects*."""
        msgs = [
            _mk_request(_mk_user_prompt("drop me " + "X" * 300)),
            _mk_request(_mk_user_prompt("keep me")),
            _mk_response(_mk_tool_call(args={"path": "z"})),
        ]
        budget = _total_est(msgs[1], msgs[2])
        result = compress_history(msgs, history_max_tokens=budget, history_keep_last=2)
        assert len(result) == 2
        assert result[0] is msgs[1]
        assert result[1] is msgs[2]

    def test_surviving_message_fields_unchanged(self):
        """Every field on surviving messages matches the original."""
        msgs = [
            _mk_request(_mk_user_prompt("drop " + "X" * 300)),
            _mk_response(_mk_tool_call(args={"path": "important.py", "offset": 1})),
            _mk_request(
                _mk_tool_return(content="result contents", tool_call_id="call_z")
            ),
        ]
        budget = _total_est(msgs[1], msgs[2])
        result = compress_history(msgs, history_max_tokens=budget, history_keep_last=2)
        assert len(result) == 2
        # Compare asdict representations — deep equality.
        assert dataclasses.asdict(result[0]) == dataclasses.asdict(msgs[1])
        assert dataclasses.asdict(result[1]) == dataclasses.asdict(msgs[2])

    # -- (e) estimation includes content & args ------------------------

    def test_estimation_includes_tool_return_content(self):
        """Larger ``ToolReturnPart.content`` increases the char/4 estimate."""
        small = _mk_request(_mk_tool_return(content="x"))
        large = _mk_request(_mk_tool_return(content="x" * 500))
        assert _est(large) > _est(small)

        # Put small first, large second; tight budget drops only small.
        history = [small, large]
        # Budget: just enough for the large message.
        budget = _est(large)
        result = compress_history(
            history, history_max_tokens=budget, history_keep_last=0
        )
        assert len(result) == 1
        assert result[0] is large

    def test_estimation_includes_tool_call_args(self):
        """Larger ``ToolCallPart.args`` increases the char/4 estimate."""
        small = _mk_response(_mk_tool_call(args={"p": "x"}))
        large = _mk_response(_mk_tool_call(args={"path": "x" * 400}))
        assert _est(large) > _est(small)

        # Put small first, large second; tight budget drops only small.
        history = [small, large]
        budget = _est(large)
        result = compress_history(
            history, history_max_tokens=budget, history_keep_last=0
        )
        assert len(result) == 1
        assert result[0] is large

    def test_estimation_sums_both_content_and_args(self):
        """A mixed history's estimate reflects both ToolReturnPart content
        and ToolCallPart args."""
        req = _mk_request(_mk_tool_return(content="Z" * 200))
        resp = _mk_response(_mk_tool_call(args={"path": "Y" * 200}))
        history = [req, resp]

        # Estimate from both messages combined.
        combined = _total_est(req, resp)
        assert combined > 0
        # Budget = combined → no drop.
        result = compress_history(
            history, history_max_tokens=combined, history_keep_last=2
        )
        assert result is history

        # Budget = combined - 1 → one message dropped from front.
        result = compress_history(
            history, history_max_tokens=combined - 1, history_keep_last=0
        )
        assert len(result) == 1
        assert result[0] is resp
