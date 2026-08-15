"""Stateless helpers extracted from ci_fix.py — formatters, hashing, and _FailingContext."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from ..core.models import Ticket
    from .base import StageContext

from ..core.text_utils import head_tail_keep
from ..core.workspace import (
    read_counter as _read_counter,
)
from ..core.workspace import (
    write_counter as _write_counter,
)
from ..forge.base import Forge
from ..vcs import git_ops

__all__ = ["_read_counter", "_write_counter"]

_log = logging.getLogger("robotsix_mill.stages.ci_fix_helpers")

_CI_REFRESH_COUNTER = "ci_fix_refresh_attempts.txt"
_CI_FAILURE_FINGERPRINT = "ci_failure_fingerprint.txt"
_CI_IDENTICAL_FAILURE_COUNT = "ci_identical_failure_count.txt"

# Check-run names that are CodeQL-related (case-insensitive contains).
_CODQL_CHECK_NAMES = frozenset({"codeql", "code-scanning", "code scanning"})


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _workspace_repo_dir(ctx: StageContext, ticket: Ticket) -> str | None:
    """Return the ticket's workspace clone dir, or None if missing."""
    ws = ctx.service.workspace(ticket)
    repo = ws.dir / "repo"
    if not (repo / ".git").exists():
        return None
    return str(repo)


def _clip_text(text: str, max_chars: int) -> str:
    """Clip *text* to *max_chars* characters, preferring a line boundary."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    clipped = text[:cut] if cut != -1 else text[:max_chars]
    omitted = len(text) - len(clipped)
    return f"{clipped}\n[... {omitted} chars omitted]"


def _bounded_multi_run_log_text(
    run_blocks: list[tuple[str, str]],
    max_chars: int,
) -> str:
    """Concatenate per-run ``(header, log_body)`` blocks under *max_chars*.

    The forge already windows each job log on the FIRST failure marker, so
    each run's body leads with its first-error window.  When the combined
    text exceeds *max_chars* we give every run an equal budget and keep a
    head+tail window of that run's body — preserving the first-error signal
    of earlier runs instead of blind tail-keeping the whole concatenation
    (which drops earlier runs entirely).  *max_chars* <= 0 returns the
    concatenation uncapped.
    """
    if not run_blocks:
        return ""
    if max_chars <= 0:
        return "\n".join(header + body for header, body in run_blocks)
    total = sum(len(header) + len(body) for header, body in run_blocks)
    if total <= max_chars:
        return "\n".join(header + body for header, body in run_blocks)

    per_run = max(500, max_chars // max(1, len(run_blocks)))
    parts: list[str] = []
    for header, body in run_blocks:
        if len(body) > per_run:
            body = head_tail_keep(body, per_run, label="job logs")
        parts.append(header + body)
    joined = "\n".join(parts)
    if len(joined) > max_chars:
        joined = head_tail_keep(joined, max_chars, label="job logs")
    return joined


def _format_code_scanning_alerts(
    alerts: list[dict[str, Any]],
    max_alerts: int = 40,
) -> str:
    """Render open code-scanning (CodeQL) alerts as a markdown block. These
    come from the security/code-scanning API, NOT the workflow job logs, so
    without them the agent can't see what a CodeQL check actually flagged.

    *max_alerts* caps the rendered lines; ``0`` disables the cap.
    """
    if not alerts:
        return ""
    lines = ["**Code-scanning alerts (CodeQL — these are NOT in the job logs):**"]
    shown = alerts if max_alerts <= 0 else alerts[:max_alerts]
    for a in shown:
        loc = a.get("path", "")
        if a.get("line"):
            loc += f":{a['line']}"
        sev = a.get("severity") or "?"
        lines.append(f"- [{sev}] `{a.get('rule', '')}` {loc}: {a.get('message', '')}")
    if max_alerts > 0 and len(alerts) > max_alerts:
        lines.append(f"- ... {len(alerts) - max_alerts} more alert(s) omitted")
    return "\n".join(lines)


def _partition_alerts_by_diff(
    alerts: list[dict[str, Any]], changed_paths: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split open code-scanning alerts into (in_scope, out_of_scope).

    An alert is IN SCOPE when its repo-relative ``path`` is among the PR's
    changed files; otherwise it is an out-of-scope candidate. Alerts with an
    empty/missing ``path`` are treated as out-of-scope (cannot prove they are
    in the diff).
    """
    in_scope: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
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
    except Exception:
        return set()


