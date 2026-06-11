"""Dedicated tests for the close_thread tool-maker module."""

import pytest
from robotsix_mill.agents.close_thread import make_close_thread_tool
from robotsix_mill.agents.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def _clear_registry():
    ToolRegistry._tools.clear()
    yield
    ToolRegistry._tools.clear()


def test_happy_path_closes_thread(settings, monkeypatch):
    """Monkeypatch current_session → ticket ID, TicketService.close_thread → success.
    Verify success string and ToolRegistry registration."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.close_thread",
        lambda self, comment_id, ticket_id=None: None,
    )

    close_thread = make_close_thread_tool(settings, "test-agent")
    result = close_thread(comment_id=7)

    assert result == "Thread closed (id=7)."
    names = {t.name for t in ToolRegistry.list_tools()}
    assert "close_thread" in names


def test_no_session_returns_error(settings, monkeypatch):
    """When current_session() returns None, the tool returns an error
    string (no session → can't determine ticket)."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: None,
    )

    close_thread = make_close_thread_tool(settings, "test-agent")
    result = close_thread(comment_id=7)

    assert "no active ticket session" in result.lower()


def test_service_valueerror_returns_error_string(settings, monkeypatch):
    """When TicketService.close_thread raises a ValueError for a reason
    other than 'already closed' (e.g. reply thread), the tool returns a
    formatted error string."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.close_thread",
        lambda self, comment_id, ticket_id=None: (_ for _ in ()).throw(
            ValueError("only top-level threads can be closed")
        ),
    )

    close_thread = make_close_thread_tool(settings, "test-agent")
    result = close_thread(comment_id=7)

    assert result == "Error: only top-level threads can be closed"


def test_already_closed_is_idempotent_success(settings, monkeypatch):
    """When the service raises ValueError('thread already closed'),
    the tool returns a success-like message instead of an error."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.close_thread",
        lambda self, comment_id, ticket_id=None: (_ for _ in ()).throw(
            ValueError("thread already closed")
        ),
    )

    close_thread = make_close_thread_tool(settings, "test-agent")
    result = close_thread(comment_id=7)

    assert "already closed" in result
    assert "Error" not in result
    assert "already resolved" in result


def test_toolinfo_description_mentions_idempotency(settings):
    """The close_thread ToolInfo description must mention idempotency
    so the model understands 'already closed' is success, not retry."""
    # Trigger registration by creating the tool.
    make_close_thread_tool(settings, "test-agent")
    infos = [t for t in ToolRegistry.list_tools() if t.name == "close_thread"]
    assert len(infos) == 1
    desc = infos[0].description.lower()
    assert "idempotent" in desc
    assert "already closed" in desc
    assert "do not retry" in desc


def test_service_keyerror_returns_error_string(settings, monkeypatch):
    """When TicketService.close_thread raises KeyError, the tool
    returns a formatted error string."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "ticket-42",
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.close_thread",
        lambda self, comment_id, ticket_id=None: (_ for _ in ()).throw(
            KeyError("comment 99 not found")
        ),
    )

    close_thread = make_close_thread_tool(settings, "test-agent")
    result = close_thread(comment_id=99)

    assert result == "Error: 'comment 99 not found'"
