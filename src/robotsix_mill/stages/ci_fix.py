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
from pathlib import Path
from typing import Any, NamedTuple

from ..agents.ci_fixing import CiFixResult, run_ci_fix_agent
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import _resolve_remote_url, github_token
from ..runners.pass_runner import load_memory, persist_memory
from ..runtime import tracing
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.ci_fix")

_CI_FIX_COUNTER = "ci_fix_attempts.txt"
_CI_NO_CHANGE_COUNTER = "ci_no_change_cycles.txt"
_CI_FIX_CYCLE_COUNTER = "ci_fix_cycles.txt"


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


def _format_code_scanning_alerts(alerts: list[dict]) -> str:
    """Render open code-scanning (CodeQL) alerts as a markdown block. These
    come from the security/code-scanning API, NOT the workflow job logs, so
    without them the agent can't see what a CodeQL check actually flagged."""
    if not alerts:
        return ""
    lines = ["**Code-scanning alerts (CodeQL — these are NOT in the job logs):**"]
    for a in alerts:
        loc = a.get("path", "")
        if a.get("line"):
            loc += f":{a['line']}"
        sev = a.get("severity") or "?"
        lines.append(f"- [{sev}] `{a.get('rule', '')}` {loc}: {a.get('message', '')}")
    return "\n".join(lines)


