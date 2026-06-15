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

        # Already mergeable? Then the conflict that put this ticket in
        # REBASING is gone — skip the rebase entirely. Rebasing here would
        # needlessly reconcile with the remote PR branch and can BLOCK on a
        # "diverged workspace clone" even though nothing needs rebasing,
        # leaving a CLEAN+MERGEABLE PR stuck oscillating rebasing↔blocked.
        # Re-poll the gates (IMPLEMENT_COMPLETE) so a green PR advances.
        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception:  # noqa: BLE001 — best-effort; fall through to rebase
            pr = None
        if pr is not None and pr.get("state") == "open" and pr.get("mergeable") is True:
            counter_path = (
                ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER
            )
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

        ok = self._fetch_and_run_rebase(
            ticket, s, ctx.repo_config, repo_dir, branch, target, attempt
        )

        if isinstance(ok, Outcome):
            return ok  # e.g. BLOCKED on a diverged PR branch
        if ok:
            return self._handle_rebase_success(
                ticket, ctx, branch, repo_dir, counter_path, attempt, max_attempts
            )
        return self._handle_rebase_failure(ticket, counter_path, attempt, max_attempts)

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
    ) -> bool | Outcome:
        """Fetch target branch and invoke the rebase agent.

        Returns True on success, False on a (retryable) rebase failure, or
        an ``Outcome`` to return directly (e.g. BLOCKED when the remote PR
        branch has diverged and must not be force-pushed over)."""
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

                # Refresh origin/<target> so the agent rebases onto
                # current main, not the stale ref frozen at clone time.
                # The sandbox has --network none; git fetch MUST run
                # here, outside the container.
                #
                # Use the per-repo remote_url + a freshly-minted
                # token — the global ``forge_remote_url`` and a
                # tokenless mint would both point at the wrong repo
                # (or carry an expired token) for any ticket whose
                # repo isn't the mill's own.
                _facade.git_ops.fetch(
                    Path(repo_dir),
                    remote_url=remote_url,
                    token=token,
                    branch=target,
                )
                rebase_memory_path = s.memory_file_for(
                    "rebase",
                    (repo_config.board_id if repo_config else "")
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
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    _facade.persist_memory(rebase_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: rebase attempt failed: %s", ticket.id, e)
            ok = False
        return ok

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
        """Handle a successful rebase: SHA guard, force-push, outcome routing."""
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        # Only force-push when the remote doesn't already have this
        # exact commit. GitHub reports mergeable=False transiently
        # right after any push (while it recomputes); pushing an
        # unchanged branch re-triggers CI + another recompute →
        # endless REBASING↔HUMAN_MR_APPROVAL ping-pong on a healthy PR (and
        # an ntfy every cycle). The merge stage fetched
        # origin/<branch> before invoking the agent, so
        # origin/<branch> is fresh.
        try:
            local = _facade.git_ops.head_sha(repo_dir)
            remote = _facade.git_ops.remote_branch_sha(repo_dir, branch)
        except Exception:  # noqa: BLE001 — be safe: fall back to push
            local, remote = None, "force-push"

        if local is not None and remote == local:
            # Nothing to push. The rebase made no change yet GitHub
            # still flags the PR — either GitHub is still recomputing
            # (it will clear on a later poll → merge) or the local
            # base is stale / the conflict is genuinely unresolvable.
            # This is NOT progress: count it (don't reset) and bound
            # the loop. Stay REBASING — a same-state no-op the worker
            # leaves alone (no transition, no ntfy) — until the
            # attempt budget is spent, then BLOCKED once.
            if attempt < max_attempts:
                _write_counter(counter_path, attempt)
                log.info(
                    "%s: rebase no-op (remote already current) — "
                    "GitHub still flags conflict; re-poll %d/%d",
                    ticket.id,
                    attempt,
                    max_attempts,
                )
                return Outcome(State.REBASING)  # silent re-poll
            _write_counter(counter_path, 0)
            log.warning(
                "%s: rebase keeps being a no-op but the PR is still "
                "conflicting after %d attempts",
                ticket.id,
                max_attempts,
            )
            return Outcome(
                State.BLOCKED,
                "rebase is a no-op yet GitHub still reports the PR "
                "conflicting — the local clone's base is likely stale "
                "or the conflict needs manual resolution. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        # Remote is behind / missing → genuine push needed.
        # Push to the *per-repo* remote with a per-repo token — the global
        # ``s.forge_remote_url`` + tokenless mint point at the mill's own
        # repo, so for any ticket on another board the rebased commit
        # lands on the wrong remote, the real PR branch never changes, and
        # the loop blocks ("force-pushed Nx but still conflicting"). Mirror
        # the fetch above, which already resolves these per-repo.
        #
        # Use push_with_lease so a concurrent human push to the PR branch
        # is never silently overwritten.
        try:
            _facade.git_ops.push_with_lease(
                Path(repo_dir),
                branch=branch,
                remote_url=_facade._resolve_remote_url(s, ctx.repo_config),
                token=_facade.github_token(s, repo_config=ctx.repo_config),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("%s: force-push after rebase failed: %s", ticket.id, e)
            _write_counter(counter_path, attempt)
            return Outcome(
                State.BLOCKED,
                f"rebase succeeded but force-push failed: {e}",
            )
        # Pushed — but a push is NOT proof the conflict is resolved
        # (git rebase rewrites SHAs every run, so "pushed" happens
        # even when the rebase keeps failing to truly resolve and
        # GitHub still reports the PR conflicting). Only an actually
        # mergeable PR clears the counter (in the HUMAN_MR_APPROVAL path).
        # So persist the attempt and bound the loop here too.
        log.info("%s: rebase succeeded, branch force-pushed", ticket.id)
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            # Route by context: no PR yet → back to implement; PR exists → re-check gates.
            try:
                pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                    source_branch=branch
                )
            except Exception:
                pr = None
            next_state = State.READY if pr is None else State.IMPLEMENT_COMPLETE
            return Outcome(next_state)
        _write_counter(counter_path, 0)  # reset for a future resume
        return Outcome(
            State.BLOCKED,
            f"rebased and force-pushed {max_attempts}x but GitHub "
            "still reports the PR conflicting — the local clone's "
            "base is likely stale or the conflict is unresolvable "
            "automatically. Resume-blocked to retry from human_mr_approval.",
        )

    def _handle_rebase_failure(
        self,
        ticket: Ticket,
        counter_path: Path,
        attempt: int,
        max_attempts: int,
    ) -> Outcome:
        """Handle a failed rebase: retry counting or BLOCKED when exhausted."""
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: rebase attempt %d/%d failed — retrying next poll",
                ticket.id,
                attempt,
                max_attempts,
            )
            return Outcome(State.REBASING)  # no-op; retry next poll

        # Exhausted all attempts.
        _write_counter(counter_path, 0)  # reset for any future resume
        return Outcome(
            State.BLOCKED,
            f"rebase failed after {max_attempts} attempt(s) — "
            "manual conflict resolution required. "
            "Resume-blocked to retry from human_mr_approval.",
        )
