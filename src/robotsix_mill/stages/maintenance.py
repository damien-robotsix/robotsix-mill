"""Maintenance stage: MAINTENANCE -> DONE (or BLOCKED, resumable).

Runs the maintenance agent to perform operational actions (create repo,
fork repo, cross-repo investigation) directly, skipping the
code-implement stage.
"""

from __future__ import annotations

import logging

from .base import Outcome, Stage, StageContext
from ..core.models import Ticket
from ..core.states import State

log = logging.getLogger(__name__)


class MaintenanceStage(Stage):
    """Run the maintenance agent to perform operational actions (create
    repo, fork repo, cross-repo investigation) and transition directly
    to DONE."""

    name = "maintenance"
    input_state = State.MAINTENANCE
    # traced defaults to True (runs an LLM agent)

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Instantiate and run the maintenance agent.

        On success returns ``Outcome(State.DONE)``.
        On escalation returns ``Outcome(State.BLOCKED, note=...)``.
        Unhandled exceptions are caught by the worker and route to
        ``ERRORED``.
        """
        # Lazy import — the maintenance agent module is created in a
        # follow-up ticket.  When it doesn't exist yet this raises
        # ImportError (which the worker surfaces as ERRORED).
        from ..agents.maintenance import run_maintenance_agent

        log.info("Running maintenance agent for %s", ticket.id)
        result = run_maintenance_agent(ticket, ctx)

        # Migration takes precedence — the investigation concluded the
        # ticket belongs to another board (the change targets a
        # different repo). Move it there as a DRAFT so the target
        # board's refine re-triages it, instead of blocking it on a
        # board where it can never be implemented.
        if result.migrate_to_board:
            try:
                ctx.service.migrate(
                    ticket.id, result.migrate_to_board, note=result.note
                )
            except (KeyError, ValueError) as exc:
                return Outcome(
                    State.BLOCKED,
                    note=(
                        f"migration to board {result.migrate_to_board!r} "
                        f"failed: {exc} — {result.note or 'no note'}"
                    ),
                )
            # migrate() already landed the ticket in DRAFT on the target
            # board (with a history event); the worker sees the state
            # matches and skips the redundant transition.
            return Outcome(State.DRAFT, note=result.note)

        # Redirect — an investigation may conclude the ticket actually
        # needs code implementation, not an operational action.  Hand
        # the ticket to the implement pipeline.
        if result.redirect_to is not None:
            return Outcome(result.redirect_to, note=result.note)

        if result.success:
            return Outcome(State.DONE, note=result.note)
        else:
            return Outcome(State.BLOCKED, note=result.note)
