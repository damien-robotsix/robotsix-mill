"""Tests for the read-only ``read_ticket`` tool.

Covers all acceptance criteria from the ticket spec:
- AC 1: Tool is callable
- AC 2: Valid ticket returns structured Markdown
- AC 3: Nonexistent ticket returns error
- AC 4: Empty/missing ID returns error
- AC 5: No write paths reachable
- AC 6: Truncation applied
- AC 9: Never raises on failure
"""

from datetime import datetime, timezone

from robotsix_mill.agents.read_ticket import make_read_ticket_tool
from robotsix_mill.core.models import TicketEvent
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


# ---------------------------------------------------------------------------
# AC 1 — Tool is callable
# ---------------------------------------------------------------------------

def test_returns_callable(settings):
    tool = make_read_ticket_tool(settings)
    assert callable(tool)


# ---------------------------------------------------------------------------
# AC 2 — Valid ticket returns structured Markdown
# ---------------------------------------------------------------------------

def test_valid_ticket_returns_structured_markdown(settings, service):
    t = service.create("Fix auth timeout", "Increase the timeout from 5s to 30s.")
    # Add some history and a comment
    service.transition(t.id, State.READY, note="approved for implementation")
    service.transition(t.id, State.CODE_REVIEW)
    service.add_comment(t.id, "Looks good, but check edge cases.", author="reviewer")

    tool = make_read_ticket_tool(settings)
    result = tool(t.id)

    # Header
    assert "## Fix auth timeout" in result
    assert f"**ID:** `{t.id}`" in result
    assert "**State:** code_review" in result
    assert "**Kind:** task" in result
    assert "**Source:** user" in result
    assert "**Created:**" in result
    assert "**Updated:**" in result

    # Description
    assert "### Description" in result
    assert "Increase the timeout from 5s to 30s." in result

    # History
    assert "### History" in result
    assert "approved for implementation" in result

    # Comments
    assert "### Comments" in result
    assert "**reviewer**" in result
    assert "check edge cases" in result


def test_ticket_without_description_shows_placeholder(settings, service):
    t = service.create("No-desc ticket")
    tool = make_read_ticket_tool(settings)
    result = tool(t.id)
    assert "(no description)" in result


def test_ticket_without_history_shows_placeholder(settings, service, monkeypatch):
    t = service.create("No-history ticket")

    # A newly created ticket always has a "created" event; patch
    # history to return [] to exercise the defensive branch.
    monkeypatch.setattr(TicketService, "history", lambda self, tid: [])

    tool = make_read_ticket_tool(settings)
    result = tool(t.id)
    assert "(no history)" in result


def test_ticket_without_comments_shows_placeholder(settings, service):
    t = service.create("No-comments ticket")
    tool = make_read_ticket_tool(settings)
    result = tool(t.id)
    assert "(no comments)" in result


# ---------------------------------------------------------------------------
# AC 3 — Nonexistent ticket returns error
# ---------------------------------------------------------------------------

def test_nonexistent_ticket_returns_error(settings):
    tool = make_read_ticket_tool(settings)
    result = tool("nonexistent-id")
    assert "no ticket found" in result
    assert "nonexistent-id" in result


# ---------------------------------------------------------------------------
# AC 4 — Empty / missing ID returns error
# ---------------------------------------------------------------------------

def test_empty_ticket_id_returns_error(settings):
    tool = make_read_ticket_tool(settings)
    result = tool("")
    assert "non-empty ticket_id" in result.lower()


def test_whitespace_only_ticket_id_returns_error(settings):
    tool = make_read_ticket_tool(settings)
    result = tool("   \n  ")
    assert "non-empty ticket_id" in result.lower()


# ---------------------------------------------------------------------------
# AC 5 — No write paths reachable
# ---------------------------------------------------------------------------

