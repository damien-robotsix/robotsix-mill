"""Phase-coordination mixin: run / loop / context load / finalize / pause."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ...agents.testing import ENV_ERROR_PREFIX
from ...config import target_branch_for
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...forge.auth import _resolve_remote_url
from ...runners.pass_runner import load_memory
from ...vcs import git_ops
from ..base import Outcome, StageContext
from ..pause import (
    build_compact_resume_message_history,
    build_resume_message_history,  # noqa: F401 — kept for debugging/rollback
    check_for_pause,
    load_conversation_state,
    save_conversation_state,
)
from ._base import _ImplementStageBase
from ._shared import (
    _ImplementContext,
    _SinglePassResult,
    log,
)


class PhaseCoordinatorMixin(_ImplementStageBase):
    """Run-loop orchestration for :class:`ImplementStage`."""

    # ------------------------------------------------------------------
    # preflight (pre-trace gate — no clone, no model, no trace overhead)
    # ------------------------------------------------------------------

    def preflight(self, ticket: Ticket, ctx: StageContext) -> Outcome | None:
        """Cheap checks that can gate implement BEFORE a Langfuse trace opens.

        Catches known-no-op conditions (empty spec, spawn limit, cycle
        limit) without consuming a spawn slot or emitting a $0.00 trace.
        """
        s = ctx.settings
        ws = ctx.service.workspace(ticket)

        # 1. Spec must exist and be non-empty — without a spec the agent
        #    has nothing to implement and would return empty/no-op.
        #    Tickets with a parent epic inherit their spec from the epic
        #    context — only block when BOTH the direct spec and the epic
        #    context are empty.
        spec = ws.read_description()
        if not spec or not spec.strip():
            epic_ctx = ctx.service.get_epic_context(ticket)
            if not epic_ctx or not epic_ctx.strip():
                return Outcome(
                    State.BLOCKED,
                    "empty or missing specification — cannot implement without a spec",
                )

        # 2. Implement spawn counter: cap the total number of
        #    implement-stage invocations per ticket so that a ticket
        #    stuck in a BLOCKED→READY→BLOCKED loop cannot burn
        #    unbounded LLM quota across re-spawns.  Counted and gated
        #    here in preflight so a ticket at the spawn limit fails
        #    fast with BLOCKED before a Langfuse trace opens.
        spawn_limit = s.implement_max_spawns_per_ticket
        if spawn_limit > 0:
            counter_path = ws.artifacts_dir / "implement_spawn_count"
            spawn_count = 0
            if counter_path.exists():
                try:
                    spawn_count = int(counter_path.read_text(encoding="utf-8").strip())
                except (ValueError, OSError):  # fmt: skip
                    spawn_count = 0
            if spawn_count >= spawn_limit:
                return Outcome(
                    State.BLOCKED,
                    f"implement spawn limit reached "
                    f"({spawn_count}/{spawn_limit}) — "
                    "escalating to BLOCKED for human inspection.  "
                    "Delete artifacts/implement_spawn_count in the "
                    "workspace to reset.",
                )
            spawn_count += 1
            try:
                counter_path.write_text(str(spawn_count), encoding="utf-8")
            except OSError:
                log.warning(
                    "%s: failed to write implement_spawn_count",
                    ticket.id,
                    exc_info=True,
                )

        # 3. Ticket-lifetime implement-cycle cap: catch the runaway
        #    implement↔review loop before we clone or open a trace.
        if (
            s.max_implement_review_cycles > 0
            and ticket.implement_cycles >= s.max_implement_review_cycles
        ):
            return Outcome(
                State.BLOCKED,
                f"Implement-review cycle limit reached "
                f"({ticket.implement_cycles}/{s.max_implement_review_cycles}) — "
                "escalating to BLOCKED for human inspection",
            )

        # 4. Stale re-spawn guard: if the last implement attempt was not
        #    successful ("BLOCKED — resumable") and the effective spec
        #    (direct description + epic context) hasn't changed since
        #    that attempt, re-spawning would produce the same result.
        #    Fail fast before a trace opens to prevent the $0.00 trace /
        #    no-op re-spawn pattern.
        implement_md = ws.artifacts_dir / "implement.md"
        if implement_md.exists():
            try:
                md_content = implement_md.read_text(encoding="utf-8")
            except OSError:
                md_content = ""
            if "BLOCKED — resumable" in md_content:
                # Assemble the effective spec the same way
                # _load_implement_context does (epic context first,
                # then direct description).
                effective = spec or ""
                if ticket.parent_id:
                    epic_ctx2 = ctx.service.get_epic_context(ticket)
                    if epic_ctx2:
                        effective = epic_ctx2 + "\n\n" + effective
                import hashlib

                current_fp = hashlib.sha256(effective.encode("utf-8")).hexdigest()[:16]
                # Extract stored fingerprint from implement.md.
                stored_fp = ""
                for line in md_content.splitlines():
                    if line.startswith("spec-fingerprint: "):
                        stored_fp = line.split("spec-fingerprint: ", 1)[1].strip()
                        break
                if stored_fp and stored_fp == current_fp:
                    return Outcome(
                        State.BLOCKED,
                        "spec unchanged since last implement attempt "
                        f"(fingerprint {current_fp}) — "
                        "re-implementing would produce the same "
                        "result.  Update the specification or delete "
                        "artifacts/implement.md to reset.",
                    )

        return None

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a READY ticket: gate on dependencies, clone the repo, create the feature branch, and drive the implementation agent loop to produce code changes."""
        s = ctx.settings

        # --- dependency gate: refuse to implement until all deps are
        # terminal (CLOSED/DONE). Same-state no-op → the reconcile
        # sweep re-enqueues this ticket each poll cycle.
        unmet = ctx.service.unmet_dependencies(ticket)
        if unmet:
            log.debug(
                "%s: unmet dependencies — deferring implement: %s",
                ticket.id,
                unmet,
            )
            return Outcome(State.READY)

        # --- meta-board cross-repo implement gate ---
        # A meta ticket that isn't a new-repo scaffold needs edits across the
        # triaged repos. Run the same triage→clone flow refine uses, then
        # branch the first clone and dive into the standard implement loop
        # with extra_roots threaded through so the agent can read/write
        # across all cloned repos. Per-repo branching and multi-repo PR
        # delivery are sibling children in the same epic — out of scope
        # here.
        extra_roots: list[Path] | None = None
        if ticket.board_id == "meta":
            from ...meta.workspace import build_triaged_meta_workspace

            ws = ctx.service.workspace(ticket)
            spec = ws.read_description()
            repo_dir, extra_roots, outcome = build_triaged_meta_workspace(
                ctx, ticket, ws, spec, author="implement"
            )
            if outcome is not None:
                return outcome
            branch = f"{s.branch_prefix}{ticket.id}"
            # Create/checkout the feature branch in EVERY workspace
            # repo, not just the first clone. Deliver needs per-repo
            # branches to open one PR per touched repo.
            for repo_path in extra_roots:
                if git_ops.branch_exists(repo_path, branch):
                    git_ops.checkout(repo_path, branch)
                else:
                    git_ops.create_branch(repo_path, branch)
            # resuming is true iff the primary repo already had the
            # branch from a prior implement pass.
            resuming = git_ops.branch_exists(repo_dir, branch)
            ctx.service.set_branch(ticket.id, branch)
        else:
            remote_url = _resolve_remote_url(s, ctx.repo_config)
            if not remote_url:
                return Outcome(State.BLOCKED, "FORGE_REMOTE_URL not configured")

            # Phase 1: clone and branch (or resume)
            result = self._clone_and_branch(ctx, ticket, s)
            if isinstance(result, Outcome):
                return result
            repo_dir, branch, resuming = result

        # --- prepare hook: let the repo run custom setup after clone,
        # before any agent executes ---
        ws = ctx.service.workspace(ticket)
        from ...hooks import run_prepare_hook

        hook_error = run_prepare_hook(repo_dir, ticket.id, ws.dir)
        if hook_error is not None:
            return Outcome(State.BLOCKED, hook_error)

        # --- spec emptiness gate: refuse to invoke the implement agent
        # when the spec is empty or trivially insufficient.  An empty
        # spec means refine failed to produce a description; the agent
        # would read nothing and return immediately — a $0.00 trace
        # that wastes a spawn slot and complicates cost analysis.
        # Meta-board tickets have their own spec routing through
        # ``build_triaged_meta_workspace`` which handles empty specs
        # separately; skip the gate for those.  Tickets with a parent
        # epic inherit their spec from the epic context — only block
        # when BOTH the direct spec and the epic context are empty.
        spec_text = ws.read_description()
        if ticket.board_id != "meta":
            if not spec_text or not spec_text.strip():
                epic_ctx = ctx.service.get_epic_context(ticket)
                if not epic_ctx or not epic_ctx.strip():
                    return Outcome(
                        State.BLOCKED,
                        "spec is empty — refine stage may have failed to "
                        "produce a ticket description.  Re-run refine or "
                        "add a description manually before re-attempting "
                        "implement.",
                    )

        # --- prerequisite gate: cheapest pre-agent check, so it runs
        # first. Verify that external symbol/import prerequisites the
        # spec declares are satisfiable in the cloned repo's environment
        # BEFORE spending the baseline run or the coordinator agent.
        prereq_outcome = self._run_prerequisite_gate(
            ctx,
            ticket,
            spec_text,
            repo_dir,
            s,
        )
        if prereq_outcome is not None:
            return prereq_outcome

        # --- test-baseline check: detect pre-existing failures BEFORE
        # the agent loop so we don't waste cycles on an unfixable base.
        # EXEMPT baseline-fix tickets: a ticket spawned to repair the red
        # base (source=IMPLEMENT_BASELINE_DEPENDENCY) must implement AGAINST
        # that still-red base — that is its whole job. Re-running the gate
        # on it would spawn yet another baseline fix, which dedups to the
        # ticket itself ("Ticket cannot depend on itself" → Fatal), wedging
        # the ticket and everything parked behind it (board-wide deadlock).
        if ticket.source != SourceKind.IMPLEMENT_BASELINE_DEPENDENCY:
            baseline_outcome = self._run_baseline_check(
                ctx,
                ticket,
                repo_dir,
                branch,
                resuming,
                s,
            )
            if baseline_outcome is not None:
                return baseline_outcome

        # --- implement↔review convergence backstop ---
        # Gate BEFORE entering the agent loop so we don't spend LLM
        # quota on a ticket that is provably stuck.

        # 1) Empty diff from a prior review round: the ticket came back
        #    from review but the current branch has no commits beyond
        #    the target — there is nothing new to implement.
        if resuming and ticket.review_rounds > 0:
            target = target_branch_for(s, ctx.repo_config)
            try:
                count = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_dir),
                        "rev-list",
                        "--count",
                        f"origin/{target}..HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip()
                ahead = int(count) if count else 0
            except (
                subprocess.CalledProcessError,
                ValueError,
            ):
                ahead = -1  # can't determine → don't block
            if ahead == 0:
                note = (
                    "empty diff after review round — branch has no commits "
                    "beyond origin/main. Re-implementing would produce no "
                    "changes (review findings may be unfixable by further "
                    "automated passes)"
                )
                self._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    note,
                    ok=False,
                    reference_files=None,
                    extra_roots=extra_roots,
                )
                return Outcome(State.BLOCKED, note)

        # Phase 2: deterministic, stage-owned implement loop.
        return self._implement_loop(
            ctx, ticket, repo_dir, branch, resuming, s, extra_roots=extra_roots
        )

    # ------------------------------------------------------------------
    # Private helpers (refactored)
    # ------------------------------------------------------------------

    @classmethod
    def _load_implement_context(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        settings,
    ) -> _ImplementContext:
        """Load all workspace artifacts needed before the fix loop."""
        ws = ctx.service.workspace(ticket)

        spec = ws.read_description()
        epic_ctx = ctx.service.get_epic_context(ticket)
        if epic_ctx:
            spec = epic_ctx + "\n\n" + spec

        memory_text = load_memory(
            settings.memory_file_for(
                "implement",
                cls._memory_board_id(ctx, ticket),
            ),
        )

        reference_files = None
        ref_files_path = ws.artifacts_dir / "reference_files.json"
        if ref_files_path.exists():
            try:
                reference_files = json.loads(ref_files_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning(
                    "%s: reference_files.json corrupted — treating as empty",
                    ticket.id,
                )
                reference_files = None

        file_map: set[str] | None = None
        file_map_path = ws.artifacts_dir / "file_map.json"
        if file_map_path.exists():
            try:
                raw = json.loads(file_map_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning(
                    "%s: file_map.json corrupted — treating as empty",
                    ticket.id,
                )
                raw = None
            if raw:  # non-empty list → extract paths
                file_map = {entry["file"] for entry in raw}

        if file_map is None:
            log.warning(
                "%s: file_map.json missing or empty — skipping scope enforcement",
                ticket.id,
            )

        feedback: str | None = None
        open_thread_ids: set[int] | None = None
        # ``mill`` and ``system`` author comments (worker trace-link
        # breadcrumbs, timeout-escalation pings) are diagnostic
        # metadata, not feedback. Including them taught implement to
        # treat unreadable Langfuse URLs as review comments and ask
        # the operator "what did the reviewer say?". Trace links now
        # write to history (see worker._post_trace_event) but the
        # filter stays as defence-in-depth.
        _NON_FEEDBACK_AUTHORS = {"mill", "system"}
        if ticket.blocked_from is None:  # not a BLOCKED resume
            comments = ctx.service.list_comments(ticket.id)
            comments = [c for c in comments if c.author not in _NON_FEEDBACK_AUTHORS]
            if comments:
                open_threads = [
                    c for c in comments if c.parent_id is None and c.closed_at is None
                ]
                if open_threads:
                    open_thread_ids = {c.id for c in open_threads}
                review_feedback = "\n".join(
                    f"[REVIEW id={c.id} @ {c.created_at.isoformat()}] {c.body}"
                    for c in comments
                )
                feedback = review_feedback

        previous_attempt_summary: str | None = None
        summary_path = ws.artifacts_dir / "implement_summary.md"
        if summary_path.exists():
            try:
                previous_attempt_summary = summary_path.read_text(
                    encoding="utf-8",
                ).strip()
            except OSError:
                log.warning(
                    "%s: failed to read implement_summary.md",
                    ticket.id,
                    exc_info=True,
                )

        return _ImplementContext(
            spec=spec,
            memory_text=memory_text,
            reference_files=reference_files,
            file_map=file_map,
            feedback=feedback,
            previous_attempt_summary=previous_attempt_summary,
            open_thread_ids=open_thread_ids,
        )

    @classmethod
    def _maybe_handle_pause(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        ws,
        summary: str,
        ref_files: list[str] | None,
        conv_state,
        new_msgs,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult | None:
        """Persist conv_state and route to AWAITING_USER_REPLY on pause."""
        if not check_for_pause(new_msgs):
            return None
        save_conversation_state(ws, conv_state, "implement")
        cls._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary or "paused",
            ok=False,
            reference_files=ref_files,
            extra_roots=extra_roots,
        )
        ctx.service.transition(
            ticket.id,
            State.AWAITING_USER_REPLY,
            note="paused — agent asked a clarifying question",
        )
        log.info(
            "%s: paused implement — agent invoked ask_user",
            ticket.id,
        )
        return _SinglePassResult(
            next_action="pause",
            outcome=Outcome(State.AWAITING_USER_REPLY),
        )

    @classmethod
    def _memory_board_id(cls, ctx: StageContext, ticket: Ticket) -> str:
        """Resolve the board_id used to key the implement memory ledger.

        Meta-board tickets have no registered ``repo_config``; their
        ledger is keyed on the ticket's own ``board_id`` (``"meta"``).
        Every other board uses ``ctx.repo_config.board_id``. This must
        match :class:`Settings.memory_file_for`'s non-empty requirement.
        """
        return ctx.repo_config.board_id if ctx.repo_config else ticket.board_id

    @classmethod
    def _implement_loop(
        cls,
        ctx,
        ticket,
        repo_dir,
        branch,
        resuming,
        settings,
        extra_roots: list[Path] | None = None,
    ):
        """Run the bounded fix loop: edit pass → test gate → route.

        The implement agent does ONE edit pass per iteration; the test
        gate runs the suite once and produces a distilled diagnosis;
        :meth:`ValidationResult.decide` routes deterministically. On
        ``retry`` the diagnosis is fed back into the next pass; on
        ``escalate`` (suite still failing after ``max_fix_iterations``)
        the ticket is BLOCKED-resumable. No LLM owns the loop or the
        bound — both are enforced here.
        """
        max_iters = max(1, settings.max_fix_iterations)
        ic = cls._load_implement_context(ctx, ticket, settings)

        # Ordered history of the per-cycle distilled diagnosis. Drives the
        # circuit breaker below: a fix loop that keeps producing the SAME
        # diagnosis is not making progress, and an ENV-ERROR diagnosis is
        # not fixable by code edits at all — both should short-circuit to
        # BLOCKED rather than exhaust ``max_fix_iterations``.
        diag_history: list[str] = []

        for attempt in range(1, max_iters + 1):
            # --- resume awareness: detect if returning from a pause ---
            resume_history: list | None = None
            if attempt == 1:
                ws = ctx.service.workspace(ticket)
                saved_state = load_conversation_state(ws, "implement")
                if saved_state is not None and any(
                    ev.state == State.AWAITING_USER_REPLY
                    for ev in ctx.service.history(ticket.id)
                ):
                    from ..pause import _collect_ask_user_replies

                    reply_text = _collect_ask_user_replies(ctx, ticket)
                    # Compute git diff --stat for the compact resume
                    # history so the agent knows which files were
                    # modified during the prior session.
                    import subprocess

                    git_stat: str | None = None
                    try:
                        git_stat = (
                            subprocess.run(
                                ["git", "diff", "--stat", "HEAD"],
                                cwd=repo_dir,
                                capture_output=True,
                                text=True,
                            ).stdout.strip()
                            or None
                        )
                    except Exception:
                        git_stat = None
                    resume_history = build_compact_resume_message_history(
                        saved_state,
                        reply_text,
                        git_stat=git_stat,
                    )
                    log.info(
                        "%s: resuming implement from pause — "
                        "loaded %d-byte conversation state",
                        ticket.id,
                        len(saved_state),
                    )
                    ic.feedback = None

            result = cls._run_single_implement_pass(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                ic,
                attempt,
                max_iters,
                resume_history,
                resuming,
                extra_roots=extra_roots,
            )

            if result.next_action == "return":
                return result.outcome
            if result.next_action == "pause":
                return result.outcome
            if result.next_action in ("proceed", "escalate"):
                return result.outcome

            # next_action == "retry" — update for next iteration.
            # Circuit breaker: track the per-cycle diagnosis and bail out
            # early when the loop is provably stuck. The retry diagnosis is
            # carried on ``result.feedback`` (main retry path) or, when the
            # guardrail produced a "continue", on ``result.ic.feedback``.
            diag = (
                result.feedback
                or (result.ic.feedback if result.ic is not None else None)
                or ""
            )
            diag_history.append(diag)
            env_repeat = (
                diag.startswith(ENV_ERROR_PREFIX)
                and len(diag_history) >= 2
                and diag_history[-2] == diag
            )
            triple_repeat = (
                len(diag_history) >= 3
                and diag != ""
                and diag_history[-2] == diag
                and diag_history[-3] == diag
            )
            if env_repeat or triple_repeat:
                note = (
                    "environment failure not fixable by code edits — "
                    f"{diag[:200]} (short-circuited after {attempt} "
                    "cycle(s) of identical diagnosis)"
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    note,
                    ok=False,
                    reference_files=ic.reference_files,
                    extra_roots=extra_roots,
                )
                return Outcome(State.BLOCKED, note)
            if result.ic is not None:
                ic = result.ic

        # Defensive fallback — should be unreachable.
        cls._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            "",
            ok=False,
            reference_files=ic.reference_files,
            extra_roots=extra_roots,
        )
        return Outcome(
            State.BLOCKED,
            "implement loop exhausted — resumable",
        )

    # ------------------------------------------------------------------
    # Existing helpers (NOT refactored)
    # ------------------------------------------------------------------

    @classmethod
    def _finalize(
        cls,
        ctx,
        ticket,
        repo_dir,
        branch,
        summary,
        *,
        ok: bool,
        reference_files: list[str] | None = None,
        extra_roots: list[Path] | None = None,
    ) -> None:
        ws = ctx.service.workspace(ticket)
        # Compute the effective spec fingerprint for the stale
        # re-spawn guard in preflight.  Must match how preflight
        # assembles the spec (epic context + direct description).
        import hashlib

        effective = ws.read_description() or ""
        if ticket.parent_id:
            epic_ctx = ctx.service.get_epic_context(ticket)
            if epic_ctx:
                effective = epic_ctx + "\n\n" + effective
        fp = hashlib.sha256(effective.encode("utf-8")).hexdigest()[:16]
        (ws.artifacts_dir / "implement.md").write_text(
            f"# Implement ({'passed' if ok else 'BLOCKED — resumable'})\n"
            f"branch: {branch}\n"
            f"spec-fingerprint: {fp}\n"
            f"\n{summary}\n",
            encoding="utf-8",
        )
        # Persist agent-curated reference_files (paths-only) for retry
        # pre-seeding. Overwrite refine's version unconditionally.
        try:
            ref_path = ws.artifacts_dir / "reference_files.json"
            ref_path.write_text(
                json.dumps(
                    [{"path": p} for p in (reference_files or [])],
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            log.warning(
                "%s: failed to write reference_files.json",
                ticket.id,
                exc_info=True,
            )
        # Persist the summary as a standalone artifact for
        # `<previous_attempt>` injection on retry.
        try:
            (ws.artifacts_dir / "implement_summary.md").write_text(
                summary,
                encoding="utf-8",
            )
        except OSError:
            log.warning(
                "%s: failed to write implement_summary.md",
                ticket.id,
                exc_info=True,
            )
        # Commit message format — identical for all repos.
        commit_message = f"mill: {ticket.title} ({ticket.id})" + (
            "" if ok else " [WIP]"
        )
        # Per-repo commit for extra_roots (multi-repo meta tickets).
        # Write a touched_repos.json artifact listing every repo that
        # received a commit so the downstream deliver stage knows which
        # repos to open PRs for.
        touched_repos: list[dict] = []
        if extra_roots is not None:
            # Check all repos for changes BEFORE committing any, so
            # has_changes returns the correct answer for every repo.
            if git_ops.has_changes(repo_dir):
                touched_repos.append(
                    {
                        "repo_id": repo_dir.name,
                        "branch": branch,
                        "repo_path": str(repo_dir),
                    }
                )
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                if git_ops.has_changes(repo_path):
                    touched_repos.append(
                        {
                            "repo_id": repo_path.name,
                            "branch": branch,
                            "repo_path": str(repo_path),
                        }
                    )
        # Commit primary repo (always — regardless of extra_roots).
        if git_ops.has_changes(repo_dir):
            from ...towncrier import maybe_generate_towncrier_fragment

            maybe_generate_towncrier_fragment(repo_dir, ticket.id, ticket.title)
            git_ops.commit_all(repo_dir, commit_message)
        # Commit extra repos (skip primary — already done above).
        if extra_roots is not None:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                if git_ops.has_changes(repo_path):
                    from ...towncrier import maybe_generate_towncrier_fragment

                    maybe_generate_towncrier_fragment(
                        repo_path, ticket.id, ticket.title
                    )
                    git_ops.commit_all(repo_path, commit_message)
            # Write the artifact — even if empty (no-change-needed path).
            try:
                (ws.artifacts_dir / "touched_repos.json").write_text(
                    json.dumps(touched_repos, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                log.warning(
                    "%s: failed to write touched_repos.json",
                    ticket.id,
                    exc_info=True,
                )
