"""CI-fix stage: FIXING_CI -> IMPLEMENT_COMPLETE (fix succeeded) | BLOCKED.

When the merge stage detects a mergeable PR with failing remote CI
checks, it transitions the ticket to FIXING_CI.  This stage invokes
the ci-fix agent to auto-resolve the failures, commits locally, and
force-pushes only the ticket branch.  On success the ticket goes back
to IMPLEMENT_COMPLETE so the merge stage re-verifies both gates before
promoting to HUMAN_MR_APPROVAL.

Failure after max attempts escalates to BLOCKED (resumable).
"""

from __future__ import annotations

import logging

from ..agents.ci_fixing import run_ci_fix_agent
from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import github_token
from ..pass_runner import load_memory, persist_memory
from ..runtime import tracing
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.ci_fix")

_CI_FIX_COUNTER = "ci_fix_attempts.txt"
_CI_NO_CHANGE_COUNTER = "ci_no_change_cycles.txt"


def _read_counter(path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError, ValueError:
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


def _build_failing_summary(failing: list[dict], log_text: str = "") -> str:
    """Build a markdown summary from the failing check list.

    When *log_text* is provided (non-empty), it is included under a
    **Job logs:** heading after the annotations.
    """
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
    if log_text:
        parts.append("**Job logs:**")
        parts.append("```")
        parts.append(log_text)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


class CIFixStage(Stage):
    """Check forge CI status and run automated fix logic to resolve CI failures on the ticket branch."""

    name = "ci_fix"
    input_state = State.FIXING_CI
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:  # noqa: C901  # TODO: split counter, clone, and agent phases (ticket: split_ci_fix_stage)
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
            status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if status is None:
            # PR disappeared.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = status.get("conclusion")

        if conclusion == "success":
            # CI turned green while we were waiting.
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion in ("pending", None):
            # Not yet complete; re-poll from human_mr_approval.
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion != "failure":
            # Unknown conclusion — treat as pending, re-poll.
            return Outcome(State.IMPLEMENT_COMPLETE)

        # --- CI is failing → attempt fix ---
        failing = status.get("failing", [])

        # Fetch job logs for richer context (only on failure, not on
        # every PR poll — this stage runs infrequently).
        log_text = ""
        try:
            forge = get_forge(s, repo_config=ctx.repo_config)
            pr = forge.pr_status(source_branch=branch)
            head_sha = (pr or {}).get("sha", "")
            if head_sha:
                runs = forge.list_workflow_runs(head_sha=head_sha)
                for run in runs:
                    if run.get("conclusion") == "failure":
                        logs = forge.fetch_workflow_job_logs(run_id=run["id"])
                        if logs:
                            log_text += (
                                f"\n--- {run.get('name', 'workflow')} "
                                f"(run {run['id']}) ---\n{logs}"
                            )
        except Exception:  # noqa: BLE001 — best-effort enrichment
            log.warning("%s: failed to fetch job logs", ticket.id)

        failing_summary = _build_failing_summary(failing, log_text)

        counter_path = ctx.service.workspace(ticket).artifacts_dir / _CI_FIX_COUNTER
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.ci_fix_max_attempts

        log.info(
            "%s: CI failing — ci-fix attempt %d/%d",
            ticket.id,
            attempt,
            max_attempts,
        )

        try:
            # ci_fix is traced=False, so wrap the LLM agent in the
            # ticket's Langfuse session (session.id = ticket.id) — same
            # reason as the rebase agent: keep its cost/traces attributed
            # to the ticket instead of an orphan root trace.
            with tracing.start_ticket_root_span(ticket.id, "ci_fix"):
                ci_fix_memory_path = s.memory_file_for(
                    "ci_fix", ctx.repo_config.board_id if ctx.repo_config else ""
                )
                memory_text = load_memory(ci_fix_memory_path)
                result = run_ci_fix_agent(
                    settings=s,
                    repo_dir=repo_dir,
                    branch=branch,
                    failing_summary=failing_summary,
                    memory=memory_text,
                    ticket_id=ticket.id,
                    board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    persist_memory(ci_fix_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ci-fix agent crashed: %s", ticket.id, e)
            ok = False

        if ok:
            # Detect no-change cycles: the agent reported success but
            # produced no commits (local HEAD matches remote).  Track
            # consecutive no-change cycles in a separate counter so a
            # flake-storm on a diff that cannot plausibly cause test
            # failures doesn't retry unboundedly.
            no_change_counter_path = (
                ctx.service.workspace(ticket).artifacts_dir / _CI_NO_CHANGE_COUNTER
            )
            no_change_cycles = _read_counter(no_change_counter_path)

            try:
                local = git_ops.head_sha(repo_dir)
                remote = git_ops.remote_branch_sha(repo_dir, branch)
            except Exception:  # noqa: BLE001 — be safe: assume changes
                local, remote = None, "force-push"

            if local is not None and remote == local:
                # Agent made no commits — count as a no-change cycle.
                no_change_cycles += 1
                max_no_change = s.ci_max_auto_retries
                if max_no_change > 0 and no_change_cycles >= max_no_change:
                    _write_counter(counter_path, 0)
                    _write_counter(no_change_counter_path, 0)
                    return Outcome(
                        State.BLOCKED,
                        f"ci fix succeeded but made no code changes "
                        f"{no_change_cycles} consecutive time(s) — "
                        f"CI failures are likely infrastructure flakes. "
                        f"Resume-blocked to retry from human_mr_approval.",
                    )
                _write_counter(no_change_counter_path, no_change_cycles)
                log.info(
                    "%s: ci fix succeeded but no code changes — "
                    "no-change cycle %d/%d",
                    ticket.id,
                    no_change_cycles,
                    max_no_change if max_no_change > 0 else float("inf"),
                )
            else:
                # Agent produced commits — reset the no-change counter.
                _write_counter(no_change_counter_path, 0)

            # Fix applied → force-push only the ticket branch.
            try:
                git_ops.push(
                    repo_dir,
                    branch=branch,
                    remote_url=s.forge_remote_url,
                    token=github_token(s),
                )
            except Exception as e:  # noqa: BLE001
                log.exception("%s: force-push after ci-fix failed: %s", ticket.id, e)
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"ci fix succeeded but force-push failed: {e}",
                )
            # Reset attempt counter on success.
            _write_counter(counter_path, 0)
            log.info("%s: ci fix succeeded, branch force-pushed", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE)  # re-check CI on next poll

        # Agent failed.
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: ci-fix attempt %d/%d failed — retrying next poll",
                ticket.id,
                attempt,
                max_attempts,
            )
            return Outcome(State.IMPLEMENT_COMPLETE)  # no-op; retry next poll

        # Exhausted all attempts.
        _write_counter(counter_path, 0)  # reset for any future resume
        return Outcome(
            State.BLOCKED,
            f"ci fix failed after {max_attempts} attempt(s) — "
            "manual intervention required. "
            "Resume-blocked to retry from human_mr_approval.",
        )
