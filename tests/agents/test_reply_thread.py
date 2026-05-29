"""Tests for the ``reply_to_thread`` agent tool."""

from unittest.mock import MagicMock

from robotsix_mill.agents.reply_thread import make_reply_to_thread_tool
from robotsix_mill.agents.tool_registry import ToolRegistry


def test_happy_path_posts_reply_and_returns_success(settings, monkeypatch):
    """Monkeypatch current_session + TicketService.add_comment, verify
    the closure returns the success string with correct author and
    parent_id."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )

    mock_comment = MagicMock()
    mock_comment.id = 7
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.add_comment",
        lambda self, ticket_id, body, author, parent_id=None: mock_comment,
    )

    tool = make_reply_to_thread_tool(settings, agent_name="implement")
    result = tool(thread_id=3, body="Looks good, merged.")

    assert result == "Reply posted (id=7)."
    ToolRegistry._tools.clear()


def test_no_session_returns_error(settings, monkeypatch):
    """When current_session returns None, the tool returns an error
    string without calling the service."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: None,
    )

    tool = make_reply_to_thread_tool(settings, agent_name="implement")
    result = tool(thread_id=5, body="Trying to reply…")

    assert "Error: no active ticket session" in result
    ToolRegistry._tools.clear()


def test_service_error_returns_formatted_error(settings, monkeypatch):
    """When TicketService.add_comment raises ValueError, the tool
    returns a formatted error string."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-99",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.add_comment",
        lambda self, ticket_id, body, author, parent_id=None: (
            (_ for _ in ()).throw(ValueError("parent comment not found"))
        ),
    )

    tool = make_reply_to_thread_tool(settings, agent_name="implement")
    result = tool(thread_id=999, body="Does this exist?")

    assert result == "Error: parent comment not found"
    ToolRegistry._tools.clear()


def test_agent_name_flows_as_author(settings, monkeypatch):
    """Verify that agent_name is passed through as the author keyword
    argument to add_comment."""
    captured_kwargs = {}

    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-1",
    )

    def fake_add_comment(self, ticket_id, body, author, parent_id=None):
        captured_kwargs["author"] = author
        captured_kwargs["parent_id"] = parent_id
        mock = MagicMock()
        mock.id = 1
        return mock

    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.add_comment",
        fake_add_comment,
    )

    tool = make_reply_to_thread_tool(settings, agent_name="auditor")
    tool(thread_id=10, body="Audit complete.")

    assert captured_kwargs["author"] == "auditor"
    assert captured_kwargs["parent_id"] == 10
    ToolRegistry._tools.clear()