def _alert_loc(a: dict[str, Any]) -> str:
    """Return the ``path`` or ``path:line`` location string for an alert."""
    loc: str = a.get("path", "")
    if a.get("line"):
        loc += f":{a['line']}"
    return loc


def _format_alert_refs(alerts: list[dict[str, Any]]) -> str:
    """Render alerts as a compact ``rule @ path:line`` semicolon list."""
    return "; ".join(f"{a.get('rule', '')} @ {_alert_loc(a)}" for a in alerts)


def _format_labelled_alerts(
    in_scope: list[dict[str, Any]],
    out_of_scope: list[dict[str, Any]],
    max_alerts: int = 40,
) -> str:
    """Render code-scanning alerts split into in-diff / untouched sections.

    Each alert is explicitly marked so the agent (and any downstream fixer)
    sees which alerts it MUST fix in-scope versus which may be out of scope.
    *max_alerts* caps each section independently; ``0`` disables the cap.
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
        shown_in = in_scope if max_alerts <= 0 else in_scope[:max_alerts]
        for a in shown_in:
            sev = a.get("severity") or "?"
            lines.append(
                f"- [{sev}] `{a.get('rule', '')}` {_alert_loc(a)}: "
                f"{a.get('message', '')} — IN THIS PR'S DIFF — must fix"
            )
        if max_alerts > 0 and len(in_scope) > max_alerts:
            lines.append(
                f"- ... {len(in_scope) - max_alerts} more in-scope alert(s) omitted"
            )
    if out_of_scope:
        lines.append("Alert(s) in untouched files (may be out of scope):")
        shown_out = out_of_scope if max_alerts <= 0 else out_of_scope[:max_alerts]
        for a in shown_out:
            sev = a.get("severity") or "?"
            lines.append(
                f"- [{sev}] `{a.get('rule', '')}` {_alert_loc(a)}: "
                f"{a.get('message', '')} — untouched file (out-of-scope candidate)"
            )
        if max_alerts > 0 and len(out_of_scope) > max_alerts:
            lines.append(
                f"- ... {len(out_of_scope) - max_alerts} more out-of-scope alert(s) omitted"
            )
    return "\n".join(lines)


def _format_alert_summary_block(
    alerts: list[dict[str, Any]] | None,
    *,
    codeql_failing: bool = False,
    max_alerts: int = 40,
) -> str:
    """Render a compact CodeQL alert summary for top-of-prompt injection.

    Returns a short bullet list of ``rule @ path:line`` entries so the
    agent sees exactly which alerts to fix without having to read through
    the full failing summary first.

    When *codeql_failing* is True and *alerts* is empty/None, emits an
    explicit could-not-retrieve notice so the ci_fix worker escalates
    rather than blocking on an un-actionable empty summary.

    *max_alerts* caps the rendered lines; ``0`` disables the cap.
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
    shown = alerts if max_alerts <= 0 else alerts[:max_alerts]
    for a in shown:
        lines.append(f"- `{a.get('rule', '?')}` @ {_alert_loc(a)}")
    if max_alerts > 0 and len(alerts) > max_alerts:
        lines.append(f"- ... {len(alerts) - max_alerts} more alert(s) omitted")
    lines.append("")
    return "\n".join(lines)


# Check-run conclusions that are terminal failures (mirrors github_ci._FAILING_CONCLUSIONS).
_FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure"}
)


def _check_indicator(conclusion: str | None) -> str:
    """Return a visual pass/fail indicator for a check run's conclusion.

    Trace-review dc631bde5ede9e369156b954ab6e00cc identified that the
    old ``Failing check #N`` header was ambiguous — the agent couldn't
    tell which checks passed and which failed without fetching logs.
    """
    if conclusion in _FAILING_CONCLUSIONS:
        return "❌ FAILED:"
    return "✅ PASSED:"