def test_no_write_paths_reachable(settings, monkeypatch):
    """The tool closure only calls get, history, list_comments, and
    workspace().read_description().  It never touches create, transition,
    add_comment, set_branch, redraft, or any other mutating method."""
    from unittest.mock import MagicMock

    from robotsix_mill.core.models import Ticket

    called_methods: set[str] = set()

    mock_ticket = Ticket(
        id="ticket-1",
        title="Test Ticket",
        state=State.DRAFT,
        kind="task",
        source="agent",
    )
    mock_workspace = MagicMock()
    mock_workspace.read_description.return_value = "desc content"

    class SpyService:
        def __init__(self, _settings):
            pass

        def get(self, ticket_id):
            called_methods.add("get")
            if ticket_id == "ticket-1":
                return mock_ticket
            return None

        def history(self, ticket_id):
            called_methods.add("history")
            return []

        def list_comments(self, ticket_id):
            called_methods.add("list_comments")
            return []

        def workspace(self, ticket):
            called_methods.add("workspace")
            return mock_workspace

    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService",
        SpyService,
    )

    tool = make_read_ticket_tool(settings)
    result = tool("ticket-1")

    assert "Test Ticket" in result

    # Only read methods were called
    assert called_methods == {"get", "history", "list_comments", "workspace"}


# ---------------------------------------------------------------------------
# AC 6 — Truncation applied
# ---------------------------------------------------------------------------

def test_truncation_long_description(settings, service):
    """Description > 3000 chars is truncated with a marker."""
    long_desc = "Line " * 800  # ~4000 chars
    t = service.create("Long desc ticket", long_desc)

    tool = make_read_ticket_tool(settings)
    result = tool(t.id)

    assert "[truncated]" in result
    # The description section should have been truncated
    assert len(result) < 6500  # well under the soft cap + marker


def test_truncation_many_history_events(settings, service, monkeypatch):
    """When there are > 30 history events, only the last 30 are shown
    and an omission note appears."""
    t = service.create("Many-events ticket")

    # Build 35 synthetic events (more than the 30 limit)
    now = datetime.now(timezone.utc)
    events = []
    for i in range(35):
        events.append(
            TicketEvent(
                ticket_id=t.id,
                state=State.DRAFT if i < 34 else State.READY,
                note=f"event {i}",
                at=now,
            )
        )

    original_history = TicketService.history

    def fake_history(self, ticket_id):
        if ticket_id == t.id:
            return events
        return original_history(self, ticket_id)

    monkeypatch.setattr(TicketService, "history", fake_history)

    tool = make_read_ticket_tool(settings)
    result = tool(t.id)

    assert "5 earlier events omitted" in result
    # Only 30 events shown — "event 0" through "event 4" should be omitted
    assert "event 0" not in result
    assert "event 34" in result  # most recent should be shown first


def test_truncation_many_comments(settings, service):
    """When there are > 15 comments, only the last 15 are shown."""
    t = service.create("Many-comments ticket")

    for i in range(20):
        service.add_comment(t.id, f"Comment {i}", author="reviewer")

    tool = make_read_ticket_tool(settings)
    result = tool(t.id)

    assert "5 earlier comments omitted" in result
    # Oldest comments omitted, newest shown
    assert "Comment 0" not in result
    assert "Comment 19" in result


def test_overall_output_truncation(settings, service, monkeypatch):
    """When the combined output exceeds ~6000 chars, it is truncated
    at a section boundary with a marker."""
    t = service.create("Overall truncation test", "x")

    # Build enough history events to push output past 6000 chars
    now = datetime.now(timezone.utc)
    events = []
    for i in range(60):
        events.append(
            TicketEvent(
                ticket_id=t.id,
                state=State.DRAFT,
                note=f"Long history event note number {i:04d} with padding " * 3,
                at=now,
            )
        )

    original_history = TicketService.history

    def fake_history(self, ticket_id):
        if ticket_id == t.id:
            return events
        return original_history(self, ticket_id)

    monkeypatch.setattr(TicketService, "history", fake_history)

    tool = make_read_ticket_tool(settings)
    result = tool(t.id)

    # Must have the truncation marker at the end
    assert result.endswith("... [truncated]")
    # Must not exceed 6200 chars (6000 soft cap + marker overhead)
    assert len(result) < 6200


# ---------------------------------------------------------------------------
# AC 9 — Never raises
# ---------------------------------------------------------------------------

def test_never_raises_on_failure(settings, monkeypatch):
    """The tool always returns a string error, never raises."""
    tool = make_read_ticket_tool(settings)

    # Make TicketService construction itself fail
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService",
        lambda _s: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    result = tool("any-id")
    assert result.startswith("read_ticket: error reading ticket")
    assert "db down" in result.lower() or "RuntimeError" in result
