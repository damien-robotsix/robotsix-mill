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

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from ..agents.ci_fixing import CiFixResult, run_ci_fix_agent
from ..config import target_branch_for
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..forge import Forge, get_forge
from ..forge.auth import _resolve_remote_url, github_token
from ..forge.github_code_scanning import CodeScanningAlertsUnavailable
from ..runners.pass_runner import load_memory, persist_memory
from ..runtime import tracing
from ..vcs import git_ops
from . import dependency_fix
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.ci_fix")

# Refresh counter (rebase / forge update-branch) and the failure fingerprint
# that gates re-rebasing remain — the retry/cycle/no-change/last-done counters
# were removed when the agent took ownership of the fix→verify loop.
_CI_REFRESH_COUNTER = "ci_fix_refresh_attempts.txt"
_CI_FAILURE_FINGERPRINT = "ci_failure_fingerprint.txt"
_CI_IDENTICAL_FAILURE_COUNT = "ci_identical_failure_count.txt"
_CODQL_FP_TRIAGE_SENTINEL = "codeql_fp_triage_ran.txt"
_CODQL_FP_TRIAGE_VERDICTS = "codeql_fp_triage_verdicts.json"

# Maximum number of alerts the codeql_fp_triage agent may dismiss in a
# single pass.  Caps the blast radius of a misjudged dismissal.
_CODQL_FP_TRIAGE_MAX_DISMISSALS = 5

# Check-run names that are CodeQL-related (case-insensitive contains).
_CODQL_CHECK_NAMES = frozenset({"codeql", "code-scanning", "code scanning"})


