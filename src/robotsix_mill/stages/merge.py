"""Merge stage: IMPLEMENT_COMPLETE -> HUMAN_MR_APPROVAL (gates passed)
                     -> DONE (merged) | BLOCKED (closed unmerged)
                     -> FIXING_CI (failing CI, deferred)
                     -> REBASING (conflicting, deferred)

HUMAN_MR_APPROVAL -> DONE (merged) | BLOCKED (closed unmerged)
              -> IMPLEMENT_COMPLETE (gate degradation — silent fallback)
              -> WAITING_AUTO_MERGE (eligible, CI pending)

REBASING -> IMPLEMENT_COMPLETE (rebase succeeded, re-verify gates)

FIXING_CI -> IMPLEMENT_COMPLETE (fix succeeded, re-verify gates)

The PR is the review. This stage is re-run by the worker's lightweight
poll while the ticket sits in IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL,
REBASING, FIXING_CI, or WAITING_AUTO_MERGE; it checks the forge:

IMPLEMENT_COMPLETE (gate-check):
- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> check CI status:
    - failing CI    -> FIXING_CI (auto-fix agent)
    - green CI      -> HUMAN_MR_APPROVAL (gates passed! notify human)
    - pending CI    -> IMPLEMENT_COMPLETE (no-op; re-poll)
- open, conflicting -> REBASING (defer rebase agent)

HUMAN_MR_APPROVAL:
- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> check CI status:
    - failing CI    -> IMPLEMENT_COMPLETE (silent fallback)
    - green CI      -> HUMAN_MR_APPROVAL (no-op; re-poll)
    - pending CI    -> HUMAN_MR_APPROVAL (no-op; re-poll)
- open, conflicting -> IMPLEMENT_COMPLETE (silent fallback)

Returning the *same* state is the worker's "leave it, re-poll" signal —
no history spam, no busy loop.
"""

from __future__ import annotations

import contextlib
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
_MERGE_REASON = "merge_reason.txt"


