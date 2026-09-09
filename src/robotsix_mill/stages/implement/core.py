"""The :class:`ImplementStage` coordinator.

Assembles the responsibility-focused mixins
(:class:`~.phase_coordinator.PhaseCoordinatorMixin`,
:class:`~.validation.ValidationMixin`,
:class:`~.implementation_logic.ImplementationLogicMixin`,
:class:`~.file_operations.FileOperationsMixin`) into the public
``Stage`` subclass via multiple inheritance.

This module is the only one in the package that imports the mixins; the
mixins never import each other or ``core`` (cross-responsibility calls
go through ``cls``/``self`` on the assembled class), so the package
import graph is a strict acyclic DAG.
"""

from __future__ import annotations

import json
import logging

from ...core.models import Ticket
from ...core.states import State
from ...core.workspace import Workspace
from ..base import Outcome, Stage, StageContext
from ._shared import clear_spawn_in_flight
from .file_operations import FileOperationsMixin
from .implementation_logic import ImplementationLogicMixin
from .phase_coordinator import PhaseCoordinatorMixin
from .validation import ValidationMixin

log = logging.getLogger(__name__)

# Counter-valued keys in the phase-timing accumulator (everything else is a
# duration in seconds).  Used to compute the duration ``total_s`` and to
# format the emitted log line.
_TIMING_COUNTER_KEYS: frozenset[str] = frozenset(
    {"passes", "sandbox_spawns", "retries", "tier_fallbacks"}
)

# Canonical emission order for the log line (present keys only; ``total`` is
# appended last).  Durations render as ``key=NN.Ns``; counters as ``key=N``.
_TIMING_KEY_ORDER: tuple[str, ...] = (
    "clone_and_branch",
    "prepare_hook",
    "prereq_gate",
    "baseline_check",
    "agent_pass",
    "test_gate",
    "retry_wait",
    "sandbox_slot_wait",
    "sandbox_run",
    "finalize",
    "passes",
    "sandbox_spawns",
    "retries",
    "tier_fallbacks",
)


class ImplementStage(
    PhaseCoordinatorMixin,
    ValidationMixin,
    ImplementationLogicMixin,
    FileOperationsMixin,
    Stage,
):
    """Clone the repo, create a feature branch, and run the implementation agent loop to produce code changes."""

    name = "implement"
    input_state = State.READY

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Run the mixin's implementation loop, then clear the
        in-flight spawn marker.

        The marker is written at the end of preflight so the NEXT
        preflight can detect a process death / SIGTERM that killed the
        attempt mid-flight.  Clearing it here — whether the run
        succeeds, blocks, or raises (the worker records raised errors
        durably, so they are never silent) — marks this attempt as
        having reached a recorded terminal state.
        """
        from ...runtime.tracing import reset_phase_timings

        # Install a fresh phase-timing accumulator so a reused worker context
        # cannot leak a prior ticket's timings into this run.
        reset_phase_timings()
        try:
            return super().run(ticket, ctx)
        finally:
            ws = ctx.service.workspace(ticket)
            clear_spawn_in_flight(ws.artifacts_dir)
            self._emit_phase_timings(ticket, ws)

    @staticmethod
    def _emit_phase_timings(ticket: Ticket, ws: Workspace) -> None:
        """Emit the implement phase-timing breakdown at the single choke point.

        Three sinks, all best-effort (a timing failure must never change the
        stage outcome):

        * stamp the snapshot (plus a computed duration ``total_s``) as the
          root-span attribute ``langfuse.trace.metadata.mill.phase_timings`` —
          the root span is still current in ``ImplementStage.run``'s ``finally``;
        * write ``<artifacts>/implement_timings.json`` — durable and
          Langfuse-independent, readable by retrospect; and
        * log one INFO ``implement-timing`` line.
        """
        try:
            from ...runtime.tracing import (
                collect_phase_timings,
                set_current_span_attribute,
            )

            timings = collect_phase_timings()
            if not timings:
                return
            # Millisecond precision: a run that raises in its first phase
            # (clone + baseline explode) legitimately totals a few tens of
            # ms, which 1-decimal rounding turned into 0.0 and made
            # ``total_s > 0`` flake in CI (2026-09-09, mill #3215's run).
            total_s = round(
                sum(
                    float(v)
                    for k, v in timings.items()
                    if k not in _TIMING_COUNTER_KEYS
                ),
                3,
            )
            payload: dict[str, float | int] = dict(timings)
            payload["total_s"] = total_s

            blob = json.dumps(payload)
            set_current_span_attribute(
                "langfuse.trace.metadata.mill.phase_timings", blob
            )
            (ws.artifacts_dir / "implement_timings.json").write_text(
                blob, encoding="utf-8"
            )
            log.info(
                "implement-timing %s: %s",
                ticket.id,
                ImplementStage._format_timing_line(payload),
            )
        except Exception:
            log.debug("%s: phase-timing emission failed", ticket.id, exc_info=True)

    @staticmethod
    def _format_timing_line(payload: dict[str, float | int]) -> str:
        """Render the ``key=value`` phase-timing summary for the INFO log."""
        parts: list[str] = []
        for key in _TIMING_KEY_ORDER:
            if key not in payload:
                continue
            if key in _TIMING_COUNTER_KEYS:
                parts.append(f"{key}={int(payload[key])}")
            else:
                parts.append(f"{key}={float(payload[key]):.1f}s")
        parts.append(f"total={float(payload.get('total_s', 0.0)):.1f}s")
        return " ".join(parts)