def _read_counter(path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError, ValueError:
        return 0


def _write_counter(path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def _write_text(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


def _pr_changed_paths(forge: Forge, branch: str) -> set[str]:
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


def _format_alert_summary_block(
    alerts: list[dict[str, Any]] | None, *, codeql_failing: bool = False
) -> str:
    """Render a compact CodeQL alert summary for top-of-prompt injection.

    Returns a short bullet list of ``rule @ path:line`` entries so the
    agent sees exactly which alerts to fix without having to read through
    the full failing summary first.

    When *codeql_failing* is True and *alerts* is empty/None, emits an
    explicit could-not-retrieve notice so the ci_fix worker escalates
    rather than blocking on an un-actionable empty summary.
    """
    if not alerts:
        if codeql_failing:
            return (
                "**CodeQL alerts could not be retrieved from the code-scanning API — "
                "the CodeQL check is failing but alert details are unavailable.**\n"
            )
        return ""
    lines = [
        "**CodeQL alerts to fix (extracted for fast reference — rule ID and location):**"
    ]
    for a in alerts:
        lines.append(f"- `{a.get('rule', '?')}` @ {_alert_loc(a)}")
    lines.append("")
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

    A compact alert summary is injected at the **top** of the prompt so the
    agent can quickly identify what to fix without speculative reasoning.
    """
    parts = []
    # Inject compact alert summary at the very top for fast reference.
    codeql_failing = _only_codeql_failing(failing)
    parts.append(_format_alert_summary_block(alerts, codeql_failing=codeql_failing))
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


def _only_codeql_failing(failing: list[dict[str, Any]]) -> bool:
    """Return True when every failing check is CodeQL code-scanning.

    A check is CodeQL-related when its name contains one of the known
    CodeQL check-name substrings (case-insensitive).  Returns False
    when *failing* is empty (no failures → nothing to triage) or when
    any non-CodeQL check is failing alongside.
    """
    if not failing:
        return False
    for chk in failing:
        name = (chk.get("name") or "").lower()
        if not any(token in name for token in _CODQL_CHECK_NAMES):
            return False
    return True


def _eligible_for_triage(
    alerts: list[dict[str, Any]], changed_paths: set[str], max_dismissals: int
) -> list[dict[str, Any]]:
    """Return the subset of *alerts* eligible for FP triage.

    An alert is eligible when ALL of the following hold:
    1. Its file is in *changed_paths* (in-scope — this PR's own diff).
    2. Its ``security_severity_level`` is ``None`` (NOT a security alert).
    3. It has a ``number`` (required for the dismissal API).

    Returns at most *max_dismissals* alerts (the rest are trimmed so
    the agent never dismisses more than the cap in one pass).
    """
    eligible: list[dict[str, Any]] = []
    for a in alerts:
        path = a.get("path", "")
        if not path or path not in changed_paths:
            continue
        if a.get("security_severity_level") is not None:
            continue
        if a.get("number") is None:
            continue
        eligible.append(a)
        if len(eligible) >= max_dismissals:
            break
    return eligible


def _codeql_block_note(  # noqa: C901
    failing: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    changed_paths: set[str],
    verdicts: list[dict[str, Any]] | None = None,
    alerts_unreadable: bool = False,
) -> str | None:
    """Return a CodeQL-specific BLOCKED note, or None when CodeQL is not a blocker.

    Detects whether CodeQL is among the failing checks (sole or contributing)
    and, when it is, builds a markdown note that lists every gating alert with
    the reason the auto-solver abstained.
    """
    # --- gate: is CodeQL among the failing checks? ---
    codeql_blocking = False
    for chk in failing:
        name = (chk.get("name") or "").lower()
        if any(token in name for token in _CODQL_CHECK_NAMES):
            codeql_blocking = True
            break
    if not codeql_blocking:
        return None

    only_codeql = _only_codeql_failing(failing)

    # --- no alert details available ---
    if not alerts:
        header = (
            "Blocked on CodeQL code-scanning"
            if only_codeql
            else "Blocked partly on CodeQL code-scanning (other checks also red)"
        )
        if alerts_unreadable:
            return (
                f"{header} — alerts are UNREADABLE (HTTP 403). "
                "The CI-fix agent will not guess suppressions when "
                "alert details are unavailable. "
                "Grant the mill GitHub App the "
                "**Code scanning alerts: read** (`security-events`) "
                "permission, then re-run."
            )
        return (
            f"{header} — a human must act.\n\n"
            "Alert details could not be retrieved from the code-scanning API."
        )

    # --- partition & sort ---
    _in_scope, _out_of_scope = _partition_alerts_by_diff(alerts, changed_paths)

    verdict_by_number: dict[int, dict[str, Any]] = {}
    if verdicts:
        for v in verdicts:
            n = v.get("alert_number")
            if n is not None:
                verdict_by_number[n] = v

    sorted_alerts = sorted(alerts, key=lambda a: a.get("number", 0))

    # --- build the note ---
    header = (
        "Blocked on CodeQL code-scanning"
        if only_codeql
        else "Blocked partly on CodeQL code-scanning (other checks also red)"
    )
    lines = [f"{header} — a human must act.", ""]

    for a in sorted_alerts:
        num = a.get("number", "?")
        rule = a.get("rule", "?")
        sev = a.get("security_severity_level")
        sev_str = str(sev) if sev is not None else "n/a"
        path = a.get("path", "?")
        line = a.get("line")
        loc = f"{path}:{line}" if line is not None else path

        lines.append(
            f"- Alert {num}: `{rule}` ({loc}), security_severity_level={sev_str}"
        )

        # Determine why the auto-solver abstained.
        if sev is not None:
            lines.append(
                f"  → security-severity (level={sev}) → "
                f"requires human sign-off (guardrail policy)"
            )
        elif path and path not in changed_paths:
            lines.append(
                "  → out-of-scope of this PR's diff "
                "(pre-existing on the base branch) → needs human review"
            )
        elif path and path in changed_paths and a.get("number") is not None:
            alert_number: int = a["number"]
            verdict = verdict_by_number.get(alert_number)
            if verdict is not None and verdict.get("verdict") == "abstain":
                rationale = verdict.get("rationale", "")
                if rationale:
                    truncated = (
                        rationale[:200] + "..." if len(rationale) > 200 else rationale
                    )
                    lines.append(f"  → codeql_fp_triage abstained: {truncated}")
                else:
                    lines.append("  → codeql_fp_triage abstained")
            elif verdicts is not None:
                # We had verdicts but none for this alert, or verdict != abstain.
                lines.append("  → agent could not produce a code fix")
            else:
                # No verdicts available at all — combined fallback.
                lines.append(
                    "  → codeql_fp_triage abstained or agent could not "
                    "produce a code fix"
                )
        else:
            # In-scope but missing number or other edge case.
            lines.append(
                "  → codeql_fp_triage abstained or agent could not produce a code fix"
            )

    return "\n".join(lines)


def _ci_failure_fingerprint(failing_summary: str, repo_id: str) -> str:
    """Compute a stable hex fingerprint for a CI failure.

    The fingerprint is derived from *failing_summary* up to the
    ``**Job logs:**`` marker (exclusive), or the first 2000 characters
    when there is no marker.  The marker-trimmed summary is combined
    with *repo_id* and hashed with SHA-256; the first 16 hex digits
    become the fingerprint.

    This is deterministic (same input → same output) and stable across
    different PRs that hit the same underlying CI failure, while
    remaining specific enough to distinguish different failures.
    """
    marker = "**Job logs:**"
    idx = failing_summary.find(marker)
    if idx != -1:
        core_summary = failing_summary[:idx].rstrip()
    else:
        core_summary = failing_summary[:2000]
    data = f"{repo_id}\n{core_summary}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


class _FailingContext(NamedTuple):
    """Data the counter/agent phases need once CI is confirmed failing."""

    repo_dir: str
    branch: str
    failing_summary: str
    failing: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    changed_paths: set[str] = set()
    alerts_unreadable: bool = False


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
        (
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            alerts_unreadable,
        ) = resolved

        # --- Early guard: CodeQL failing but alerts unreadable (403) ---
        # When CodeQL is among the failing checks and the alerts API
        # returned 403 (permission gap), block immediately with an
        # actionable note — the ci-fix agent must never reach the
        # blind-suppression path when alert details are unavailable.
        if alerts_unreadable and any(
            any(
                token in (chk.get("name") or "").lower() for token in _CODQL_CHECK_NAMES
            )
            for chk in failing
        ):
            codeql_note = _codeql_block_note(
                failing, alerts, changed_paths, alerts_unreadable=True
            )
            return Outcome(State.BLOCKED, codeql_note or "")

        # --- CodeQL FP triage: early trigger before consuming attempts ---
        # If CodeQL is the sole remaining red check, try FP triage
        # immediately.  The triage call has its own guardrails
        # (feature flag, run-once sentinel, eligible alerts, etc.) and
        # returns None when not applicable.
        triage_outcome = self._try_codeql_fp_triage(
            ticket, ctx, failing, alerts, changed_paths
        )
        if triage_outcome is not None:
            return triage_outcome

        # NOTE: there is intentionally no proactive rebase here. The merge
        # poll already routes branch-introduced failures straight to ci_fix
        # (pre-existing main-branch debt is blocked there, conflicts go to
        # REBASING), so by the time we reach this stage the PR is mergeable
        # and the failure is the branch's own. A local rebase + force-push
        # here cannot fix a branch-own lint/type/vulture failure — it only
        # burns a cycle (and used to pre-seed the identical-failure
        # fingerprint, biasing the backstop toward an early BLOCK before the
        # agent ever ran). The agent OWNS the fix: it has bridged git tools
        # and the target branch, so it can rebase itself when (and only when)
        # it determines the failure is base-caused. Genuine "behind main"
        # staleness is still handled by the server-side update-branch backstop
        # in the OUT_OF_SCOPE path below.

        # Identical-failure gate: when the same CI failure fingerprint repeats
        # ci_fix_max_identical_failures times in a row, escalate to BLOCKED.
        identical_outcome = self._check_consecutive_identical_failure(
            ticket, ctx, failing_summary
        )
        if identical_outcome is not None:
            return identical_outcome

        # Agent phase: the ci-fix agent now OWNS the fix→push→verify loop —
        # it fixes, pushes, and calls wait_for_ci to re-check, iterating up to
        # ci_fix_max_iterations before giving up. There is no external
        # FIXING_CI ⇄ IMPLEMENT_COMPLETE retry loop and no per-ticket cycle
        # counter: the iteration budget lives inside the wait_for_ci tool.
        log.info(
            "%s: CI failing — running ci-fix agent (owns fix/verify loop)",
            ticket.id,
        )
        return self._run_agent_and_finalize(
            ticket,
            ctx,
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
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
        failing_summary, alerts, changed_paths, alerts_unreadable = (
            self._build_failure_detail(ticket, ctx, branch, failing)
        )
        # Persist the failure detail for observability.
        self._write_failing_summary_artifact(ctx, ticket, failing_summary, failing)
        return _FailingContext(
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            alerts_unreadable,
        )

    def _build_failure_detail(  # noqa: C901 — enrichment is inherently branchy
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        failing: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], set[str], bool]:
        """Enrich the failing-check list with job logs + code-scanning alerts.

        Returns ``(failing_summary, alerts, changed_paths, alerts_unreadable)``
        so callers can inspect the raw alert data (e.g. for FP triage gating)
        and detect when alerts were unreadable due to a 403 permission gap.
        """
        s = ctx.settings

        # Fetch job logs + code-scanning alerts for richer context (only on
        # failure, not on every PR poll — this stage runs infrequently).
        log_text = ""
        alerts: list[dict[str, Any]] = []
        changed_paths: set[str] = set()
        alerts_unreadable = False
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
        except CodeScanningAlertsUnavailable:
            log.warning(
                "%s: code-scanning alerts unreadable (HTTP 403) — "
                "token lacks 'security-events' permission",
                ticket.id,
            )
            alerts_unreadable = True
            # Still try to fetch changed_paths and job logs — do not lose
            # log enrichment just because alerts are unreadable.
            try:
                forge = get_forge(s, repo_config=ctx.repo_config)
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
                log.warning("%s: failed to fetch job logs", ticket.id)
        except Exception:  # noqa: BLE001 — best-effort enrichment
            log.warning("%s: failed to fetch job logs / alerts", ticket.id)

        return (
            _build_failing_summary(failing, log_text, alerts, changed_paths),
            alerts,
            changed_paths,
            alerts_unreadable,
        )

    def _write_failing_summary_artifact(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        failing: list[dict[str, Any]],
    ) -> None:
        """Persist the failure detail to ``failing_summary.txt``.

        Best-effort: a write failure is logged, not raised.
        If *failing_summary* is empty, falls back to the raw check names
        so the file is never silently empty.
        """
        try:
            content = failing_summary.strip()
            if not content:
                names = [chk.get("name", "?") for chk in failing]
                content = f"(no detail available) failing checks: {', '.join(names)}"
            path = ctx.service.workspace(ticket).artifacts_dir / "failing_summary.txt"
            _write_text(path, content)
        except Exception:
            log.exception("%s: failed to write failing_summary.txt artifact", ticket.id)

    def _check_consecutive_identical_failure(
        self,
        ticket: Ticket,
        ctx: StageContext,
        failing_summary: str,
    ) -> Outcome | None:
        """Return ``Outcome(State.BLOCKED, ...)`` when the same CI failure
        fingerprint has repeated ``ci_fix_max_identical_failures`` times in a
        row without the agent making progress, or ``None`` when the stage
        should proceed to the agent phase.

        The fingerprint is read/written from the artifacts dir.  A separate
        counter file tracks how many times the *current* fingerprint has been
        seen consecutively; it is reset to zero whenever the fingerprint
        changes (or on first run). The counter now reflects only genuine
        agent attempts on the same failure — nothing pre-seeds it before the
        agent runs.

        Short-circuits to ``None`` when ``ci_fix_max_identical_failures == 0``
        (disabled).
        """
        s = ctx.settings

        # Disabled short-circuit.
        if s.ci_fix_max_identical_failures == 0:
            return None

        repo_id = ctx.repo_config.board_id if ctx.repo_config else ""
        current_fp = _ci_failure_fingerprint(failing_summary, repo_id)
        artifacts = ctx.service.workspace(ticket).artifacts_dir
        fp_path = artifacts / _CI_FAILURE_FINGERPRINT
        counter_path = artifacts / _CI_IDENTICAL_FAILURE_COUNT

        try:
            stored_fp = fp_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            stored_fp = ""

        if current_fp == stored_fp and stored_fp:
            # Same failure as last cycle — increment the consecutive counter.
            count = _read_counter(counter_path) + 1
            _write_counter(counter_path, count)
            if count >= s.ci_fix_max_identical_failures:
                return Outcome(
                    State.BLOCKED,
                    f"Same CI failure fingerprint ({current_fp}) repeated "
                    f"{count} consecutive times without progress — "
                    "escalating to BLOCKED. Resume to retry.",
                )
            return None

        # Fingerprint changed (or first run) — reset the counter and store
        # the new fingerprint for the next cycle's comparison.
        _write_counter(counter_path, 0)
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text(current_fp, encoding="utf-8")
        return None

    def _try_codeql_fp_triage(  # noqa: C901 — guardrail chain is inherently branchy
        self,
        ticket: Ticket,
        ctx: StageContext,
        failing: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        changed_paths: set[str],
    ) -> Outcome | None:
        """Try the codeql_fp_triage sub-agent before blocking on CodeQL FPs.

        Returns an ``Outcome(State.IMPLEMENT_COMPLETE)`` when the agent
        dismissed at least one alert (so CI should re-poll green), or
        ``None`` when triage is not applicable / ran but dismissed nothing
        / is disabled — the caller then falls through to BLOCKED.
        """
        s = ctx.settings

        # --- gate: feature flag ---
        if not s.codeql_fp_triage_enabled:
            return None

        # --- gate: only CodeQL is failing ---
        if not _only_codeql_failing(failing):
            return None

        # --- gate: run-once sentinel ---
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        sentinel_path = artifacts_dir / _CODQL_FP_TRIAGE_SENTINEL
        if sentinel_path.exists():
            log.info(
                "%s: codeql_fp_triage already ran for this ticket — skipping",
                ticket.id,
            )
            return None
        # Write sentinel BEFORE running the agent so a crash doesn't retry.
        _write_counter(sentinel_path, 1)

        # --- gate: filter eligible alerts ---
        eligible = _eligible_for_triage(alerts, changed_paths, max_dismissals=5)
        if not eligible:
            log.info(
                "%s: no CodeQL alerts eligible for FP triage "
                "(all are security-severity, out-of-scope, or absent)",
                ticket.id,
            )
            return None

        log.info(
            "%s: attempting codeql_fp_triage on %d eligible alert(s)",
            ticket.id,
            len(eligible),
        )

        # --- run the agent ---
        import json
        from pathlib import Path

        from ..agents.codeql_fp_triage import run_codeql_fp_triage_agent

        repo_dir = _workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return None

        try:
            result = run_codeql_fp_triage_agent(
                settings=s,
                repo_dir=Path(repo_dir),
                alerts_json=json.dumps(eligible),
                ticket_id=ticket.id,
                board_id=ctx.repo_config.board_id if ctx.repo_config else "",
            )
        except Exception:  # noqa: BLE001 — best-effort
            log.warning("%s: codeql_fp_triage agent crashed", ticket.id, exc_info=True)
            return None

        # Persist verdicts for the block-note builder.
        try:
            verdicts_path = artifacts_dir / _CODQL_FP_TRIAGE_VERDICTS
            verdicts_path.parent.mkdir(parents=True, exist_ok=True)
            verdicts_path.write_text(
                json.dumps([v.model_dump() for v in result.verdicts]),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — best-effort
            log.warning(
                "%s: failed to persist codeql_fp_triage verdicts",
                ticket.id,
                exc_info=True,
            )

        # --- dismiss alerts the agent greenlit ---
        dismissals = [v for v in result.verdicts if v.verdict == "dismiss"]
        if not dismissals:
            log.info(
                "%s: codeql_fp_triage abstained on all %d alert(s) — blocking",
                ticket.id,
                len(eligible),
            )
            return None

        forge = get_forge(s, repo_config=ctx.repo_config)
        dismissed_count = 0
        dismissal_notes: list[str] = []
        for v in dismissals:
            ok = forge.dismiss_code_scanning_alert(
                number=v.alert_number,
                reason="false positive",
                comment=v.rationale[:4000],  # GitHub dismiss comment limit
            )
            if ok:
                dismissed_count += 1
                dismissal_notes.append(
                    f"- Alert #{v.alert_number}: {v.rationale[:200]}"
                )
            else:
                log.warning(
                    "%s: failed to dismiss code-scanning alert %d",
                    ticket.id,
                    v.alert_number,
                )

        # --- record audit trail ---
        try:
            note_lines = [
                "## codeql_fp_triage: auto-dismissed CodeQL false positive(s)",
                "",
                f"Dismissed {dismissed_count} alert(s) out of "
                f"{len(eligible)} eligible:",
                "",
            ]
            note_lines.extend(dismissal_notes)
            note_lines.append("")
            note_lines.append(
                "These dismissals persist by fingerprint and will also "
                "clear the alert on `main` after merge.  A human can "
                "re-open any alert via the GitHub security tab."
            )
            ctx.service.add_history_note(ticket.id, "\n".join(note_lines))
        except Exception:  # noqa: BLE001 — best-effort
            log.warning("%s: failed to record codeql_fp_triage note", ticket.id)

        if dismissed_count > 0:
            log.info(
                "%s: codeql_fp_triage dismissed %d alert(s) — "
                "returning to IMPLEMENT_COMPLETE for re-poll",
                ticket.id,
                dismissed_count,
            )
            return Outcome(State.IMPLEMENT_COMPLETE)

        return None

    def _run_agent_and_finalize(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        failing_summary: str,
        failing: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        changed_paths: set[str],
    ) -> Outcome:
        """Reconcile, run the agent (which owns the loop), and route its verdict.

        The agent fixes, pushes, and verifies on real CI via wait_for_ci,
        iterating internally until CI is green (DONE) or its budget is spent
        (FAILED). There is no external retry loop here — DONE → re-poll,
        FAILED/crash → BLOCKED, OUT_OF_SCOPE → dependency fix.
        """
        s = ctx.settings

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

        # Write the per-cycle ci_fix.md artifact and an informative
        # history note (both best-effort) so the ticket history surfaces
        # what the agent saw and what it did.
        self._write_ci_fix_artifact(ctx, ticket, failing_summary, result)
        self._add_ci_fix_history_note(ctx, ticket, failing_summary, result)

        if result is not None and result.status == "DONE":
            return self._finalize_success(ticket, ctx, repo_dir, branch)

        if result is not None and result.status == "OUT_OF_SCOPE":
            return self._handle_out_of_scope(
                ticket, ctx, branch, result, failing_summary
            )

        # FAILED, or None on crash — the agent could not turn CI green within
        # its iteration budget (or hit an unrecoverable error). Block so a
        # human can intervene; resume re-enters from human_mr_approval.
        #
        # Before emitting the generic message, check whether CodeQL code-
        # scanning is the blocker and, when it is, produce a specific note
        # naming every gating alert and explaining why the auto-solver
        # abstained.
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        verdicts: list[dict[str, Any]] | None = None
        try:
            import json as _json

            vp = artifacts_dir / _CODQL_FP_TRIAGE_VERDICTS
            if vp.exists():
                verdicts = _json.loads(vp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001, S110 — best-effort; silent fallback
            pass

        codeql_note = _codeql_block_note(failing, alerts, changed_paths, verdicts)
        if codeql_note is not None:
            return Outcome(State.BLOCKED, codeql_note)

        return Outcome(
            State.BLOCKED,
            "ci fix agent could not turn CI green within its iteration budget "
            "— manual intervention required. "
            "Resume-blocked to retry from human_mr_approval.",
        )

    def _write_ci_fix_artifact(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        result: CiFixResult | None,
    ) -> None:
        """Write the per-cycle ``ci_fix.md`` artifact (single latest, overwrite).

        Includes the detected failure detail and, when the agent produced a
        result, a recap of what it did and its verdict.  Best-effort only.
        """
        try:
            parts: list[str] = []
            parts.append("# CI Fix Cycle\n")
            parts.append("## Detected Failure\n")
            parts.append(failing_summary.strip() or "(no detail available)")
            parts.append("\n")
            if result is not None:
                parts.append("## Agent Recap\n")
                parts.append(f"**Verdict:** {result.status}\n")
                if result.summary:
                    parts.append(result.summary)
            else:
                parts.append("## Agent Recap\n")
                parts.append("The ci-fix agent crashed before producing a result.")
            path = ctx.service.workspace(ticket).artifacts_dir / "ci_fix.md"
            _write_text(path, "\n".join(parts))
        except Exception:
            log.exception("%s: failed to write ci_fix.md artifact", ticket.id)

    def _add_ci_fix_history_note(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        result: CiFixResult | None,
    ) -> None:
        """Record one informative history note per ci-fix cycle.

        Contains the detected failure detail and the agent's recap.
        Best-effort: a failure to write the note is logged, not raised.
        """
        try:
            lines: list[str] = []
            lines.append("**CI Fix Cycle**\n")
            lines.append("### Detected Failure\n")
            lines.append(failing_summary.strip() or "(no detail available)")
            if result is not None:
                lines.append("\n### Agent Result\n")
                lines.append(f"**Verdict:** {result.status}")
                if result.summary:
                    lines.append(result.summary)
            else:
                lines.append("\n### Agent Result\n")
                lines.append("The ci-fix agent crashed before producing a result.")
            ctx.service.add_history_note(ticket.id, "\n".join(lines))
        except Exception:
            log.exception("%s: failed to write ci-fix history note", ticket.id)

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

                # Pass the per-repo remote_url and token so the agent's
                # bridged git tools can drive fetch + push host-side.
                # The token is captured in the closure and NEVER exposed
                # to the sandbox or the agent's prompt.
                remote_url = _resolve_remote_url(s, ctx.repo_config)
                token = github_token(s, repo_config=ctx.repo_config)
                target = target_branch_for(s, ctx.repo_config)

                result = run_ci_fix_agent(
                    settings=s,
                    repo_dir=repo_dir,
                    branch=branch,
                    failing_summary=failing_summary,
                    memory=memory_text,
                    ticket_id=ticket.id,
                    board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                    target=target,
                    remote_url=remote_url,
                    token=token,
                    ci_status_fn=self._make_ci_status_fn(ticket, ctx, branch),
                    ci_log_fetch_fn=self._make_ci_log_fetch_fn(ctx, branch),
                )
                if result.updated_memory:
                    persist_memory(ci_fix_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ci-fix agent crashed: %s", ticket.id, e)
            return None
        return result

    def _make_ci_status_fn(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> "Callable[[], tuple[str, str]]":
        """Build the host-side forge probe the agent's wait_for_ci tool calls.

        Returns a closure that fetches the branch's CI conclusion and returns
        ``(conclusion, failing_summary)`` where conclusion is one of
        ``success`` / ``failure`` / ``pending`` / ``gone``. On a fresh failure
        it builds the enriched failing summary (job logs + code-scanning
        alerts) so the agent gets actionable detail for its next iteration.
        Transient forge errors map to ``pending`` so the agent keeps waiting
        rather than giving up on a blip.
        """
        s = ctx.settings

        def status_fn() -> tuple[str, str]:
            try:
                status = get_forge(s, repo_config=ctx.repo_config).check_status(
                    source_branch=branch
                )
            except Exception:  # noqa: BLE001 — transient; keep waiting
                log.warning(
                    "%s: check_status failed during CI wait — treating as pending",
                    ticket.id,
                )
                return ("pending", "")
            if status is None:
                return ("gone", "")
            conclusion = status.get("conclusion")
            if conclusion == "success":
                return ("success", "")
            if conclusion == "failure":
                failing = status.get("failing", [])
                summary, _alerts, _changed, _unreadable = self._build_failure_detail(
                    ticket, ctx, branch, failing
                )
                return ("failure", summary)
            # pending / None / unknown — not terminal yet.
            return ("pending", "")

        return status_fn

    def _make_ci_log_fetch_fn(
        self, ctx: StageContext, branch: str
    ) -> "Callable[[int, bool], str]":
        """Build the host-side forge probe the agent's ``fetch_ci_logs`` tool calls.

        Returns a closure that calls ``forge.fetch_workflow_job_logs()`` for
        a given run id and *full_log* flag, returning the log text.  Transient
        forge errors raise through to the tool's error handler.
        """
        s = ctx.settings

        def fetch_fn(run_id: int, full_log: bool) -> str:
            forge = get_forge(s, repo_config=ctx.repo_config)
            return forge.fetch_workflow_job_logs(run_id=run_id, full_log=full_log)

        return fetch_fn

    def _partition_open_alerts(
        self, ctx: StageContext, branch: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        failing_summary: str,
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
            # re-poll so the ci-fix agent re-runs against the in-scope-labelled
            # failing_summary. The agent's own wait_for_ci iteration budget
            # bounds an agent that keeps refusing, so the loop stays safe.
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

        fingerprint = _ci_failure_fingerprint(
            failing_summary,
            ctx.repo_config.board_id if ctx.repo_config else "",
        )
        outcome = dependency_fix.spawn_dependency_fix(
            ticket,
            ctx,
            title=title,
            description=description,
            source_kind=SourceKind.CI_FIX_DEPENDENCY,
            block_reason_prefix=block_reason,
            priority=ticket.priority,
            dedup_labels=[f"ci_fp:{fingerprint}"],
        )

        # Reset the per-ticket refresh counter so a later re-entry (after
        # auto-unblock + a fresh pipeline pass) starts clean.
        _write_counter(artifacts_dir / _CI_REFRESH_COUNTER, 0)

        return outcome

    def _finalize_success(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
    ) -> Outcome:
        """On agent DONE: deterministically verify the agent's push landed
        and clobbered no foreign commits, then return to IMPLEMENT_COMPLETE so
        the merge stage re-verifies CI and promotes to HUMAN_MR_APPROVAL.

        The agent already confirmed CI green via wait_for_ci, so this is a
        cheap safety net (foreign-push / lost-push detection), not a retry
        loop. On a clean landing the refresh counter is reset so a later,
        independent staleness can rebase once more.
        """
        s = ctx.settings
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        token = github_token(s, repo_config=ctx.repo_config)
        target = target_branch_for(s, ctx.repo_config)

        # Deterministic post-check: verify the agent's push actually
        # landed and no foreign commits were clobbered.
        check = git_ops.post_push_check(
            Path(repo_dir),
            branch=branch,
            target=target,
            remote_url=remote_url,
            token=token,
        )

        if check is git_ops.PostPushResult.PASS:
            # Genuine forward progress — allow a future staleness to refresh again.
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / _CI_REFRESH_COUNTER, 0
            )
            log.info("%s: ci fix reported DONE, push verified", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if check is git_ops.PostPushResult.NOT_LANDED:
            log.warning(
                "%s: ci-fix post-check failed — remote HEAD != local HEAD; "
                "push did not land",
                ticket.id,
            )
            return Outcome(
                State.BLOCKED,
                "ci fix agent reported DONE but the push did not land "
                "(remote HEAD != local HEAD). The agent may have hit a "
                "lease rejection it could not recover from. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        if check is git_ops.PostPushResult.FOREIGN_DIVERGENCE:
            log.warning(
                "%s: ci-fix post-check failed — remote branch carries "
                "foreign-authored commits; a human may have pushed",
                ticket.id,
            )
            return Outcome(
                State.BLOCKED,
                "ci fix agent reported DONE but the remote branch carries "
                "foreign-authored commits — a human likely pushed to the PR "
                "branch. Manual reconciliation required. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        # UNAVAILABLE — transient fetch failure, re-poll.
        log.warning(
            "%s: ci-fix post-check unavailable (fetch failed) — re-polling",
            ticket.id,
        )
        return Outcome(State.IMPLEMENT_COMPLETE)
