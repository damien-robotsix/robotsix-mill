"""Deterministic stale-CI gate for ``CI failure: <workflow> on <branch>`` tickets.

The ``ci`` ticket source keeps filing failure tickets whose problem is
already fixed on main; they used to travel refine → approval → implement
only to close as "no change needed" (cost-monitor, 5/5 in a month).  This
gate runs the forge checks *before* a ticket can reach ``ready`` — at filing
time (``poll_loops_ci``), at refine entry and on human approval:

* **green-since** — the workflow has completed green on the target branch
  at or after the failing commit (same rule as ci-auto-close) → close;
* **stale-run** — the failing run is older than ``ci_stale_failure_days``
  and the workflow file changed on the target branch since → close;
* **upstream-only** — every failing job/step lives inside a reusable
  workflow / composite action from ``robotsix-github-workflows`` → block,
  ci-auto-close closes it once the workflow goes green.

No LLM; any forge error degrades to "no verdict" (the ticket proceeds).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import yaml

from ..agents.runners.ci_auto_close_runner import (
    _green_since_failure,
    _resolve_anchors,
    _run_url_id,
)
from ..core.constants import STALE_CI_FAILURE_PREFIX
from ..core.models import SourceKind

log = logging.getLogger("robotsix_mill.ci_stale_gate")

_TITLE_RE = re.compile(r"^\s*CI failure: .+ on \S+\s*$", re.IGNORECASE)
_BODY_PATH_RE = re.compile(r"\*\*Path:\*\*\s*(\S+)")

# Shared workflow repo whose reusable workflows / composite actions a caller
# cannot fix from its own tree.
UPSTREAM_WORKFLOWS_REPO = "robotsix-github-workflows"

_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
# Failing steps that run the caller's OWN code (tests, lint, type-check)
# inside a shared workflow are fixable here — never treat them as
# upstream-only.
_CALLER_CODE_STEP_RE = re.compile(
    r"\b(test|tests|pytest|lint|ruff|mypy|pyright|type[- ]?check|format|"
    r"vulture|pre-commit|coverage)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StaleCiVerdict:
    """Outcome of :func:`evaluate_stale_ci`: ``close`` (DONE) or ``block``."""

    action: Literal["close", "block"]
    note: str


def is_ci_failure_ticket(ticket: Any) -> bool:
    """True for a ``source=ci`` ticket or one titled ``CI failure: X on Y``."""
    return ticket.source == SourceKind.CI or bool(_TITLE_RE.match(ticket.title or ""))


def _failing_run(
    runs: list[dict[str, Any]], wf: str, f_sha: str, run_id: int | None
) -> dict[str, Any] | None:
    for r in runs:
        if run_id is not None and r.get("id") == run_id:
            return r
    for r in runs:
        if (r.get("name") or "") == wf and (r.get("head_sha") or "") == f_sha:
            return r
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _find_job(jobs: dict[str, Any], label: str) -> dict[str, Any] | None:
    """Return the workflow job whose key or ``name`` produced run-job *label*
    (matrix jobs render as ``<name> (<axis>)``).
    """
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for cand in {str(key), str(job.get("name") or key)}:
            if label == cand or label.startswith(f"{cand} ("):
                return job
    return None


def _upstream_ref(run_job: dict[str, Any], wf_jobs: dict[str, Any]) -> str | None:
    """Return the upstream ``uses:`` ref a failing *run_job* lives in, or None.

    ``None`` whenever the failure may be fixable from this repo: the job is
    not from ``robotsix-github-workflows``, or no failing step is known, or a
    failing step runs the caller's own code (tests / lint / type-check).
    """
    name = str(run_job.get("name") or "")
    failed_steps = [
        str(st.get("name") or "")
        for st in run_job.get("steps") or []
        if (st.get("conclusion") or "") in _FAILED_CONCLUSIONS
    ]
    if not failed_steps or any(_CALLER_CODE_STEP_RE.search(s) for s in failed_steps):
        return None
    if " / " in name:
        # ``<caller> / <reusable job>``: a job of a called reusable workflow.
        job = _find_job(wf_jobs, name.split(" / ", 1)[0])
        uses = str((job or {}).get("uses") or "")
        return uses if UPSTREAM_WORKFLOWS_REPO in uses else None
    job = _find_job(wf_jobs, name)
    if job is None:
        return None
    refs: list[str] = []
    for step_name in failed_steps:
        ref = None
        for st in job.get("steps") or []:
            uses = str((st or {}).get("uses") or "")
            if UPSTREAM_WORKFLOWS_REPO in uses and step_name in (
                str(st.get("name") or ""),
                f"Run {uses}",
            ):
                ref = uses
                break
        if ref is None:
            return None
        refs.append(ref)
    return refs[0] if refs else None


def _upstream_only(forge: Any, run_id: int | None, path: str | None) -> str | None:
    """Return the upstream ref when EVERY failing job of *run_id* is internal
    to a ``robotsix-github-workflows`` reusable workflow / composite action.
    """
    if run_id is None or not path:
        return None
    try:
        jobs = forge.list_workflow_run_jobs(run_id=run_id)
    except Exception:
        return None
    if not isinstance(jobs, list):
        return None
    failing = [j for j in jobs if (j.get("conclusion") or "") in _FAILED_CONCLUSIONS]
    if not failing:
        return None
    text = forge.get_workflow_file(path=path)
    if not isinstance(text, str) or not text:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    wf_jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(wf_jobs, dict):
        return None
    refs = [_upstream_ref(j, wf_jobs) for j in failing]
    if not refs or any(r is None for r in refs):
        return None
    return refs[0]


def evaluate_stale_ci(
    forge: Any,
    ticket: Any,
    body: str,
    *,
    stale_days: int,
    now: datetime | None = None,
) -> StaleCiVerdict | None:
    """Run the three stale-CI checks for *ticket*; ``None`` = proceed normally."""
    if not is_ci_failure_ticket(ticket):
        return None
    wf, branch, f_sha, created, _ = _resolve_anchors(forge, ticket, body)
    if not wf or not branch or not f_sha:
        return None
    try:
        runs = forge.list_workflow_runs(branch=branch)
    except Exception:
        log.warning("ci_stale_gate: list_workflow_runs failed for %s", ticket.id)
        runs = []
    if not isinstance(runs, list):
        runs = []

    # 1. Green-since check.
    green, _ = _green_since_failure(runs, wf, f_sha, failing_created_at=created)
    if green:
        url = green[0].get("html_url") or green[0].get("id")
        return StaleCiVerdict(
            "close",
            f"{STALE_CI_FAILURE_PREFIX} workflow '{wf}' is green on {branch} at "
            f"or after the failing commit {f_sha} (green run: {url}). "
            "No change needed.",
        )

    run_id = _run_url_id(body)
    failing = _failing_run(runs, wf, f_sha, run_id)
    if run_id is None and failing is not None:
        run_id = failing.get("id")
    m = _BODY_PATH_RE.search(body or "")
    path = m.group(1) if m else (failing or {}).get("path")
    created = created or (failing or {}).get("created_at")

    # 2. Stale-run check.
    created_ts = _parse_ts(created)
    now = now or datetime.now(UTC)
    if created_ts and path and now - created_ts > timedelta(days=stale_days):
        commits = forge.list_path_commits_since(
            path=path, branch=branch, since=created_ts.isoformat()
        )
        if isinstance(commits, list) and commits:
            return StaleCiVerdict(
                "close",
                f"{STALE_CI_FAILURE_PREFIX} workflow '{wf}' failed at {f_sha} "
                f"more than {stale_days} days ago and {path} changed on "
                f"{branch} since (commit {commits[0]}). No change needed.",
            )

    # 3. Upstream-only check.
    ref = _upstream_only(forge, run_id, path)
    if ref:
        return StaleCiVerdict(
            "block",
            f"Not fixable from this repo: failure is internal to {ref}. "
            f"ci-auto-close will close this once '{wf}' goes green on {branch}.",
        )
    return None


def check_ticket(
    settings: Any, repo_config: Any, ticket: Any, body: str
) -> StaleCiVerdict | None:
    """Resolve the forge for *repo_config* and run :func:`evaluate_stale_ci`.

    Never raises — any error returns ``None`` (the ticket proceeds).
    """
    if not is_ci_failure_ticket(ticket):
        return None
    try:
        from ..scm.forge import get_forge

        forge = get_forge(settings, repo_config=repo_config)
        return evaluate_stale_ci(
            forge, ticket, body, stale_days=settings.ci_stale_failure_days
        )
    except Exception:
        log.warning("ci_stale_gate: check failed for %s", ticket.id, exc_info=True)
        return None
