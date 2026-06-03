"""ProposedActionService — CRUD + approve/reject + idempotent executor.

Provides the persistence and execution layer for proposed actions
emitted by periodic agents (e.g. board-cleanup).  Every mutation
is gated on human approval: the agent proposes, a human approves
or rejects, and only then does the executor apply the change.

No API routes — this is callable programmatically; a follow-up
child ticket wires it to the HTTP layer and board UI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db
from ..config import Settings
from .models import ActionType, ProposedAction, ProposedActionStatus
from .states import State
from .service import TicketService, TransitionError


class ProposedActionService:
    """CRUD + lifecycle + execution for :class:`ProposedAction` rows.

    Depends on a :class:`TicketService` instance for the executor
    (to call ``transition``, ``mark_done``, ``create``).  Constructor
    injection follows the same pattern used by stage runners.
    """

    def __init__(self, settings: Settings, ticket_service: TicketService):
        self.settings = settings
        self.ticket_svc = ticket_service

    @property
    def board_id(self) -> str:
        return self.ticket_svc.board_id

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create_proposed_action(
        self,
        *,
        action_type: ActionType,
        target_ticket_id: str | None = None,
        proposed_title: str | None = None,
        proposed_body: str | None = None,
        rationale: str,
        source: str,
    ) -> ProposedAction:
        """Persist a new PENDING proposed action.

        Raises :class:`ValueError` when required fields for the given
        *action_type* are missing:
        - ``CLOSE_TICKET`` requires *target_ticket_id*.
        - ``CREATE_TICKET`` requires *proposed_title* and
          *proposed_body* (at least one must be non-None; body may be
          an empty string).
        """
        if action_type == ActionType.CLOSE_TICKET:
            if not target_ticket_id:
                raise ValueError(
                    "target_ticket_id is required for CLOSE_TICKET actions"
                )
        elif action_type == ActionType.CREATE_TICKET:
            if not proposed_title:
                raise ValueError(
                    "proposed_title is required for CREATE_TICKET actions"
                )
        else:
            raise ValueError(f"Unknown action_type: {action_type}")

        pa = ProposedAction(
            action_type=action_type,
            target_ticket_id=target_ticket_id,
            proposed_title=proposed_title,
            proposed_body=proposed_body,
            rationale=rationale,
            source=source,
            board_id=self.board_id,
        )
        with db.session(self.settings, self.board_id) as s:
            s.add(pa)
            s.commit()
            s.refresh(pa)
            return pa

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def list_proposed_actions(
        self,
        status: ProposedActionStatus | None = None,
        source: str | None = None,
    ) -> list[ProposedAction]:
        """Return proposed actions scoped to this board, ordered by
        ``created_at`` descending.

        Optionally filter by *status* and/or *source*.
        """
        from sqlmodel import select

        with db.session(self.settings, self.board_id) as s:
            stmt = select(ProposedAction).where(
                ProposedAction.board_id == self.board_id
            )
            if status is not None:
                stmt = stmt.where(ProposedAction.status == status)
            if source is not None:
                stmt = stmt.where(ProposedAction.source == source)
            stmt = stmt.order_by(ProposedAction.created_at.desc())
            return list(s.exec(stmt).all())

    def get_proposed_action(self, id: int) -> ProposedAction:
        """Return a single proposed action by *id*.

        Scoped to this board — raises :class:`KeyError` if the row
        does not exist or belongs to a different board.
        """
        with db.session(self.settings, self.board_id) as s:
            pa = s.get(ProposedAction, id)
            if pa is None or pa.board_id != self.board_id:
                raise KeyError(id)
            return pa

    # ------------------------------------------------------------------
    # approve / reject
    # ------------------------------------------------------------------

    def approve_proposed_action(
        self, id: int, approver_id: str
    ) -> ProposedAction:
        """Approve a PENDING proposed action.

        Sets status to ``APPROVED``, records *approver_id* and
        ``approved_at``.

        Raises :class:`TransitionError` if the action is not PENDING.
        """
        with db.session(self.settings, self.board_id) as s:
            pa = s.get(ProposedAction, id)
            if pa is None or pa.board_id != self.board_id:
                raise KeyError(id)
            if pa.status != ProposedActionStatus.PENDING:
                raise TransitionError(
                    f"ProposedAction {id}: cannot approve — "
                    f"status is {pa.status.value}, not pending"
                )
            pa.status = ProposedActionStatus.APPROVED
            pa.approved_at = datetime.now(timezone.utc)
            pa.approver_id = approver_id
            s.add(pa)
            s.commit()
            s.refresh(pa)
            return pa

    def reject_proposed_action(
        self, id: int, approver_id: str, reason: str = ""
    ) -> ProposedAction:
        """Reject a PENDING proposed action.

        Sets status to ``REJECTED``, records *approver_id* and
        ``rejected_at``.  The *reason* string is appended to the
        ``rationale`` field (preserving the original rationale).

        Raises :class:`TransitionError` if the action is not PENDING.
        """
        with db.session(self.settings, self.board_id) as s:
            pa = s.get(ProposedAction, id)
            if pa is None or pa.board_id != self.board_id:
                raise KeyError(id)
            if pa.status != ProposedActionStatus.PENDING:
                raise TransitionError(
                    f"ProposedAction {id}: cannot reject — "
                    f"status is {pa.status.value}, not pending"
                )
            pa.status = ProposedActionStatus.REJECTED
            pa.rejected_at = datetime.now(timezone.utc)
            pa.approver_id = approver_id
            if reason:
                pa.rationale = pa.rationale + f"\nRejection reason: {reason}"
            s.add(pa)
            s.commit()
            s.refresh(pa)
            return pa

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    def execute_proposed_action(self, id: int) -> ProposedAction:
        """Execute an APPROVED proposed action.

        Idempotent: if already EXECUTED, returns immediately (no-op).
        Raises :class:`TransitionError` if the action is PENDING,
        REJECTED, or in any state other than APPROVED or EXECUTED.

        Dispatch:
        - ``CLOSE_TICKET``: transition target to CLOSED, falling back
          to ``mark_done`` + ``transition(CLOSED)`` when the ticket is
          not in a directly-closeable state.
        - ``CREATE_TICKET``: call ``TicketService.create()`` with the
          proposed title/body.

        On success sets status to EXECUTED and records ``executed_at``.
        On failure records ``error_message`` and leaves status as
        APPROVED (allowing retry).  Never re-raises — the caller
        inspects ``error_message``.
        """
        # Load and validate state.
        with db.session(self.settings, self.board_id) as s:
            pa = s.get(ProposedAction, id)
            if pa is None or pa.board_id != self.board_id:
                raise KeyError(id)

            if pa.status == ProposedActionStatus.EXECUTED:
                return pa  # idempotent — already done

            if pa.status != ProposedActionStatus.APPROVED:
                raise TransitionError(
                    f"ProposedAction {id}: cannot execute — "
                    f"status is {pa.status.value}, not approved"
                )

            # Dispatch.
            try:
                if pa.action_type == ActionType.CLOSE_TICKET:
                    self._execute_close(pa)
                elif pa.action_type == ActionType.CREATE_TICKET:
                    self._execute_create(pa)
                else:
                    raise ValueError(f"Unknown action_type: {pa.action_type}")

                pa.status = ProposedActionStatus.EXECUTED
                pa.executed_at = datetime.now(timezone.utc)
                pa.error_message = None
            except Exception as exc:
                pa.error_message = str(exc)
                # Leave status = APPROVED so the operator can retry.
                # Do NOT re-raise.

            s.add(pa)
            s.commit()
            s.refresh(pa)
            return pa

    # ------------------------------------------------------------------
    # dispatch helpers
    # ------------------------------------------------------------------

    def _execute_close(self, pa: ProposedAction) -> None:
        """Close the *target_ticket_id* referenced by *pa*.

        Attempts a direct ``transition(CLOSED)`` first.  If the state
        machine rejects that (``TransitionError``), falls back to
        ``mark_done`` + ``transition(CLOSED)`` — a two-step path that
        handles tickets stuck in intermediate states (READY,
        CODE_REVIEW, etc.).
        """
        if not pa.target_ticket_id:
            raise ValueError("target_ticket_id is required for CLOSE_TICKET")

        rationale = pa.rationale or "proposed-action close"
        try:
            self.ticket_svc.transition(
                pa.target_ticket_id, State.CLOSED, note=rationale
            )
        except TransitionError:
            # Ticket isn't in a directly-closeable state — use the
            # two-step escape hatch: mark_done then transition to CLOSED.
            self.ticket_svc.mark_done(
                pa.target_ticket_id,
                note=rationale,
                author="proposed-action-executor",
            )
            self.ticket_svc.transition(
                pa.target_ticket_id, State.CLOSED, note=rationale
            )
        except KeyError:
            raise ValueError(
                f"target ticket {pa.target_ticket_id} not found"
            )

    def _execute_create(self, pa: ProposedAction) -> None:
        """Create a new ticket from the proposed title/body in *pa*."""
        if not pa.proposed_title:
            raise ValueError("proposed_title is required for CREATE_TICKET")

        self.ticket_svc.create(
            title=pa.proposed_title,
            description=pa.proposed_body or "",
            source=pa.source,
            board_id=self.board_id,
        )
