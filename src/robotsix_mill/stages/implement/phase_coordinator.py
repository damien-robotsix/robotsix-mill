"""Phase-coordination mixin: run / loop / context load / finalize / pause."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..._resources import agent_definitions_dir
from ...agents.testing import ENV_ERROR_PREFIX
from ...agents.yaml_loader import load_agent_definition
from ...config import effective_target_branch
from ...core.models import SourceKind, Ticket, TicketKind
from ...core.states import State
from ...deploy import check_deploy_freshness
from ...forge import get_forge
from ...forge.auth import _resolve_remote_url, github_token
from ...runners.pass_runner import load_memory
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome, StageContext
from ..pause import (
    build_compact_resume_message_history,
    check_for_pause,
    clear_conversation_state,
    load_conversation_state,
    save_conversation_state,
)
from ._base import _ImplementStageBase
from ._shared import (
    _ImplementContext,
    _SinglePassResult,
    log,
)

# --- stuck-loop detection thresholds -----------------------------------

# Number of consecutive passes with no file edits (empty git diff) after
# which the implement loop is aborted as "stuck".  A pass that produces at
# least one file mutation resets the counter.  Default 3 is tight enough to
# catch the read_ticket / list_epic_children loop the trace inspector
# surfaced (11 wasted pro model calls between first read_ticket and first
# test run) without penalising legitimate multi-pass fixes.
_STUCK_NO_DIFF_PASSES = 3

# Cumulative tool-call budget across all passes without a git diff.  When
# the agent exhausts this budget without producing a single file change the
# loop is aborted.  Default 50 is well below ``coordinator_max_tool_calls``
# (300) and catches the per-pass budget-reset pattern where each fresh pass
# gets a new 300-call quota but never makes progress.
_STUCK_MAX_TOOL_CALLS_NO_DIFF = 50

# Number of consecutive identical non-progress tool calls at the tail of a
# pass that signal a stuck loop (e.g. the agent calling ``read_ticket``
# over and over).  Checked via
# :func:`robotsix_mill.stages.short_circuit_verify.analyze_pass_progress`.
_STUCK_SAME_TOOL_WINDOW = 5


class PhaseCoordinatorMixin(_ImplementStageBase):
    """Run-loop orchestration for :class:`ImplementStage`."""

    # Stuck-loop detection counters (reset per ticket in
    # :meth:`_implement_loop`).
    _stuck_no_diff_passes: int = 0
    _stuck_total_tool_calls_no_diff: int = 0

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

        # 0. Epic guard: implement is for TASK tickets only.  An epic
        #    reaching this stage signals a dispatch bug — block it
        #    before any trace opens so a human can triage.
        if ticket.kind == TicketKind.EPIC:
            return Outcome(
                State.BLOCKED,
                "epic ticket routed to implement stage — epics must "
                "be broken into child tasks; re-route to epic_breakdown "
                "or refine for child generation",
            )

        # 0.5. Deploy-freshness gate: when the deploy server reports an
        #      image update is available, the running worker predates the
        #      latest commit.  Any implement attempt on stale code risks
        #      reproducing bugs already fixed in the newer image.  Park
        #      the ticket with explicit digest info so the operator can
        #      trigger a redeploy before retrying.
        deploy_status = check_deploy_freshness(s.deploy_api_url)
        if deploy_status is not None and deploy_status.update_available:
            return Outcome(
                State.BLOCKED,
                f"worker image is stale — running {deploy_status.running_digest} "
                f"predates latest {deploy_status.latest_digest}.  "
                "Redeploy the mill worker before resuming blocked tickets.",
            )

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
                note = (
                    f"implement spawn limit reached "
                    f"({spawn_count}/{spawn_limit}) — "
                    "escalating to BLOCKED for human inspection.  "
                    "Delete artifacts/implement_spawn_count in the "
                    "workspace to reset."
                )
                # Append the tail of the last implement summary so the
                # operator sees the genuine failure cause instead of only
                # the generic limit message.
                summary_path = ws.artifacts_dir / "implement_summary.md"
                if summary_path.exists():
                    try:
                        summary_text = summary_path.read_text(encoding="utf-8")
                    except OSError, UnicodeDecodeError:
                        summary_text = ""
                    if summary_text:
                        tail = summary_text[-500:].strip()
                        if tail:
                            note += f"\n\nLast attempt summary tail:\n{tail}"
                # Discard any stale conversation state so a
                # resume-blocked restart begins a fresh agent
                # conversation instead of replaying the prior
                # transcript.
                clear_conversation_state(ws, "implement")
                return Outcome(State.BLOCKED, note)
            # Only increment on genuine re-spawns, not transient
            # infrastructure retries.  Transient failures (sandbox EOF,
            # OOM, etc.) must not burn the ticket's spawn budget —
            # otherwise a single flaky runner can permanently deadlock
            # a ticket against the spawn limit.
            if ticket.retry_attempt == 0:
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
                        "spec unchanged since last spec-determined "
                        "implement attempt "
                        f"(fingerprint {current_fp}) — "
                        "re-implementing would produce the same "
                        "result.  Update the specification to change "
                        "the fingerprint, or force a retry via "
                        "resume-blocked with a justification note, or "
                        "use the reset-fingerprint endpoint to clear "
                        "the guard.",
                    )

        # 4.5. Cross-spawn stall guard: if a prior implement cycle
        #      already tripped the stall detector (summary unchanged
        #      across consecutive BLOCKED attempts despite open review
        #      feedback), block BEFORE incrementing the spawn counter
        #      so a manual resume doesn't silently burn another round.
        #      The stall state lives in implement.md and, for continuity
        #      across resume-blocked cycles, in implement_stall_state.json.
        _stall_count = 0
        _stall_summary = ""
        if implement_md.exists():
            try:
                _md_stall = implement_md.read_text(encoding="utf-8")
            except OSError:
                _md_stall = ""
            for _line in _md_stall.splitlines():
                if _line.startswith("stall-count: "):
                    try:
                        _stall_count = int(_line.split("stall-count: ", 1)[1].strip())
                    except ValueError:
                        _stall_count = 0
                    break
        # Fall back to the persisted JSON stall state — survives
        # _clear_stale_implement_guard on resume-blocked.
        if _stall_count == 0:
            _ss_path = ws.artifacts_dir / "implement_stall_state.json"
            if _ss_path.exists():
                try:
                    _ss = json.loads(_ss_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError, OSError:
                    _ss = {}
                _stall_count = _ss.get("stall_count", 0)
        if _stall_count > 0:
            _threshold = getattr(s, "implement_stall_threshold", 2)
            if _threshold > 0 and _stall_count >= _threshold:
                # Surface the stall diagnostic from the last
                # attempt's summary — it already includes review
                # comment ids and the recommended remedy.
                _summary_path = ws.artifacts_dir / "implement_summary.md"
                if _summary_path.exists():
                    try:
                        _stall_summary = _summary_path.read_text(
                            encoding="utf-8"
                        ).strip()
                    except OSError:
                        # _stall_summary is already ""; the file is
                        # non-critical — swallow and continue.
                        pass
                if _stall_summary and _stall_summary.startswith("STALL DETECTED"):
                    return Outcome(State.BLOCKED, _stall_summary)
                return Outcome(
                    State.BLOCKED,
                    f"stall guard — {_stall_count} consecutive "
                    "no-progress implement cycles detected.  "
                    "The implement agent is not converging.  "
                    "Consider re-scoping or splitting the ticket.",
                )

        # 5. Agent tool-definition integrity: the assembled tool list
        #    must be non-empty before we open a trace.  Load the agent-
        #    definition YAML and verify it declares at least one tool.
        #    An empty tools list signals a misconfigured or corrupted
        #    agent definition that would produce a no-op agent with no
        #    ability to explore, read, or edit.
        try:
            definition = load_agent_definition(
                agent_definitions_dir() / "implement.yaml"
            )
        except Exception as exc:
            return Outcome(
                State.BLOCKED,
                f"failed to load implement agent definition: {exc}",
            )
        if not definition.tools:
            return Outcome(
                State.BLOCKED,
                "implement agent definition has no tools configured — "
                "the tools list in agent_definitions/implement.yaml "
                "is empty",
            )

        # 6. Skill-file integrity: every skill referenced by the agent
        #    definition must exist on disk before the model runs.  A
        #    missing skill silently degrades the system prompt (the
        #    ``compose_prompt`` warning is invisible to the model) and
        #    produces a no-op loop.  Resolved through the same
        #    packaged-dir fallback ``compose_prompt`` uses, so a stale
        #    CWD-relative ``skills_dir`` override degrades to the
        #    bundled skills instead of hard-blocking every ticket
        #    (2026-07-19: a relative override bricked the whole board,
        #    including the ticket that would have fixed the config).
        from robotsix_mill._resources import (
            effective_language_instructions_dir,
            effective_skills_dir,
        )

        skills_root = effective_skills_dir(s.skills_dir)
        for name in definition.skills or ():
            skill_path = skills_root / name / "SKILL.md"
            if not skill_path.is_file():
                return Outcome(
                    State.BLOCKED,
                    f"missing skill file: {skill_path}",
                )

        # 7. Language-instruction integrity: a missing built-in snippet
        #    directory (e.g. ``agent_definitions/language_instructions``)
        #    silently returns ``""`` for every language, degrading the
        #    prompt for every non-mill repo that declares a language.
        #    Checked through the packaged-dir fallback the snippet
        #    loader uses, so only a genuinely unresolvable directory
        #    blocks.
        if not effective_language_instructions_dir(
            s.language_instructions_dir
        ).is_dir():
            return Outcome(
                State.BLOCKED,
                f"language_instructions_dir not found or not a directory: "
                f"{s.language_instructions_dir}",
            )

        # 8. Workspace integrity: the ticket workspace directory must
        #    be present and accessible.  If the workspace root has been
        #    deleted or the filesystem is unavailable, fail fast
        #    instead of spinning a model pass that cannot persist
        #    artifacts.
        if not ws.dir.exists() or not ws.dir.is_dir():
            return Outcome(
                State.BLOCKED,
                f"workspace directory absent or inaccessible: {ws.dir}",
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
            # When cross_repo_target is set, use the fork remote URL
            # instead of the managed repo's remote; the implement
            # agent must clone the target repo, not mill.
            if ctx.repo_config.cross_repo_target:
                remote_url = ctx.repo_config.cross_repo_target.fork_remote_url
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
        from ..hooks import run_prepare_hook

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
            target = effective_target_branch(s, ctx.repo_config)
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
                # The branch has no commits beyond origin/<target> —
                # there is genuinely nothing to merge. Re-implementing
                # would loop forever (empty PR → close → BLOCKED →
                # resume). This is a true no-op: terminate DONE with a
                # clear terminal note instead. ``ahead == 0`` (rev-list
                # count) is a precise "no commits beyond base" signal, so
                # there is no real work to lose.
                note = (
                    "already satisfied — no changes needed (empty diff vs base "
                    f"after review round; branch has no commits beyond "
                    f"origin/{target})"
                )
                self._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    note,
                    ok=True,
                    reference_files=None,
                    extra_roots=extra_roots,
                )
                return Outcome(State.DONE, note)

        # --- existing-branch resume guard ---
        # When resuming from a prior implement run (local branch already
        # exists), check whether the remote branch carries commits with
        # all-green CI but no open PR.  If so, skip the implement loop
        # and route to IMPLEMENT_COMPLETE so the deliver stage re-opens
        # a PR from the existing branch.  This prevents a redraft/re-block
        # loop where re-implementing an already-complete branch triggers
        # the stale-spec guard or produces unnecessary additional commits.
        if resuming:
            try:
                remote_url = _resolve_remote_url(s, ctx.repo_config)
                if remote_url:
                    token = github_token(s, repo_config=ctx.repo_config)
                    sha = git_ops.ls_remote_sha(
                        remote_url, f"refs/heads/{branch}", token
                    )
                    if sha is not None:
                        forge = get_forge(s, repo_config=ctx.repo_config)
                        ci = forge.commit_ci_conclusion(sha=sha)
                        if ci is not None and ci.get("conclusion") == "success":
                            pr = forge.pr_status(source_branch=branch)
                            if pr is None or pr.get("state") != "open":
                                note = (
                                    "branch exists with green CI but no open PR — "
                                    "re-routing to IMPLEMENT_COMPLETE so deliver "
                                    "stage re-opens PR"
                                )
                                log.info("%s: %s", ticket.id, note)
                                return Outcome(State.IMPLEMENT_COMPLETE, note)
            except Exception:
                log.warning(
                    "%s: branch/PR resume check failed — proceeding normally",
                    ticket.id,
                    exc_info=True,
                )

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

        # Reset stuck-detection counters for this ticket's loop.
        # (Class attributes would otherwise persist across tickets
        # since the mixin class is shared.)
        cls._stuck_no_diff_passes = 0
        cls._stuck_total_tool_calls_no_diff = 0

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
                if saved_state is not None:
                    has_pause = any(
                        ev.state == State.AWAITING_USER_REPLY
                        for ev in ctx.service.history(ticket.id)
                    )
                    if has_pause:
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
                    else:
                        # Budget exhaustion resume: the coordinator hit its
                        # request limit or an explore sub-agent exhausted its
                        # budget.  Replay the full conversation state so the
                        # agent can continue where it left off instead of
                        # restarting from scratch.
                        from pydantic_ai.messages import (
                            ModelMessagesTypeAdapter,
                        )

                        resume_history = ModelMessagesTypeAdapter.validate_json(
                            saved_state
                        )
                        log.info(
                            "%s: resuming implement from budget exhaustion "
                            "— loaded %d-byte conversation state",
                            ticket.id,
                            len(saved_state),
                        )

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

            # --- stuck-loop detection (cross-pass) ---------------------------
            # Two orthogonal signals, each sufficient to BLOCK:
            #
            # 1. Consecutive passes with no file edits (empty git diff).
            #    A pass that produces at least one commit-able change resets
            #    the counter.
            #
            # 2. Cumulative tool-call budget exhausted without a single git
            #    diff.  The per-pass ``coordinator_max_tool_calls`` resets
            #    each iteration; this cap tracks the REAL spend across passes
            #    so the loop cannot burn quota indefinitely.
            #
            # Both gates are checked HERE — on every retry — so they fire
            # BEFORE the next agent invocation.
            no_diff_passes = getattr(cls, "_stuck_no_diff_passes", 0)
            total_tool_calls_no_diff = getattr(
                cls, "_stuck_total_tool_calls_no_diff", 0
            )
            try:
                has_diff = git_ops.has_changes(repo_dir)
            except Exception:
                has_diff = True
            if extra_roots:
                for repo_path in extra_roots:
                    if repo_path == repo_dir:
                        continue
                    try:
                        if git_ops.has_changes(repo_path):
                            has_diff = True
                            break
                    except Exception:
                        has_diff = True

            # Always analyze pass progress — even when has_diff is True,
            # because leftover changes from a prior session can mask a
            # current pass that made zero tool calls (silently burning
            # iterations until the loop exhausts).
            progress = short_circuit_verify.analyze_pass_progress(
                result.new_msgs,
                same_tool_window=_STUCK_SAME_TOOL_WINDOW,
            )

            if not has_diff:
                no_diff_passes += 1
                total_tool_calls_no_diff += progress["total"]
            else:
                no_diff_passes = 0
                total_tool_calls_no_diff = 0

            cls._stuck_no_diff_passes = no_diff_passes
            cls._stuck_total_tool_calls_no_diff = total_tool_calls_no_diff

            # Zero-tool-call pass — the agent issued no tool calls at all,
            # which is a distinct failure mode (prompt/context assembly,
            # workspace inaccessibility, or a spec the agent cannot act on).
            # Surface immediately instead of waiting for the 3-pass no-edit
            # threshold.  Fires regardless of has_diff: leftover changes
            # from a prior session should not mask a pass that contributed
            # nothing.  Only fires when new_msgs is present (a real LLM
            # pass); rename-only / spec-exact / test-stub passes carry
            # new_msgs=None and are exempt.
            if progress["total"] == 0 and result.new_msgs is not None:
                detail = (
                    "and produced no file changes"
                    if not has_diff
                    else (
                        "but the working tree carries uncommitted changes "
                        "(likely from a prior session)"
                    )
                )
                note = (
                    f"zero tool calls — the implement agent completed "
                    f"pass {attempt} without issuing a single tool call "
                    f"{detail}.  This indicates a prompt/context assembly "
                    "failure, workspace inaccessibility, or a spec the "
                    "agent cannot act on.  Short-circuiting to BLOCKED."
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

            if no_diff_passes >= _STUCK_NO_DIFF_PASSES:
                # progress is always set in the not-has_diff branch above.
                if progress is None:  # pragma: no cover — defensive
                    progress = {"stuck_same_tool": None, "total": 0, "edit_calls": 0}
                same_tool = progress.get("stuck_same_tool")
                detail = (
                    f"same-tool loop ({same_tool!r} × "
                    f"{progress.get('last_non_progress_run', 0)} calls)"
                    if same_tool
                    else f"{progress['total']} tool calls, "
                    f"{progress['edit_calls']} edit calls"
                )
                note = (
                    f"stuck — {no_diff_passes} consecutive passes with no file "
                    f"edits after {attempt} iteration(s). "
                    f"Last pass: {detail}. "
                    "Short-circuiting to BLOCKED."
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

            if total_tool_calls_no_diff >= _STUCK_MAX_TOOL_CALLS_NO_DIFF:
                note = (
                    f"stuck — {total_tool_calls_no_diff} cumulative tool calls "
                    f"across {no_diff_passes} passes without a single file edit. "
                    f"Budget cap ({_STUCK_MAX_TOOL_CALLS_NO_DIFF}) exhausted after "
                    f"{attempt} iteration(s). Short-circuiting to BLOCKED."
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
            # --- end stuck-loop detection -----------------------------------

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
            if env_repeat:
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
                    transient=True,
                )
                return Outcome(State.BLOCKED, note)
            if triple_repeat:
                note = (
                    f"identical diagnosis repeated {len(diag_history)} "
                    f"times — {diag[:200]} (short-circuited after "
                    f"{attempt} cycle(s))"
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
        transient: bool = False,
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

        # --- cross-spawn stall detection ---------------------------------
        # Detect when the implement agent's output is byte-identical to
        # the previous blocked attempt — a sign that corrective review
        # feedback is being ignored and the agent is stuck in a
        # convergence loop.  Track via summary-fingerprint comparison
        # and a stall counter persisted alongside the spec fingerprint.
        implement_md_path = ws.artifacts_dir / "implement.md"
        stall_state_path = ws.artifacts_dir / "implement_stall_state.json"
        old_summary_fp = ""
        old_stall_count = 0
        if implement_md_path.exists():
            try:
                old_md = implement_md_path.read_text(encoding="utf-8")
            except OSError:
                old_md = ""
            for line in old_md.splitlines():
                if line.startswith("summary-fingerprint: "):
                    old_summary_fp = line.split("summary-fingerprint: ", 1)[1].strip()
                elif line.startswith("stall-count: "):
                    try:
                        old_stall_count = int(line.split("stall-count: ", 1)[1].strip())
                    except ValueError:
                        old_stall_count = 0
        elif stall_state_path.exists():
            # Fall back to the persisted stall state (survives
            # operator-initiated resume-blocked clears of implement.md).
            try:
                _stall = json.loads(stall_state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError, OSError:
                _stall = {}
            old_summary_fp = _stall.get("summary_fingerprint", "")
            old_stall_count = _stall.get("stall_count", 0)

        new_summary_fp = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]

        # Manage stall counter: increment on no-progress BLOCKED cycles,
        # reset on progress or success, leave untouched for transient errors.
        stall_count = old_stall_count
        if transient:
            pass  # don't touch — transient env errors aren't agent failures
        elif ok:
            stall_count = 0
        else:
            # BLOCKED — resumable: compare with previous attempt
            if old_summary_fp and old_summary_fp == new_summary_fp:
                stall_count += 1
            else:
                stall_count = 0

        # Check against threshold and build stall diagnostic when tripped.
        stall_threshold = getattr(ctx.settings, "implement_stall_threshold", 2)
        if stall_threshold > 0 and stall_count >= stall_threshold:
            # Fetch open (non-system) review comment ids for the diagnostic
            # so the operator knows which corrective instructions went
            # unapplied — this distinguishes a convergence loop from a
            # resourcing problem.
            review_ids: list[str] = []
            try:
                _NON_FEEDBACK = {"mill", "system"}
                comments = ctx.service.list_comments(ticket.id)
                for c in comments:
                    if (
                        c.parent_id is None
                        and c.closed_at is None
                        and c.author not in _NON_FEEDBACK
                    ):
                        review_ids.append(str(c.id))
            except Exception:
                log.warning(
                    "%s: failed to list comments for stall diagnostic",
                    ticket.id,
                    exc_info=True,
                )

            stall_note = (
                f"STALL DETECTED — {stall_count} consecutive implement "
                "cycles produced no meaningful change (identical summary, "
                "no new diff). "
            )
            if review_ids:
                stall_note += (
                    "Unaddressed review comment(s): "
                    + ", ".join(f"#{rid}" for rid in review_ids)
                    + ". "
                )
            stall_note += (
                "The implement agent is not making progress despite "
                "corrective feedback.  Consider re-scoping or splitting "
                "the ticket, or hand-applying the fix."
            )
            summary = stall_note + "\n\n" + summary
        # --- end stall detection -----------------------------------------

        if transient:
            # Transient/environmental abort — do NOT persist a spec
            # fingerprint.  Writing one would poison the next pass
            # with a false "spec unchanged" block even though the
            # prior attempt did not reach a spec-determined outcome.
            header = "TRANSIENT — retryable"
            fp_line = ""
        elif ok:
            header = "passed"
            fp_line = f"spec-fingerprint: {hashlib.sha256(effective.encode('utf-8')).hexdigest()[:16]}\n"
        else:
            header = "BLOCKED — resumable"
            fp_line = f"spec-fingerprint: {hashlib.sha256(effective.encode('utf-8')).hexdigest()[:16]}\n"

        summary_fp_line = f"summary-fingerprint: {new_summary_fp}\n"
        stall_line = f"stall-count: {stall_count}\n"

        (ws.artifacts_dir / "implement.md").write_text(
            f"# Implement ({header})\nbranch: {branch}\n"
            f"{fp_line}{summary_fp_line}{stall_line}\n{summary}\n",
            encoding="utf-8",
        )
        # Persist stall-detection state to a standalone JSON file so the
        # cross-spawn stall guard survives operator-initiated resume-blocked
        # clears of implement.md (which wipes the stall metadata).
        try:
            (ws.artifacts_dir / "implement_stall_state.json").write_text(
                json.dumps(
                    {
                        "summary_fingerprint": new_summary_fp,
                        "stall_count": stall_count,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            log.warning(
                "%s: failed to write implement_stall_state.json",
                ticket.id,
                exc_info=True,
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
            from ..towncrier import maybe_generate_towncrier_fragment

            maybe_generate_towncrier_fragment(repo_dir, ticket.id, ticket.title)
            git_ops.commit_all(repo_dir, commit_message)
        # Commit extra repos (skip primary — already done above).
        if extra_roots is not None:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                if git_ops.has_changes(repo_path):
                    from ..towncrier import maybe_generate_towncrier_fragment

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
