"""Unit tests for robotsix_mill.runtime.board_adapter (MillBoardAdapter).

Exercises every public method of ``MillBoardAdapter`` plus the
module-level ``_ticket`` helper (type-narrowing guard).  Covers
the ``RenderMode`` None fallback, badge logic, timestamp
formatting, and move-endpoint URL construction.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from robotsix_mill.core.models import TicketKind, TicketRead
from robotsix_mill.runtime.board_adapter import MillBoardAdapter, _COLUMNS, _ticket


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_ticket(**overrides) -> TicketRead:
    """Return a minimal, valid TicketRead with optional overrides.

    All required fields are populated with neutral defaults so tests
    only override what they care about.
    """
    data = {
        "created_at": datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 3, 2, 14, 30, 0, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return TicketRead(
        id=data.pop("id", "t-1"),
        title=data.pop("title", "Test ticket"),
        state=data.pop("state", "draft"),
        kind=data.pop("kind", TicketKind.TASK),
        branch=data.pop("branch", None),
        parent_id=data.pop("parent_id", None),
        source=data.pop("source", "user"),
        origin_session=data.pop("origin_session", None),
        origin_session_url=data.pop("origin_session_url", None),
        cost_usd=data.pop("cost_usd", 0.0),
        depends_on=data.pop("depends_on", None),
        unmet_deps=data.pop("unmet_deps", []),
        retry_attempt=data.pop("retry_attempt", 0),
        last_transient_error=data.pop("last_transient_error", None),
        next_retry_at=data.pop("next_retry_at", None),
        created_at=data.pop("created_at"),
        updated_at=data.pop("updated_at"),
        **data,
    )


# ---------------------------------------------------------------------------
# columns()
# ---------------------------------------------------------------------------


def test_columns_returns_same_keys_as_module_constant():
    adapter = MillBoardAdapter()
    cols = adapter.columns()
    assert [k for k, _ in cols] == [k for k, _ in _COLUMNS]


def test_columns_returns_list_of_tuples_of_str_str():
    adapter = MillBoardAdapter()
    cols = adapter.columns()
    assert isinstance(cols, list)
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in cols)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in cols)


def test_columns_is_a_copy_not_the_same_object():
    adapter = MillBoardAdapter()
    cols = adapter.columns()
    assert cols is not _COLUMNS
    assert cols == _COLUMNS


# ---------------------------------------------------------------------------
# card_id()
# ---------------------------------------------------------------------------


def test_card_id_returns_ticket_id():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(id="abc-123")
    assert adapter.card_id(ticket) == "abc-123"


def test_card_id_raises_typeerror_for_non_ticket():
    adapter = MillBoardAdapter()
    with pytest.raises(TypeError, match="expects TicketRead"):
        adapter.card_id(object())


def test_card_id_raises_typeerror_for_str():
    adapter = MillBoardAdapter()
    with pytest.raises(TypeError, match="expects TicketRead"):
        adapter.card_id("not a ticket")


# ---------------------------------------------------------------------------
# card_title()
# ---------------------------------------------------------------------------


def test_card_title_returns_ticket_title():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(title="Fix login bug")
    assert adapter.card_title(ticket) == "Fix login bug"


def test_card_title_raises_typeerror_for_non_ticket():
    adapter = MillBoardAdapter()
    with pytest.raises(TypeError, match="expects TicketRead"):
        adapter.card_title(None)


# ---------------------------------------------------------------------------
# card_badges()
# ---------------------------------------------------------------------------


def test_card_badges_no_priority_no_kind_no_source():
    """priority=False, kind='task', source='user' → no badges at all."""
    adapter = MillBoardAdapter()
    ticket = _make_ticket(priority=False, kind=TicketKind.TASK, source="user")
    assert adapter.card_badges(ticket) == []


def test_card_badges_priority_star():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(priority=True, kind=TicketKind.TASK, source="user")
    assert adapter.card_badges(ticket) == ["★ priority"]


def test_card_badges_kind_not_task():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(priority=False, kind=TicketKind.EPIC, source="user")
    assert adapter.card_badges(ticket) == ["epic"]


def test_card_badges_kind_empty_string_suppressed():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(priority=False, kind="", source="user")
    assert adapter.card_badges(ticket) == []


def test_card_badges_source_not_user():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(priority=False, kind=TicketKind.TASK, source="api")
    assert adapter.card_badges(ticket) == ["api"]


def test_card_badges_source_empty_string():
    """source='' is falsy → no source badge (same as 'user' suppression)."""
    adapter = MillBoardAdapter()
    ticket = _make_ticket(priority=False, kind=TicketKind.TASK, source="")
    assert adapter.card_badges(ticket) == []


def test_card_badges_all_three():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(priority=True, kind=TicketKind.EPIC, source="api")
    assert adapter.card_badges(ticket) == ["★ priority", "epic", "api"]


def test_card_badges_raises_typeerror_for_non_ticket():
    adapter = MillBoardAdapter()
    with pytest.raises(TypeError, match="expects TicketRead"):
        adapter.card_badges(42)


# ---------------------------------------------------------------------------
# card_badges() — BLOCKED state badges
# ---------------------------------------------------------------------------


def test_card_badges_blocked_waiting_on_ticket():
    """When state=blocked and unmet_deps is non-empty → waiting badge."""
    adapter = MillBoardAdapter()
    ticket = _make_ticket(state="blocked", unmet_deps=["t-2"])
    badges = adapter.card_badges(ticket)
    assert adapter.BLOCKED_WAITING in badges
    assert adapter.BLOCKED_NEEDS_HUMAN not in badges


def test_card_badges_blocked_needs_human():
    """When state=blocked and unmet_deps is empty → needs-human badge."""
    adapter = MillBoardAdapter()
    ticket = _make_ticket(state="blocked", unmet_deps=[])
    badges = adapter.card_badges(ticket)
    assert adapter.BLOCKED_NEEDS_HUMAN in badges
    assert adapter.BLOCKED_WAITING not in badges


def test_card_badges_blocked_combined_with_existing_badges():
    """Blocked badge composes correctly after priority/kind/source."""
    adapter = MillBoardAdapter()
    ticket = _make_ticket(
        priority=True,
        kind=TicketKind.EPIC,
        source="api",
        state="blocked",
        unmet_deps=["t-2"],
    )
    assert adapter.card_badges(ticket) == [
        "★ priority",
        "epic",
        "api",
        adapter.BLOCKED_WAITING,
    ]


def test_card_badges_non_blocked_state_no_blocked_badge():
    """Non-blocked states (ready/draft) never get a blocked badge."""
    adapter = MillBoardAdapter()
    for state in ("ready", "draft"):
        ticket = _make_ticket(state=state, unmet_deps=["t-2"])
        badges = adapter.card_badges(ticket)
        assert adapter.BLOCKED_WAITING not in badges
        assert adapter.BLOCKED_NEEDS_HUMAN not in badges


def test_card_badges_non_blocked_with_unmet_deps():
    """Even with unmet_deps, non-blocked states get no blocked badge."""
    adapter = MillBoardAdapter()
    ticket = _make_ticket(
        state="ready",
        kind=TicketKind.TASK,
        unmet_deps=["t-2"],
    )
    badges = adapter.card_badges(ticket)
    assert adapter.BLOCKED_WAITING not in badges
    assert adapter.BLOCKED_NEEDS_HUMAN not in badges
    # Should still have normal badges (none for kind=task, source=user)
    assert badges == []


# ---------------------------------------------------------------------------
# card_timestamps()
# ---------------------------------------------------------------------------


def test_card_timestamps_formats_yyyymmdd_hhmm():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(
        created_at=datetime(2025, 6, 12, 9, 5, 0, tzinfo=timezone.utc),
        updated_at=datetime(2025, 6, 12, 17, 42, 0, tzinfo=timezone.utc),
    )
    ts = adapter.card_timestamps(ticket)
    assert ts == {"created": "2025-06-12 09:05", "updated": "2025-06-12 17:42"}


def test_card_timestamps_midnight():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(
        created_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2025, 12, 31, 23, 59, 0, tzinfo=timezone.utc),
    )
    ts = adapter.card_timestamps(ticket)
    assert ts == {"created": "2025-01-01 00:00", "updated": "2025-12-31 23:59"}


def test_card_timestamps_raises_typeerror_for_non_ticket():
    adapter = MillBoardAdapter()
    with pytest.raises(TypeError, match="expects TicketRead"):
        adapter.card_timestamps([])


# ---------------------------------------------------------------------------
# move_endpoint()
# ---------------------------------------------------------------------------


def test_move_endpoint_returns_url_and_post():
    adapter = MillBoardAdapter()
    ticket = _make_ticket(id="move-me-999")
    url, method = adapter.move_endpoint(ticket)
    assert url == "/board/move/move-me-999/{target_status}"
    assert method == "POST"


def test_move_endpoint_raises_typeerror_for_non_ticket():
    adapter = MillBoardAdapter()
    with pytest.raises(TypeError, match="expects TicketRead"):
        adapter.move_endpoint({})


# ---------------------------------------------------------------------------
# move_endpoint_template()
# ---------------------------------------------------------------------------


def test_move_endpoint_template_is_static():
    adapter = MillBoardAdapter()
    assert adapter.move_endpoint_template() == "/board/move/{card_id}/{target_status}"


def test_move_endpoint_template_accepts_no_args():
    """Call with no arguments (card is not needed for the template)."""
    adapter = MillBoardAdapter()
    result = adapter.move_endpoint_template()
    assert "{card_id}" in result
    assert "{target_status}" in result


# ---------------------------------------------------------------------------
# render_mode()
# ---------------------------------------------------------------------------


def test_render_mode_returns_json_hydration_when_available():
    robotsix_board = pytest.importorskip("robotsix_board")
    adapter = MillBoardAdapter()
    assert adapter.render_mode() == robotsix_board.RenderMode.JSON_HYDRATION


# ---------------------------------------------------------------------------
# _ticket() helper
# ---------------------------------------------------------------------------


def test_ticket_helper_returns_same_ticketread():
    ticket = _make_ticket(id="helper-test")
    assert _ticket(ticket) is ticket


def test_ticket_helper_raises_typeerror_for_int():
    with pytest.raises(TypeError, match="expects TicketRead"):
        _ticket(123)


def test_ticket_helper_raises_typeerror_for_dict():
    with pytest.raises(TypeError, match="expects TicketRead"):
        _ticket({"id": "fake"})


def test_ticket_helper_error_message_includes_actual_type():
    with pytest.raises(TypeError) as exc:
        _ticket(3.14)
    assert "float" in str(exc.value)


# ---------------------------------------------------------------------------
# Protocol conformance: isinstance check
# ---------------------------------------------------------------------------


def test_adapter_is_runtime_checkable_boardadapter():
    """MillBoardAdapter passes isinstance against the runtime-checkable protocol."""
    robotsix_board = pytest.importorskip("robotsix_board")
    adapter = MillBoardAdapter()
    assert isinstance(adapter, robotsix_board.BoardAdapter)


def test_columns_empty_list_if_no_columns_module_constant_len():
    """Sanity: _COLUMNS is non-empty so the board has visible columns."""
    assert len(_COLUMNS) > 0
    adapter = MillBoardAdapter()
    assert len(adapter.columns()) == len(_COLUMNS)
