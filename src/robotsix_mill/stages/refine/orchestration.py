"""Refine-agent orchestration for the refine stage.

A mixin (:class:`RefineAgentMixin`) holding ``_run_refine_agent`` — the
big phase that drives ``refining.run_refine_agent``, applies the
single-scope / split / promote-to-epic / no-change-needed result modes,
runs the spec-conciseness review, and handles pause/resume.  The
conciseness-review loop (previously duplicated across the single-spec and
split paths) is factored into ``review_spec_conciseness`` in
``_result_paths.py``.

``_run_refine_agent`` itself is a thin orchestrator: each logical phase
(reviewer-comment gather, split-child fast-path, triage skip, agent
invocation, agent-output side-effects, and the no-change / promote /
single-scope / multi-scope outcome paths) lives in its own sub-module
(``_triage.py``, ``_reconcile.py``, ``_result_paths.py``,
``_checkpoint.py``), and the repeated ``Outcome`` + thread-acknowledgment
+ ``file_map.json`` write patterns are factored into
``resolved_outcome``, ``ack_threads``, and ``write_file_map`` in the
respective sub-modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ...agents import refining
from ...config.settings import Settings
from ...core.models import Ticket
from ...core.states import State
from ...core.workspace import Workspace
from ...runtime.tracing import set_current_span_attribute
from ..base import Outcome, StageContext
from ..pause import (
    acknowledge_unanswered_threads,  # noqa: F401 — re-exported for test monkeypatch compat
    build_resume_message_history,
    check_for_pause,
    clear_conversation_state,
    load_conversation_state,
    save_conversation_state,
    _collect_ask_user_replies,
)
from . import _checkpoint
from . import _reconcile
from . import _result_paths
from . import _triage
from .helpers import (
    OPERATOR_SENDBACK_PREFIX,
    _build_deployed_log_summary,
    _load_refine_memory,
    _persist_refine_memory,  # noqa: F401 — re-exported for test monkeypatch compat
    log,
)

# Re-export triage I/O helpers for backward compatibility (tests import
# these symbols from the orchestration module).
from ._reconcile import (  # noqa: F401
    read_triage_complexity as _read_triage_complexity,
    read_triage_findings as _read_triage_findings,
    read_triage_trivial as _read_triage_trivial,
    write_triage_complexity as _write_triage_complexity,
)
from ._reconcile import persist_triage_complexity as _persist_triage_complexity  # noqa: F401
from ._triage import (  # noqa: F401
    _MIGRATE_NOTE_PREFIX,
    _anti_bounce_escalate,
    _parse_prior_boards,
    is_sendback_reentry as _is_sendback_reentry,
)


class RefineAgentMixin:
    """Refine-agent pipeline staticmethods mixed into :class:`RefineStage`."""

    # -- delegation methods (thin wrappers around sub-module functions) ------

    @staticmethod
    def _review_spec_conciseness(
        s: Settings,
        ws: Workspace,
        ticket: Ticket,
        spec: str,
        verbose_filename: str,
        child_index: int | None = None,
    ) -> str:
        """Delegate to :func:`_result_paths.review_spec_conciseness`."""
        return _result_paths.review_spec_conciseness(
            s, ws, ticket, spec, verbose_filename, child_index=child_index
        )

    @staticmethod
    def _short_circuit_for_internal_failure(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws: Workspace,
        s: Settings,
        reviewer_comments: str | None,
    ) -> Outcome | None:
        """Delegate to :func:`_reconcile.short_circuit_for_internal_failure`."""
        return _reconcile.short_circuit_for_internal_failure(
            ctx, ticket, draft, ws, s, reviewer_comments
        )

    @staticmethod
    def _clear_refine_checkpoint(ws: Workspace) -> None:
        """Delegate to :func:`_checkpoint.clear_refine_checkpoint`."""
        return _checkpoint.clear_refine_checkpoint(ws)

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
        """Delegate to :func:`_triage.triage_skip`."""
        return _triage.triage_skip(
            ctx, ticket, draft, repo_dir, extra_roots, title, ws, s, reviewer_comments
        )

    # -- main orchestrator --------------------------------------------------

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
        outcomes.  Each phase lives in its own helper in a sub-module;
        this body is the FSM driver that short-circuits on the first
        phase to produce an :class:`Outcome`.
        """
        reviewer_comments, open_thread_ids = (
            RefineAgentMixin._collect_reviewer_comments(ctx, ticket)
        )

        _is_delta_reuse = (
            s.refine_delta_reuse_enabled
            and bool(reviewer_comments)
            and _triage.is_sendback_reentry(ctx.service, ticket.id)
        )

        if not _is_delta_reuse:
            outcome = _reconcile.reviewer_agreement_guard(
                ctx, ticket, draft, ws, s, reviewer_comments
            )
            if outcome is not None:
                return outcome

        outcome = _triage.split_child_fast_path(
            ctx, ticket, draft, ws, reviewer_comments
        )
        if outcome is not None:
            return outcome

        if (
            not _is_delta_reuse
            and not (ws.artifacts_dir / "triage_complexity.json").exists()
        ):
            outcome = _triage.triage_skip(
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

        outcome = _reconcile.short_circuit_for_internal_failure(
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
        result = cast(refining.RefineResult, result)

        outcome = _reconcile.gitignored_guard(ticket, result, repo_dir)
        if outcome is not None:
            return outcome

        _reconcile.apply_agent_side_effects(ctx, ticket, draft, ws, s, epic_ctx, result)

        outcome = _result_paths.no_change_path(
            ctx, ticket, draft, repo_dir, title, ws, result
        )
        if outcome is not None:
            return outcome

        if result.promote_to_epic and not result.split:
            return _result_paths.promote_to_epic_path(ctx, ticket, draft, ws, s, result)

        if not result.split:
            return _result_paths.single_scope_path(
                ctx, ticket, ws, s, result, reviewer_comments, open_thread_ids
            )

        return _result_paths.multi_scope_path(
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
        memory_board_id = (
            ctx.repo_config.board_id if ctx.repo_config else ticket.board_id
        )
        memory_text = _load_refine_memory(s, memory_board_id)

        resume_history: list | None = None
        saved_state = load_conversation_state(ws, "refine")
        if saved_state is not None:
            own_history = ctx.service.history(ticket.id)
            was_paused = any(
                ev.state == State.AWAITING_USER_REPLY for ev in own_history
            )
            if was_paused:
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

        if saved_state is None:
            checkpoint_result, _ = _checkpoint.load_refine_checkpoint(ws)
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
            triage_complexity = _reconcile.read_triage_complexity(ws)
            _explore_simple = triage_complexity == "simple"

            triage_findings = _reconcile.read_triage_findings(ws)

            _trivial = _reconcile.read_triage_trivial(ws)
            _cheap_route = False
            refine_level: int | None = None
            if s.refine_trivial_routing_enabled and _trivial:
                refine_level = s.refine_trivial_model_level
                _cheap_route = True

            if (
                reviewer_comments
                and s.max_re_refine_cycles_before_cheap > 0
                and refine_level is None
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
                    _cheap_route = True
                    set_current_span_attribute("refine.forced_cheap_re_refine", True)

            resolved_level = refine_level if refine_level is not None else 3
            set_current_span_attribute("refine.model_level", resolved_level)
            set_current_span_attribute("refine.routed_trivial", _trivial)

            refine_model: str | None = None
            request_limit_override: int | None = None
            if _cheap_route:
                refine_model = s.refine_trivial_subscription_model
                request_limit_override = s.refine_request_limit_simple
            elif refine_level is None and s.refine_subscription_tier_routing_enabled:
                if triage_complexity == "simple":
                    refine_model = s.refine_subscription_model_default
                    request_limit_override = s.refine_request_limit_simple
                elif (
                    s.refine_findings_downgrade_enabled
                    and triage_findings is not None
                    and len(triage_findings.strip())
                    >= s.refine_findings_downgrade_min_chars
                ):
                    refine_model = s.refine_subscription_model_findings
                    set_current_span_attribute("refine.findings_downgrade", True)
                else:
                    refine_model = s.refine_subscription_model_complex

            if resolved_level == 3:
                set_current_span_attribute(
                    "refine.model_alias", refine_model if refine_model else "opus"
                )
            else:
                set_current_span_attribute(
                    "refine.model_alias", f"deepseek-l{resolved_level}"
                )

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
        except RuntimeError as e:
            from ...runtime.transient_errors import reraise_if_transient

            reraise_if_transient(e)
            return Outcome(State.BLOCKED, str(e)), None

        _checkpoint.save_refine_checkpoint(ws, result)

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

        clear_conversation_state(ws, "refine")
        return None, result
