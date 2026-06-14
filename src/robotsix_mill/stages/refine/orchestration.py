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
from ...core.models import Ticket
from ...core.states import State
from ...core.workspace import Workspace
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
    UNMERGED_BRANCH_PREFIX,
    _COMMIT_SHA_RE,
    _TICKET_ID_RE,
    _build_deployed_log_summary,
    _rationale_claims_external_fix,
    _resolve_next_state,
    _spec_is_degenerate,
    _verify_cited_fix_at_head,
    log,
)


class RefineAgentMixin:
    """Refine-agent pipeline staticmethods mixed into :class:`RefineStage`."""

    @staticmethod
    def _review_spec_conciseness(
        s,
        ws,
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
    ) -> Outcome:
        """Resolve the next state for *spec* and build the closing Outcome.

        Encapsulates the repeated ``_resolve_next_state`` → "append the
        auto-approve note when present" → ``Outcome`` pattern shared by the
        split-child, triage-skip, single-scope, and split paths.
        """
        next_state, auto_note = _resolve_next_state(ctx, spec, ticket_id, source=source)
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
        ws, entries: list[dict], *, only_if_absent: bool = False
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

        outcome = RefineAgentMixin._split_child_fast_path(
            ctx, ticket, draft, ws, reviewer_comments
        )
        if outcome is not None:
            return outcome

        outcome = RefineAgentMixin._triage_skip(
            ctx, ticket, draft, repo_dir, extra_roots, title, ws, s, reviewer_comments
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
            if triage.decision == "MAINTENANCE" and s.maintenance_triage_enabled:
                # LLM detected a maintenance request the keyword
                # classifier missed.  Route to MAINTENANCE without
                # running the full refine agent.
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                return Outcome(
                    State.MAINTENANCE,
                    f"maintenance triage (LLM): {triage.reason} — {title}",
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
                )
        except Exception:
            log.warning(
                "%s: triage failed, falling through to full refine",
                ticket.id,
                exc_info=True,
            )
        return None

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
        # Resolve the patchable module-level seams (``load_memory``,
        # ``persist_memory``, ``_verify_branch_merged``) through the package
        # façade so tests that patch ``robotsix_mill.stages.refine.<name>``
        # (module-level seams in the pre-split module) still take effect.
        from robotsix_mill.stages import refine as _facade

        # Meta tickets have no registered repo_config; their memory ledger
        # is keyed on the ticket's own board_id ("meta"). Every other board
        # uses its repo_config.board_id.
        memory_board_id = (
            ctx.repo_config.board_id if ctx.repo_config else ticket.board_id
        )
        refine_memory_path = s.memory_file_for("refine", memory_board_id)
        memory_text = _facade.load_memory(
            refine_memory_path, max_chars=s.max_memory_chars
        )

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
        from robotsix_mill.stages import refine as _facade

        if result.updated_memory:
            memory_board_id = (
                ctx.repo_config.board_id if ctx.repo_config else ticket.board_id
            )
            refine_memory_path = s.memory_file_for("refine", memory_board_id)
            _facade.persist_memory(refine_memory_path, result.updated_memory)

        if result.title and result.title.strip():
            ctx.service.set_title(ticket.id, result.title.strip())

        # --- epic body handling (non-split path) ---
        # In autonomous mode: apply immediately to the epic.
        # In gated mode: store as artifact in child workspace for
        # later application on approval.
        if result.epic_body and result.epic_body.strip() and epic_ctx:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == "epic":
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
        # elsewhere" rationale (from the LLM mode-4 path OR the
        # deterministic memory short-circuit — both converge
        # here) is NOT trusted on its word. A reverted fix leaves
        # the original commit as an ancestor of origin/main, so
        # ancestry alone cannot detect the bug's return (the
        # 2026-06-09 incident). Synthesize a verification spec and
        # route to implement, which works against live HEAD and
        # re-applies the fix if the bug recurred (or cheaply
        # closes via its empty-diff path if genuinely resolved).
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
                    kind="task",
                    parent_id=ticket.id,
                )
                created_children.append((child.id, child_title, child_body))
            created_ids = [cid for cid, _t, _b in created_children]
            # Dependency wiring: a linear chain (C0 → C1 → C2 → …) by
            # default — matching the /generate-children route — but
            # when the batch includes a create/initialize-repo child
            # the repo-populating siblings depend on it so they cannot
            # run before the repo exists.
            for child_id, deps in plan_child_dependencies(created_children).items():
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
            )
            child_ids.append(child_ticket.id)

        # Reparent children: if the ticket already belongs to an
        # epic, reparent to that epic; otherwise create a new
        # umbrella epic so children appear under a visible grouping
        # entity rather than a closed parent.
        existing_epic_id: str | None = None
        if ticket.parent_id is not None:
            parent_candidate = ctx.service.get(ticket.parent_id)
            if parent_candidate is not None and parent_candidate.kind == "epic":
                existing_epic_id = ticket.parent_id
                for cid in child_ids:
                    ctx.service.set_parent(cid, existing_epic_id)
        if existing_epic_id is None:
            epic_title = (result.title and result.title.strip()) or ticket.title.strip()
            epic_desc = (result.spec_markdown and result.spec_markdown.strip()) or draft
            epic = ctx.service.create(
                title=epic_title,
                description=epic_desc,
                kind="epic",
                source=ticket.source,
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
            if parent is not None and parent.kind == "epic":
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