def _format_annotations(
    anns: list[dict[str, Any]],
    max_annotations: int,
) -> list[str]:
    """Render ``**Annotations:**`` lines, capped to *max_annotations* (0 = uncapped)."""
    if not anns:
        return []
    shown = anns if max_annotations <= 0 else anns[:max_annotations]
    lines: list[str] = ["\n**Annotations:**"]
    for a in shown:
        loc = f"{a['path']}"
        if a.get("start_line"):
            loc += f":{a['start_line']}"
        lines.append(f"- [{a['level']}] {loc}: {a['message']}")
    if max_annotations > 0 and len(anns) > max_annotations:
        lines.append(f"- ... {len(anns) - max_annotations} more annotation(s) omitted")
    return lines


def _collect_annotation_lines(failing: list[dict[str, Any]]) -> list[str]:
    """Collect ``- [level] path:line: message`` lines for all failing checks."""
    lines: list[str] = []
    for chk in failing:
        for a in chk.get("annotations") or []:
            loc = f"{a['path']}"
            if a.get("start_line"):
                loc += f":{a['start_line']}"
            lines.append(f"- [{a.get('level', '?')}] {loc}: {a.get('message', '')}")
    return lines


def _collect_alert_lines(
    alerts: list[dict[str, Any]] | None,
    changed_paths: set[str] | None,
) -> list[str]:
    """Collect compact code-scanning alert lines for the compact summary."""
    if not alerts:
        return []
    in_scope, out_of_scope = (
        _partition_alerts_by_diff(alerts, changed_paths)
        if changed_paths is not None
        else (alerts, [])
    )
    lines: list[str] = []
    for a in in_scope:
        lines.append(
            f"- [{a.get('severity', '?')}] `{a.get('rule', '')}` "
            f"{_alert_loc(a)}: {a.get('message', '')}"
        )
    for a in out_of_scope:
        lines.append(
            f"- [{a.get('severity', '?')}] `{a.get('rule', '')}` "
            f"{_alert_loc(a)}: {a.get('message', '')} (out-of-scope candidate)"
        )
    return lines


def _render_bounded_signatures(
    title: str,
    lines: list[str],
    limit: int,
    noun: str,
) -> list[str]:
    """Render a bounded ``<title>`` + bullet-list section for the compact summary."""
    if not lines:
        return []
    out: list[str] = [title]
    out.extend(lines[:limit])
    if len(lines) > limit:
        out.append(f"- ... {len(lines) - limit} more {noun} omitted")
    out.append("")
    return out


def _build_failing_summary(
    failing: list[dict[str, Any]],
    log_text: str = "",
    alerts: list[dict[str, Any]] | None = None,
    changed_paths: set[str] | None = None,
    max_annotations: int = 40,
    max_alerts: int = 40,
) -> str:
    """Build a markdown summary from the failing check list.

    When *log_text* is provided (non-empty), it is included under a
    **Job logs:** heading. When *alerts* (open code-scanning/CodeQL alerts)
    are provided they are listed too — they don't appear in the job logs.
    When *changed_paths* is provided, the alerts are partitioned against the
    PR's own diff and rendered with explicit in-scope / out-of-scope labels.

    *max_annotations* / *max_alerts* bound the non-log portions (check
    annotations and code-scanning alert lists) so a CodeQL-heavy failure
    can't blow up the prompt; ``0`` disables the respective cap.

    A compact alert summary is injected at the **top** of the prompt so the
    agent can quickly identify what to fix without speculative reasoning.
    """
    parts = []
    # Inject compact alert summary at the very top for fast reference.
    codeql_failing = _only_codeql_failing(failing)
    parts.append(
        _format_alert_summary_block(
            alerts, codeql_failing=codeql_failing, max_alerts=max_alerts
        )
    )
    for chk in failing:
        parts.append(
            f"## {_check_indicator(chk.get('conclusion', 'failure'))} {chk['name']}"
        )
        if chk.get("summary"):
            parts.append(f"\n**Summary:**\n{chk['summary']}")
        if chk.get("text"):
            parts.append(f"\n**Details:**\n{chk['text']}")
        anns = chk.get("annotations") or []
        parts.extend(_format_annotations(anns, max_annotations))
        parts.append("")
    if changed_paths is None:
        alert_block = _format_code_scanning_alerts(alerts or [], max_alerts=max_alerts)
    else:
        in_scope, out_of_scope = _partition_alerts_by_diff(alerts or [], changed_paths)
        alert_block = _format_labelled_alerts(
            in_scope, out_of_scope, max_alerts=max_alerts
        )
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


