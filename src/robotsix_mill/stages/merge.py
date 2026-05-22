"""Merge stage: IN_REVIEW -> DONE (merged) | BLOCKED (closed unmerged)
                                    -> REBASING (conflicting, deferred)
                                    -> FIXING_CI (failing CI, deferred).

The PR is the review. This stage is re-run by the worker's lightweight
poll while the ticket sits in IN_REVIEW, REBASING, or FIXING_CI; it
checks the forge:

IN_REVIEW:
- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> check CI status:
    - failing CI    -> FIXING_CI (auto-fix agent)
    - green CI      -> IN_REVIEW (no-op; re-poll)
    - pending CI    -> IN_REVIEW (no-op; re-poll)
- open, conflicting -> REBASING (deferred; the rebase agent runs on
                       the next poll via the REBASING path, not inline).

REBASING:
- invokes rebase agent; on success force-pushes the ticket branch and
  returns to IN_REVIEW; on failure with attempts remaining stays in
  REBASING for a retry; on exhaustion → BLOCKED.

Returning the *same* state is the worker's "leave it, re-poll" signal —
no history spam, no busy loop.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..agents.rebasing import run_rebase_agent
from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import github_token
from ..pass_runner import load_memory, persist_memory
from ..runtime import tracing
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.merge")

_REBASE_COUNTER = "rebase_attempts.txt"


def _read_counter(path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_counter(path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def _workspace_repo_dir(ctx, ticket) -> str | None:
    """Return the ticket's workspace clone dir, or None if missing."""
    ws = ctx.service.workspace(ticket)
    repo = ws.dir / "repo"
    if not (repo / ".git").exists():
        return None
    return str(repo)


