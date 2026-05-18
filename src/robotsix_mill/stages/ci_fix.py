"""CI-fix stage: FIXING_CI -> IN_REVIEW (fix succeeded) | BLOCKED.

When the merge stage detects a mergeable PR with failing remote CI
checks, it transitions the ticket to FIXING_CI.  This stage invokes
the ci-fix agent to auto-resolve the failures, commits locally, and
force-pushes only the ticket branch.  On success the ticket goes back
to IN_REVIEW so the merge stage re-checks CI.

Failure after max attempts escalates to BLOCKED (resumable).
"""

from __future__ import annotations

import logging

from ..agents.ci_fixing import run_ci_fix_agent
from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import github_token
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.ci_fix")

_CI_FIX_COUNTER = "ci_fix_attempts.txt"


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


def _build_failing_summary(failing: list[dict]) -> str:
    """Build a markdown summary from the failing check list."""
    parts = []
    for i, chk in enumerate(failing):
        parts.append(f"## Failing check #{i + 1}: {chk['name']}")
        if chk.get("summary"):
            parts.append(f"\n**Summary:**\n{chk['summary']}")
        if chk.get("text"):
            parts.append(f"\n**Details:**\n{chk['text']}")
        anns = chk.get("annotations") or []
        if anns:
            parts.append("\n**Annotations:**")
            for a in anns:
                loc = f"{a['path']}"
                if a.get("start_line"):
                    loc += f":{a['start_line']}"
                parts.append(f"- [{a['level']}] {loc}: {a['message']}")
        parts.append("")
    return "\n".join(parts)


class CIFixStage(Stage):
    name = "ci_fix"
    input_state = State.FIXING_CI
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings

        # Guard: forge configured.
        if s.forge_kind == "none" or not s.forge_remote_url:
            return Outcome(State.BLOCKED, "forge not configured")
        try:
            github_token(s)  # surfaces a clear config error early
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        # Guard: workspace clone must exist.
        repo_dir = _workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return Outcome(
                State.BLOCKED,
                "workspace clone is missing; cannot fix CI. "
                "Re-run implement to recreate the clone.",
            )

        # Fetch check status from the forge.
        try:
            status = get_forge(s).check_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.IN_REVIEW)

        if status is None:
            # PR disappeared.
            return Outcome(State.IN_REVIEW)

        conclusion = status.get("conclusion")

        if conclusion == "success":
            # CI turned green while we were waiting.
            return Outcome(State.IN_REVIEW)

        if conclusion in ("pending", None):
            # Not yet complete; re-poll from in_review.
            return Outcome(State.IN_REVIEW)

        if conclusion != "failure":
            # Unknown conclusion — treat as pending, re-poll.
            return Outcome(State.IN_REVIEW)

        # --- CI is failing → attempt fix ---
        failing = status.get("failing", [])
        failing_summary = _build_failing_summary(failing)

        counter_path = (
            ctx.service.workspace(ticket).artifacts_dir / _CI_FIX_COUNTER
        )
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.ci_fix_max_attempts

        log.info(
            "%s: CI failing — ci-fix attempt %d/%d",
            ticket.id, attempt, max_attempts,
        )

        try:
            ok = run_ci_fix_agent(
                settings=s,
                repo_dir=repo_dir,
                branch=branch,
                failing_summary=failing_summary,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ci-fix agent crashed: %s", ticket.id, e)
            ok = False

        if ok:
            # Fix applied → force-push only the ticket branch.
            try:
                git_ops.push(
                    repo_dir,
                    branch=branch,
                    remote_url=s.forge_remote_url,
                    token=github_token(s),
                )
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "%s: force-push after ci-fix failed: %s", ticket.id, e
                )
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"ci fix succeeded but force-push failed: {e}",
                )
            # Reset counter on success.
            _write_counter(counter_path, 0)
            log.info("%s: ci fix succeeded, branch force-pushed", ticket.id)
            return Outcome(State.IN_REVIEW)  # re-check CI on next poll

        # Agent failed.
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: ci-fix attempt %d/%d failed — retrying next poll",
                ticket.id, attempt, max_attempts,
            )
            return Outcome(State.IN_REVIEW)  # no-op; retry next poll

        # Exhausted all attempts.
        _write_counter(counter_path, 0)  # reset for any future resume
        return Outcome(
            State.BLOCKED,
            f"ci fix failed after {max_attempts} attempt(s) — "
            "manual intervention required. "
            "Resume-blocked to retry from in_review.",
        )