def _build_compact_failing_summary(
    failing: list[dict[str, Any]],
    log_text: str = "",
    alerts: list[dict[str, Any]] | None = None,
    changed_paths: set[str] | None = None,
    max_chars: int = 2000,
) -> str:
    """Build a bounded, compact failure digest for late wait_for_ci iterations.

    Unlike :func:`_build_failing_summary` — which inlines the full (capped)
    log window plus every annotation and alert — this keeps only:

      * the pass/fail check headers (so the agent can still compare scope),
      * a short summary per failing check,
      * a bounded "key error signatures" section (the first annotations and
        alerts — the failure-relevant path:line hunks),
      * a short first-error window of the job log.

    The result is then clipped to *max_chars* so each late-iteration tool
    result contributes O(1) context instead of re-sending a full summary on
    every turn.  *max_chars* <= 0 falls back to the full summary (compacting
    disabled).
    """
    if max_chars <= 0:
        return _build_failing_summary(failing, log_text, alerts, changed_paths)

    lines: list[str] = [
        (
            "**CI still failing — compact summary "
            "(full logs available via fetch_ci_logs)**"
        ),
        "",
    ]
    for chk in failing:
        indicator = _check_indicator(chk.get("conclusion", "failure"))
        name = chk.get("name", "?")
        lines.append(f"## {indicator} {name}")
        if chk.get("summary"):
            lines.append(f"Summary: {_clip_text(str(chk['summary']), 160)}")
    lines.append("")

    # Annotations are the failure-relevant hunks (path:line:message).
    ann_lines = _collect_annotation_lines(failing)
    lines.extend(
        _render_bounded_signatures(
            "**Key error signatures (annotations):**",
            ann_lines,
            10,
            "annotation(s)",
        )
    )

    alert_lines = _collect_alert_lines(alerts, changed_paths)
    lines.extend(
        _render_bounded_signatures(
            "**Key error signatures (code-scanning alerts):**",
            alert_lines,
            10,
            "alert(s)",
        )
    )

    if log_text:
        # The forge already windows each job log on the first failure
        # marker, so the HEAD of the concatenated text carries the first
        # error(s) — keep that and drop the bulk of the window.
        lines.append("**First error window:**")
        lines.append("```")
        lines.append(_clip_text(log_text, 600))
        lines.append("```")
        lines.append("")

    body = "\n".join(lines)
    if len(body) > max_chars:
        body = head_tail_keep(body, max_chars, label="failure summary")
    return body


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


