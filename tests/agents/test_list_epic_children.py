"""Tests for the read-only ``list_epic_children`` tool.

Covers the ticket spec's required cases:
- A ticket with a parent epic and >=2 siblings → all children listed with
  ids/titles/states, current ticket marked ``(this ticket)``.
- A ticket whose ``parent_id`` is ``None`` → the "no parent epic" message.
- An unknown/empty ``current_ticket_id`` → graceful not-found message.
- Read-only / never raises out: internal exceptions return an error string.
"""

from robotsix_mill.agents.list_epic_children import make_list_epic_children_tool
from robotsix_mill.core.models import TicketKind


def test_returns_callable(settings):
    tool = make_list_epic_children_tool(settings, "some-id")
    assert callable(tool)


def test_lists_siblings_and_marks_current(settings, service):
    epic = service.create("Test feature epic", "Epic body", kind=TicketKind.EPIC)
    child_a = service.create(
        "Implement the real feature", "Do the substantive work.", parent_id=epic.id
    )
    child_b = service.create(
        "Placeholder feature", "Fallback placeholder.", parent_id=epic.id
    )

    tool = make_list_epic_children_tool(settings, child_a.id)
    result = tool()

    # Both children listed with ids, titles, and states.
    assert f"`{child_a.id}`" in result
    assert f"`{child_b.id}`" in result
    assert "Implement the real feature" in result
    assert "Placeholder feature" in result
    assert "draft" in result
    # Description excerpts are rendered.
    assert "Do the substantive work." in result
    assert "Fallback placeholder." in result
    # The current ticket is marked, the sibling is not.
    assert f"`{child_a.id}` (this ticket)" in result
    assert f"`{child_b.id}` (this ticket)" not in result


def test_no_parent_returns_clear_message(settings, service):
    t = service.create("Top-level ticket", "No parent here.")
    tool = make_list_epic_children_tool(settings, t.id)
    result = tool()
    assert "no parent epic" in result
    assert "error" not in result


def test_unknown_id_returns_graceful_message(settings):
    tool = make_list_epic_children_tool(settings, "does-not-exist")
    result = tool()
    assert "no ticket found" in result

    empty_tool = make_list_epic_children_tool(settings, "")
    empty_result = empty_tool()
    assert "no ticket found" in empty_result


def test_never_raises_returns_error_string(settings, service, monkeypatch):
    epic = service.create("Epic", "body", kind=TicketKind.EPIC)
    child = service.create("Child", "body", parent_id=epic.id)

    # Force an internal failure on the read path; the closure must swallow
    # it and return an error string rather than propagating.
    import robotsix_mill.core.service as service_mod

    def _boom(self, ticket_id):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(service_mod.TicketService, "list_children", _boom)

    tool = make_list_epic_children_tool(settings, child.id)
    result = tool()
    assert result.startswith("list_epic_children: error")
