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
from . import dependency_fix
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.ci_fix")

_CI_FIX_COUNTER = "ci_fix_attempts.txt"
_CI_NO_CHANGE_COUNTER = "ci_no_change_cycles.txt"
_CI_FIX_CYCLE_COUNTER = "ci_fix_cycles.txt"
_CI_REFRESH_COUNTER = "ci_fix_refresh_attempts.txt"


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


def _partition_alerts_by_diff(
    alerts: list[dict], changed_paths: set[str]
) -> tuple[list[dict], list[dict]]:
    """Split open code-scanning alerts into (in_scope, out_of_scope).

    An alert is IN SCOPE when its repo-relative ``path`` is among the PR's
    changed files; otherwise it is an out-of-scope candidate. Alerts with an
    empty/missing ``path`` are treated as out-of-scope (cannot prove they are
    in the diff).
    """
    in_scope: list[dict] = []
    out_of_scope: list[dict] = []
    for a in alerts:
        path = a.get("path", "")
        if path and path in changed_paths:
            in_scope.append(a)
        else:
            out_of_scope.append(a)
    return in_scope, out_of_scope


def _pr_changed_paths(forge, branch: str) -> set[str]:
    # Best-effort: if pr_files cannot be fetched, the set is empty → no alert
    # is provably in-diff → the stage falls back to today's behaviour (may
    # spawn). This is the conservative direction and is intentional.
    try:
        return {f.get("path", "") for f in forge.pr_files(source_branch=branch)} - {""}
    except Exception:  # noqa: BLE001 — best-effort; degrade to empty set
        return set()


def _alert_loc(a: dict) -> str:
    """Return the ``path`` or ``path:line`` location string for an alert."""
    loc = a.get("path", "")
    if a.get("line"):
        loc += f":{a['line']}"
    return loc


def _format_alert_refs(alerts: list[dict]) -> str:
    """Render alerts as a compact ``rule @ path:line`` semicolon list."""
    return "; ".join(f"{a.get('rule', '')} @ {_alert_loc(a)}" for a in alerts)


def _format_labelled_alerts(in_scope: list[dict], out_of_scope: list[dict]) -> str:
    """Render code-scanning alerts split into in-diff / untouched sections.

    Each alert is explicitly marked so the agent (and any downstream fixer)
    sees which alerts it MUST fix in-scope versus which may be out of scope.
    """
    if not in_scope and not out_of_scope:
        return ""
    lines = ["**Code-scanning alerts (CodeQL — these are NOT in the job logs):**"]
    if in_scope:
        lines.append(
            "The following CodeQL alert(s) are located in THIS PR's own changed "
            "files and MUST be fixed in-scope — do NOT report OUT_OF_SCOPE for "
            "them:"
        )
        for a in in_scope:
            sev = a.get("severity") or "?"
            lines.append(
                f"- [{sev}] `{a.get('rule', '')}` {_alert_loc(a)}: "
                f"{a.get('message', '')} — IN THIS PR'S DIFF — must fix"
            )
    if out_of_scope:
        lines.append("Alert(s) in untouched files (may be out of scope):")
        for a in out_of_scope:
            sev = a.get("severity") or "?"
            lines.append(
                f"- [{sev}] `{a.get('rule', '')}` {_alert_loc(a)}: "
                f"{a.get('message', '')} — untouched file (out-of-scope candidate)"
            )
    return "\n".join(lines)