class MergeStage(Stage):
    name = "merge"
    input_state = State.IN_REVIEW
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings
        if s.forge_kind == "none" or not s.forge_remote_url:
            return Outcome(State.BLOCKED, "forge not configured")
        try:
            github_token(s)  # surfaces a clear config error early
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        # REBASING path: skip PR status, go straight to rebase execution.
        if ticket.state is State.REBASING:
            return self._run_rebase(ticket, ctx)

        # IN_REVIEW path: poll PR status.
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        try:
            pr = get_forge(s).pr_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.IN_REVIEW)  # no-op

        if pr is None:
            return Outcome(State.IN_REVIEW)  # not visible yet — re-poll
        if pr.get("merged"):
            ctx.service.workspace(ticket).artifacts_dir.joinpath(
                "merge.md"
            ).write_text(f"merged: {pr.get('url', '')}\n", encoding="utf-8")
            log.info("%s: PR merged → done", ticket.id)
            return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )

        # PR is open.  Check mergeability.
        mergeable = pr.get("mergeable")
        if mergeable is False:
            # PR is open and conflicting → defer to the REBASING state.
            # The (expensive, LLM-driven) rebase agent runs on the next
            # poll via the REBASING path — keeping the IN_REVIEW poll
            # cheap and the rebase activity visible (#26). Running it
            # inline here regressed that and starved the worker pool.
            log.info(
                "%s: PR conflicting — deferring to REBASING state",
                ticket.id,
            )
            return Outcome(
                State.REBASING,
                "PR is conflicting; rebase agent will run next poll",
            )

        # mergeable=True or None (unchecked) → no conflict. This is the
        # only true "rebase made progress" signal — clear the rebase
        # attempt counter so a *later* genuine conflict gets a fresh
        # budget (and so the counter can't accumulate across unrelated
        # conflicts).
        _write_counter(
            ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER,
            0,
        )

        # Check remote CI before returning no-op.
        try:
            ci_status = get_forge(s).check_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning(
                "%s: check_status failed (retry): %s", ticket.id, e
            )
            return Outcome(State.IN_REVIEW)

        if ci_status is None:
            # No PR or no data — standard wait.
            return Outcome(State.IN_REVIEW)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info("%s: mergeable PR has failing CI → fixing_ci", ticket.id)
            return Outcome(State.FIXING_CI)

        # success, pending, or None — standard wait.
        return Outcome(State.IN_REVIEW)

    def _run_rebase(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Execute the rebase agent for a ticket already in REBASING."""
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        return self._handle_conflict(ticket, ctx, branch)

    def _handle_conflict(  # noqa: C901  # TODO: split into smaller functions (ticket: split_merge_stage)
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> Outcome:
        """Attempt rebase for a conflicting PR."""
        s = ctx.settings
        repo_dir = _workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return Outcome(
                State.BLOCKED,
                "PR is conflicting but workspace clone is missing; "
                "cannot rebase. Re-run implement to recreate the clone.",
            )

        counter_path = (
            ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER
        )
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.rebase_max_attempts

        target = s.forge_target_branch
        log.info(
            "%s: PR conflicting — rebase attempt %d/%d onto %s",
            ticket.id, attempt, max_attempts, target,
        )

        try:
            # The merge stage is traced=False (poll-driven, normally no
            # LLM), so the worker does NOT open the ticket's root span.
            # The rebase agent IS an LLM run — wrap it in the ticket's
            # Langfuse session (session.id = ticket.id) so its cost and
            # traces are attributed to the ticket, not an orphan root
            # trace. (This is what made the overnight rebase cost
            # invisible in the per-ticket session total.)
            with tracing.start_ticket_root_span(ticket.id, "rebase"):
                # Refresh origin/<target> so the agent rebases onto
                # current main, not the stale ref frozen at clone time.
                # The sandbox has --network none; git fetch MUST run
                # here, outside the container.
                git_ops.fetch(
                    Path(repo_dir),
                    remote_url=s.forge_remote_url,
                    token=github_token(s),
                    branch=target,
                )
                memory_text = load_memory(s.rebase_memory_file)
                result = run_rebase_agent(
                    settings=s,
                    repo_dir=repo_dir,
                    branch=branch,
                    target=target,
                    memory=memory_text,
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    persist_memory(s.rebase_memory_file, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: rebase attempt failed: %s", ticket.id, e)
            ok = False

        if ok:
            # Only force-push when the remote doesn't already have this
            # exact commit. GitHub reports mergeable=False transiently
            # right after any push (while it recomputes); pushing an
            # unchanged branch re-triggers CI + another recompute →
            # endless REBASING↔IN_REVIEW ping-pong on a healthy PR (and
            # an ntfy every cycle). The merge stage fetched
            # origin/<branch> before invoking the agent, so
            # origin/<branch> is fresh.
            try:
                local = git_ops.head_sha(repo_dir)
                remote = git_ops.remote_branch_sha(repo_dir, branch)
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
                        ticket.id, attempt, max_attempts,
                    )
                    return Outcome(State.REBASING)  # silent re-poll
                _write_counter(counter_path, 0)
                log.warning(
                    "%s: rebase keeps being a no-op but the PR is still "
                    "conflicting after %d attempts", ticket.id, max_attempts,
                )
                return Outcome(
                    State.BLOCKED,
                    "rebase is a no-op yet GitHub still reports the PR "
                    "conflicting — the local clone's base is likely stale "
                    "or the conflict needs manual resolution. "
                    "Resume-blocked to retry from in_review.",
                )

            # Remote is behind / missing → genuine push needed.
            try:
                git_ops.push(
                    repo_dir,
                    branch=branch,
                    remote_url=s.forge_remote_url,
                    token=github_token(s),
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
            # mergeable PR clears the counter (in the IN_REVIEW path).
            # So persist the attempt and bound the loop here too.
            log.info("%s: rebase succeeded, branch force-pushed", ticket.id)
            if attempt < max_attempts:
                _write_counter(counter_path, attempt)
                return Outcome(State.IN_REVIEW)  # re-check; may now merge
            _write_counter(counter_path, 0)  # reset for a future resume
            return Outcome(
                State.BLOCKED,
                f"rebased and force-pushed {max_attempts}x but GitHub "
                "still reports the PR conflicting — the local clone's "
                "base is likely stale or the conflict is unresolvable "
                "automatically. Resume-blocked to retry from in_review.",
            )

        # Agent failed.
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: rebase attempt %d/%d failed — retrying next poll",
                ticket.id, attempt, max_attempts,
            )
            return Outcome(State.REBASING)  # no-op; retry next poll

        # Exhausted all attempts.
        _write_counter(counter_path, 0)  # reset for any future resume
        return Outcome(
            State.BLOCKED,
            f"rebase failed after {max_attempts} attempt(s) — "
            "manual conflict resolution required. "
            "Resume-blocked to retry from in_review.",
        )