def _ci_failure_fingerprint(
    failing_summary: str,
    repo_id: str,
    head_sha: str = "",
) -> str:
    """Compute a stable hex fingerprint for a CI failure.

    The fingerprint is derived from *failing_summary* up to the
    ``**Job logs:**`` marker (exclusive), or the first 2000 characters
    when there is no marker.  The marker-trimmed summary is combined
    with *repo_id* and *head_sha* (the branch's current HEAD commit)
    and hashed with SHA-256; the first 16 hex digits become the
    fingerprint.

    Including *head_sha* ensures that a rebased branch (which triggers
    a fresh CI run) produces a different fingerprint even when the
    failure content is identical — preventing the consecutive-identical
    backstop from re-blocking a ticket whose branch has been refreshed
    against current main.
    """
    marker = "**Job logs:**"
    idx = failing_summary.find(marker)
    if idx != -1:
        core_summary = failing_summary[:idx].rstrip()
    else:
        core_summary = failing_summary[:2000]
    data = f"{repo_id}\n{head_sha}\n{core_summary}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _normalize_ci_failure_reason(
    failing: list[dict[str, Any]], failing_summary: str = ""
) -> str:
    """Compute a stable, deterministic normalized-reason key for a CI failure.

    The key strips transient detail — job-log output, file paths, line
    numbers, timestamps — so that genuinely recurring failure modes
    (e.g. "ruff check on every ticket") cluster under the same key
    across different tickets and commits.

    The algorithm:
    1. Joins the sorted failing check names into a namespaces prefix.
    2. Takes the summary text up to (but excluding) the ``**Job logs:**``
       marker — the structured part.
    3. Strips annotation lines (``path:line: message``) and timestamps.
    4. Returns the first 16 hex digits of the SHA-256 hash of the result.
    """
    import re

    names = sorted(chk.get("name", "unknown") for chk in failing)
    names_key = "|".join(names)

    marker = "**Job logs:**"
    idx = failing_summary.find(marker)
    core = failing_summary[:idx].rstrip() if idx != -1 else failing_summary[:2000]

    # Strip annotation-level file-path and line-number detail — those are
    # inherently per-ticket and prevent clustering.
    core = re.sub(r"\n\s*- \[.*?\] .*?:\d+: .*", "", core)
    core = re.sub(r"\[.*?\] .*?:\d+: .*", "", core)
    # Strip ISO-8601 timestamps and run IDs (e.g. "run 1234567890").
    core = re.sub(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
        "",
        core,
    )
    core = re.sub(r"run \d{8,}", "", core)
    # Collapse whitespace for stability.
    core = re.sub(r"\s+", " ", core).strip()

    combined = f"{names_key}\n{core}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def _detect_merge_conflict(
    ticket_id: str,
    repo_dir: str,
    target: str,
    pr: dict[str, Any] | None,
) -> str | None:
    """Check whether a PR branch has merge conflicts with its target branch.

    Uses the forge's ``mergeable_state`` field (``"dirty"`` = conflicts).
    When *pr* is ``None`` or *mergeable_state* is absent / not ``"dirty"``,
    returns ``None`` (no conflict detected — proceed normally).

    On detection, attempts a local ``git merge`` to identify the specific
    conflicting files so the block note can list them.  The merge attempt
    is best-effort — if it fails the block note still contains clear
    instructions to manually rebase.

    Returns:
        A block-reason string ready for ``Outcome(State.BLOCKED, ...)``,
        or ``None`` when no merge conflict is detected.
    """
    if pr is None:
        return None

    mergeable_state = pr.get("mergeable_state")
    if mergeable_state != "dirty":
        return None

    # Attempt to identify the specific conflicting files.
    conflicting = _conflicting_files_via_merge(Path(repo_dir), f"origin/{target}")

    if conflicting:
        files_list = "\n".join(f"  - `{f}`" for f in conflicting)
        return (
            f"Merge conflict detected — the PR branch conflicts with "
            f"`{target}` in the following file(s):\n\n{files_list}\n\n"
            f"CI cannot be fixed until the branch is rebased, so the ticket "
            f"is handed to the rebase agent. No operator action is needed "
            f"unless the rebase agent itself reports failure."
        )
    else:
        return (
            f"Merge conflict detected — the PR branch has conflicts "
            f"with `{target}` but the conflicting files could not be "
            f"determined. CI cannot be fixed until the branch is rebased, "
            f"so the ticket is handed to the rebase agent. No operator "
            f"action is needed unless the rebase agent itself reports failure."
        )


