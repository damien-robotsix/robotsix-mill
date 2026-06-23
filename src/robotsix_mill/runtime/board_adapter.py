"""BoardAdapter implementation for robotsix-mill's Ticket model.

Implements the ``robotsix_board.BoardAdapter`` Protocol so that
robotsix-board's ``render_board()`` and ``render_config_script()`` can
drive the mill kanban board.
"""

from __future__ import annotations

from robotsix_board import RenderMode

from ..core.models import TicketRead

from ..core.states import State

# Ordered column definitions for the board (left → right).
# The LAST column is treated as the terminal/closed column by board.js.
_COLUMNS: list[tuple[str, str]] = [
    ("epic_open", "Epic Open"),
    ("draft", "Draft"),
    ("human_issue_approval", "Approval"),
    ("ready", "Ready"),
    ("documenting", "Documenting"),
    ("code_review", "Code Review"),
    ("deliverable", "Deliverable"),
    ("human_mr_approval", "MR Approval"),
    ("implement_complete", "Implement Complete"),
    ("waiting_auto_merge", "Waiting Auto-Merge"),
    ("addressing_review", "Addressing Review"),
    ("rebasing", "Rebasing"),
    ("fixing_ci", "Fixing CI"),
    ("maintenance", "Maintenance"),
    ("done", "Done"),
    ("closed", "Closed"),
    ("epic_closed", "Epic Closed"),
    ("errored", "Errored"),
    ("blocked", "Blocked"),
    ("asked", "Asked"),
    ("answered", "Answered"),
    ("awaiting_user_reply", "Awaiting Reply"),
]


class MillBoardAdapter:
    """BoardAdapter for mill's ``TicketRead`` objects.

    Implements the ``BoardAdapter`` Protocol when ``robotsix_board`` is
    installed.  When the library is absent, the class is still
    importable but ``render_mode()`` raises ``RuntimeError``.
    """

    BLOCKED_WAITING = "⛔ waiting on ticket"
    BLOCKED_NEEDS_HUMAN = "🙋 needs human"

    def columns(self) -> list[tuple[str, str]]:
        """Return the ordered ``(status_key, label)`` pairs for the board."""
        return list(_COLUMNS)

    def card_id(self, card: object) -> str:
        """Return the ticket id."""
        return _ticket(card).id

    def card_title(self, card: object) -> str:
        """Return the ticket title."""
        return _ticket(card).title

    def card_badges(self, card: object) -> list[str]:
        """Return badge labels: priority, kind, source, and blocked status."""
        t = _ticket(card)
        badges: list[str] = []
        if t.priority:
            badges.append("★ priority")
        if t.kind not in ("task", ""):
            badges.append(t.kind)
        # Source badge
        if t.source and t.source != "user":
            badges.append(t.source)
        # Blocked-state badges: distinguish auto-unblock vs. human-needed.
        if t.state == State.BLOCKED:
            if t.unmet_deps:
                badges.append(self.BLOCKED_WAITING)
            else:
                badges.append(self.BLOCKED_NEEDS_HUMAN)
        return badges

    def card_timestamps(self, card: object) -> dict[str, str]:
        """Return created / updated timestamps."""
        t = _ticket(card)
        return {
            "created": t.created_at.strftime("%Y-%m-%d %H:%M"),
            "updated": t.updated_at.strftime("%Y-%m-%d %H:%M"),
        }

    def move_endpoint(self, card: object) -> tuple[str, str]:
        """Return the ``(url, http_method)`` to move a card between columns."""
        t = _ticket(card)
        return (f"/board/move/{t.id}/{{target_status}}", "POST")

    def move_endpoint_template(self) -> str:
        """Return the URL template for the board config."""
        return "/board/move/{card_id}/{target_status}"

    def render_mode(self) -> RenderMode:
        """Mill uses JSON_HYDRATION (FastAPI + board.js)."""
        return RenderMode.JSON_HYDRATION


def _ticket(card: object) -> TicketRead:
    """Cast *card* to ``TicketRead``.

    The ``BoardAdapter`` Protocol accepts ``object`` so consumers can
    pass any card type.  This helper narrows to mill's concrete type.
    """
    from ..core.models import TicketRead

    if not isinstance(card, TicketRead):
        raise TypeError(
            f"MillBoardAdapter expects TicketRead, got {type(card).__name__}"
        )
    return card
