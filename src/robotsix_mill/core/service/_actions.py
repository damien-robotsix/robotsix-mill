"""Proposed-action surface of :class:`TicketService` (``_ActionMixin``)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlmodel import col, select

from .. import db
from ..models import (
    ActionType,
    ProposedAction,
    ProposedActionStatus,
)
from ..states import State
from ._base import _ServiceBase
from ._helpers import TransitionError, _parse_labels

log = logging.getLogger("robotsix_mill.service")


class _ActionMixin(_ServiceBase):
    """Create, approve/reject, and execute proposed actions."""

    def create_proposed_action(
        self,
        source: str,
        target_ticket_id: str,
        action_type: str,
        rationale: str,
        payload: str | None = None,
    ) -> ProposedAction | None:
        """Create a ``ProposedAction`` row with status ``PENDING``.

        Validates *action_type* against :class:`ActionType`.  On
        invalid action_type or FK violation (non-existent target
        ticket), logs a warning and returns ``None`` — never raises
        for a single bad proposal, so one failure doesn't crash the
        whole pass.
        """
        try:
            ActionType(action_type)
        except ValueError:
            log.warning(
                "create_proposed_action: invalid action_type %r — skipping",
                action_type,
            )
            return None

        try:
            with db.session(self.settings, self.board_id) as s:
                pa = ProposedAction(
                    source=source,
                    target_ticket_id=target_ticket_id,
                    action_type=ActionType(action_type),
                    payload=payload,
                    rationale=rationale,
                    status=ProposedActionStatus.PENDING,
                )
                s.add(pa)
                s.commit()
                s.refresh(pa)
        except Exception:
            log.warning(
                "create_proposed_action: failed to persist proposal "
                "(%s on %s) — target ticket may not exist",
                action_type,
                target_ticket_id,
                exc_info=True,
            )
            return None

        # Purge stale proposals AFTER the commit succeeds so a purge
        # failure cannot hide an already-committed proposal row (the
        # caller would see None and may retry, creating a duplicate).
        try:
            self._maybe_purge_stale_proposed_actions()
        except Exception:
            log.warning(
                "create_proposed_action: purge stale actions failed",
                exc_info=True,
            )
        return pa

    def approve_proposed_action(
        self, action_id: int, decided_by: str = "human"
    ) -> ProposedAction:
        """Approve a pending action and execute it.

        Transitions PENDING → APPROVED, stamps *decided_at* /
        *decided_by*, commits, then calls ``execute_proposed_action``
        (which sets EXECUTED or FAILED, captures ``failure_reason`` on
        failure, and writes audit/history notes for the mutation).

        Raises ``KeyError`` for an unknown *action_id* and
        ``ValueError`` if the action is not PENDING (including
        already-EXECUTED actions — safe to call, no double-execution).
        """
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            if action is None:
                raise KeyError(action_id)
            if action.status != ProposedActionStatus.PENDING:
                raise ValueError(
                    f"ProposedAction {action_id}: cannot approve — "
                    f"status is {action.status.value}, not PENDING"
                )
            action.status = ProposedActionStatus.APPROVED
            action.decided_at = datetime.now(timezone.utc)
            action.decided_by = decided_by
            s.add(action)
            s.commit()
            s.refresh(action)

        self.execute_proposed_action(action_id, decided_by)

        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            s.refresh(action)
            return action

    def reject_proposed_action(
        self, action_id: int, decided_by: str = "human"
    ) -> ProposedAction:
        """Reject a pending action (no execution).

        Transitions PENDING → REJECTED.  Same error semantics as
        :meth:`approve_proposed_action` (``KeyError`` for unknown id,
        ``ValueError`` if not PENDING).
        """
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            if action is None:
                raise KeyError(action_id)
            if action.status != ProposedActionStatus.PENDING:
                raise ValueError(
                    f"ProposedAction {action_id}: cannot reject — "
                    f"status is {action.status.value}, not PENDING"
                )
            action.status = ProposedActionStatus.REJECTED
            action.decided_at = datetime.now(timezone.utc)
            action.decided_by = decided_by
            s.add(action)
            s.commit()
            s.refresh(action)
            return action

    # --- proposed-action upkeep ------------------------------------------

    def _maybe_purge_stale_proposed_actions(self) -> None:
        """Purge oldest terminal-status ``ProposedAction`` rows when
        the cap is exceeded.

        Reads ``max_proposed_actions`` from settings.  If <= 0 the
        purge is disabled.  Queries all ``ProposedAction`` rows; if the
        count exceeds the cap, deletes the oldest rows in terminal
        statuses (REJECTED, EXECUTED, FAILED) until the count is within
        the cap.  PENDING and APPROVED rows are never purged — they are
        still actionable.  If the cap is exceeded but no terminal rows
        exist, logs a warning and returns without deleting anything.
        """
        max_actions = self.settings.max_proposed_actions
        if max_actions <= 0:
            return

        _TERMINAL = {
            ProposedActionStatus.REJECTED,
            ProposedActionStatus.EXECUTED,
            ProposedActionStatus.FAILED,
        }

        with db.session(self.settings, self.board_id) as s:
            total = len(s.exec(select(ProposedAction)).all())

        if total <= max_actions:
            return

        excess = total - max_actions

        with db.session(self.settings, self.board_id) as s:
            stmt = (
                select(ProposedAction)
                .where(ProposedAction.status.in_(_TERMINAL))
                .order_by(col(ProposedAction.created_at))
            )
            terminal_rows = list(s.exec(stmt).all())

        if not terminal_rows:
            log.warning(
                "_maybe_purge_stale_proposed_actions: cap %d exceeded "
                "(total %d) but no terminal-status rows exist — "
                "nothing to purge",
                max_actions,
                total,
            )
            return

        # Batch all deletes inside a single session + commit.
        to_delete_ids = [
            pa.id for pa in terminal_rows[:excess]
        ]
        if to_delete_ids:
            with db.session(self.settings, self.board_id) as s:
                for pa_id in to_delete_ids:
                    row = s.get(ProposedAction, pa_id)
                    if row is not None:
                        s.delete(row)
                s.commit()

    # --- proposed-action executor ----------------------------------------

    @staticmethod
    def _action_note(verb: str, source: str, rationale: str) -> str:
        """Format a ``TicketEvent`` note for a proposed action.

        Examples::

            "[health] closed via proposed action: stale ticket"
            "[trace-review] transitioned to ready via proposed action: …"
        """
        return f"[{source}] {verb} via proposed action: {rationale}"

    def execute_proposed_action(
        self, action_id: int, decided_by: str
    ) -> ProposedAction:
        """Execute an approved proposed action against its target ticket.

        Idempotent: calling on an already-EXECUTED or FAILED row returns
        it unchanged. Only APPROVED rows are dispatched.

        Raises :class:`KeyError` when *action_id* does not exist, and
        :class:`ValueError` when ``self.board_id`` is empty.
        """
        if not self.board_id:
            raise ValueError(
                "execute_proposed_action requires a board_id; "
                "call through a bound service instance"
            )

        # --- idempotency gate (load in a short-lived session) ---
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            if action is None:
                raise KeyError(action_id)
            if action.status != ProposedActionStatus.APPROVED:
                return action
            # Snapshot fields before closing the session.
            action_type = action.action_type
            target_id = action.target_ticket_id
            payload = action.payload
            rationale = action.rationale
            source = action.source

        # --- dispatch ---
        failure: str | None = None
        try:
            if action_type == ActionType.CLOSE:
                self._execute_close(target_id, rationale, source)
            elif action_type == ActionType.TRANSITION:
                self._execute_transition(target_id, payload, rationale, source)
            elif action_type == ActionType.COMMENT:
                self._execute_comment(target_id, rationale, source)
            elif action_type == ActionType.RELABEL:
                self._execute_relabel(target_id, payload, rationale, source)
            else:
                raise ValueError(f"unknown action type: {action_type!r}")
        except (KeyError, TransitionError, ValueError, json.JSONDecodeError) as exc:
            failure = str(exc)
        except NotImplementedError as exc:
            failure = str(exc)

        # --- persist outcome ---
        status = (
            ProposedActionStatus.FAILED if failure else ProposedActionStatus.EXECUTED
        )
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            # Double-check: the row may have been changed since our
            # first read (rare, but possible). If the status is no
            # longer APPROVED, bail out — someone else decided it.
            if action.status != ProposedActionStatus.APPROVED:
                return action
            action.status = status
            action.decided_at = datetime.now(timezone.utc)
            action.decided_by = decided_by
            action.failure_reason = failure
            s.add(action)
            s.commit()
            s.refresh(action)
            return action

    # -- dispatch helpers ------------------------------------------------

    def _execute_close(self, target_id: str, rationale: str, source: str) -> str:
        """Transition *target_id* to CLOSED with a proposed-action note."""
        self.transition(
            target_id,
            State.CLOSED,
            note=self._action_note("closed", source, rationale),
        )
        return "closed"

    def _execute_transition(
        self,
        target_id: str,
        payload: str | None,
        rationale: str,
        source: str,
    ) -> str:
        """Parse *payload* for a target state and transition *target_id*."""
        data = json.loads(payload or "{}")
        state_str = data["state"]
        dst = State(state_str)
        self.transition(
            target_id,
            dst,
            note=self._action_note(f"transitioned to {dst.value}", source, rationale),
        )
        return f"transitioned to {dst.value}"

    def _execute_comment(self, target_id: str, rationale: str, source: str) -> str:
        """Post *rationale* as a comment on *target_id* and leave a
        history breadcrumb."""
        self.add_comment(target_id, body=rationale, author=source)
        self.add_history_note(
            target_id,
            note=self._action_note("comment added", source, rationale),
        )
        return "comment added"

    def _execute_relabel(
        self,
        target_id: str,
        payload: str | None,
        rationale: str,
        source: str,
    ) -> str:
        """Apply a relabel *payload* to *target_id* and leave a history
        breadcrumb.

        Payload schema (JSON object):

        * ``set`` (optional ``list[str]``) — the ticket's labels become
          exactly this list.
        * otherwise ``add`` then ``remove`` (both optional ``list[str]``)
          are applied on top of the ticket's current labels.

        Raises :class:`ValueError` when none of ``set``/``add``/``remove``
        is present or any provided value is not a list of strings, and
        :class:`KeyError` when *target_id* is unknown.
        """
        data = json.loads(payload or "{}")

        def _as_str_list(value: object, key: str) -> list[str]:
            if not isinstance(value, list) or not all(
                isinstance(x, str) for x in value
            ):
                raise ValueError(f"relabel {key!r} must be a list of strings")
            return value

        new_labels: list[str]
        if "set" in data:
            new_labels = _as_str_list(data["set"], "set")
        elif "add" in data or "remove" in data:
            ticket = self.get(target_id)
            if ticket is None:
                raise KeyError(target_id)
            current = _parse_labels(ticket.labels)
            add = _as_str_list(data["add"], "add") if "add" in data else []
            remove = _as_str_list(data["remove"], "remove") if "remove" in data else []
            removed = set(remove)
            new_labels = [lbl for lbl in [*current, *add] if lbl not in removed]
        else:
            raise ValueError("relabel payload requires one of: set, add, remove")

        self.set_labels(target_id, new_labels)
        self.add_history_note(
            target_id,
            note=self._action_note("relabeled", source, rationale),
        )
        return f"relabeled: {new_labels}"
