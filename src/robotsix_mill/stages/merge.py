"""Merge stage: IN_REVIEW -> DONE (merged) | BLOCKED (closed unmerged).

The PR is the review. This stage is re-run by the worker's lightweight
poll while the ticket sits in IN_REVIEW; it checks the forge:

- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> no-op (return IN_REVIEW; the poll retries later)
- open, conflicting -> invoke rebase agent; on success force-push the
                       ticket branch and stay IN_REVIEW; on failure
                       after MILL_REBASE_MAX_ATTEMPTS → BLOCKED.

Returning the *same* state is the worker's "leave it, re-poll" signal —
no history spam, no busy loop.
"""

from __future__ import annotations

import logging

from ..agents.rebasing import run_rebase_agent
from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import github_token
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
        if mergeable is not False:
            # mergeable=True or None (unchecked) → no conflict; standard wait.
            return Outcome(State.IN_REVIEW)  # still open — re-poll

        # --- PR is open and conflicting → attempt rebase ---
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
            ok = run_rebase_agent(
                settings=s,
                repo_dir=repo_dir,
                branch=branch,
                target=target,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("%s: rebase agent crashed: %s", ticket.id, e)
            ok = False

        if ok:
            # Clean rebase → force-push only the ticket branch.
            # Use the minted App/PAT token (like deliver) — s.forge_token
            # is the raw FORGE_TOKEN env, which is empty in App mode, so
            # the push went unauthenticated -> git exit 128.
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
            # Reset counter on success.
            _write_counter(counter_path, 0)
            log.info("%s: rebase succeeded, branch force-pushed", ticket.id)
            return Outcome(State.IN_REVIEW)  # stay in_review; next poll re-checks

        # Agent failed.
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: rebase attempt %d/%d failed — retrying next poll",
                ticket.id, attempt, max_attempts,
            )
            return Outcome(State.IN_REVIEW)  # no-op; retry next poll

        # Exhausted all attempts.
        _write_counter(counter_path, 0)  # reset for any future resume
        return Outcome(
            State.BLOCKED,
            f"rebase failed after {max_attempts} attempt(s) — "
            "manual conflict resolution required. "
            "Resume-blocked to retry from in_review.",
        )