def _build_failing_summary(
    failing: list[dict],
    log_text: str = "",
    alerts: list[dict] | None = None,
    changed_paths: set[str] | None = None,
) -> str:
    """Build a markdown summary from the failing check list.

    When *log_text* is provided (non-empty), it is included under a
    **Job logs:** heading. When *alerts* (open code-scanning/CodeQL alerts)
    are provided they are listed too — they don't appear in the job logs.
    When *changed_paths* is provided, the alerts are partitioned against the
    PR's own diff and rendered with explicit in-scope / out-of-scope labels.
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
    if changed_paths is None:
        alert_block = _format_code_scanning_alerts(alerts or [])
    else:
        in_scope, out_of_scope = _partition_alerts_by_diff(alerts or [], changed_paths)
        alert_block = _format_labelled_alerts(in_scope, out_of_scope)
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
            #
            # Do reset the refresh counter, though: CI going green is genuine
            # forward progress, so a later, independent staleness can be
            # refreshed once more.
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / _CI_REFRESH_COUNTER, 0
            )
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
        changed_paths: set[str] = set()
        try:
            forge = get_forge(s, repo_config=ctx.repo_config)
            alerts = forge.list_code_scanning_alerts(source_branch=branch)
            changed_paths = _pr_changed_paths(forge, branch)
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

        return _build_failing_summary(failing, log_text, alerts, changed_paths)

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
        s = ctx.settings
        counter_path = ctx.service.workspace(ticket).artifacts_dir / _CI_FIX_COUNTER

        # Reconcile with remote PR branch before running the agent so it
        # works from the latest remote state (includes any foreign commits).
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        token = github_token(s, repo_config=ctx.repo_config)
        reconciled = git_ops.reconcile_with_remote_pr(
            Path(repo_dir), remote_url, branch, token
        )
        if reconciled is git_ops.ReconcileResult.DIVERGED:
            return Outcome(
                State.BLOCKED,
                "PR branch diverged from the workspace clone (a human likely pushed to "
                "it) — manual reconciliation required. The mill refuses to "
                "force-push here: push_with_lease cannot protect this case "
                "because reconcile's own fetch already advanced the tracking "
                "ref to the foreign commit, so a lease push would pass its "
                "compare-and-swap and SILENTLY OVERWRITE that commit.",
            )
        if reconciled is git_ops.ReconcileResult.UNAVAILABLE:
            log.warning(
                "%s: could not reach the remote PR branch to reconcile — "
                "proceeding; push_with_lease backstops a stale push",
                ticket.id,
            )

        result = self._invoke_agent(ticket, ctx, repo_dir, branch, failing_summary)

        if result is not None and result.status == "DONE":
            return self._finalize_success(
                ticket, ctx, repo_dir, branch, counter_path, attempt
            )

        if result is not None and result.status == "OUT_OF_SCOPE":
            return self._handle_out_of_scope(ticket, ctx, branch, result)

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
                memory_text = load_memory(
                    ci_fix_memory_path, max_chars=s.max_memory_chars
                )
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

    def _partition_open_alerts(
        self, ctx: StageContext, branch: str
    ) -> tuple[list[dict], list[dict]]:
        """Fetch open code-scanning alerts + PR changed files and partition.

        All forge calls are best-effort: any failure degrades to "no in-scope
        alerts" (empty in_scope) so a forge outage falls back to the existing
        spawn path rather than crashing the stage.
        """
        s = ctx.settings
        try:
            forge = get_forge(s, repo_config=ctx.repo_config)
            alerts = forge.list_code_scanning_alerts(source_branch=branch)
            changed_paths = _pr_changed_paths(forge, branch)
            return _partition_alerts_by_diff(alerts, changed_paths)
        except Exception:  # noqa: BLE001 — best-effort; degrade to no in-scope
            log.warning("ci-fix in-diff alert guard failed; falling back")
            return [], []

    def _handle_out_of_scope(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        result: CiFixResult,
    ) -> Outcome:
        """Route an out-of-scope CI failure to a dedicated fix ticket.

        Before spawning, detect a *stale* branch (one behind its base, where
        the failure may already be fixed on main) and refresh it once via the
        forge's server-side update-branch primitive instead of spawning a
        dependency fix. Otherwise delegates the spawn-or-reuse + wire + park
        logic to :func:`~.dependency_fix.spawn_dependency_fix`, which is shared
        with the implement-stage baseline check (and, later, verify /
        review / merge).
        """
        s = ctx.settings

        # Deterministic in-diff guard: the LLM's OUT_OF_SCOPE verdict must not
        # be the only safety net. If ANY open code-scanning alert lives in this
        # PR's own diff, the verdict is wrong for at least those — do NOT spawn
        # a dependency fixer; route back to re-run the agent against the
        # in-scope-labelled summary instead.
        in_scope_alerts, out_of_scope_alerts = self._partition_open_alerts(ctx, branch)

        if in_scope_alerts:
            # OUT_OF_SCOPE is wrong for these alerts — suppress the spawn and
            # re-run the ci-fix agent (now driven by the in-scope-labelled
            # failing_summary). Do NOT reset the cycle counters: the hard
            # ceiling in _enforce_cycle_ceiling bounds an agent that keeps
            # refusing, so the loop stays safe.
            try:
                ctx.service.add_history_note(
                    ticket.id,
                    "ci-fix suppressed out-of-scope spawn: the following CodeQL "
                    "alert(s) are located in THIS PR's own changed files and "
                    "must be fixed in-scope: " + _format_alert_refs(in_scope_alerts),
                )
            except Exception:  # noqa: BLE001 — history note is best-effort
                log.warning("%s: failed to record in-scope-alert note", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE)

        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        refresh_path = artifacts_dir / _CI_REFRESH_COUNTER

        # Stale-branch backstop: when this branch is behind its base, the
        # failure may already be fixed on main (a fast-moving main races the
        # ci-fix agent). Refresh the branch once via the forge's server-side
        # update-branch and re-poll CI instead of spawning a dependency fix.
        # Use the forge's server-side "behind" signal (NOT the local-clone
        # branch_is_behind_main, which never advances after a server-side
        # refresh and would loop forever).
        if _read_counter(refresh_path) == 0:
            try:
                pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                    source_branch=branch
                )
            except Exception:  # noqa: BLE001 — treat as not-behind, fall through
                pr = None
            if (pr or {}).get("mergeable_state") == "behind":
                res = get_forge(s, repo_config=ctx.repo_config).update_branch(
                    source_branch=branch
                )
                if res.get("updated") or res.get("reason") == "already up to date":
                    _write_counter(refresh_path, 1)
                    try:
                        ctx.service.add_history_note(
                            ticket.id,
                            "branch was stale — refreshed via forge "
                            "update-branch before classifying out-of-scope; "
                            "re-running CI",
                        )
                    except Exception:  # noqa: BLE001 — history note is best-effort
                        log.warning(
                            "%s: failed to record branch-refresh note", ticket.id
                        )
                    return Outcome(State.IMPLEMENT_COMPLETE)
                # update_branch failed (PR not found / HTTP error) — fall
                # through to the normal spawn path so we don't get stuck.

        # Deterministic title so the spawn is idempotent across cycles.
        title = (
            f"ci_fix: out-of-scope CI failure — "
            f"{result.failing_check} in {result.required_change_area}"
        )
        description = (
            f"## Out-of-scope CI failure routed from {ticket.id}\n\n"
            f"**Failing check:** {result.failing_check}\n\n"
            f"**Required change area:** {result.required_change_area}\n\n"
            f"**Why out of scope:** {result.out_of_scope_reason}\n"
        )
        if out_of_scope_alerts:
            # Name the specific out-of-scope rule ids + paths so the dependency
            # fixer knows exactly which alerts to address (AC3).
            description += (
                "\n**Out-of-scope code-scanning alert(s):** "
                f"{_format_alert_refs(out_of_scope_alerts)}\n"
            )
        block_reason = "CI failure is out of scope for this ticket"

        outcome = dependency_fix.spawn_dependency_fix(
            ticket,
            ctx,
            title=title,
            description=description,
            source_kind=SourceKind.CI_FIX_DEPENDENCY,
            block_reason_prefix=block_reason,
            priority=ticket.priority,
        )

        # Reset the per-ticket ci_fix counters so a later re-entry (after
        # auto-unblock + a fresh pipeline pass) starts clean.
        for counter in (
            _CI_FIX_COUNTER,
            _CI_NO_CHANGE_COUNTER,
            _CI_FIX_CYCLE_COUNTER,
            _CI_REFRESH_COUNTER,
        ):
            _write_counter(artifacts_dir / counter, 0)

        return outcome

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

        # Fix applied → force-push only the ticket branch with a lease so
        # a concurrent human push is never silently overwritten. Use the
        # per-repo remote + token; the global s.forge_remote_url and a
        # tokenless mint point at the mill's own repo, so a ci-fix on
        # another board would push to the wrong remote.
        try:
            git_ops.push_with_lease(
                Path(repo_dir),
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
        # Genuine forward progress — allow a future staleness to refresh again.
        _write_counter(counter_path.parent / _CI_REFRESH_COUNTER, 0)
        log.info("%s: ci fix succeeded, branch force-pushed", ticket.id)
        return Outcome(State.IMPLEMENT_COMPLETE)  # re-check CI on next poll
