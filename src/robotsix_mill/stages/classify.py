"""Classify stage: run ops/scope/dedup classification on freshly-ingested tickets.

This lightweight stage runs BEFORE refine on tickets created by
``POST /tickets/ingest``.  It performs the LLM-based classification
steps that were previously inline in the HTTP handler:

1. **ops_classify** — reject operational-maintenance reports (credential
   rotation, redeploy) that don't require code changes.
2. **LLM dedup** — detect semantic duplicates of existing tickets.
3. **scope_classify** — promote broad multi-concern reports to
   auto-decomposed epics.

The stage is designed to be fast (a few LLM calls, no sandbox) and
dispatches with high priority so classification never queues behind
long-running implement sessions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..agents.dedup import (
    any_candidate_overlap,
    rank_candidates_by_similarity,
    run_dedup_check,
)
from ..agents.ops_classify import OpsClassifyVerdict, run_ops_classify_agent
from ..agents.runners.diagnostic_events import emit_diagnostic_event
from ..agents.scope_classify import run_scope_classify_agent
from ..config import Settings
from ..core.models import SourceKind, Ticket, TicketKind
from ..core.states import State
from .base import Outcome, Stage, StageContext

log = logging.getLogger(__name__)


def _normalize_title(title: str) -> str:
    """Import the normalize function from the ingest route.

    We re-use the same normalization logic for diagnostic events.
    """
    from ..runtime.routes._tickets_ingest import _normalize_title as _norm

    return _norm(title)


class ClassifyStage(Stage):
    """Run ops/scope/dedup classification on freshly-ingested tickets."""

    name = "classify"
    input_state = State.CLASSIFYING

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Classify a freshly-ingested ticket.

        Runs ops_classify, LLM dedup, and scope_classify in sequence.
        Returns an Outcome that transitions the ticket to the appropriate
        next state.
        """
        s = ctx.settings
        board_id = ticket.board_id
        title = ticket.title.strip()
        body = ctx.service.workspace(ticket).read_description().strip()

        # --- Phase 1: ops_classify ---
        # Reject operational-maintenance reports (credential rotation,
        # redeploy, infra console changes) that don't require code work.
        ops_verdict = self._run_ops_classify(ticket, title, body, board_id, s)
        if ops_verdict is not None and ops_verdict.classification == "OPERATIONAL":
            log.info(
                "%s: classified as OPERATIONAL — closing: %s",
                ticket.id,
                ops_verdict.reason,
            )
            return Outcome(
                State.CLOSED,
                f"operational-maintenance: {ops_verdict.reason}"[:200],
            )

        # --- Phase 2: LLM dedup ---
        # Check if this ticket is a semantic duplicate of an existing one.
        dup_id = self._run_llm_dedup(ticket, title, body, board_id, ctx)
        if dup_id is not None:
            log.info("%s: LLM dedup match found — closing (duplicate of %s)", ticket.id, dup_id)
            # Record the dedup hit on the existing ticket.
            try:
                ctx.service.add_history_note(
                    dup_id,
                    f"re-reported by ingest on {datetime.now(UTC).date().isoformat()} "
                    f"(LLM dedup match from {ticket.id})",
                )
            except Exception:
                log.warning("failed to add dedup history note to %s", dup_id, exc_info=True)
            return Outcome(
                State.CLOSED,
                f"duplicate of {dup_id}"[:200],
            )

        # --- Phase 3: scope_classify ---
        # Promote broad multi-concern reports to auto-decomposed epics.
        epic_outcome = self._maybe_promote_to_epic(ticket, title, body, board_id, ctx, s)
        if epic_outcome is not None:
            return epic_outcome

        # All classification passed — proceed to refine.
        return Outcome(State.DRAFT, "classification complete")

    def _run_ops_classify(
        self,
        ticket: Ticket,
        title: str,
        body: str,
        board_id: str,
        s: Settings,
    ) -> OpsClassifyVerdict | None:
        """Run the operational-maintenance classifier.

        Returns an OpsClassifyVerdict on success, or None when the LLM
        call fails (fail-open).
        """
        try:
            verdict = run_ops_classify_agent(
                settings=s,
                title=title,
                body=body,
            )
        except Exception as exc:
            log.warning(
                "%s: ops-classify failed, proceeding as code (fail-open): %s",
                ticket.id,
                exc,
            )
            return None

        # Record the classification decision for auditability.
        try:
            emit_diagnostic_event(
                settings=s,
                board_id=board_id,
                category="OPS_CLASSIFY",
                ticket_id=ticket.id,
                reason=(
                    f"classification={verdict.classification} "
                    f"title={title!r} reason={verdict.reason!r}"
                ),
                normalized_key=_normalize_title(title),
            )
        except Exception:
            log.debug("ops-classify diagnostic event emission failed", exc_info=True)

        return verdict

    def _run_llm_dedup(
        self,
        ticket: Ticket,
        title: str,
        body: str,
        board_id: str,
        ctx: StageContext,
    ) -> str | None:
        """Run the LLM dedup check against existing tickets.

        Returns a ticket_id when a duplicate is found, or None when no
        duplicate was detected (fail-open: LLM errors also return None).
        """
        s = ctx.settings
        board_svc = ctx.service

        # Candidate selection — scope to the target board.
        all_tickets = board_svc.list()
        now = datetime.now(UTC)
        lookback_cutoff = now - timedelta(days=s.dedup_lookback_days)
        candidates = [
            t
            for t in all_tickets
            if t.board_id == board_id
            and t.id != ticket.id
            and (
                t.state not in {State.CLOSED, State.ERRORED}
                or (t.state == State.CLOSED and t.updated_at >= lookback_cutoff)
            )
        ]

        if not candidates:
            return None

        # Cheap prefilter — skip LLM when no token overlap.
        candidate_texts: list[str] = []
        for t in candidates:
            try:
                desc = board_svc.workspace(t).read_description()
            except Exception:
                desc = ""
            candidate_texts.append(f"{t.title} {desc}")
        if not any_candidate_overlap(
            draft_title=title,
            draft_body=body,
            candidates_texts=candidate_texts,
        ):
            return None

        # LLM dedup.
        top = rank_candidates_by_similarity(
            draft_title=title,
            draft_body=body,
            candidates=candidates,
            max_candidates=s.dedup_max_candidates,
        )
        # Build candidates_json: one H2 section per candidate.
        lines: list[str] = []
        for t in top:
            try:
                desc = board_svc.workspace(t).read_description()
            except Exception:
                desc = ""
            snippet = desc[: s.dedup_candidate_body_max_chars]
            lines.append(
                f"## {t.id}\n**Title**: {t.title}\n**State**: {t.state}\n\n{snippet}\n"
            )
        candidates_json = "\n".join(lines)

        try:
            verdict = run_dedup_check(
                settings=s,
                draft_title=title,
                draft_body=body,
                candidates_json=candidates_json,
                repo_dir=None,
            )
        except Exception as exc:
            log.warning("%s: dedup LLM failed, proceeding (fail-open): %s", ticket.id, exc)
            return None

        return verdict.get("duplicate_of")

    def _maybe_promote_to_epic(
        self,
        ticket: Ticket,
        title: str,
        body: str,
        board_id: str,
        ctx: StageContext,
        s: Settings,
    ) -> Outcome | None:
        """Promote a clearly multi-concern report to an auto-decomposed epic.

        Returns an Outcome when the ticket should be promoted, or None
        when it should proceed as a single task.
        """
        if not s.auto_epic_enabled:
            return None

        try:
            verdict = run_scope_classify_agent(
                settings=s,
                title=title,
                body=body,
            )
        except Exception as exc:
            log.warning(
                "%s: scope-classify failed, proceeding as task (fail-open): %s",
                ticket.id,
                exc,
            )
            return None

        # Record the classification decision for auditability.
        try:
            emit_diagnostic_event(
                settings=s,
                board_id=board_id,
                category="SCOPE_CLASSIFY",
                ticket_id=ticket.id,
                reason=(
                    f"classification={verdict.classification} "
                    f"confidence={verdict.confidence:.2f} "
                    f"title={title!r} reason={verdict.reason!r}"
                ),
                normalized_key=_normalize_title(title),
            )
        except Exception:
            log.debug("scope-classify diagnostic event emission failed", exc_info=True)

        if verdict.classification != "EPIC":
            return None
        if verdict.confidence < s.auto_epic_min_confidence:
            log.info(
                "%s: scope-classify EPIC below threshold (%.2f < %.2f); staying a task: %s",
                ticket.id,
                verdict.confidence,
                s.auto_epic_min_confidence,
                verdict.reason,
            )
            return None

        # Promote to epic.
        ctx.service.promote_to_epic(ticket.id)
        ctx.service.add_history_note(
            ticket.id,
            f"auto-classified as epic at classify (scope gate, confidence "
            f"{verdict.confidence:.2f}): {verdict.reason}",
        )
        log.info(
            "%s: promoted to epic (confidence %.2f)",
            ticket.id,
            verdict.confidence,
        )

        # Decompose the epic into child tickets.
        try:
            self._decompose_epic(ticket, body, board_id, ctx)
        except Exception:
            log.exception(
                "%s: epic-breakdown after classify promotion failed — epic body "
                "is in place, children left for /generate-children",
                ticket.id,
            )

        return Outcome(State.EPIC_OPEN, f"promoted to epic: {verdict.reason}"[:200])

    def _decompose_epic(
        self,
        epic: Ticket,
        body: str,
        board_id: str,
        ctx: StageContext,
    ) -> None:
        """Break *epic* into dependency-ordered child tickets."""
        from ..agents.epic_breakdown import (
            plan_child_dependencies,
            run_epic_breakdown_agent,
        )

        s = ctx.settings
        board_svc = ctx.service

        breakdown = run_epic_breakdown_agent(
            settings=s,
            epic_title=epic.title,
            epic_description=body,
        )
        created_children: list[tuple[str, str, str]] = []
        for child_title, child_body in zip(
            breakdown.child_titles,
            breakdown.child_bodies,
            strict=True,
        ):
            child = board_svc.create(
                title=child_title,
                description=child_body,
                source=epic.source or SourceKind.USER,
                kind=TicketKind.TASK,
                parent_id=epic.id,
                board_id=board_id,
            )
            created_children.append((child.id, child_title, child_body))

        for child_id, deps in plan_child_dependencies(created_children).items():
            board_svc.set_depends_on(child_id, deps)

        # Adopt the breakdown agent's revised epic body when it reworked one.
        if breakdown.epic_body and breakdown.epic_body.strip():
            new_hash = board_svc.workspace(epic).write_description(
                breakdown.epic_body.strip()
            )
            board_svc.set_content_hash(epic.id, new_hash)

        for _child_id, _t, _b in created_children:
            # Child tickets are created in DRAFT state and will be
            # picked up by the worker on the next tick.
            pass

        board_svc.add_history_note(
            epic.id,
            f"epic-breakdown spawned {len(created_children)} child ticket(s)",
        )