def _conflicting_files_via_merge(repo: Path, merge_target: str) -> list[str]:
    """Attempt a ``git merge`` and return unresolvable file paths.

    Runs ``git merge --no-commit --no-ff <merge_target>``, then collects
    unmerged paths via :func:`git_ops.conflicted_files`.  Aborts the merge
    and resets the working tree afterward regardless of outcome.

    Best-effort: returns ``[]`` on any failure (clean tree, merge succeeded
    unexpectedly, network error, etc.) so the caller can still produce a
    useful block note without the file list.
    """
    try:
        cmd = [
            "git",
            "-C",
            str(repo),
            "merge",
            "--no-commit",
            "--no-ff",
            merge_target,
        ]
        subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Collect unmerged paths while the tree is still in conflict state.
        conflicted = git_ops.conflicted_files(repo)
        # Always clean up — abort the merge and reset the working tree.
        with contextlib.suppress(Exception):
            abort_cmd = ["git", "-C", str(repo), "merge", "--abort"]
            subprocess.run(
                abort_cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        with contextlib.suppress(Exception):
            reset_cmd = ["git", "-C", str(repo), "reset", "--hard", "HEAD"]
            subprocess.run(
                reset_cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        return conflicted
    except Exception:
        return []


class _FailingContext(NamedTuple):
    """Data the counter/agent phases need once CI is confirmed failing."""

    repo_dir: str
    branch: str
    failing_summary: str
    failing: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    changed_paths: set[str] = set()
    alerts_unreadable: bool = False
    head_sha: str = ""
    failing_run_ids: list[int] = []
    failing_run_urls: list[str] = []


def _check_upstream_ci_breakage(
    ticket_id: str,
    settings: Any,
    repo_config: Any,
    repo_dir: str,
    failing: list[dict[str, Any]],
) -> str | None:
    """Check whether the PR's CI failures also exist on the target branch.

    Compares failing check names between the PR branch and the target
    branch's latest commit.  When the SAME check names are failing on
    both, the failure is upstream (pre-existing on the target branch) and
    the PR's changes are not the cause — the ci-fix agent should not burn
    cycles trying to fix it.

    Returns a block-reason string when upstream breakage is detected, or
    ``None`` when the target branch is green, has no CI configured, or
    the target SHA cannot be resolved (in which case we fall through to
    the normal ci-fix path).
    """
    from ..config.repos import target_branch_for
    from ..forge import get_forge
    from ..vcs import git_ops

    # 1. Get the target branch name.
    target = target_branch_for(settings, repo_config)

    # 2. Resolve the target branch's HEAD from the local clone.
    try:
        target_sha = git_ops.remote_branch_sha(Path(repo_dir), target)
    except Exception:
        _log.warning(
            "%s: could not resolve target branch SHA for '%s' — "
            "skipping upstream CI breakage check",
            ticket_id,
            target,
        )
        return None
    if target_sha is None:
        _log.info(
            "%s: target branch '%s' has no remote ref — skipping upstream check",
            ticket_id,
            target,
        )
        return None

    # 3. Check CI status on the target branch's HEAD.
    try:
        forge = get_forge(settings, repo_config=repo_config)
        target_ci = forge.commit_ci_conclusion(sha=target_sha)
    except Exception:
        _log.warning(
            "%s: commit_ci_conclusion failed for target branch '%s' "
            "(%s) — skipping upstream check",
            ticket_id,
            target,
            target_sha,
        )
        return None

    if target_ci is None:
        _log.info(
            "%s: no CI status available for target branch '%s' (%s) — "
            "skipping upstream check",
            ticket_id,
            target,
            target_sha,
        )
        return None

    target_conclusion = target_ci.get("conclusion")
    if target_conclusion != "failure":
        # Target branch CI is green/pending/unknown — the PR's failures
        # are not upstream.
        return None

    # 4. Compare failing check names.
    target_failing = target_ci.get("failing", [])
    target_failing_names = {
        chk.get("name", "") for chk in target_failing if chk.get("name")
    }
    pr_failing_names = {chk.get("name", "") for chk in failing if chk.get("name")}

    common = target_failing_names & pr_failing_names
    if not common:
        return None

    # 5. Same checks failing on both — upstream breakage.
    common_list = sorted(common)
    check_names = ", ".join(common_list)
    return (
        f"Upstream CI breakage detected: the following check(s) are failing "
        f"on both this PR **and** the target branch `{target}` "
        f"({target_sha[:8]}): {check_names}. "
        f"The target branch CI is broken — this PR's changes are not the cause. "
        f"Fix the target branch CI first, then resume this ticket."
    )