def _read_counter(path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_counter(path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def _read_reason(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _write_reason(path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reason, encoding="utf-8")


def _workspace_repo_dir(ctx, ticket) -> str | None:
    """Return the ticket's workspace clone dir, or None if missing."""
    ws = ctx.service.workspace(ticket)
    repo = ws.dir / "repo"
    if not (repo / ".git").exists():
        return None
    return str(repo)


class MergeStage(Stage):
    name = "merge"
    input_state = State.HUMAN_MR_APPROVAL
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings
        if s.forge_kind == "none" or not s.forge_remote_url:
            return Outcome(State.BLOCKED, "forge not configured")
        try:
            github_token(s)  # surfaces a clear config error early
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        # IMPLEMENT_COMPLETE path: poll gates (CI + mergeability).
        if ticket.state is State.IMPLEMENT_COMPLETE:
            return self._poll_implement_complete(ticket, ctx)

        # REBASING path: skip PR status, go straight to rebase execution.
        if ticket.state is State.REBASING:
            return self._run_rebase(ticket, ctx)

        # WAITING_AUTO_MERGE path: re-poll CI, try auto-merge when green.
        if ticket.state is State.WAITING_AUTO_MERGE:
            return self._poll_waiting_auto_merge(ticket, ctx)

        # HUMAN_MR_APPROVAL path: poll PR status.
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        try:
            pr = get_forge(s).pr_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.HUMAN_MR_APPROVAL)  # no-op

        if pr is None:
            return Outcome(State.HUMAN_MR_APPROVAL)  # not visible yet — re-poll
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
            # PR is open and conflicting → silent fallback to
            # IMPLEMENT_COMPLETE so the robot can auto-fix (via
            # REBASING) without notifying the human.
            log.info(
                "%s: PR conflicting — falling back to IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                "PR is now conflicting; gates no longer pass",
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
            return Outcome(State.HUMAN_MR_APPROVAL)

        if ci_status is None:
            # No PR or no data — standard wait.
            return Outcome(State.HUMAN_MR_APPROVAL)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info("%s: mergeable PR has failing CI → falling back to IMPLEMENT_COMPLETE", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE, "CI is failing; gates no longer pass")

        # success, pending, or None — evaluate auto-merge eligibility.
        eligible, eligibility_reason = self._auto_merge_eligible(ticket, ctx)

        if conclusion == "success":
            if eligible:
                # CI green + eligible → auto-merge now.
                result = get_forge(s).merge_pr(source_branch=branch)
                if result.get("merged"):
                    ctx.service.workspace(ticket).artifacts_dir.joinpath(
                        "merge.md"
                    ).write_text(
                        f"auto-merged: {pr.get('url', '')}\n",
                        encoding="utf-8",
                    )
                    log.info("%s: auto-merged → done", ticket.id)
                    return Outcome(
                        State.DONE,
                        f"auto-merged: {pr.get('url', '')}",
                    )
                # Forge rejected the merge.
                reason_text = (
                    f"forge merge failed: {result.get('reason', 'unknown')}"
                )
                self._maybe_comment(ticket, ctx, reason_text)
                log.warning(
                    "%s: auto-merge failed: %s — falling back to human",
                    ticket.id, result.get("reason", "unknown"),
                )
                return Outcome(State.HUMAN_MR_APPROVAL, reason_text)
            else:
                # CI green but not eligible → human approval needed.
                self._maybe_comment(ticket, ctx, eligibility_reason)
                return Outcome(State.HUMAN_MR_APPROVAL)

        # pending or None — not yet green.
        if eligible:
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        # Not eligible + CI pending → standard human wait.
        self._maybe_comment(ticket, ctx, eligibility_reason)
        return Outcome(State.HUMAN_MR_APPROVAL)

    def _auto_merge_eligible(self, ticket: Ticket, ctx: StageContext) -> tuple[bool, str]:
        """Return ``(eligible, reason)`` for auto-merge.

        *eligible* is True when ALL of the following hold:
        1. ``settings.auto_merge_enabled`` is True
        2. ``settings.review_enabled`` is True
        3. Review artifact exists at ``{workspace}/artifacts/review.md``
        4. Artifact contains the literal string ``"auto_merge_eligible: true"``

        *reason* explains the blocking condition when eligible is False.
        """
        s = ctx.settings
        if not s.auto_merge_enabled:
            return False, "auto-merge disabled in config"
        if not s.review_enabled:
            return False, "review gate disabled — human approval required"

        review_artifact = (
            ctx.service.workspace(ticket).artifacts_dir / "review.md"
        )
        if not review_artifact.exists():
            return False, "no review artifact — human approval required"

        review_text = review_artifact.read_text(encoding="utf-8")
        if "auto_merge_eligible: true" not in review_text:
            # Try to read the verdict line for context.
            verdict_note = ""
            for line in review_text.splitlines():
                if line.startswith("verdict:"):
                    verdict_note = " (" + line[len("verdict:"):].strip()[:200] + ")"
                    break
            return False, "reviewer marked not auto-merge eligible" + verdict_note

        return True, "eligible"

    def _maybe_comment(self, ticket: Ticket, ctx: StageContext, reason: str) -> None:
        """Write a de-duplicated comment naming the auto-merge blocking condition.

        Reads ``merge_reason.txt`` from the workspace; skips the comment
        if the stored reason matches *reason* exactly. Otherwise writes
        the comment, then persists the new reason.
        """
        reason_path = (
            ctx.service.workspace(ticket).artifacts_dir / _MERGE_REASON
        )
        stored = _read_reason(reason_path)
        if stored == reason:
            return  # already commented — de-dupe
        ctx.service.add_comment(ticket.id, reason, author="merge")
        _write_reason(reason_path, reason)

    def _poll_waiting_auto_merge(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Re-poll CI for a ticket in WAITING_AUTO_MERGE.

        The ticket was already determined eligible for auto-merge; CI was
        pending. On each poll:
        - CI success → try auto-merge (DONE or HUMAN_MR_APPROVAL on forge reject)
        - CI failure → FIXING_CI
        - CI still pending → WAITING_AUTO_MERGE (same-state no-op)
        - Eligibility lost → HUMAN_MR_APPROVAL with comment
        """
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        # First, re-check eligibility (review artifact may have changed).
        eligible, reason = self._auto_merge_eligible(ticket, ctx)
        if not eligible:
            self._maybe_comment(ticket, ctx, reason)
            return Outcome(State.HUMAN_MR_APPROVAL, reason)

        # Re-check PR status (could have become conflicting).
        try:
            pr = get_forge(s).pr_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.WAITING_AUTO_MERGE)

        if pr is None:
            return Outcome(State.WAITING_AUTO_MERGE)
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
        mergeable = pr.get("mergeable")
        if mergeable is False:
            log.info(
                "%s: PR became conflicting while waiting for CI → IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(State.IMPLEMENT_COMPLETE, "PR is now conflicting; gates no longer pass")

        # Check CI.
        try:
            ci_status = get_forge(s).check_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.WAITING_AUTO_MERGE)

        if ci_status is None:
            # No CI data yet — keep waiting.
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info("%s: CI failed while waiting for auto-merge → IMPLEMENT_COMPLETE", ticket.id)
            self._maybe_comment(ticket, ctx, "CI failed — falling back to gate check")
            return Outcome(State.IMPLEMENT_COMPLETE, "CI failed; gates no longer pass")

        if conclusion == "success":
            # CI is green — attempt auto-merge.
            result = get_forge(s).merge_pr(source_branch=branch)
            if result.get("merged"):
                ctx.service.workspace(ticket).artifacts_dir.joinpath(
                    "merge.md"
                ).write_text(
                    f"auto-merged: {pr.get('url', '')}\n",
                    encoding="utf-8",
                )
                log.info("%s: auto-merged → done", ticket.id)
                return Outcome(
                    State.DONE,
                    f"auto-merged: {pr.get('url', '')}",
                )
            # Forge rejected the merge.
            reason_text = f"forge merge failed: {result.get('reason', 'unknown')}"
            self._maybe_comment(ticket, ctx, reason_text)
            log.warning(
                "%s: auto-merge failed: %s — falling back to human",
                ticket.id, result.get("reason", "unknown"),
            )
            return Outcome(State.HUMAN_MR_APPROVAL, reason_text)

        # Pending or None — keep waiting.
        self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
        return Outcome(State.WAITING_AUTO_MERGE)

    def _poll_implement_complete(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status for a ticket in IMPLEMENT_COMPLETE.

        Verify two gates before promoting to HUMAN_MR_APPROVAL:
        1. CI is green.
        2. PR is mergeable (no conflict with target).

        - Both gates pass → HUMAN_MR_APPROVAL (notify human).
        - CI failing → FIXING_CI (defer CI-fix agent).
        - Conflicting → REBASING (defer rebase agent).
        - CI pending / no data → same-state IMPLEMENT_COMPLETE (re-poll).
        - PR merged while polling → DONE.
        - PR closed → BLOCKED.
        """
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        try:
            pr = get_forge(s).pr_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if pr is None:
            return Outcome(State.IMPLEMENT_COMPLETE)  # not visible yet — re-poll
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
            log.info(
                "%s: PR conflicting in IMPLEMENT_COMPLETE → REBASING",
                ticket.id,
            )
            return Outcome(
                State.REBASING,
                "PR is conflicting; rebase agent will run next poll",
            )

        # mergeable=True or None (unchecked) → no conflict.
        # Clear rebase attempt counter — this is signal of progress.
        _write_counter(
            ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER,
            0,
        )

        # Check remote CI.
        try:
            ci_status = get_forge(s).check_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning(
                "%s: check_status failed (retry): %s", ticket.id, e
            )
            return Outcome(State.IMPLEMENT_COMPLETE)

        if ci_status is None:
            # No CI data yet — keep waiting.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info("%s: CI failing → FIXING_CI", ticket.id)
            return Outcome(State.FIXING_CI)

        if conclusion == "success":
            # Both gates passed! Promote to human review.
            log.info("%s: gates passed → HUMAN_MR_APPROVAL", ticket.id)
            return Outcome(State.HUMAN_MR_APPROVAL)

        # pending or None — keep waiting.
        return Outcome(State.IMPLEMENT_COMPLETE)

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
            # Build the context-manager stack based on attempt number.
            # On the first attempt: open a ticket-root span so
            # Langfuse attributes the rebase agent's LLM cost/traces
            # to the ticket's session.  Retries (attempt > 1) skip
            # the root span to avoid creating duplicate Langfuse
            # traces for the same logical rebase operation.
            stack = contextlib.ExitStack()
            if attempt == 1:
                stack.enter_context(tracing.start_ticket_root_span(ticket.id, "rebase"))
            stack.enter_context(tracing.trace_stage("rebase"))
            with stack:
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
            # endless REBASING↔HUMAN_MR_APPROVAL ping-pong on a healthy PR (and
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
                    "Resume-blocked to retry from human_mr_approval.",
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
            # mergeable PR clears the counter (in the HUMAN_MR_APPROVAL path).
            # So persist the attempt and bound the loop here too.
            log.info("%s: rebase succeeded, branch force-pushed", ticket.id)
            if attempt < max_attempts:
                _write_counter(counter_path, attempt)
                # Route by context: no PR yet → back to implement; PR exists → re-check gates.
                try:
                    pr = get_forge(s).pr_status(source_branch=branch)
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
            "Resume-blocked to retry from human_mr_approval.",
        )
