"""BoardAdapter implementation for robotsix-mill's Ticket model.

Implements the ``robotsix_board.BoardAdapter`` Protocol so that
robotsix-board's ``render_board()`` and ``render_config_script()`` can
drive the mill kanban board.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robotsix_board import RenderMode

if TYPE_CHECKING:
    from ..core.models import TicketRead

from ..core.models import TicketKind
from ..core.states import State

# Ordered column definitions for the board (left → right).
# The LAST column is treated as the terminal/closed column by board.js.
_COLUMNS: list[tuple[str, str]] = [
    (State.EPIC_OPEN.value, "Epic Open"),
    (State.DRAFT.value, "Draft"),
    (State.HUMAN_ISSUE_APPROVAL.value, "Approval"),
    (State.READY.value, "Ready"),
    (State.DOCUMENTING.value, "Documenting"),
    (State.CODE_REVIEW.value, "Code Review"),
    (State.DELIVERABLE.value, "Deliverable"),
    (State.HUMAN_MR_APPROVAL.value, "MR Approval"),
    (State.IMPLEMENT_COMPLETE.value, "Implement Complete"),
    (State.WAITING_AUTO_MERGE.value, "Waiting Auto-Merge"),
    (State.ADDRESSING_REVIEW.value, "Addressing Review"),
    (State.REBASING.value, "Rebasing"),
    (State.FIXING_CI.value, "Fixing CI"),
    (State.DONE.value, "Done"),
    (State.CLOSED.value, "Closed"),
    (State.EPIC_CLOSED.value, "Epic Closed"),
    (State.ERRORED.value, "Errored"),
    (State.BLOCKED.value, "Blocked"),
    (State.ASKED.value, "Asked"),
    (State.ANSWERED.value, "Answered"),
    (State.AWAITING_USER_REPLY.value, "Awaiting Reply"),
]


class MillBoardAdapter:
    """BoardAdapter for mill's ``TicketRead`` objects.

    Implements the ``robotsix_board.BoardAdapter`` Protocol.
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
        if t.kind not in (TicketKind.TASK, ""):
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
