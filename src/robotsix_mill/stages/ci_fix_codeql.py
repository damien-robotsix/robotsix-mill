"""CodeQL false-positive triage subsystem extracted from ci_fix.py."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from .base import Outcome, StageContext
from .ci_fix_helpers import (
    _only_codeql_failing,
    _partition_alerts_by_diff,
    _pr_changed_paths,
    _workspace_repo_dir,
    _write_counter,
)

log = logging.getLogger("robotsix_mill.stages.ci_fix_codeql")

_CODQL_FP_TRIAGE_SENTINEL = "codeql_fp_triage_ran.txt"
_CODQL_FP_TRIAGE_VERDICTS = "codeql_fp_triage_verdicts.json"

# Maximum number of alerts the codeql_fp_triage agent may dismiss in a
# single pass.  Caps the blast radius of a misjudged dismissal.
_CODQL_FP_TRIAGE_MAX_DISMISSALS = 5

# Check-run names that are CodeQL-related (case-insensitive contains).
_CODQL_CHECK_NAMES = frozenset({"codeql", "code-scanning", "code scanning"})


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


def _try_codeql_fp_triage(  # noqa: C901 — guardrail chain is inherently branchy
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
            board_id=ctx.repo_config.repo_id if ctx.repo_config else "",
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
            dismissal_notes.append(f"- Alert #{v.alert_number}: {v.rationale[:200]}")
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
            f"Dismissed {dismissed_count} alert(s) out of {len(eligible)} eligible:",
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


def _partition_open_alerts(
    ctx: StageContext, branch: str
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
