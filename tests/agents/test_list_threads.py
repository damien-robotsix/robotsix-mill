"""Dedicated tests for the list_threads tool-maker module."""

import pytest
from robotsix_mill.agents.list_threads import make_list_threads_tool
from robotsix_mill.agents.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def _clear_registry():
    ToolRegistry._tools.clear()
    yield
    ToolRegistry._tools.clear()


class FakeComment:
    """Minimal stub matching the Comment model's relevant fields."""

    def __init__(self, id, parent_id, body, closed_at=None):
        self.id = id
        self.parent_id = parent_id
        self.body = body
        self.closed_at = closed_at


def test_happy_path_lists_threads(settings, monkeypatch):
    """When current_session returns a ticket ID and list_comments returns
    a mix of top-level and child comments, the tool lists only top-level
    threads with [open]/[closed] and the first line of each body."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )

    comments = [
        FakeComment(
            id=3,
            parent_id=None,
            body="Please review the dependency updates\nDetailed notes here.",
            closed_at=None,
        ),
        FakeComment(
            id=5, parent_id=3, body="Will do!", closed_at=None
        ),  # child — should be filtered out
        FakeComment(
            id=7,
            parent_id=None,
            body="Initial design question about...",
            closed_at="2025-01-01T00:00:00Z",
        ),
    ]

    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.list_comments",
        lambda self, ticket_id: comments,
    )

    list_threads = make_list_threads_tool(settings, "test-agent")
    result = list_threads()

    assert "id=3" in result
    assert "[open]" in result
    assert "Please review the dependency updates" in result
    assert "id=7" in result
    assert "[closed]" in result
    assert "Initial design question about..." in result
    # Child comment must NOT appear
    assert "id=5" not in result
    assert "Will do!" not in result


def test_no_threads_returns_placeholder(settings, monkeypatch):
    """When list_comments returns an empty list, the tool returns
    '(no threads)'."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.list_comments",
        lambda self, ticket_id: [],
    )

    list_threads = make_list_threads_tool(settings, "test-agent")
    result = list_threads()

    assert result == "(no threads)"


def test_no_comments_returns_placeholder(settings, monkeypatch):
    """When list_comments returns only child comments (no top-level),
    the tool returns '(no threads)'."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.list_comments",
        lambda self, ticket_id: [
            FakeComment(id=2, parent_id=1, body="Reply only", closed_at=None),
        ],
    )

    list_threads = make_list_threads_tool(settings, "test-agent")
    result = list_threads()

    assert result == "(no threads)"


def test_no_session_returns_error(settings, monkeypatch):
    """When current_session() returns None, the tool returns an error
    string (no session → can't determine ticket)."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: None,
    )

    list_threads = make_list_threads_tool(settings, "test-agent")
    result = list_threads()

    assert "no active ticket session" in result.lower()


def test_service_keyerror_returns_error(settings, monkeypatch):
    """When TicketService.list_comments raises KeyError (ticket not
    found), the tool returns a formatted error string."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "nonexistent",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.list_comments",
        lambda self, ticket_id: (_ for _ in ()).throw(KeyError("nonexistent")),
    )

    list_threads = make_list_threads_tool(settings, "test-agent")
    result = list_threads()

    assert "Error:" in result
    assert "nonexistent" in result


def test_tool_registered_in_registry(settings, monkeypatch):
    """After calling make_list_threads_tool, 'list_threads' appears in
    ToolRegistry.list_tools() names."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-1",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.list_comments",
        lambda self, ticket_id: [],
    )

    make_list_threads_tool(settings, "test-agent")
    names = {t.name for t in ToolRegistry.list_tools()}
    assert "list_threads" in names


def test_long_body_truncated(settings, monkeypatch):
    """First line longer than 80 chars is truncated with '...'."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-1",
    )
    long_title = "A" * 100
    comments = [
        FakeComment(id=1, parent_id=None, body=long_title, closed_at=None),
    ]
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.list_comments",
        lambda self, ticket_id: comments,
    )

    list_threads = make_list_threads_tool(settings, "test-agent")
    result = list_threads()

    assert "A" * 77 + "..." in result
    # The full 100-char string should not appear
    assert long_title not in result


def test_empty_body_handled(settings, monkeypatch):
    """Comments with None or empty body are handled gracefully."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-1",
    )
    comments = [
        FakeComment(id=1, parent_id=None, body=None, closed_at=None),
        FakeComment(id=2, parent_id=None, body="", closed_at=None),
    ]
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.list_comments",
        lambda self, ticket_id: comments,
    )

    list_threads = make_list_threads_tool(settings, "test-agent")
    result = list_threads()

    assert "id=1" in result
    assert "id=2" in result
    # Should not crash