def _build_failing_summary(
    failing: list[dict], log_text: str = "", alerts: list[dict] | None = None
) -> str:
    """Build a markdown summary from the failing check list.

    When *log_text* is provided (non-empty), it is included under a
    **Job logs:** heading. When *alerts* (open code-scanning/CodeQL alerts)
    are provided they are listed too — they don't appear in the job logs.
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
    alert_block = _format_code_scanning_alerts(alerts or [])
    if alert_block:
        parts.append(alert_block)
        parts.append("")
    if log_text:
        parts.append("**Job logs:**")
        parts.append("```")
        parts.append(log_text)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


class _FailingContext(NamedTuple):
    """Data the counter/agent phases need once CI is confirmed failing."""

    repo_dir: str
    branch: str
    failing_summary: str


class CIFixStage(Stage):
    """Check forge CI status and run automated fix logic to resolve CI failures on the ticket branch."""

    name = "ci_fix"
    input_state = State.FIXING_CI
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a FIXING_CI ticket: poll forge CI status on the ticket branch and, on failure, run the automated CI-fix agent to push corrective commits."""
        # Clone phase: guards, branch resolution, and CI status routing.
        resolved = self._resolve_clone_and_status(ticket, ctx)
        if isinstance(resolved, Outcome):
            return resolved
        repo_dir, branch, failing_summary = resolved

        # Counter phase: enforce the hard per-ticket cycle ceiling.
        ceiling = self._enforce_cycle_ceiling(ticket, ctx, failing_summary)
        if ceiling is not None:
            return ceiling

        s = ctx.settings
        counter_path = ctx.service.workspace(ticket).artifacts_dir / _CI_FIX_COUNTER
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.ci_fix_max_attempts

        log.info(
            "%s: CI failing — ci-fix attempt %d/%d",
            ticket.id,
            attempt,
            max_attempts,
        )

        # Agent phase: run the ci-fix agent and route the result.
        return self._run_agent_and_finalize(
            ticket, ctx, repo_dir, branch, failing_summary, attempt, max_attempts
        )

    def _resolve_clone_and_status(
        self, ticket: Ticket, ctx: StageContext
    ) -> Outcome | _FailingContext:
        """Run the guards, resolve the clone, and route on CI status.

        Returns an early ``Outcome`` for every non-failure path (guards
        failing → BLOCKED; transient/None/pending/success/unknown →
        IMPLEMENT_COMPLETE). When CI is genuinely failing, returns a
        ``_FailingContext`` carrying the data the later phases need.
        """
        s = ctx.settings

        # Guard: forge configured.
        if s.forge_kind == "none":
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
            # CI turned green while we were waiting — re-poll; merge will
            # promote to HUMAN_MR_APPROVAL.
            #
            # Do NOT reset the hard cycle ceiling here. A flickering CI (a
            # repo with several workflows / re-runs) returns a momentary
            # "success" between failing cycles; resetting on that transient
            # green was exactly what let a runaway ci_fix loop survive ~200
            # cycles (the counter never reached the ceiling). The counter is
            # reset only on GENUINE forward progress — when merge advances the
            # ticket out of the CI gate to HUMAN_MR_APPROVAL (merge.py).
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion in ("pending", None):
            # Not yet complete; re-poll from human_mr_approval.
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion != "failure":
            # Unknown conclusion — treat as pending, re-poll.
            return Outcome(State.IMPLEMENT_COMPLETE)

        # --- CI is failing → attempt fix ---
        failing = status.get("failing", [])
        failing_summary = self._build_failure_detail(ticket, ctx, branch, failing)
        return _FailingContext(repo_dir, branch, failing_summary)

    def _build_failure_detail(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        failing: list[dict[str, Any]],
    ) -> str:
        """Enrich the failing-check list with job logs + code-scanning alerts."""
        s = ctx.settings

        # Fetch job logs + code-scanning alerts for richer context (only on
        # failure, not on every PR poll — this stage runs infrequently).
        log_text = ""
        alerts: list[dict] = []
        try:
            forge = get_forge(s, repo_config=ctx.repo_config)
            alerts = forge.list_code_scanning_alerts(source_branch=branch)
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
            log.warning("%s: failed to fetch job logs / alerts", ticket.id)

        return _build_failing_summary(failing, log_text, alerts)

    def _enforce_cycle_ceiling(
        self, ticket: Ticket, ctx: StageContext, failing_summary: str
    ) -> Outcome | None:
        """Apply the hard per-ticket cycle ceiling.

        On a ceiling hit, resets the cycle counter, logs, records the
        best-effort history note and returns the BLOCKED ``Outcome``.
        Otherwise increments the cycle counter and returns ``None``.
        """
        s = ctx.settings

        # Hard per-ticket cycle ceiling: count every cycle that actually runs
        # the agent on still-failing CI, regardless of self-reported status or
        # whether commits were produced.  Reset only when CI is observed green
        # (the conclusion == "success" branch above).  This bounds a runaway
        # loop that keeps committing useless churn while remote CI stays red —
        # a loop that resets both the attempt and no-change counters every
        # cycle and would otherwise never escalate.
        cycle_counter_path = (
            ctx.service.workspace(ticket).artifacts_dir / _CI_FIX_CYCLE_COUNTER
        )
        cycles = _read_counter(cycle_counter_path)
        if s.ci_fix_max_cycles > 0 and cycles >= s.ci_fix_max_cycles:
            # Stop before spending another full agent run.
            _write_counter(cycle_counter_path, 0)
            log.warning(
                "%s: ci-fix hit hard ceiling of %d cycle(s) without turning "
                "CI green — escalating to BLOCKED without running the agent",
                ticket.id,
                s.ci_fix_max_cycles,
            )
            # Persist WHAT failed to the ticket history so a human doesn't have
            # to dig into GitHub/Langfuse to learn why ci-fix gave up.
            try:
                ctx.service.add_history_note(
                    ticket.id,
                    "ci-fix gave up — last CI failure:\n\n"
                    + (failing_summary or "(no failure detail captured)")[:3000],
                )
            except Exception:  # noqa: BLE001 — history note is best-effort
                log.warning("%s: failed to record ci-fix failure note", ticket.id)
            return Outcome(
                State.BLOCKED,
                f"ci fix exhausted hard ceiling of {s.ci_fix_max_cycles} "
                f"cycle(s) without turning CI green — manual intervention "
                f"required. Resume-blocked to retry from human_mr_approval.",
            )
        _write_counter(cycle_counter_path, cycles + 1)
        return None

    def _run_agent_and_finalize(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        failing_summary: str,
        attempt: int,
        max_attempts: int,
    ) -> Outcome:
        """Run the ci-fix agent and route success / retry / exhausted cases."""
        counter_path = ctx.service.workspace(ticket).artifacts_dir / _CI_FIX_COUNTER

        result = self._invoke_agent(ticket, ctx, repo_dir, branch, failing_summary)

        if result is not None and result.status == "DONE":
            return self._finalize_success(
                ticket, ctx, repo_dir, branch, counter_path, attempt
            )

        if result is not None and result.status == "OUT_OF_SCOPE":
            return self._handle_out_of_scope(ticket, ctx, result)

        # Agent failed (result is None on crash, or status == "FAILED").
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

    def _invoke_agent(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        failing_summary: str,
    ) -> CiFixResult | None:
        """Run the ci-fix agent inside the ticket span.

        Returns the full :class:`CiFixResult` so the caller can route on
        the agent's status (DONE / FAILED / OUT_OF_SCOPE), or ``None`` when
        the agent crashes (treated as a failure by the caller).
        """
        s = ctx.settings
        try:
            # ci_fix is traced=False, so wrap the LLM agent in the
            # ticket's Langfuse session (session.id = ticket.id) — same
            # reason as the rebase agent: keep its cost/traces attributed
            # to the ticket instead of an orphan root trace.
            with tracing.start_ticket_root_span(ticket.id, "ci_fix"):
                ci_fix_memory_path = s.memory_file_for(
                    "ci_fix", ctx.memory_board_id(ticket)
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
                if result.updated_memory:
                    persist_memory(ci_fix_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ci-fix agent crashed: %s", ticket.id, e)
            return None
        return result

    def _handle_out_of_scope(
        self,
        ticket: Ticket,
        ctx: StageContext,
        result: CiFixResult,
    ) -> Outcome:
        """Route an out-of-scope CI failure to a dedicated fix ticket.

        Instead of churning the ci-fix loop on repo debt this ticket never
        introduced (then blocking a ticket that is actually fine), spawn
        (or reuse) a fix ticket, wire a dependency both ways, and park THIS
        ticket to BLOCKED. The service's ``_fire_unblocks`` path moves it
        ``BLOCKED -> DRAFT`` once the fix ticket reaches DONE, re-running the
        full pipeline rebased on the now-fixed main.

        Scoped to ci_fix per the operator's ask. The same
        park-on-out-of-scope-dependency pattern could generalize to the
        verify / review / merge stages later, but is intentionally NOT
        applied there here.
        """
        board_id = ctx.repo_config.board_id if ctx.repo_config else None

        # Deterministic title so the spawn is idempotent across cycles.
        title = (
            f"ci_fix: out-of-scope CI failure — "
            f"{result.failing_check} in {result.required_change_area}"
        )

        # Dedup: reuse a still-open fix ticket with this exact title rather
        # than create a second one on a later out-of-scope cycle.
        fix_id: str | None = None
        for cand in ctx.service.recent_proposals_for(
            SourceKind.CI_FIX_DEPENDENCY, limit=100
        ):
            if cand.title == title and cand.state not in (State.CLOSED, State.DONE):
                fix_id = cand.id
                break

        if fix_id is None:
            description = (
                f"## Out-of-scope CI failure routed from {ticket.id}\n\n"
                f"**Failing check:** {result.failing_check}\n\n"
                f"**Required change area:** {result.required_change_area}\n\n"
                f"**Why out of scope:** {result.out_of_scope_reason}\n"
            )
            fix = ctx.service.create(
                title=title,
                description=description,
                source=SourceKind.CI_FIX_DEPENDENCY,
                kind="task",
                board_id=board_id,
            )
            fix_id = fix.id

        # Wire both directions: the original depends on the fix; the fix
        # auto-unblocks the original when it completes.
        ctx.service.set_depends_on(ticket.id, [fix_id])
        ctx.service.set_unblocks(fix_id, [ticket.id])

        # Link the two tickets via history notes (best-effort, like
        # _enforce_cycle_ceiling's failure note).
        try:
            ctx.service.add_history_note(
                ticket.id,
                f"parked pending out-of-scope CI fix {fix_id}: {result.failing_check}",
            )
        except Exception:  # noqa: BLE001 — history note is best-effort
            log.warning("%s: failed to record out-of-scope park note", ticket.id)
        try:
            ctx.service.add_history_note(
                fix_id,
                f"spawned by {ticket.id} for out-of-scope CI failure: "
                f"{result.out_of_scope_reason}",
            )
        except Exception:  # noqa: BLE001 — history note is best-effort
            log.warning("%s: failed to record out-of-scope spawn note", fix_id)

        # Reset the per-ticket ci_fix counters so a later re-entry (after
        # auto-unblock + a fresh pipeline pass) starts clean.
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        for counter in (_CI_FIX_COUNTER, _CI_NO_CHANGE_COUNTER, _CI_FIX_CYCLE_COUNTER):
            _write_counter(artifacts_dir / counter, 0)

        return Outcome(
            State.BLOCKED,
            f"CI failure is out of scope for this ticket; parked pending fix "
            f"ticket {fix_id}. Auto-resumes when that fix reaches DONE.",
        )

    def _finalize_success(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        counter_path: Path,
        attempt: int,
    ) -> Outcome:
        """On agent success: no-change detection, force-push, counter resets."""
        s = ctx.settings

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
                "%s: ci fix succeeded but no code changes — no-change cycle %d/%s",
                ticket.id,
                no_change_cycles,
                max_no_change if max_no_change > 0 else float("inf"),
            )
        else:
            # Agent produced commits — reset the no-change counter.
            _write_counter(no_change_counter_path, 0)

        # Fix applied → force-push only the ticket branch. Use the
        # per-repo remote + token; the global s.forge_remote_url and a
        # tokenless mint point at the mill's own repo, so a ci-fix on
        # another board would push to the wrong remote.
        try:
            git_ops.push(
                repo_dir,
                branch=branch,
                remote_url=_resolve_remote_url(s, ctx.repo_config),
                token=github_token(s, repo_config=ctx.repo_config),
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
