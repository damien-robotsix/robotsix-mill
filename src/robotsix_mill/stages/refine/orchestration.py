"""Refine-agent orchestration for the refine stage.

A mixin (:class:`RefineAgentMixin`) holding ``_run_refine_agent`` — the
big phase that drives ``refining.run_refine_agent``, applies the
single-scope / split / promote-to-epic / no-change-needed result modes,
runs the spec-conciseness review, and handles pause/resume.  The
conciseness-review loop (previously duplicated across the single-spec and
split paths) is factored into :meth:`_review_spec_conciseness`.

``_run_refine_agent`` itself is a thin orchestrator: each logical phase
(reviewer-comment gather, split-child fast-path, triage skip, agent
invocation, agent-output side-effects, and the no-change / promote /
single-scope / multi-scope outcome paths) lives in its own helper method,
and the repeated ``Outcome`` + thread-acknowledgment + ``file_map.json``
write patterns are factored into :meth:`_resolved_outcome`,
:meth:`_ack_threads`, and :meth:`_write_file_map`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from ...agents import refining
from ...config.settings import Settings
from ...core.models import SourceKind, Ticket, TicketKind
from ...core.service import TicketService
from ...core.states import State
from ...core.workspace import Workspace
from ...runtime.tracing import set_current_span_attribute
from ...vcs import git_ops
from ..base import Outcome, StageContext
from ..pause import (
    acknowledge_unanswered_threads,
    build_resume_message_history,
    check_for_pause,
    clear_conversation_state,
    load_conversation_state,
    save_conversation_state,
    _collect_ask_user_replies,
)
from .helpers import (
    OPERATOR_SENDBACK_PREFIX,
    UNMERGED_BRANCH_PREFIX,
    _COMMIT_SHA_RE,
    _TICKET_ID_RE,
    _build_deployed_log_summary,
    _draft_has_complete_spec,
    _load_refine_memory,
    _persist_refine_memory,
    _rationale_claims_external_fix,
    _AUTO_APPROVE_SOURCES,
    _resolve_next_state,
    _spec_is_degenerate,
    _summarize_spec_for_auto_approve,
    _verify_cited_fix_at_head,
    log,
)


def _write_triage_complexity(
    ws,
    complexity: str,
    trivial_scope: bool | None = None,
    findings: str | None = None,
) -> None:
    """Persist the triage complexity verdict (and optionally the trivial-scope
    flag and exploration findings) for downstream consumption."""
    data: dict = {"complexity": complexity}
    if trivial_scope is not None:
        data["trivial_scope"] = trivial_scope
    (ws.artifacts_dir / "triage_complexity.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    if findings:
        (ws.artifacts_dir / "triage_findings.json").write_text(
            json.dumps({"findings": findings}), encoding="utf-8"
        )


def _read_triage_complexity(ws: Workspace) -> str:
    """Read the triage complexity verdict; returns ``"needs-exploration"``
    when the file is absent (conservative default)."""
    path = ws.artifacts_dir / "triage_complexity.json"
    if not path.exists():
        return "needs-exploration"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cast(str, data.get("complexity", "needs-exploration"))
    except json.JSONDecodeError, KeyError:
        return "needs-exploration"


def _read_triage_findings(ws: Workspace) -> str | None:
    """Read the triage exploration findings; returns ``None`` when the
    artifact is absent or unparseable (conservative default — no block)."""
    path = ws.artifacts_dir / "triage_findings.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        findings = data.get("findings")
        return cast(str | None, findings) if findings else None
    except json.JSONDecodeError, KeyError:
        return None


def _read_triage_trivial(ws: Workspace) -> bool:
    """Read the triage trivial-scope verdict; returns ``False`` when the
    file or key is absent (conservative default — no cheap-model routing)."""
    path = ws.artifacts_dir / "triage_complexity.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cast(bool, data.get("trivial_scope", False))
    except json.JSONDecodeError, KeyError:
        return False


_MIGRATE_NOTE_PREFIX = "migrated from board "


def _parse_prior_boards(service: TicketService, ticket_id: str) -> tuple[set[str], int]:
    """Parse migration-history events to find boards this ticket has been on.

    Returns ``(prior_boards, migration_count)``.  ``prior_boards`` is the
    set of destination board ids extracted from ``"migrated from board …"``
    notes.  ``migration_count`` is the total number of migration events.
    """
    prior_boards: set[str] = set()
    migration_count = 0
    for ev in service.history(ticket_id):
        note = ev.note or ""
        if note.startswith(_MIGRATE_NOTE_PREFIX):
            migration_count += 1
            to_pos = note.find(" to ")
            if to_pos != -1:
                rest = note[to_pos + 4 :]  # after " to "
                suffix_pos = rest.find(" (was ")
                if suffix_pos == -1:
                    suffix_pos = rest.find(": ")
                dst_repr = rest[:suffix_pos] if suffix_pos != -1 else rest
                dst_board = dst_repr.strip().strip("'\"")
                if dst_board:
                    prior_boards.add(dst_board)
    return prior_boards, migration_count


def _anti_bounce_escalate(
    ctx: StageContext,
    ws: Workspace,
    draft: str,
    ticket: Ticket,
    triage: Any,
    resolved_board: str,
) -> Outcome | None:
    """Check migration anti-bounce guard; escalate to human if triggered.

    Derives prior boards from migration history via
    :func:`_parse_prior_boards`.  If history cannot be read, or if the
    ticket has already been migrated at least once (or the target board
    is a prior destination), writes the standard draft-artifact + empty
    file_map and returns a human-escalation :class:`Outcome`.  Returns
    ``None`` when migration is safe to proceed.
    """
    try:
        prior_boards, migration_count = _parse_prior_boards(ctx.service, ticket.id)
    except Exception:
        log.warning(
            "%s: could not read ticket history for anti-bounce check, "
            "escalating to human",
            ticket.id,
            exc_info=True,
        )
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )
        RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
        return RefineAgentMixin._resolved_outcome(
            ctx,
            draft,
            ticket.id,
            f"triage MIGRATE anti-bounce error: {triage.reason}",
            source=ticket.source,
            triage_note=triage.reason,
        )

    if migration_count >= 1 or resolved_board in prior_boards:
        log.info(
            "%s: anti-bounce blocked MIGRATE to %r "
            "(prior boards=%r, migration_count=%d) — escalating to human",
            ticket.id,
            resolved_board,
            prior_boards,
            migration_count,
        )
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )
        RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
        return RefineAgentMixin._resolved_outcome(
            ctx,
            draft,
            ticket.id,
            f"triage MIGRATE anti-bounce blocked: {triage.reason}",
            source=ticket.source,
            triage_note=triage.reason,
        )

    return None


def _persist_triage_complexity(
    ws: Workspace,
    triage: Any,
) -> None:
    """Persist the triage complexity verdict for downstream exploration gating."""
    complexity = triage.complexity
    if complexity is None:
        # Default: needs-exploration for backward compat / safety.
        complexity = "needs-exploration"
    _write_triage_complexity(
        ws,
        complexity,
        trivial_scope=triage.trivial_scope,
        findings=triage.exploration_findings,
    )


class RefineAgentMixin:
    """Refine-agent pipeline staticmethods mixed into :class:`RefineStage`."""

    @staticmethod
    def _review_spec_conciseness(
        s: Settings,
        ws: Workspace,
        ticket: Ticket,
        spec: str,
        verbose_filename: str,
        child_index: int | None = None,
    ) -> str:
        """Run the conciseness review on *spec*, returning the concise spec.

        Saves the verbose original to ``ws.artifacts_dir / verbose_filename``
        and returns the reviewed concise spec.  On a degenerate
        (empty/placeholder) review result or any failure, returns the
        original verbose *spec* unchanged.  When *child_index* (1-based) is
        given, log messages name the child — preserving the two original
        message variants exactly.
        """
        try:
            review_result = refining.review_spec_for_conciseness(
                settings=s,
                spec_markdown=spec,
            )
            (ws.artifacts_dir / verbose_filename).write_text(
                spec,
                encoding="utf-8",
            )
            concise = review_result.concise_spec
            if _spec_is_degenerate(concise):
                if child_index is None:
                    log.warning(
                        "%s: spec review returned empty/placeholder "
                        "concise spec, using verbose spec",
                        ticket.id,
                    )
                else:
                    log.warning(
                        "%s: spec review child %d returned empty/placeholder "
                        "concise spec, using verbose spec",
                        ticket.id,
                        child_index,
                    )
                return spec
            if child_index is None:
                log.info(
                    "%s: spec review: %s",
                    ticket.id,
                    review_result.stripped_summary,
                )
            else:
                log.info(
                    "%s: spec review child %d: %s",
                    ticket.id,
                    child_index,
                    review_result.stripped_summary,
                )
            return concise
        except Exception:
            if child_index is None:
                log.warning(
                    "%s: spec review failed, using verbose spec",
                    ticket.id,
                    exc_info=True,
                )
            else:
                log.warning(
                    "%s: spec review failed for child %d, using verbose spec",
                    ticket.id,
                    child_index,
                    exc_info=True,
                )
            return spec

    # -- shared outcome / thread / artifact helpers -------------------------

    @staticmethod
    def _resolved_outcome(
        ctx: StageContext,
        spec: str,
        ticket_id: str,
        base_note: str,
        *,
        source: str | None = None,
        triage_note: str | None = None,
    ) -> Outcome:
        """Resolve the next state for *spec* and build the closing Outcome.

        Encapsulates the repeated ``_resolve_next_state`` → "append the
        auto-approve note when present" → ``Outcome`` pattern shared by the
        split-child, triage-skip, single-scope, and split paths.
        """
        next_state, auto_note = _resolve_next_state(
            ctx, spec, ticket_id, source=source, triage_note=triage_note
        )
        note = base_note
        if auto_note:
            note += f" | {auto_note}"
        return Outcome(next_state, note)

    @staticmethod
    def _ack_threads(
        ctx: StageContext,
        ticket: Ticket,
        reviewer_comments: str | None,
        open_thread_ids: set[int],
    ) -> None:
        """Acknowledge any open reviewer threads after the agent ran.

        No-op unless there were reviewer comments *and* open threads — the
        guard the original repeated at every outcome return.
        """
        if reviewer_comments and open_thread_ids:
            acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)

    @staticmethod
    def _write_file_map(
        ws: Workspace, entries: list[dict[str, str]], *, only_if_absent: bool = False
    ) -> None:
        """Write ``file_map.json`` to the workspace artifacts dir.

        *entries* is a list of ``{"file": ..., "note": ...}`` dicts (``[]``
        renders as the empty file map). When *only_if_absent* is set, an
        existing file is left untouched — the scope-free / triage-skip
        behaviour that must not clobber a previously written map.
        """
        file_map_path = ws.artifacts_dir / "file_map.json"
        if only_if_absent and file_map_path.exists():
            return
        file_map_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    @staticmethod
    def _run_refine_agent(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        epic_ctx: str,
        title: str,
        ws: Workspace,
        s: Settings,
        extra_roots: list[Path] | None = None,
    ) -> Outcome:
        """Run the full refine-agent pipeline and handle the result.

        Covers split-child fast-path, reviewer-comment collection,
        triage skip, agent invocation, pause detection, artifact
        persistence, spec review, single-scope and multi-scope split
        outcomes.  Each phase lives in its own helper method; this body
        is the FSM driver that short-circuits on the first phase to
        produce an :class:`Outcome`.
        """
        reviewer_comments, open_thread_ids = (
            RefineAgentMixin._collect_reviewer_comments(ctx, ticket)
        )

        outcome = RefineAgentMixin._reviewer_agreement_guard(
            ctx, ticket, draft, ws, s, reviewer_comments
        )
        if outcome is not None:
            return outcome

        outcome = RefineAgentMixin._split_child_fast_path(
            ctx, ticket, draft, ws, reviewer_comments
        )
        if outcome is not None:
            return outcome

        # Triage already ran in RefineStage.run() (Phase 2.2) — skip
        # the duplicate call when the complexity artifact exists.
        if not (ws.artifacts_dir / "triage_complexity.json").exists():
            outcome = RefineAgentMixin._triage_skip(
                ctx,
                ticket,
                draft,
                repo_dir,
                extra_roots,
                title,
                ws,
                s,
                reviewer_comments,
            )
            if outcome is not None:
                return outcome

        # Short-circuit: when the draft already carries CI/test/type/lint
        # failure logs, skip the expensive refine agent and produce a
        # minimal spec that points implement at the logged failures.
        outcome = RefineAgentMixin._short_circuit_for_internal_failure(
            ctx, ticket, draft, ws, s, reviewer_comments
        )
        if outcome is not None:
            return outcome

        outcome, result = RefineAgentMixin._run_and_collect(
            ctx,
            ticket,
            draft,
            repo_dir,
            epic_ctx,
            ws,
            s,
            extra_roots,
            reviewer_comments,
        )
        if outcome is not None:
            return outcome
        # Contract: when ``_run_and_collect`` returns no short-circuit
        # outcome, the ``RefineResult`` is always present.
        result = cast(refining.RefineResult, result)

        outcome = RefineAgentMixin._gitignored_guard(ticket, result, repo_dir)
        if outcome is not None:
            return outcome

        RefineAgentMixin._apply_agent_side_effects(
            ctx, ticket, draft, ws, s, epic_ctx, result
        )

        outcome = RefineAgentMixin._no_change_path(
            ctx, ticket, draft, repo_dir, title, ws, result
        )
        if outcome is not None:
            return outcome

        if result.promote_to_epic and not result.split:
            return RefineAgentMixin._promote_to_epic_path(
                ctx, ticket, draft, ws, s, result
            )

        if not result.split:
            return RefineAgentMixin._single_scope_path(
                ctx, ticket, ws, s, result, reviewer_comments, open_thread_ids
            )

        return RefineAgentMixin._multi_scope_path(
            ctx,
            ticket,
            draft,
            ws,
            s,
            epic_ctx,
            result,
            reviewer_comments,
            open_thread_ids,
        )

    # -- phase: reviewer-comment gather (sendback guard) --------------------

    @staticmethod
    def _collect_reviewer_comments(
        ctx: StageContext, ticket: Ticket
    ) -> tuple[str | None, set[int]]:
        """Gather open reviewer comments for the sendback guard.

        ``mill`` and ``system`` author comments (trace-link auto-posts
        from runtime.worker._post_trace_comment; timeout-escalation
        pings) are diagnostic notes, not human feedback. Including
        them taught refine to treat an inaccessible Langfuse URL as
        reviewer comments and ask_user what the reviewer said.
        """
        _NON_FEEDBACK_AUTHORS = {"mill", "system"}
        reviewer_comments: str | None = None
        open_thread_ids: set[int] = set()
        try:
            comments = ctx.service.list_comments(ticket.id)
            if comments:
                # Only count non-closed, non-system top-level threads
                # for sendback detection.
                open_threads = [
                    c
                    for c in comments
                    if c.parent_id is None
                    and c.closed_at is None
                    and c.author not in _NON_FEEDBACK_AUTHORS
                ]
                if open_threads:
                    open_thread_ids = {c.id for c in open_threads}
                    closed_ids = {c.id for c in comments if c.closed_at is not None}
                    reviewer_comments = "\n".join(
                        f"[id={c.id} @ {c.created_at.isoformat()}] {c.body}"
                        for c in comments
                        if c.id not in closed_ids
                        and c.parent_id not in closed_ids
                        and c.author not in _NON_FEEDBACK_AUTHORS
                    )
                    if not reviewer_comments:
                        reviewer_comments = None
        except Exception:
            log.warning("%s: list_comments failed, proceeding without", ticket.id)
        return reviewer_comments, open_thread_ids

    # -- phase: reviewer-agreement guard (pre-Opus cost saver) ------------

    @staticmethod
    def _reviewer_agreement_guard(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws: Workspace,
        s: Settings,
        reviewer_comments: str | None,
    ) -> Outcome | None:
        """Pre-Opus guard: when reviewer feedback confirms the draft's
        no-change-needed conclusion, short-circuit to DONE — skipping the
        expensive Opus refine agent.

        Gated by ``reviewer_agreement_gate_enabled`` AND
        ``refine_triage_enabled`` (both must be True), and only runs when
        ``reviewer_comments`` is present (truthy).  A single cheap L1
        classifier (DeepSeek flash, ~$0.0003) replaces what would
        otherwise be a full Opus refine call (~$0.28).

        Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
        through to the full pipeline.
        """
        if not (
            s.reviewer_agreement_gate_enabled
            and s.refine_triage_enabled
            and reviewer_comments
        ):
            return None
        try:
            agreement = refining.triage_reviewer_agreement(
                settings=s,
                draft=f"{ticket.title}\n\n{draft}",
                reviewer_comments=reviewer_comments,
            )
        except Exception:
            log.warning(
                "%s: reviewer-agreement triage failed, falling through",
                ticket.id,
                exc_info=True,
            )
            return None

        if agreement.decision != "AGREE":
            return None

        # Reviewer agrees with the draft's conclusion — short-circuit.
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )
        RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
        short = agreement.reason[:400] + ("…" if len(agreement.reason) > 400 else "")
        return Outcome(
            State.DONE,
            f"reviewer agreement — no change needed: {short}",
        )

    # -- phase: split-child fast-path ---------------------------------------

    @staticmethod
    def _split_child_fast_path(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws: Workspace,
        reviewer_comments: str | None,
    ) -> Outcome | None:
        """Skip re-refinement for split children.

        A child ticket created from a split already has a refined
        spec in its description.md.  Detect this by checking whether
        the parent is CLOSED with a "split into" note — the canonical
        signal that this ticket's description is already the refined
        output.  When children are reparented to an umbrella epic
        the direct parent is no longer CLOSED, so also check the
        ticket's own history for a "split from" transition note.
        We must NOT short-circuit for retrospect-spawned drafts
        (whose parent is also CLOSED but for a different reason and
        whose description is a raw draft, not a spec).
        IMPORTANT: even split children must fall through to the full
        refine agent when there are open reviewer comments — the
        human requested changes that the spec must address.

        Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
        through to the full pipeline.
        """
        is_split_child = False
        if ticket.parent_id is not None:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.state == State.CLOSED:
                # Only short-circuit if the parent was closed by a
                # split — otherwise (e.g. retrospect spawn) the
                # draft still needs refinement.
                parent_history = ctx.service.history(parent.id)
                is_split_child = any(
                    ev.state == State.CLOSED
                    and ev.note
                    and ev.note.startswith("split into")
                    for ev in parent_history
                )
        if not is_split_child:
            # Fallback: check the ticket's own history for a
            # "split from" note (children reparented to an epic).
            own_history = ctx.service.history(ticket.id)
            is_split_child = any(
                ev.note and ev.note.startswith("split from") for ev in own_history
            )
        if not (is_split_child and not reviewer_comments):
            return None

        # Split children are already refined — no exploration needed.
        _write_triage_complexity(ws, "simple")

        spec = draft
        if not spec.strip():
            return Outcome(State.BLOCKED, "split child has empty description")
        # Preserve the raw draft if not already preserved.
        draft_original = ws.artifacts_dir / "draft-original.md"
        if not draft_original.exists():
            draft_original.write_text(
                "(split child — spec written by parent's refine agent)",
                encoding="utf-8",
            )
        # Split children skip the refine agent — but implement still
        # demands a file_map.json. Write an empty one so the
        # downstream gate treats this as scope-free mode rather
        # than "refine broken" → BLOCKED.
        RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
        return RefineAgentMixin._resolved_outcome(
            ctx,
            spec,
            ticket.id,
            "split child — spec already refined",
            source=ticket.source,
        )

    # -- phase: triage skip / maintenance -----------------------------------

    @staticmethod
    def _triage_skip(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        extra_roots: list[Path] | None,
        title: str,
        ws: Workspace,
        s: Settings,
        reviewer_comments: str | None,
    ) -> Outcome | None:
        """Triage phase 1: LLM classifier (3-way: SKIP / MAINTENANCE / REFINE).

        A single cheap LLM call classifies the draft.  If it's
        already a precise, implementation-ready spec, skip the
        expensive refine agent entirely.  If it's a maintenance
        (operational) request the keyword classifier missed, route
        to MAINTENANCE.  ONLY run when:
        - the feature flag is enabled, AND
        - no reviewer sendback (human-flagged changes always refine).

        Also captures the complexity verdict from the triage classifier
        and persists it to ``ws.artifacts_dir / "triage_complexity.json"``
        so ``_run_and_collect`` can read it and pass it to
        ``run_refine_agent`` for exploration gating.

        Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
        through to the full refine agent.
        """
        if not (s.refine_triage_enabled and not reviewer_comments):
            return None
        try:
            triage = refining.triage_refine(
                settings=s,
                title=title,
                draft=draft,
                repo_dir=repo_dir,
                extra_roots=extra_roots,
            )
            _persist_triage_complexity(ws, triage)

            if (
                triage.decision == "MAINTENANCE"
                and s.maintenance_triage_enabled
                and ticket.source != SourceKind.CI
            ):
                # LLM detected a maintenance request the keyword
                # classifier missed.  Route to MAINTENANCE without
                # running the full refine agent.
                #
                # NB: a CI-failure ticket (source == ci) is deliberately
                # NOT routed here even when triage says MAINTENANCE. CI
                # tickets carry the failing logs and are a code/config fix
                # in THIS repo (e.g. a workflow-permissions YAML edit);
                # the maintenance agent is READ-ONLY and cannot edit
                # files, so routing a CI failure there always dead-ends in
                # a "needs a human" misdiagnosis (live class: the GHCR
                # docker-release `packages: write` tickets were mis-triaged
                # to MAINTENANCE and blocked). For source == ci we fall
                # through to the full refine agent so it scopes a real fix.
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                return Outcome(
                    State.MAINTENANCE,
                    f"maintenance triage (LLM): {triage.reason} — {title}",
                )
            if triage.decision == "NO_CHANGE":
                # Triage verified at level 2 that the deliverable already
                # exists on disk. Honoured directly — no further confirmation.
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
                short_reason = triage.reason[:400] + (
                    "…" if len(triage.reason) > 400 else ""
                )
                return Outcome(
                    State.DONE,
                    f"triage NO_CHANGE: {short_reason}",
                )
            if triage.decision == "SKIP":
                # The draft IS the spec — preserve it unchanged.
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                # Try to extract backtick-quoted file paths from
                # the draft so the implement stage can enforce
                # scope even when we skip the refine agent.
                # Pattern: backtick-quoted strings that look like
                # file paths (contain a '/' directory separator
                # and a file extension).
                _PATH_RE = re.compile(r"`([^`]*/[^`]*\.[a-zA-Z]{1,10})`")
                extracted = _PATH_RE.findall(draft)
                if extracted:
                    RefineAgentMixin._write_file_map(
                        ws,
                        [{"file": p, "note": "from draft"} for p in extracted],
                        only_if_absent=True,
                    )
                else:
                    # No paths extracted — write empty file_map so implement
                    # treats this as scope-free mode rather than "refine broken".
                    RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
                return RefineAgentMixin._resolved_outcome(
                    ctx,
                    draft,
                    ticket.id,
                    f"triage SKIP: {triage.reason}",
                    source=ticket.source,
                    triage_note=triage.reason,
                )

            if triage.decision == "MIGRATE":
                # --- MIGRATE: self-reroot a confidently-misrouted ticket ---
                #
                # The triage classifier identified the ticket as belonging to
                # another board and named a specific target_board from the
                # registered-boards catalog.  Validate, apply anti-bounce
                # guards, and migrate — or fall through to human escalation
                # when anything looks off.

                from ...config import get_repos_config

                # Determine the set of valid board ids (mirrors TicketService.migrate
                # resolution: accepts board-id or repo-id, plus the synthetic
                # cross-repo "meta" board).
                try:
                    repos_config = get_repos_config()
                    known: dict[str, str] = {"meta": "meta"}
                    for rc in repos_config.repos.values():
                        known[rc.repo_id] = rc.board_id
                        known[rc.board_id] = rc.board_id
                except Exception:
                    log.warning(
                        "%s: could not load repos config for MIGRATE validation, "
                        "escalating to human",
                        ticket.id,
                        exc_info=True,
                    )
                    known = {}

                target = (triage.target_board or "").strip()
                resolved_board = known.get(target) if known else None

                # Validate: target must be non-empty, resolvable, and != current board.
                if (
                    not target
                    or resolved_board is None
                    or resolved_board == ticket.board_id
                ):
                    log.info(
                        "%s: MIGRATE target invalid (target=%r, resolved=%r, current=%r) "
                        "— escalating to human",
                        ticket.id,
                        target,
                        resolved_board,
                        ticket.board_id,
                    )
                    # Fall through to human escalation (same as SKIP path) —
                    # write artifacts then resolve.
                    (ws.artifacts_dir / "draft-original.md").write_text(
                        draft if draft else "(title-only ticket, no body provided)",
                        encoding="utf-8",
                    )
                    RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
                    return RefineAgentMixin._resolved_outcome(
                        ctx,
                        draft,
                        ticket.id,
                        f"triage MIGRATE invalid target: {triage.reason}",
                        source=ticket.source,
                        triage_note=triage.reason,
                    )

                # Anti-bounce cap: escalate to human when the ticket has already
                # been migrated (or the target is a board it has been on before).
                anti_bounce = _anti_bounce_escalate(
                    ctx, ws, draft, ticket, triage, resolved_board
                )
                if anti_bounce is not None:
                    return anti_bounce

                # Perform the migration.
                try:
                    ctx.service.migrate(
                        ticket.id,
                        resolved_board,
                        note=triage.reason,
                    )
                except (KeyError, ValueError) as exc:
                    log.warning(
                        "%s: MIGRATE call failed: %s — escalating to human",
                        ticket.id,
                        exc,
                    )
                    (ws.artifacts_dir / "draft-original.md").write_text(
                        draft if draft else "(title-only ticket, no body provided)",
                        encoding="utf-8",
                    )
                    RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
                    return RefineAgentMixin._resolved_outcome(
                        ctx,
                        draft,
                        ticket.id,
                        f"triage MIGRATE failed: {exc} — {triage.reason}",
                        source=ticket.source,
                        triage_note=triage.reason,
                    )

                # Success: migrate() already landed the ticket in DRAFT on the
                # target board.  Write artifacts and return a DRAFT outcome
                # matching the maintenance-stage precedent.
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)
                return Outcome(
                    State.DRAFT,
                    f"migrated to board {resolved_board!r}: {triage.reason}",
                )

            # --- mechanical draft fast-path: when a mill-internal
            # automated ticket passes triage with REFINE but the
            # auto-approve classifier confirms it is purely mechanical,
            # skip the expensive refine agent entirely.  The draft IS
            # the spec — preserve it unchanged, exactly like the SKIP
            # path above, but with the extra safety of the auto-approve
            # gate confirming there are no design decisions.
            #
            # Runs for mill-internal automated proposals when
            # auto-approve is enabled.  "user" source is always excluded
            # (human-written tickets always run the full refine agent).
            # "ci" source is admitted ONLY when the draft is a complete
            # self-contained spec (has ## Problem + ## Scope headings)
            # — raw error dumps with no scope section route to the full
            # refine agent and are NOT admitted here.
            if s.auto_approve_enabled and (
                ticket.source not in ("user", "ci")
                or (ticket.source == "ci" and _draft_has_complete_spec(draft))
            ):
                try:
                    # --- deterministic short-circuit ---
                    # Sources in _AUTO_APPROVE_SOURCES are by construction
                    # no-design-risk: skip the LLM entirely and go straight
                    # to the existing fast-path exit.  The post-refine gate
                    # (_resolve_next_state) will return READY via the same
                    # deterministic rule.
                    if ticket.source in _AUTO_APPROVE_SOURCES:
                        (ws.artifacts_dir / "draft-original.md").write_text(
                            draft if draft else "(title-only ticket, no body provided)",
                            encoding="utf-8",
                        )
                        _PATH_RE = re.compile(r"`([^`]*/[^`]*\.[a-zA-Z]{1,10})`")
                        extracted = _PATH_RE.findall(draft)
                        if extracted:
                            RefineAgentMixin._write_file_map(
                                ws,
                                [{"file": p, "note": "from draft"} for p in extracted],
                                only_if_absent=True,
                            )
                        else:
                            RefineAgentMixin._write_file_map(
                                ws, [], only_if_absent=True
                            )
                        return RefineAgentMixin._resolved_outcome(
                            ctx,
                            draft,
                            ticket.id,
                            f"mechanical draft fast-path "
                            f"(deterministic source {ticket.source!r}) "
                            f"— skipped refine LLM",
                            source=ticket.source,
                            triage_note=triage.reason,
                        )

                    # Remaining non-deterministic-source tickets: run the
                    # bounded auto-approve classifier.  Match the post-refine
                    # gate by using _summarize_spec_for_auto_approve instead
                    # of the full unbounded draft.
                    auto = refining.triage_auto_approve(
                        settings=s,
                        spec=_summarize_spec_for_auto_approve(
                            f"{ticket.title}\n\n{draft}"
                        ),
                    )
                    if auto.decision == "APPROVE":
                        (ws.artifacts_dir / "draft-original.md").write_text(
                            draft if draft else "(title-only ticket, no body provided)",
                            encoding="utf-8",
                        )
                        _PATH_RE = re.compile(r"`([^`]*/[^`]*\.[a-zA-Z]{1,10})`")
                        extracted = _PATH_RE.findall(draft)
                        if extracted:
                            RefineAgentMixin._write_file_map(
                                ws,
                                [{"file": p, "note": "from draft"} for p in extracted],
                                only_if_absent=True,
                            )
                        else:
                            RefineAgentMixin._write_file_map(
                                ws, [], only_if_absent=True
                            )
                        return RefineAgentMixin._resolved_outcome(
                            ctx,
                            draft,
                            ticket.id,
                            f"mechanical draft fast-path — "
                            f"auto-approve APPROVE: {auto.reason}",
                            source=ticket.source,
                            triage_note=(
                                f"triage REFINE → auto-approve APPROVE: {auto.reason}"
                            ),
                        )
                except Exception:
                    log.warning(
                        "%s: mechanical fast-path auto-approve failed, falling through",
                        ticket.id,
                        exc_info=True,
                    )
        except Exception:
            log.warning(
                "%s: triage failed, falling through to full refine",
                ticket.id,
                exc_info=True,
            )
        return None

    # -- phase: short-circuit internal toolchain failures -------------------

    @staticmethod
    def _short_circuit_for_internal_failure(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws: Workspace,
        s: Settings,
        reviewer_comments: str | None,
    ) -> Outcome | None:
        """Short-circuit refine to a minimal spec for internal toolchain failures.

        When the draft already carries concrete CI/test/type/lint failure
        output, there is no need to re-derive root cause via the expensive
        refine agent — produce a minimal spec that points implement at the
        logged failures.

        Gate conditions (ALL must hold):
        - No reviewer sendback (human-flagged changes always get full refinement)
        - Draft is non-empty
        - ``is_internal_toolchain_failure(draft)`` is ``True``

        Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
        through to the full refine agent.
        """
        if reviewer_comments:
            return None
        if not draft or not draft.strip():
            return None
        if not refining.is_internal_toolchain_failure(draft):
            return None

        log.info(
            "%s: short-circuiting refine — draft carries internal toolchain "
            "failure logs; producing minimal spec for implement",
            ticket.id,
        )

        # Build a minimal spec that points implement at the logged failures.
        evidence_note = ""
        evidence_path = ws.artifacts_dir / "evidence.txt"
        if evidence_path.exists():
            try:
                evidence_text = evidence_path.read_text(encoding="utf-8")[:4000]
                evidence_note = (
                    f"\nAdditional evidence from `artifacts/evidence.txt`:\n\n"
                    f"```\n{evidence_text}\n```\n"
                )
            except Exception:
                log.warning(
                    "%s: failed to read evidence.txt, skipping",
                    ticket.id,
                    exc_info=True,
                )

        # Truncate the draft body for embedding — keep enough to show the
        # failure but avoid ballooning the spec.
        draft_excerpt = draft[:3000]
        if len(draft) > 3000:
            draft_excerpt += "\n… [truncated]"

        spec = (
            "## Problem\n\n"
            "An internal toolchain failure (CI/type/lint/test) was detected. "
            "The draft already carries the failing logs — fix locally so the "
            "check passes.\n\n"
            "## Scope\n\n"
            "Fix the failing check. The draft body contains the error details:\n\n"
            f"```\n{draft_excerpt}\n```\n"
            f"{evidence_note}"
            "\n## Acceptance criteria\n\n"
            "- The failing check passes.\n\n"
            "## Out of scope / constraints\n\n"
            "- Do not expand scope beyond fixing this specific toolchain failure.\n"
            "- This is a local code/config fix — no external investigation needed.\n"
        )

        # Persist the raw draft if not already preserved.
        draft_original = ws.artifacts_dir / "draft-original.md"
        if not draft_original.exists():
            draft_original.write_text(draft, encoding="utf-8")

        # Write the minimal spec to the workspace description so implement
        # picks it up (unlike the triage-skip and split-child paths, which
        # keep the draft as-is, this path produces a new spec).
        new_hash = ws.write_description(spec)
        ctx.service.set_content_hash(ticket.id, new_hash)

        # Write an empty file_map so implement treats this as scope-free mode.
        RefineAgentMixin._write_file_map(ws, [], only_if_absent=True)

        # Record complexity so downstream gates don't re-triage.
        _write_triage_complexity(ws, "simple")

        return RefineAgentMixin._resolved_outcome(
            ctx,
            spec,
            ticket.id,
            "short-circuited refine — internal toolchain failure with logs",
            source=ticket.source,
        )

    # -- phase: run the refine agent + pause detection ----------------------

    # -- error-recovery checkpoint helpers ---------------------------------

    @staticmethod
    def _save_refine_checkpoint(
        ws: Workspace,
        result: refining.RefineResult,
    ) -> None:
        """Persist essential ``RefineResult`` fields so a resume-from-BLOCKED
        can skip re-running the expensive refine agent.

        The conversation state is already saved separately by
        :func:`save_conversation_state` for the pause mechanism; this
        checkpoint captures the structured output fields needed to
        reconstruct a ``RefineResult`` without calling the agent again.
        """
        import base64

        children_data = None
        if result.children:
            children_data = [
                {
                    "title": c.title,
                    "spec_markdown": c.spec_markdown,
                    "depends_on": c.depends_on,
                }
                for c in result.children
            ]
        file_map_data = None
        if result.file_map:
            file_map_data = [{"file": e.file, "note": e.note} for e in result.file_map]
        data: dict[str, Any] = {
            "spec_markdown": result.spec_markdown,
            "split": result.split,
            "children": children_data,
            "promote_to_epic": result.promote_to_epic,
            "epic_body": result.epic_body,
            "updated_memory": result.updated_memory,
            "file_map": file_map_data,
            "title": result.title,
            "reference_files": result.reference_files or [],
            "conversation_state_b64": (
                base64.b64encode(result.conversation_state).decode("ascii")
                if result.conversation_state
                else None
            ),
        }
        (ws.artifacts_dir / "refine_checkpoint.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    @staticmethod
    def _load_refine_checkpoint(
        ws: Workspace,
    ) -> tuple[refining.RefineResult | None, bytes | None]:
        """Load a saved refine error-recovery checkpoint.

        Returns ``(RefineResult, conversation_state_bytes)`` or
        ``(None, None)`` when no checkpoint exists.
        """
        import base64

        path = ws.artifacts_dir / "refine_checkpoint.json"
        if not path.exists():
            return None, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, KeyError:
            log.warning("refine checkpoint corrupt — ignoring")
            return None, None

        conv_state: bytes | None = None
        if data.get("conversation_state_b64"):
            try:
                conv_state = base64.b64decode(data["conversation_state_b64"])
            except Exception:
                conv_state = None

        children = None
        if data.get("children"):
            children = [
                refining.ChildSpec(
                    title=c["title"],
                    spec_markdown=c["spec_markdown"],
                    depends_on=c.get("depends_on", []),
                )
                for c in data["children"]
            ]
        file_map = None
        if data.get("file_map"):
            file_map = [
                refining.FileMapEntry(file=e["file"], note=e["note"])
                for e in data["file_map"]
            ]

        result = refining.RefineResult(
            spec_markdown=data.get("spec_markdown"),
            split=data.get("split", False),
            children=children,
            promote_to_epic=data.get("promote_to_epic", False),
            epic_body=data.get("epic_body"),
            updated_memory=data.get("updated_memory", ""),
            file_map=file_map,
            title=data.get("title"),
            reference_files=data.get("reference_files", []),
            conversation_state=conv_state,
        )
        return result, conv_state

    @staticmethod
    def _clear_refine_checkpoint(ws: Workspace) -> None:
        """Remove the error-recovery checkpoint when refine completes."""
        path = ws.artifacts_dir / "refine_checkpoint.json"
        if path.exists():
            path.unlink()

    # -- phase: run the refine agent + pause detection ----------------------

    @staticmethod
    def _run_and_collect(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        epic_ctx: str,
        ws: Workspace,
        s: Settings,
        extra_roots: list[Path] | None,
        reviewer_comments: str | None,
    ) -> tuple[Outcome | None, refining.RefineResult | None]:
        """Invoke ``refining.run_refine_agent`` and handle pause/errors.

        Resolves memory, resume-from-pause history, and the deployed-log
        folder, runs the agent, then handles transient/fatal RuntimeErrors
        and pause detection.  Returns ``(outcome, result)`` — exactly one is
        non-``None``: an :class:`Outcome` to short-circuit, or the
        ``RefineResult`` to continue with.
        """
        # Meta tickets have no registered repo_config; their memory ledger
        # is keyed on the ticket's own board_id ("meta"). Every other board
        # uses its repo_config.board_id.
        memory_board_id = (
            ctx.repo_config.board_id if ctx.repo_config else ticket.board_id
        )
        memory_text = _load_refine_memory(s, memory_board_id)

        # extra_roots is passed in (non-empty for meta-board multi-repo
        # workspaces; None for the normal single-repo path).

        # --- resume awareness: detect if returning from a pause ---
        resume_history: list | None = None
        saved_state = load_conversation_state(ws, "refine")
        if saved_state is not None:
            # Check whether the ticket is resuming from a pause by
            # looking for a prior AWAITING_USER_REPLY event in the
            # ticket history.
            own_history = ctx.service.history(ticket.id)
            was_paused = any(
                ev.state == State.AWAITING_USER_REPLY for ev in own_history
            )
            if was_paused:
                # Collect operator replies from every closed [ASK_USER]
                # thread.  The agent may have asked multiple questions
                # across pause/resume cycles; each answered question
                # contributes its replies.
                reply_text = _collect_ask_user_replies(ctx, ticket)
                resume_history = build_resume_message_history(
                    saved_state,
                    reply_text,
                )
                log.info(
                    "%s: resuming refine from pause — "
                    "loaded %d-byte conversation state",
                    ticket.id,
                    len(saved_state),
                )

        # --- error-recovery checkpoint: skip the agent if a prior run
        # produced a usable result before being interrupted ---
        if saved_state is None:
            # Not resuming from a pause — check for a refine checkpoint
            # saved from a prior run that was interrupted after the agent
            # call succeeded but before post-processing completed.
            checkpoint_result, _ = RefineAgentMixin._load_refine_checkpoint(ws)
            if checkpoint_result is not None:
                log.info(
                    "%s: resuming refine from error-recovery checkpoint — "
                    "skipping agent call",
                    ticket.id,
                )
                return None, checkpoint_result

        from ...config.repo_settings import (
            resolve_language_instructions,
            warn_if_deprecated_log_folder,
        )

        language_instructions = resolve_language_instructions(s, repo_dir)

        # --- deployed log folder (refine-only) ---
        # Deployment-specific host path: sourced from the operator's central
        # ``config/repos.yaml`` (RepoConfig), NOT the managed repo's committed
        # ``.robotsix-mill/config.yaml`` (a host path must not be committed).
        warn_if_deprecated_log_folder(repo_dir)
        deployed_log_folder_str = (
            ctx.repo_config.deployed_log_folder if ctx.repo_config else None
        )
        if deployed_log_folder_str is not None:
            deployed_log_folder_str = deployed_log_folder_str.strip() or None
        deployed_log_summary = ""
        deployed_log_dir: Path | None = None
        if deployed_log_folder_str is not None:
            log_path = Path(deployed_log_folder_str)
            if not log_path.is_absolute():
                log.warning(
                    "%s: deployed_log_folder '%s' is relative — "
                    "resolving against repo_dir (absolute path is canonical)",
                    ticket.id,
                    deployed_log_folder_str,
                )
                log_path = (repo_dir / log_path).resolve()
            else:
                log_path = log_path.resolve()
            if log_path.is_dir():
                # Append to extra_roots so the agent's filesystem tools
                # can access files under the deployed log folder.
                if extra_roots is None:
                    extra_roots = [log_path]
                else:
                    extra_roots = list(extra_roots) + [log_path]
                deployed_log_summary = _build_deployed_log_summary(
                    log_path, deployed_log_folder_str
                )
                deployed_log_dir = log_path
            else:
                log.warning(
                    "%s: deployed_log_folder '%s' (resolved to '%s') "
                    "does not exist or is not a directory — skipping",
                    ticket.id,
                    deployed_log_folder_str,
                    log_path,
                )

        try:
            # Read the triage complexity verdict to gate exploration tools.
            triage_complexity = _read_triage_complexity(ws)
            _explore_simple = triage_complexity == "simple"

            # Read the triage exploration findings so the refine agent
            # can skip re-exploring files/symbols the classifier already
            # verified.
            triage_findings = _read_triage_findings(ws)

            # Read the trivial-scope verdict to route the refine model level.
            # Because `ws.artifacts_dir` persists across refine rounds (the
            # workspace is keyed on `ticket.id` and never wiped), this file
            # — written by the FIRST refine round — is still present on every
            # later re-refine.  Therefore `_read_triage_trivial(ws)` already
            # reflects the first-run verdict; a first-run-trivial ticket stays
            # on the cheap model during re-refine with no extra logic.
            _trivial = _read_triage_trivial(ws)
            refine_level: int | None = None
            if s.refine_trivial_routing_enabled and _trivial:
                refine_level = s.refine_trivial_model_level

            # Re-refine round counter: force the cheap model after a
            # configurable threshold of operator "changes requested"
            # send-backs.  This caps the cost of repeated full-Opus
            # re-refine runs when the triage verdict was non-trivial
            # (or triage was disabled / short-circuited).  The counter
            # is independent of the persisted triage verdict so it
            # catches re-refine runaway regardless of first-run
            # classification.
            if (
                reviewer_comments
                and s.max_re_refine_cycles_before_cheap > 0
                and refine_level is None  # not already downgraded above
            ):
                re_refine_rounds = sum(
                    1
                    for ev in ctx.service.history(ticket.id)
                    if ev.state == State.DRAFT
                    and ev.note
                    and ev.note.startswith(OPERATOR_SENDBACK_PREFIX)
                )
                if re_refine_rounds >= s.max_re_refine_cycles_before_cheap:
                    refine_level = s.refine_trivial_model_level
                    set_current_span_attribute("refine.forced_cheap_re_refine", True)

            # Record the routing decision on the current span for Langfuse.
            set_current_span_attribute(
                "refine.model_level", refine_level if refine_level is not None else 3
            )
            set_current_span_attribute("refine.routed_trivial", _trivial)

            # Compute the Claude model alias for level-3 non-trivial refines.
            # Gate: feature flag ON, not already downgraded to DeepSeek.
            refine_model: str | None = None
            request_limit_override: int | None = None
            if refine_level is None and s.refine_subscription_tier_routing_enabled:
                if triage_complexity == "simple":
                    refine_model = s.refine_subscription_model_default
                    request_limit_override = s.refine_request_limit_simple
                else:
                    refine_model = s.refine_subscription_model_complex

            set_current_span_attribute(
                "refine.model_alias", refine_model if refine_model else "opus"
            )

            # When reviewer comments are present (sendback path), disable
            # exploration sub-agents — the agent's only job is text-level
            # spec revision against the reviewer's feedback.
            _sendback = bool(reviewer_comments)
            result = refining.run_refine_agent(
                settings=s,
                title=ticket.title,
                draft=draft,
                repo_dir=repo_dir,
                repo_config=ctx.repo_config,
                reviewer_comments=reviewer_comments,
                memory=memory_text,
                epic_context=epic_ctx,
                extra_roots=extra_roots,
                message_history=resume_history,
                board_id=memory_board_id,
                current_ticket_id=ticket.id,
                language_instructions=language_instructions,
                deployed_log_summary=deployed_log_summary,
                deployed_log_dir=deployed_log_dir,
                screenshot_paths=ws.list_screenshots(),
                include_explore=not _explore_simple and not _sendback,
                include_parallel_explore=not _explore_simple and not _sendback,
                refine_level=refine_level,
                refine_model=refine_model,
                request_limit_override=request_limit_override,
                triage_findings=triage_findings,
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            # ModelHTTPError subclasses RuntimeError, so a transient model
            # blip (OpenRouter 5xx/429/timeout, DeepSeek reasoning-400) is
            # caught here too — re-raise it so the worker stage-retries a
            # fresh refine run instead of a hard BLOCK. Fatal RuntimeErrors
            # (missing API key) fall through and block as before.
            from ...runtime.transient_errors import reraise_if_transient

            reraise_if_transient(e)
            return Outcome(State.BLOCKED, str(e)), None

        # --- save error-recovery checkpoint ---
        # Persist the refine result so a resume-from-BLOCKED can skip
        # re-running the expensive agent call.  The checkpoint is cleared
        # when the stage completes successfully (see ``run()``).
        RefineAgentMixin._save_refine_checkpoint(ws, result)

        # --- pause detection ---
        # check_for_pause looks at THIS run's new messages so an old
        # ask_user sentinel from a prior turn (still in the saved
        # transcript on resume) doesn't re-trigger. The full transcript
        # (``conversation_state``) is still what gets persisted for
        # resume.
        if check_for_pause(result.new_messages):
            save_conversation_state(ws, result.conversation_state, "refine")
            ctx.service.transition(
                ticket.id,
                State.AWAITING_USER_REPLY,
                note="paused — agent asked a clarifying question",
            )
            log.info(
                "%s: paused refine — agent invoked ask_user",
                ticket.id,
            )
            return Outcome(State.AWAITING_USER_REPLY), None

        # Refine produced a normal output (no pause) — clear any stale
        # saved state from earlier pause/resume cycles so it cannot leak
        # into downstream stages as a phantom resume context.
        clear_conversation_state(ws, "refine")
        return None, result

    # -- phase: gitignored file_map guard -----------------------------------

    @staticmethod
    def _gitignored_guard(
        ticket: Ticket, result: refining.RefineResult, repo_dir: Path | None
    ) -> Outcome | None:
        """Reject a spec whose deliverable files target gitignored paths.

        Deterministically reject a spec whose deliverable files target
        paths gitignored in the repo clone (e.g. a manifest board whose
        ``.gitignore`` carries ``/src/*`` for vcs-imported sub-repos).
        Those edits would land on disk but be invisible to git, dying at
        implement as an opaque "no changes produced" block. Catch it here
        — before any memory/title/epic side-effects — with an actionable
        note. Meta/multi-repo workspaces are skipped: a path tracked in
        one clone can look ignored relative to another, and robust
        per-repo resolution belongs with manifest-aware delivery.
        """
        if ticket.board_id != "meta" and result.file_map and repo_dir is not None:
            blocked = git_ops.ignored_paths(repo_dir, [e.file for e in result.file_map])
            if blocked:
                hit_list = ", ".join(f"`{p}`" for p in blocked)
                return Outcome(
                    State.BLOCKED,
                    f"refine produced a spec targeting gitignored path(s): "
                    f"{hit_list}. This board cannot deliver changes there — the "
                    "paths are vcs-imported / vendored sub-trees (e.g. `/src/*` "
                    "managed via repos.yaml), invisible to git. Re-scope the "
                    "spec to target git-tracked files in this repo (e.g. the "
                    "manifest / repos.yaml and the board's own sources), not "
                    "the cloned workspace sources.",
                )
        return None

    # -- phase: persist agent output side-effects ---------------------------

    @staticmethod
    def _apply_agent_side_effects(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws: Workspace,
        s: Settings,
        epic_ctx: str,
        result: refining.RefineResult,
    ) -> None:
        """Persist memory, title, epic body, draft, and artifact files.

        Runs after the gitignored guard for every non-short-circuit path:
        updated memory, an agent-supplied title, the non-split epic body,
        the raw-draft preservation, and the ``file_map`` / ``reference_files``
        artifacts.
        """
        if result.updated_memory:
            memory_board_id = (
                ctx.repo_config.board_id if ctx.repo_config else ticket.board_id
            )
            _persist_refine_memory(s, memory_board_id, result.updated_memory)

        if result.title and result.title.strip():
            ctx.service.set_title(ticket.id, result.title.strip())

        # --- epic body handling (non-split path) ---
        # In autonomous mode: apply immediately to the epic.
        # In gated mode: store as artifact in child workspace for
        # later application on approval.
        if result.epic_body and result.epic_body.strip() and epic_ctx:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == TicketKind.EPIC:
                if not ctx.settings.require_approval:
                    new_hash = ctx.service.workspace(parent).write_description(
                        result.epic_body.strip()
                    )
                    ctx.service.set_content_hash(parent.id, new_hash)
                else:
                    (ws.artifacts_dir / "epic-body-proposed.md").write_text(
                        result.epic_body.strip(), encoding="utf-8"
                    )

        # --- preserve the raw draft (always, for traceability) ---
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )

        # --- write file map artifact ---
        if result.file_map:
            RefineAgentMixin._write_file_map(
                ws, [{"file": e.file, "note": e.note} for e in result.file_map]
            )

        # --- write reference_files artifact ---
        if result.reference_files:
            ref_path = ws.artifacts_dir / "reference_files.json"
            ref_path.write_text(
                json.dumps(
                    [{"path": p} for p in result.reference_files],
                    indent=2,
                ),
                encoding="utf-8",
            )

    # -- phase: no-change-needed --------------------------------------------

    @staticmethod
    def _no_change_path(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        title: str,
        ws: Workspace,
        result: refining.RefineResult,
    ) -> Outcome | None:
        """Handle the ``no_change_needed`` result mode.

        When refine concludes the spec is informational — full
        investigation already in the body, acceptance criteria are
        "post a comment explaining why no change is needed", or a
        parallel ticket already shipped the fix — it returns
        no_change_needed=true. The stage files the rationale as a
        top-level comment on the ticket and transitions
        DRAFT → DONE, skipping implement / review / document /
        deliver / merge. This is the bypass that catches the
        d129-style "implement gets stuck because there's nothing
        to write" failure mode.

        Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
        through (no-change does not apply / degrades to the normal paths).
        """
        from robotsix_mill.stages import refine as _facade

        if not (
            result.no_change_needed and not result.split and not result.promote_to_epic
        ):
            return None

        rationale = (result.no_change_rationale or "").strip()
        if not rationale:
            # Degrade to single-spec; the operator can see the
            # spec and decide. Don't transition to DONE on an
            # empty rationale — that would close the ticket with
            # no explanation, which is worse than a normal
            # approval.
            log.warning(
                "%s: no_change_needed but no rationale — "
                "degrading to normal single-spec path",
                ticket.id,
            )
            return None

        # If this ticket was previously implemented (has a
        # branch), verify the implementation is actually
        # merged to the base branch before closing as DONE.
        # Otherwise the work lives only on an orphaned
        # branch and will be lost when the ticket closes.
        if ticket.branch and not _facade._verify_branch_merged(repo_dir, ticket):
            return Outcome(
                State.BLOCKED,
                f"{UNMERGED_BRANCH_PREFIX} '{ticket.branch}' "
                "but is not merged to main. "
                "Merge the PR or manually close.",
            )

        # Live re-verification gate: an "already shipped
        # elsewhere" rationale (from the LLM refine agent) is NOT
        # trusted on its word. A reverted fix leaves the original
        # commit as an ancestor of origin/main, so ancestry alone
        # cannot detect the bug's return (the 2026-06-09 incident).
        # Synthesize a verification spec and route to implement,
        # which works against live HEAD and re-applies the fix if
        # the bug recurred (or cheaply closes via its empty-diff
        # path if genuinely resolved).
        if _rationale_claims_external_fix(rationale):
            cited_refs = _TICKET_ID_RE.findall(rationale) + _COMMIT_SHA_RE.findall(
                rationale.lower()
            )
            cited = (
                ", ".join(dict.fromkeys(cited_refs))
                or "the prior ticket / commit named in the rationale"
            )
            ancestry_ok = _verify_cited_fix_at_head(repo_dir, rationale)
            log.info(
                "%s: no_change_needed rationale claims an external "
                "fix (%s) — routing to implement for live re-check "
                "(cited-commit ancestry check: %s)",
                ticket.id,
                cited,
                "passed (NOT sufficient — see revert subtlety)"
                if ancestry_ok
                else "not proven",
            )
            verification_spec = (
                "## Problem\n\n"
                "A prior refine pass concluded this ticket needs no "
                "change because the fix was already shipped elsewhere "
                f"({cited}). That claim was NOT verified against the "
                "live tree. A `git revert` re-introduces a bug while "
                "leaving the original fix commit as an ancestor of "
                "`origin/main`, so the cited fix may not actually be "
                "present at HEAD — re-verify before closing.\n\n"
                f"Original ticket: {title}\n\n"
                "Original problem / draft:\n\n"
                f"{draft or '(no draft body)'}\n\n"
                "Refine's unverified rationale:\n\n"
                f"{rationale}\n\n"
                "## Scope\n\n"
                "Inspect the relevant file(s) / condition named in the "
                "original problem at the current HEAD and determine "
                "whether the bug condition is still present.\n\n"
                "## Acceptance criteria\n\n"
                "- If the bug condition is still present at HEAD (e.g. "
                "the cited fix was reverted or overwritten), re-apply "
                "the fix so the condition is resolved (with a test "
                "where appropriate).\n"
                "- If the condition is genuinely already resolved at "
                "HEAD, make no change — the implement empty-diff path "
                "will close the ticket.\n\n"
                "## Out of scope / constraints\n\n"
                "- Do not expand scope beyond verifying and (if needed) "
                f"re-applying the fix for: {title}.\n"
                "- Ancestry of the cited commit is NOT sufficient proof "
                "(a revert leaves it an ancestor); verify the actual "
                "bug condition against the working tree.\n"
            )
            new_hash = ws.write_description(verification_spec)
            ctx.service.set_content_hash(ticket.id, new_hash)
            return RefineAgentMixin._resolved_outcome(
                ctx,
                verification_spec,
                ticket.id,
                "refined | unverified 'already implemented' claim "
                "routed to implement for live re-check",
                source=ticket.source,
            )

        # The rationale is the agent's conclusion — into
        # history (note), not comments. Truncate to keep the
        # event row scannable; the full rationale lives in
        # the refine artifact (draft-original.md captures
        # spec-shape context too).
        short = rationale[:400] + ("…" if len(rationale) > 400 else "")
        return Outcome(
            State.DONE,
            f"no change needed — {short}",
        )

    # -- phase: promote-to-epic ---------------------------------------------

    @staticmethod
    def _promote_to_epic_path(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws: Workspace,
        s: Settings,
        result: refining.RefineResult,
    ) -> Outcome:
        """Handle the ``promote_to_epic`` result mode.

        When refine decides the spec is too varied for one pass
        (manifest-driven, ≥6 children, per-item deep specs needed),
        it returns promote_to_epic=True. The stage converts the
        ticket to an epic, writes the strategic epic_body to the
        workspace description, and synchronously invokes
        epic-breakdown to spawn the children. After that the epic
        sits in EPIC_OPEN — its children flow through refine
        individually on their own cycles.
        """
        from ...agents.epic_breakdown import (
            plan_child_dependencies,
            run_epic_breakdown_agent,
        )

        epic_body = (result.epic_body or result.spec_markdown or "").strip()
        if not epic_body:
            log.warning(
                "%s: promote_to_epic but no epic_body — falling back to original draft",
                ticket.id,
            )
            epic_body = draft or ticket.title
        new_hash = ws.write_description(epic_body)
        ctx.service.set_content_hash(ticket.id, new_hash)
        ctx.service.promote_to_epic(ticket.id)
        try:
            breakdown = run_epic_breakdown_agent(
                settings=s,
                epic_title=ticket.title,
                epic_description=epic_body,
            )
            # Advisory pre-filing dedup: flag (never drop) children
            # whose scope overlaps a recent ticket or an earlier
            # sibling in this batch. Best-effort — a failure here must
            # not block filing.
            from ...core.dedup import annotate_child_body, find_child_overlaps

            child_titles = list(breakdown.child_titles)
            child_bodies = list(breakdown.child_bodies)
            overlap_notes = find_child_overlaps(
                ctx.service,
                ticket.id,
                child_titles,
                child_bodies,
                s,
                datetime.now(timezone.utc),
            )
            created_children: list[tuple[str, str, str]] = []
            for child_title, child_body, dup_note in zip(
                child_titles,
                child_bodies,
                overlap_notes,
                strict=True,
            ):
                if dup_note:
                    log.warning(
                        "epic %s: child '%s' flagged as possible duplicate — %s",
                        ticket.id,
                        child_title,
                        dup_note,
                    )
                    child_body = annotate_child_body(child_body, dup_note)
                child = ctx.service.create(
                    title=child_title,
                    description=child_body,
                    kind=TicketKind.TASK,
                    parent_id=ticket.id,
                )
                created_children.append((child.id, child_title, child_body))
            created_ids = [cid for cid, _t, _b in created_children]
            # Dependency wiring: a linear chain (C0 → C1 → C2 → …) by
            # default — matching the /generate-children route — but
            # when the batch includes a create/initialize-repo child
            # the repo-populating siblings depend on it so they cannot
            # run before the repo exists.  Cross-repo producer→consumer
            # edges and bump-child synthesis are also applied when
            # children target different repos.
            for child_id, deps in plan_child_dependencies(
                created_children,
                child_board_id=lambda cid: (
                    _t.board_id
                    if (_t := ctx.service.get(cid)) is not None
                    else ctx.service.board_id
                ),
                create_child=lambda title, body: (
                    ctx.service.create(
                        title=title,
                        description=body,
                        kind=TicketKind.TASK,
                        parent_id=ticket.id,
                    ).id
                ),
            ).items():
                ctx.service.set_depends_on(child_id, deps)
            # Apply the breakdown's revised epic body if any.
            if breakdown.epic_body and breakdown.epic_body.strip():
                revised_hash = ws.write_description(
                    breakdown.epic_body.strip(),
                )
                ctx.service.set_content_hash(ticket.id, revised_hash)
            note = f"promoted to epic; spawned {len(created_ids)} child(ren)"
        except Exception:
            log.exception(
                "%s: epic-breakdown after promote_to_epic failed — "
                "epic body is in place, children left for "
                "/generate-children",
                ticket.id,
            )
            note = (
                "promoted to epic; breakdown failed — use /generate-children to retry"
            )
        return Outcome(State.EPIC_OPEN, note)

    # -- phase: normal single-scope -----------------------------------------

    @staticmethod
    def _single_scope_path(
        ctx: StageContext,
        ticket: Ticket,
        ws: Workspace,
        s: Settings,
        result: refining.RefineResult,
        reviewer_comments: str | None,
        open_thread_ids: set[int],
    ) -> Outcome:
        """Handle the normal (non-split) single-scope result."""
        spec = result.spec_markdown or ""
        if _spec_is_degenerate(spec):
            log.warning(
                "%s: refiner produced no usable spec (empty or "
                "placeholder %r) — proceeding with original draft",
                ticket.id,
                spec[:60],
            )
            next_state, _auto_reason = _resolve_next_state(
                ctx, "", ticket.id, source=ticket.source
            )
            return Outcome(next_state, "refined (no usable spec — kept original draft)")

        # --- spec review (conciseness pass) ---
        if s.spec_review_enabled and not reviewer_comments:
            spec = RefineAgentMixin._review_spec_conciseness(
                s, ws, ticket, spec, "refine-verbose.md"
            )

        new_hash = ws.write_description(spec)
        ctx.service.set_content_hash(ticket.id, new_hash)

        # --- post-agent thread acknowledgment ---
        RefineAgentMixin._ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)

        return RefineAgentMixin._resolved_outcome(
            ctx, spec, ticket.id, "refined", source=ticket.source
        )

    # -- phase: multi-scope split -------------------------------------------

    @staticmethod
    def _multi_scope_path(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws: Workspace,
        s: Settings,
        epic_ctx: str,
        result: refining.RefineResult,
        reviewer_comments: str | None,
        open_thread_ids: set[int],
    ) -> Outcome:
        """Handle the multi-scope split result (validate, split, reparent)."""
        children_raw = result.children
        if not children_raw or len(children_raw) == 0:
            # Degrade gracefully: treat as single-spec with whatever we got.
            spec = result.spec_markdown or ""
            if _spec_is_degenerate(spec):
                log.warning(
                    "%s: refiner produced no usable spec "
                    "(split with no children) — "
                    "proceeding with original draft",
                    ticket.id,
                )
                next_state, _auto_reason = _resolve_next_state(
                    ctx, "", ticket.id, source=ticket.source
                )
                # --- post-agent thread acknowledgment ---
                RefineAgentMixin._ack_threads(
                    ctx, ticket, reviewer_comments, open_thread_ids
                )
                return Outcome(
                    next_state,
                    "refined (empty spec, split degraded — kept original draft)",
                )
            new_hash = ws.write_description(spec)
            ctx.service.set_content_hash(ticket.id, new_hash)

            # --- post-agent thread acknowledgment ---
            RefineAgentMixin._ack_threads(
                ctx, ticket, reviewer_comments, open_thread_ids
            )

            return RefineAgentMixin._resolved_outcome(
                ctx,
                spec,
                ticket.id,
                "refined (split degraded — no valid children)",
                source=ticket.source,
            )

        # Validate and collect valid children.
        valid_children: list[dict[str, Any]] = []
        for spec_child in children_raw:
            child_title = (spec_child.title or "").strip()
            spec_md = (spec_child.spec_markdown or "").strip()
            if not child_title or not spec_md:
                continue
            deps = spec_child.depends_on or []
            if not isinstance(deps, list):
                deps = []
            # Keep only non-negative integer indices.
            deps = [d for d in deps if isinstance(d, int) and d >= 0]
            valid_children.append(
                {
                    "title": child_title,
                    "spec_markdown": spec_md,
                    "depends_on": deps,
                }
            )

        if len(valid_children) == 0:
            # --- post-agent thread acknowledgment ---
            RefineAgentMixin._ack_threads(
                ctx, ticket, reviewer_comments, open_thread_ids
            )
            return Outcome(State.BLOCKED, "refiner produced no valid split children")

        # --- spec review for split children (conciseness pass) ---
        if s.spec_review_enabled and not reviewer_comments:
            for i, child in enumerate(valid_children):
                child["spec_markdown"] = RefineAgentMixin._review_spec_conciseness(
                    s,
                    ws,
                    ticket,
                    child["spec_markdown"],
                    f"refine-verbose-child-{i + 1}.md",
                    child_index=i + 1,
                )

        if len(valid_children) == 1:
            # Only one valid child — fall back to single-spec path.
            child = valid_children[0]
            new_hash = ws.write_description(child["spec_markdown"])
            ctx.service.set_content_hash(ticket.id, new_hash)
            # Update the ticket title: agent's explicit title beats
            # the child's title (which is a fallback).
            if not (result.title and result.title.strip()):
                ctx.service.set_title(ticket.id, child["title"])

            # --- post-agent thread acknowledgment ---
            RefineAgentMixin._ack_threads(
                ctx, ticket, reviewer_comments, open_thread_ids
            )

            return RefineAgentMixin._resolved_outcome(
                ctx,
                child["spec_markdown"],
                ticket.id,
                "refined (single child, no split)",
                source=ticket.source,
            )

        # Create child tickets.
        child_ids: list[str] = []
        for _i, child in enumerate(valid_children):
            child_ticket = ctx.service.create(
                title=child["title"],
                description=child["spec_markdown"],
                source=ticket.source,
                board_id=ticket.board_id,
            )
            child_ids.append(child_ticket.id)

        # Reparent children: if the ticket already belongs to an
        # epic, reparent to that epic; otherwise create a new
        # umbrella epic so children appear under a visible grouping
        # entity rather than a closed parent.
        existing_epic_id: str | None = None
        if ticket.parent_id is not None:
            parent_candidate = ctx.service.get(ticket.parent_id)
            if (
                parent_candidate is not None
                and parent_candidate.kind == TicketKind.EPIC
            ):
                existing_epic_id = ticket.parent_id
                for cid in child_ids:
                    ctx.service.set_parent(cid, existing_epic_id)
        if existing_epic_id is None:
            epic_title = (result.title and result.title.strip()) or ticket.title.strip()
            epic_desc = (result.spec_markdown and result.spec_markdown.strip()) or draft
            epic = ctx.service.create(
                title=epic_title,
                description=epic_desc,
                kind=TicketKind.EPIC,
                source=ticket.source,
                board_id=ticket.board_id,
            )
            for cid in child_ids:
                ctx.service.set_parent(cid, epic.id)

        # Resolve depends_on indices → real ticket IDs.
        for i, child in enumerate(valid_children):
            if child["depends_on"]:
                resolved = []
                for idx in child["depends_on"]:
                    if 0 <= idx < i and idx < len(child_ids):
                        resolved.append(child_ids[idx])
                if resolved:
                    ctx.service.set_depends_on(child_ids[i], resolved)

        # Transition each child to HUMAN_ISSUE_APPROVAL or READY.
        for i, cid in enumerate(child_ids):
            child_state, auto_note = _resolve_next_state(
                ctx,
                valid_children[i]["spec_markdown"],
                cid,
            )
            child_note = f"split from {ticket.id}"
            if auto_note:
                child_note += f" | {auto_note}"
            ctx.service.transition(cid, child_state, note=child_note)

        # Apply epic body immediately in split path regardless of
        # require_approval — the children each go through their own
        # approval flow, and the original ticket is closed so there
        # is no single approval event to gate on.
        if result.epic_body and result.epic_body.strip() and epic_ctx:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == TicketKind.EPIC:
                new_hash = ctx.service.workspace(parent).write_description(
                    result.epic_body.strip()
                )
                ctx.service.set_content_hash(parent.id, new_hash)

        # Close the original ticket.
        ids_note = ", ".join(child_ids)

        # --- post-agent thread acknowledgment ---
        RefineAgentMixin._ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)

        return Outcome(
            State.CLOSED,
            f"split into {ids_note}",
        )
