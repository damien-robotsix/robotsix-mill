"""The :class:`RefineStage` orchestrator.

Assembles the gate phases (:class:`RefineGatesMixin`) and the refine-agent
pipeline (:class:`RefineAgentMixin`) into the public ``Stage`` subclass and
holds ``run`` (the orchestrator) plus ``_clone_or_resume``.
"""

from __future__ import annotations


from pathlib import Path

from ...agents import refining
from ...config import target_branch_for
from ...core.constants import NON_IMPLEMENTATION_CLOSE_PREFIXES
from ...core.models import Ticket, TicketKind
from ...core.states import State
from ...forge.auth import github_token
from ...vcs import git_ops
from ..base import Outcome, Stage, StageContext
from .gates import RefineGatesMixin
from .helpers import log
from .orchestration import RefineAgentMixin


class RefineStage(RefineGatesMixin, RefineAgentMixin, Stage):
    """Refine a draft ticket into a detailed, self-contained engineering specification."""

    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a DRAFT ticket: gate on dependencies, refine the draft into a self-contained engineering spec (or split into children / promote to epic) via the refining agent."""
        ws = ctx.service.workspace(ticket)
        draft = ws.read_description().strip()
        epic_ctx = ctx.service.get_epic_context(ticket)
        title = ticket.title.strip()
        if not title and not draft:
            return Outcome(State.BLOCKED, "empty title and draft — nothing to refine")

        # --- dependency gate: refuse to refine until all deps are
        # terminal (CLOSED/DONE). Same-state no-op → the reconcile
        # sweep re-enqueues this ticket each poll cycle.
        unmet = ctx.service.unmet_dependencies(ticket)
        if unmet:
            log.debug(
                "%s: unmet dependencies — deferring refine: %s",
                ticket.id,
                unmet,
            )
            return Outcome(State.DRAFT)

        s = ctx.settings

        # --- triage phase 0: maintenance keyword check (before clone) ---
        # Deterministic, no LLM, no workspace.  When the draft requests
        # a create-repo, fork-repo, or cross-repo investigation, route
        # directly to MAINTENANCE — skip the clone + full triage.
        if s.maintenance_triage_enabled:
            action = refining._classify_maintenance_draft(title, draft)
            if action is not None:
                # Preserve the original draft as an artifact for
                # traceability (mirrors the triage-SKIP and normal-refine
                # paths).
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                # Tag the ticket with a maintenance:$action label so the
                # maintenance stage can optionally dispatch on action type
                # without re-parsing the draft.
                try:
                    ctx.service.set_labels(ticket.id, [f"maintenance:{action}"])
                except Exception:
                    log.warning(
                        "%s: set_labels failed for maintenance triage — "
                        "continuing anyway",
                        ticket.id,
                        exc_info=True,
                    )
                return Outcome(
                    State.MAINTENANCE,
                    f"maintenance triage: routed to MAINTENANCE "
                    f"(action={action}) — {title}",
                )
        # --- end triage phase 0 ---

        # Phase 1: build the workspace. A meta-board ticket is cross-repo:
        # a triage agent picks the required registered repos and we clone
        # those into a multi-repo workspace (repo_dir = first, extra_roots =
        # all), so the refine agent can read across them. Every other board
        # is the normal single-repo clone.
        extra_roots: list[Path] | None = None
        if ticket.board_id == "meta":
            from ...meta.workspace import build_triaged_meta_workspace

            repo_dir, extra_roots, outcome = build_triaged_meta_workspace(
                ctx, ticket, ws, draft, author="refine"
            )
            if outcome is not None:
                return outcome
        else:
            result = RefineStage._clone_or_resume(ctx, ticket, ws)
            if isinstance(result, Outcome):
                return result
            repo_dir = result

        # --- prepare hook: let the repo run custom setup after clone,
        # before any agent executes ---
        if repo_dir is not None:
            from ...hooks import run_prepare_hook

            hook_error = run_prepare_hook(repo_dir, ticket.id, ws.dir)
            if hook_error is not None:
                return Outcome(State.BLOCKED, hook_error)

        # Phase 2: freshness gate — verify cited evidence against HEAD
        # before spending any LLM budget on refine.  Runs before the
        # dedup guard because it is deterministic (no LLM call).
        stale = RefineStage._run_freshness_gate(ctx, ticket, draft, repo_dir, s)
        if stale is not None:
            return RefineStage._guard_implementation_done(ctx, ticket, stale)

        # Phase 2.1: mill-misroute gate — detect drafts that name
        # mill-specific source paths absent from this checkout and
        # redirect them to the mill maintenance board.  Deterministic,
        # no LLM; runs before the first LLM-invoking gate.
        misrouted = RefineStage._run_mill_misroute_gate(ctx, ticket, draft, repo_dir, s)
        if misrouted is not None:
            return RefineStage._guard_implementation_done(ctx, ticket, misrouted)

        # Phase 2.2: triage classifier — a single cheap LLM call that
        # classifies the draft as SKIP / NO_CHANGE / MAINTENANCE /
        # REFINE.  Run BEFORE any expensive LLM gates (obsolescence,
        # dedup) so tickets that are already satisfied on disk
        # (NO_CHANGE) or already-precise specs (SKIP) short-circuit
        # without wasting LLM budget on the dedup check or full refine
        # agent.  Collect reviewer comments first so we don't run triage
        # on sendback drafts (human feedback always goes through full
        # refine).
        reviewer_comments, _ = RefineStage._collect_reviewer_comments(ctx, ticket)
        triage = RefineStage._triage_skip(
            ctx, ticket, draft, repo_dir, extra_roots, title, ws, s, reviewer_comments
        )
        if triage is not None:
            return RefineStage._guard_implementation_done(ctx, ticket, triage)

        # Phase 2.5: obsolescence gate — for *spawned* follow-up drafts,
        # re-evaluate (via a cheap LLM call) whether the cited gap was
        # already resolved in place by a parallel/parent ticket.  Runs
        # after the deterministic freshness gate and before the dedup
        # guard.
        obsolete = RefineStage._run_obsolescence_gate(ctx, ticket, draft, repo_dir, s)
        if obsolete is not None:
            return RefineStage._guard_implementation_done(ctx, ticket, obsolete)

        # Phase 3: dedup guard
        dup = RefineStage._run_dedup_guard(ctx, ticket, draft, repo_dir, s)
        if dup is not None:
            return RefineStage._guard_implementation_done(ctx, ticket, dup)

        # Phase 3.5: advisory dedup against CONCURRENT in-flight tickets.
        # The dedup guard above can only close against a genuinely-DONE
        # candidate, so two drafts that converge while both in flight both
        # survive it.  This best-effort pass flags (never closes) such an
        # overlap so refine/the operator can decide.
        draft = RefineStage._run_inflight_advisory(ctx, ticket, draft, ws, s)

        # Phase 3.6: cheap verification of any carried advisory. Short-circuit
        # to DONE on a confirmed valid duplicate; otherwise clear the advisory
        # and proceed to the full refine.
        verified = RefineStage._verify_advisory_dedup(
            ctx, ticket, draft, repo_dir, ws, s
        )
        if isinstance(verified, Outcome):
            return RefineStage._guard_implementation_done(ctx, ticket, verified)
        draft = verified

        # Phase 4: refine agent + result handling
        outcome = RefineStage._run_refine_agent(
            ctx, ticket, draft, repo_dir, epic_ctx, title, ws, s, extra_roots
        )
        # Apply the implementation-DONE guard (defense-in-depth) and
        # clear the error-recovery checkpoint.
        outcome = RefineStage._guard_implementation_done(ctx, ticket, outcome)
        if outcome.next_state not in (State.BLOCKED, State.AWAITING_USER_REPLY):
            from .orchestration import RefineAgentMixin

            RefineAgentMixin._clear_refine_checkpoint(ws)
        return outcome

    @staticmethod
    def _guard_implementation_done(
        ctx: StageContext,
        ticket: Ticket,
        outcome: Outcome,
    ) -> Outcome:
        """Guard: refuse to auto-close a TASK ticket without a branch.

        When the refine stage (or one of its gates) produces a DONE
        outcome for a TASK-kind ticket that has no implementation
        branch, and the note does not signal a recognised
        non-implementation shortcut (dedup / freshness / obsolescence
        / misroute), redirect the ticket toward READY instead so
        implement verifies the claim against the live tree.

        This is the defense-in-depth counterpart to the per-path fixes
        in ``_result_paths.no_change_path``, ``_reconcile.reviewer_agreement_guard``,
        and ``_triage.triage_skip`` — it catches any future code path
        that tries to close an unimplemented feature ticket from DRAFT.
        """
        if (
            outcome.next_state == State.DONE
            and ticket.kind == TicketKind.TASK
            and not ticket.branch
        ):
            note_lower = (outcome.note or "").lower()
            if not note_lower.startswith(
                tuple(p.lower() for p in NON_IMPLEMENTATION_CLOSE_PREFIXES)
            ):
                log.warning(
                    "%s: DONE outcome blocked by implementation guard "
                    "(no branch, TASK kind) — redirecting to READY. "
                    "Original note: %s",
                    ticket.id,
                    outcome.note,
                )
                return Outcome(
                    State.READY,
                    f"refine guard: spec clear, routing to implement — "
                    f"(was: {outcome.note})",
                )
        return outcome

    @staticmethod
    def _clone_or_resume(
        ctx: StageContext, ticket: Ticket, ws
    ) -> Path | Outcome | None:
        """Resolve remote URL, reuse or clone repo, escalate clone failures.

        Returns the ``repo_dir`` ``Path`` when a clone exists or is
        successfully created.  On clone failure, adds a BLOCKED comment
        via ``ctx.service.add_comment`` and returns an ``Outcome``.
        Returns ``None`` when no ``remote_url`` is configured (caller
        treats ``None`` as "no repo available").
        """
        # Resolve through the package façade so a test that patches
        # ``robotsix_mill.stages.refine._resolve_remote_url`` (a module-level
        # seam in the pre-split module) still takes effect.
        from robotsix_mill.stages import refine as _facade

        s = ctx.settings
        remote_url = _facade._resolve_remote_url(s, ctx.repo_config)
        if not remote_url:
            return None

        cand = ws.dir / "repo"
        if (cand / ".git").exists():
            return cand  # idempotent: reuse an existing clone

        try:
            token = github_token(s, repo_config=ctx.repo_config)
        except RuntimeError:
            token = None  # no credentials configured — clone will fail
        git_ops.clone(
            remote_url,
            cand,
            target_branch_for(s, ctx.repo_config),
            token,
        )
        return cand
