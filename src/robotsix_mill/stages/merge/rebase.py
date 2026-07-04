"""RebaseMixin: rebase handling for the merge stage.

Handles the REBASING path: detects conflicts, delegates to the rebase
agent, force-pushes, and bounds retries with a per-ticket attempt counter.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from ...config import target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...forge import get_forge

# _resolve_remote_url, github_token, load_memory, persist_memory
# are accessed through the _facade import inside method bodies
# (so monkeypatching merge_mod.<name> propagates).
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _REBASE_COUNTER,
    _read_counter,
    _write_counter,
    log,
)


class RebaseMixin(_MergeStageBase):
    """Rebase handling: conflict detection, agent delegation, force-push, retry bounding."""

    def _run_rebase(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Execute the rebase agent for a ticket already in REBASING."""
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        # Genuinely CLEAN? Then the conflict that put this ticket in
        # REBASING is gone — skip the rebase entirely. Rebasing here would
        # needlessly reconcile with the remote PR branch and can BLOCK on a
        # "diverged workspace clone" even though nothing needs rebasing,
        # leaving a CLEAN+MERGEABLE PR stuck oscillating rebasing↔blocked.
        # Re-poll the gates (IMPLEMENT_COMPLETE) so a green PR advances.
        #
        # Require ``mergeable_state == "clean"``, NOT merely ``mergeable``:
        # a PR can be ``mergeable`` (no conflicts) yet ``behind`` main with
        # failing CI (``mergeable_state`` "behind"/"unstable"). Skipping
        # those would strand them — the merge stage routes a CI-failing,
        # behind-main ticket here precisely so the rebase catches it up to a
        # (now-fixed) main; skipping on bare ``mergeable`` made it oscillate
        # implement_complete↔rebasing forever without ever rebasing (live:
        # 4ed9/1084/6883/81f1 stuck 4 commits behind a freshly-fixed main).
        # ``mergeable_state`` is GitHub-specific; other forges omit it
        # (``None`` ≠ "clean") so they keep the always-attempt-rebase path.
        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception:  # noqa: BLE001 — best-effort; fall through to rebase
            pr = None
        if (
            pr is not None
            and pr.get("state") == "open"
            and pr.get("mergeable") is True
            and pr.get("mergeable_state") == "clean"
        ):
            counter_path = ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER
            _write_counter(counter_path, 0)
            log.info(
                "%s: PR already mergeable — skipping rebase, re-checking gates",
                ticket.id,
            )
            return Outcome(State.IMPLEMENT_COMPLETE)

        return self._handle_conflict(ticket, ctx, branch)

    def _handle_conflict(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> Outcome:
        """Attempt rebase for a conflicting PR."""
        s = ctx.settings

        repo_dir = self._validate_workspace_for_rebase(ctx, ticket)
        if isinstance(repo_dir, Outcome):
            return repo_dir

        counter_path, attempt, max_attempts = self._read_rebase_attempt(ctx, ticket, s)

        target = target_branch_for(s, ctx.repo_config)
        log.info(
            "%s: PR conflicting — rebase attempt %d/%d onto %s",
            ticket.id,
            attempt,
            max_attempts,
            target,
        )

        run = self._fetch_and_run_rebase(
            ticket, s, ctx.repo_config, repo_dir, branch, target, attempt
        )

        if isinstance(run, Outcome):
            return run  # e.g. BLOCKED on a diverged PR branch
        ok, detail = run
        if ok:
            return self._handle_rebase_success(
                ticket, ctx, branch, repo_dir, counter_path, attempt, max_attempts
            )
        return self._handle_rebase_failure(
            ticket, repo_dir, counter_path, attempt, max_attempts, detail
        )

    def _validate_workspace_for_rebase(
        self, ctx: StageContext, ticket: Ticket
    ) -> str | Outcome:
        """Return the repo_dir string, or an Outcome to return early if missing."""
        from robotsix_mill.stages import merge as _facade

        repo_dir = _facade._workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return Outcome(
                State.BLOCKED,
                "PR is conflicting but workspace clone is missing; "
                "cannot rebase. Re-run implement to recreate the clone.",
            )
        return repo_dir

    def _read_rebase_attempt(
        self, ctx: StageContext, ticket: Ticket, s
    ) -> tuple[Path, int, int]:
        """Return (counter_path, attempt, max_attempts) for the current rebase."""
        counter_path = ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.rebase_max_attempts
        return counter_path, attempt, max_attempts

    def _fetch_and_run_rebase(
        self,
        ticket: Ticket,
        s,
        repo_config,
        repo_dir: str,
        branch: str,
        target: str,
        attempt: int,
    ) -> tuple[bool, str] | Outcome:
        """Invoke the rebase agent with bridged git tools.

        The agent now drives its own fetch + rebase + push via the
        bridged git tools.  This method only builds the context
        (remote_url, token) and delegates to the agent.

        Returns ``(ok, detail)`` — ``ok`` True on success, False on a
        (retryable) rebase failure; ``detail`` is the agent's summary of
        what it found / why it could not resolve (surfaced in the BLOCKED
        note so a human sees the actual conflict, not a generic message).
        May instead return an ``Outcome`` to return directly (e.g. BLOCKED
        when the remote PR branch has diverged and must not be force-pushed
        over)."""
        from robotsix_mill.stages import merge as _facade

        try:
            # The merge stage is traced=False (poll-driven, normally no
            # LLM), so the worker does NOT open the ticket's root span.
            # The rebase agent IS an LLM run — wrap it in the ticket's
            # Langfuse session (session.id = ticket.id) so its cost and
            # traces are attributed to the ticket, not an orphan root
            # trace. (This is what made the overnight rebase cost
            # invisible in the per-ticket session total.)
            # Build the context-manager stack based on attempt number.
            # On the first attempt: open a ticket-root span so
            # Langfuse attributes the rebase agent's LLM cost/traces
            # to the ticket's session.  Retries (attempt > 1) skip
            # the root span to avoid creating duplicate Langfuse
            # traces for the same logical rebase operation.
            stack = contextlib.ExitStack()
            if attempt == 1:
                stack.enter_context(
                    _facade.tracing.start_ticket_root_span(ticket.id, "rebase")
                )
            stack.enter_context(_facade.tracing.trace_stage("rebase"))
            with stack:
                remote_url = _facade._resolve_remote_url(s, repo_config)
                token = _facade.github_token(s, repo_config=repo_config)

                # Reconcile with the remote PR branch first so the
                # rebase agent operates on a branch that includes any
                # foreign commits (e.g. a human pushed a fix directly).
                reconciled = _facade.git_ops.reconcile_with_remote_pr(
                    Path(repo_dir), remote_url, branch, token
                )
                if reconciled is _facade.git_ops.ReconcileResult.DIVERGED:
                    return Outcome(
                        State.BLOCKED,
                        "PR branch diverged from the workspace clone (a human likely pushed to "
                        "it) — manual reconciliation required. The mill refuses to "
                        "force-push here: push_with_lease cannot protect this case "
                        "because reconcile's own fetch already advanced the tracking "
                        "ref to the foreign commit, so a lease push would pass its "
                        "compare-and-swap and SILENTLY OVERWRITE that commit.",
                    )
                if reconciled is _facade.git_ops.ReconcileResult.UNAVAILABLE:
                    log.warning(
                        "%s: could not reach the remote PR branch to reconcile "
                        "— proceeding; push_with_lease backstops a stale push",
                        ticket.id,
                    )

                # The agent now drives fetch + rebase + push via bridged
                # git tools — pass the per-repo remote_url and token so
                # the tool closures can execute host-side. The token is
                # captured in the closure and NEVER exposed to the
                # sandbox or the agent's prompt.
                rebase_memory_path = s.memory_file_for(
                    "rebase",
                    (repo_config.repo_id if repo_config else "")
                    or s.board_id
                    or ticket.board_id,
                )
                memory_text = _facade.load_memory(rebase_memory_path)
                result = _facade.run_rebase_agent(
                    settings=s,
                    repo_dir=repo_dir,
                    branch=branch,
                    target=target,
                    memory=memory_text,
                    remote_url=remote_url,
                    token=token,
                )
                ok = result.status == "DONE"
                detail = result.summary or ""
                if result.updated_memory:
                    _facade.persist_memory(rebase_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: rebase attempt failed: %s", ticket.id, e)
            ok = False
            detail = f"rebase agent crashed: {e}"
        return (ok, detail)

    def _handle_rebase_success(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        repo_dir: str,
        counter_path: Path,
        attempt: int,
        max_attempts: int,
    ) -> Outcome:
        """Post-check after the agent-driven rebase+push.

        The agent already pushed via ``git_push_with_lease``.  This
        method runs a deterministic host-side post-check to verify the
        push actually landed and no foreign commits were clobbered.
        """
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        target = target_branch_for(s, ctx.repo_config)

        remote_url = _facade._resolve_remote_url(s, ctx.repo_config)
        token = _facade.github_token(s, repo_config=ctx.repo_config)

        check = _facade.git_ops.post_push_check(
            Path(repo_dir),
            branch=branch,
            target=target,
            remote_url=remote_url,
            token=token,
        )

        if check is _facade.git_ops.PostPushResult.PASS:
            # Push landed, no foreign commits — genuine success.
            log.info("%s: rebase succeeded, push verified", ticket.id)
            try:
                pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                    source_branch=branch
                )
            except Exception:
                pr = None

            if pr is None:
                # No PR exists — route to READY so the ticket re-enters implement.
                _write_counter(counter_path, 0)
                return Outcome(State.READY)

            mergeable = pr.get("mergeable")
            if mergeable is True and pr.get("mergeable_state") == "clean":
                # PR is genuinely clean — reset counter.
                _write_counter(counter_path, 0)
                return Outcome(State.IMPLEMENT_COMPLETE)
            if mergeable is None:
                # GitHub may report mergeable=None transiently after a
                # push — re-poll rather than block.
                log.info(
                    "%s: post-check passed but mergeable=None (transient) — re-polling",
                    ticket.id,
                )
                return Outcome(State.IMPLEMENT_COMPLETE)

            # PR still not mergeable — bound retries.
            if attempt < max_attempts:
                _write_counter(counter_path, attempt)
                return Outcome(State.IMPLEMENT_COMPLETE)
            _write_counter(counter_path, 0)
            return Outcome(
                State.BLOCKED,
                f"rebased and pushed {max_attempts}x but GitHub "
                "still reports the PR conflicting — the local clone's "
                "base is likely stale or the conflict is unresolvable "
                "automatically. Resume-blocked to retry from human_mr_approval.",
            )

        if check is _facade.git_ops.PostPushResult.NOT_LANDED:
            log.warning(
                "%s: post-check failed — remote HEAD does not match local HEAD; "
                "push did not land",
                ticket.id,
            )
            _write_counter(counter_path, attempt)
            return Outcome(
                State.BLOCKED,
                "rebase agent reported DONE but the push did not land "
                "(remote HEAD != local HEAD). The agent may have hit a "
                "lease rejection it could not recover from. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        if check is _facade.git_ops.PostPushResult.FOREIGN_DIVERGENCE:
            log.warning(
                "%s: post-check failed — remote branch carries foreign-authored "
                "commits ahead of target; a human may have pushed",
                ticket.id,
            )
            _write_counter(counter_path, attempt)
            return Outcome(
                State.BLOCKED,
                "rebase agent reported DONE but the remote branch carries "
                "foreign-authored commits — a human likely pushed to the PR "
                "branch. Manual reconciliation required. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        # UNAVAILABLE — transient fetch failure, re-poll.
        log.warning(
            "%s: post-check unavailable (fetch failed) — re-polling",
            ticket.id,
        )
        return Outcome(State.IMPLEMENT_COMPLETE)

    def _handle_rebase_failure(
        self,
        ticket: Ticket,
        repo_dir: str,
        counter_path: Path,
        attempt: int,
        max_attempts: int,
        detail: str = "",
    ) -> Outcome:
        """Handle a failed rebase: retry counting or BLOCKED when exhausted.

        *detail* is the rebase agent's own summary of what it found / why it
        could not resolve the conflict. On the final (BLOCKED) attempt this
        is combined with a deterministic list of still-conflicted files so
        the operator sees exactly which files need manual resolution instead
        of a generic "manual conflict resolution required"."""
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: rebase attempt %d/%d failed — retrying next poll%s",
                ticket.id,
                attempt,
                max_attempts,
                f": {detail}" if detail else "",
            )
            return Outcome(State.REBASING)  # no-op; retry next poll

        # Exhausted all attempts.
        _write_counter(counter_path, 0)  # reset for any future resume

        from robotsix_mill.stages import merge as _facade

        conflicts = _facade.git_ops.conflicted_files(Path(repo_dir))
        note_parts = [f"rebase failed after {max_attempts} attempt(s)."]
        if conflicts:
            shown = ", ".join(f"`{p}`" for p in conflicts[:10])
            more = f" (+{len(conflicts) - 10} more)" if len(conflicts) > 10 else ""
            note_parts.append(f"Conflicting file(s): {shown}{more}.")
        if detail:
            note_parts.append(f"Rebase agent: {detail.strip()}")
        note_parts.append(
            "Manual conflict resolution required. "
            "Resume-blocked to retry from human_mr_approval."
        )
        return Outcome(State.BLOCKED, " ".join(note_parts))
