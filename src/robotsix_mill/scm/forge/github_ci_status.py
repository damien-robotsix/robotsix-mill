"""GitHub CI/checks status-derivation helpers.

Split from ``github_ci.py`` (module-size reduction).  Module-level
check-run/status/conclusion/failure-summary derivation helpers used only
from ``github_ci.py``; the ``GitHubForgeCIMixin`` facade lives there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

# App permission each endpoint needs. On a PRIVATE repo an installation
# without ``checks``/``statuses`` gets a 403 from these endpoints (public
# repos are readable regardless), which hides the billing-block annotation.
_ENDPOINT_PERMISSION = {
    "check-runs": "checks: read",
    "annotations": "checks: read",
    "status": "statuses: read",
}

# (owner/repo, permission) pairs already warned about in this process.
_warned_unreadable: set[tuple[str, str]] = set()


def _note_unreadable(
    owner: str, repo: str, endpoint: str, seen: set[str] | None = None
) -> None:
    """Record a 403 on *endpoint*; WARN once per (repo, missing permission)."""
    if seen is not None:
        seen.add(endpoint)
    permission = _ENDPOINT_PERMISSION[endpoint]
    key = (f"{owner}/{repo}", permission)
    if key in _warned_unreadable:
        return
    _warned_unreadable.add(key)
    log.warning(
        "GitHub App installation lacks '%s' on %s/%s: GET %s returned 403, so "
        "CI check detail is unreadable (add the permission to the App and "
        "accept it on the installation)",
        permission,
        owner,
        repo,
        endpoint,
    )


def _trigger_event_names(on: Any) -> list[str] | None:
    """Normalise a workflow ``on:`` value to a list of event names.

    GitHub Actions ``on:`` may be a string (``on: push``), a list
    (``on: [push, schedule]``) or a mapping
    (``on: {schedule: [...], workflow_dispatch: {}}``).  Returns the event
    names, or ``None`` when *on* is absent/unrecognised.
    """
    if on is None:
        return None
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(e) for e in on]
    if isinstance(on, dict):
        return [str(k) for k in on]
    return None


# Regex for stripping ANSI escape sequences (CSI / SGR).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]{0,30}[a-zA-Z]")

# Maximum number of failed jobs whose logs are fetched per run.
_MAX_FAILED_JOBS = 10

# Earliest-failure markers in a GitHub Actions job log. In an
# ``if: always()`` cascade the step that REALLY failed errors FIRST; later
# steps (gated on always()) re-error with misleading input near the tail. So
# a plain tail-cap of the job log shows only the masking error. We instead
# anchor the captured window on the EARLIEST of these markers.
_LOG_FAILURE_RE = re.compile(
    r"(?:##\[error\]|^[^\n]*?\bFATAL\b|\bError:|exit code [1-9]|"
    r"Process completed with exit code [1-9])",
    re.MULTILINE,
)
# When anchoring, keep a little of the log AFTER the first error and spend the
# rest of the budget on the lead-up (where the real error message lives).
_LOG_FAILURE_TAIL_CONTEXT = 4096

# Check-run conclusions that are genuine, terminal failures.
_FAILING_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
    }
)

# Workflow-RUN conclusions (Actions API) that terminally fail the merge gate.
# ``startup_failure`` is what GitHub records for a run it could not start —
# the signature of a workflow that failed to PARSE (invalid ``uses:``,
# malformed YAML). Such a run registers NO check-runs and posts NO commit
# status, so it is invisible to the check-runs/statuses aggregation and must
# be caught by cross-checking the Actions API (see _merge_workflow_run_failures).
_WORKFLOW_FAILING_CONCLUSIONS = frozenset({"failure", "startup_failure"})

# Inconclusive conclusions: the check produced NO verdict because a newer
# run superseded it (GitHub Actions ``concurrency: cancel-in-progress``
# marks the old run ``cancelled``; ``stale`` is the equivalent for status
# checks). Treating these as failures turned routine concurrency churn
# into false CI failures — which spawned ci_fix tickets whose pushes
# cancelled yet more runs, a self-sustaining loop. Classify them as
# PENDING instead so the merge gate waits for a real verdict; once the
# false failures stop, the last (uncancelled) run completes and resolves.
_INCONCLUSIVE_CONCLUSIONS = frozenset({"cancelled", "stale"})

# Statuses that mean the check is still in-flight.
_PENDING_STATUSES = frozenset(
    {
        "in_progress",
        "queued",
        "waiting",
        "requested",
        "pending",
    }
)


def _statuses_to_check_runs(statuses_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert combined statuses response into check-run–shaped dicts."""
    statuses = statuses_data.get("statuses", [])
    if not statuses:
        return []
    # Collapse per-context into a single item (the combined-status endpoint
    # returns statuses newest-first, so the first entry per context is the
    # latest update).
    by_context: dict[str, Any] = {}
    for st in statuses:
        ctx = st.get("context", "")
        by_context.setdefault(ctx, []).append(st)
    runs: list[dict[str, Any]] = []
    for ctx, entries in by_context.items():
        # The conclusion for a context comes from THAT context's own
        # ``state`` — NOT the combined ``state`` field, which is the
        # aggregate across every context ("failure" when ANY one of them
        # failed).  Using the combined state smeared one failing context
        # onto every other: a green context such as "All CI checks passed"
        # was reported as a failing check whenever an unrelated context
        # failed, which misassembled the cross-repo ci-fix failing list and
        # gated merges on checks that had actually passed (observed
        # 2026-09-03 on robotsix-chat#1807's merge polling).
        latest = entries[0]
        state = latest.get("state") or statuses_data.get("state", "success")
        # Commit-status state vocabulary is error/failure/pending/success;
        # translate it into the check-run *conclusion* vocabulary here, at the
        # boundary.  "error" (an infrastructure/exception failure that GitHub
        # rolls up into the combined "failure" state) has no Checks-API
        # conclusion equivalent, so normalize it to "failure" — otherwise
        # _conclusion_for_check would classify the errored context "neutral"
        # and it would stop gating the merge (regression vs the old combined
        # roll-up, which reported "failure" whenever any context errored).
        if state == "pending":
            conclusion = None
        elif state == "error":
            conclusion = "failure"
        else:
            conclusion = state
        runs.append(
            {
                "id": None,  # no detail fetch for statuses
                "name": ctx,
                "status": "completed" if state != "pending" else "in_progress",
                "conclusion": conclusion,
                "output": {
                    "summary": None,
                    "text": None,
                    "annotations": [],
                },
            }
        )
    return runs


def _in_flight(cr: dict[str, Any]) -> bool:
    """True when a check run is still running.

    ``status`` alone lies: GitHub can leave a check run at
    ``status: in_progress`` forever while recording a ``conclusion`` and a
    ``completed_at`` for it (robotsix-chat job 119955826312, "Auto-format
    with ruff": ``in_progress`` / ``success`` / completed 2026-10-22
    16:44:06Z, its workflow run ``completed``/``success``). Read as pending,
    that one zombie kept chat #2188 (ticket 1529) re-polling at
    IMPLEMENT_COMPLETE every two minutes for four days with ``pending
    checks: ['Auto-format with ruff']`` and "CI still running — skipping
    branch refresh". A run that carries a conclusion AND a completion time
    is finished whatever ``status`` says.
    """
    if cr.get("status", "") not in _PENDING_STATUSES:
        return False
    return not (cr.get("conclusion") and cr.get("completed_at"))


def _conclusion_for_check(cr: dict[str, Any]) -> str:
    """Classify a single check run as 'pending', 'failure', or 'neutral'."""
    if _in_flight(cr):
        return "pending"
    conclusion = cr.get("conclusion")
    if conclusion in _INCONCLUSIVE_CONCLUSIONS:
        # Superseded / no-verdict → wait for the authoritative run rather
        # than reporting a false failure (see _INCONCLUSIVE_CONCLUSIONS).
        return "pending"
    if conclusion in _FAILING_CONCLUSIONS:
        return "failure"
    return "neutral"


def _latest_definitive_runs(check_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse multiple runs of the SAME check name to one representative.

    A check context (e.g. ``ci (3.11) / tests``) can have several runs at
    one commit: GitHub's concurrency control ``cancelled`` the superseded
    run when a newer one started, so the same name carries both a
    ``cancelled`` AND a ``success`` run. ``_conclusion_for_check`` maps
    ``cancelled``→``pending`` (so a genuinely-cancelling churn isn't read as
    a false failure — see ``_INCONCLUSIVE_CONCLUSIONS``); but feeding BOTH
    runs to the aggregator makes the whole PR read ``pending`` forever even
    though the authoritative run is green — the ticket then sits in
    IMPLEMENT_COMPLETE and never merges (live: llmio c273/55f1/d932/fcf4).

    Per name, prefer the latest run with a DEFINITIVE conclusion
    (success/failure — not cancelled/stale/running); fall back to the
    latest run overall when only inconclusive/in-flight runs exist (so a
    still-churning check correctly stays pending). Ordering is by
    ``started_at`` (ISO strings sort chronologically).
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for cr in check_runs:
        by_name.setdefault(cr.get("name", ""), []).append(cr)
    reps: list[dict[str, Any]] = []
    for runs in by_name.values():
        runs_sorted = sorted(runs, key=lambda r: r.get("started_at") or "")
        definitive = [
            r
            for r in runs_sorted
            if not _in_flight(r)
            and (r.get("conclusion") or "") not in _INCONCLUSIVE_CONCLUSIONS
        ]
        reps.append(definitive[-1] if definitive else runs_sorted[-1])
    return reps


def _extract_annotations(
    client,
    api: str,
    owner: str,
    repo: str,
    headers: dict[str, Any],
    cr: dict[str, Any],
) -> dict[str, Any]:
    """Fetch and parse annotations for a failing check run (best-effort)."""
    cr_id = cr.get("id")
    name = cr.get("name", "unknown")
    summary = None
    text = None
    annotations: list[dict[str, Any]] = []

    if cr_id is not None:
        try:
            detail = client.get(
                f"{api}/repos/{owner}/{repo}/check-runs/{cr_id}",
                headers=headers,
            )
            detail.raise_for_status()
            output = detail.json().get("output", {}) or {}
            summary = output.get("summary")
            text = output.get("text")
            raw_anns = output.get("annotations") or []
            # GitHub's check-run detail never embeds the annotations: it
            # carries ``annotations_count`` + ``annotations_url`` and the
            # list lives behind that URL. Reading only the inline key meant
            # every failing check reported "no annotations" — including the
            # account-billing block ("The job was not started because recent
            # account payments have failed …"), whose ONLY evidence is such an
            # annotation, so the ci_fix / CI-monitor account-block gates never
            # fired (robotsix-invest b291 / c4cd, 2026-10-28; the unseen
            # 2026-09-18..22 outage).
            if not raw_anns and (output.get("annotations_count") or 0) > 0:
                ann_url = output.get("annotations_url") or (
                    f"{api}/repos/{owner}/{repo}/check-runs/{cr_id}/annotations"
                )
                try:
                    ar = client.get(ann_url, headers=headers)
                    ar.raise_for_status()
                    fetched = ar.json()
                    if isinstance(fetched, list):
                        raw_anns = fetched
                except Exception:
                    pass  # annotations fetch is best-effort too
            annotations = [
                {
                    "path": a.get("path", ""),
                    "start_line": a.get("start_line"),
                    "message": a.get("message", ""),
                    "level": a.get("annotation_level", "failure"),
                }
                for a in raw_anns[:20]
            ]
        except Exception:
            pass  # detail fetch is best-effort

    # Apply truncation.
    if summary and len(summary) > 2000:
        summary = summary[:1999] + "…"
    if text and len(text) > 4000:
        text = text[:3999] + "…"

    return {
        "name": name,
        "summary": summary,
        "text": text,
        "annotations": annotations,
        "conclusion": cr.get("conclusion"),
    }


def _derive_check_conclusion(
    client,
    api: str,
    owner: str,
    repo: str,
    headers: dict[str, Any],
    check_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive the overall conclusion and build the failing/pending/jobs lists."""
    if not check_runs:
        return {"conclusion": None, "failing": [], "pending": [], "jobs": []}

    # Collapse same-name reruns so a superseded ``cancelled`` run doesn't
    # mask the authoritative ``success`` and pin the PR at pending forever.
    check_runs = _latest_definitive_runs(check_runs)

    has_pending = False
    has_failure = False
    failing: list[dict[str, Any]] = []
    pending: list[str] = []
    jobs: list[dict[str, Any]] = []

    for cr in check_runs:
        cat = _conclusion_for_check(cr)
        name = cr.get("name", "unknown")
        conclusion = cr.get("conclusion")
        jobs.append({"name": name, "conclusion": conclusion})
        if cat == "pending":
            has_pending = True
            pending.append(name)
        elif cat == "failure":
            has_failure = True
            failing.append(_extract_annotations(client, api, owner, repo, headers, cr))

    result: dict[str, Any]
    if has_failure:
        result = {
            "conclusion": "failure",
            "failing": failing,
            "pending": pending,
            "jobs": jobs,
        }
    elif has_pending:
        result = {
            "conclusion": "pending",
            "failing": [],
            "pending": pending,
            "jobs": jobs,
        }
    else:
        result = {"conclusion": "success", "failing": [], "pending": [], "jobs": jobs}
    return result


def _latest_failing_workflow_runs(
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Latest completed run per ``workflow_id`` that concluded a failure.

    The most recent completed run per ``workflow_id`` wins (compared by the
    ``created_at`` string), so a later green re-run supersedes an earlier
    red one for the same workflow (and vice-versa). Runs with a ``None``
    conclusion (still in-flight) are ignored — they cannot mask a completed
    failure. Returns the winning runs whose ``conclusion`` is in
    ``_WORKFLOW_FAILING_CONCLUSIONS``.
    """
    latest: dict[Any, dict[str, Any]] = {}
    for run in runs:
        if run.get("conclusion") is None:
            continue  # skip in-progress runs — only completed runs count
        wid = run.get("workflow_id")
        if wid not in latest or run.get("created_at", "") > latest[wid].get(
            "created_at", ""
        ):
            latest[wid] = run
    return [
        r
        for r in latest.values()
        if r.get("conclusion") in _WORKFLOW_FAILING_CONCLUSIONS
    ]


def _fetch_run_job_failures(
    client: httpx.Client,
    api: str,
    owner: str,
    repo: str,
    headers: dict[str, Any],
    run_id: Any,
    unreadable: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Best-effort ``(failed_jobs, annotations)`` for a workflow run.

    Lists the run's jobs (``GET .../actions/runs/{id}/jobs``) and, for each
    job that concluded a failure, its check-run annotations
    (``GET .../check-runs/{job_id}/annotations``). A job refused before it
    started (GitHub billing / spending-limit block) has an empty ``steps``
    array and carries the refusal reason ONLY as such an annotation. Any
    error resolves to empty lists so a flaky API never breaks the gate.
    A 403 on the annotations endpoint (App lacks ``checks: read``) is
    recorded in *unreadable*.
    """
    if run_id is None:
        return [], []
    try:
        r = client.get(
            f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            headers=headers,
        )
        r.raise_for_status()
        raw_jobs = r.json().get("jobs") or []
    except Exception:
        return [], []
    failed = [
        j
        for j in raw_jobs
        if isinstance(j, dict) and j.get("conclusion") in _WORKFLOW_FAILING_CONCLUSIONS
    ]
    annotations: list[dict[str, Any]] = []
    for job in failed:
        job_id = job.get("id")
        if job_id is None:
            continue
        raw_anns: Any = []
        try:
            ar = client.get(
                f"{api}/repos/{owner}/{repo}/check-runs/{job_id}/annotations",
                headers=headers,
            )
            if ar.status_code == 403:
                _note_unreadable(owner, repo, "annotations", unreadable)
            ar.raise_for_status()
            raw_anns = ar.json() or []
        except Exception:
            pass  # annotation fetch is best-effort
        if not isinstance(raw_anns, list):
            continue
        for a in raw_anns:
            if isinstance(a, dict) and a.get("message"):
                annotations.append(
                    {
                        "path": a.get("path", ""),
                        "start_line": a.get("start_line"),
                        "message": a.get("message", ""),
                        "level": a.get("annotation_level", "failure"),
                    }
                )
    return failed, annotations[:20]


def _synthesized_run_summary(
    run: dict[str, Any],
    failed_jobs: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
) -> str:
    """Summary for a failing workflow run that registered no check-run."""
    url = run.get("html_url", "")
    conclusion = run.get("conclusion")
    if annotations:
        messages = " | ".join(a["message"] for a in annotations)
        summary = f"Workflow run concluded {conclusion!r}: {messages} {url}"
    elif failed_jobs and all(not j.get("steps") for j in failed_jobs):
        summary = (
            f"Workflow run concluded {conclusion!r}: its jobs never started "
            f"(no steps, no annotations). {url}"
        )
    else:
        summary = (
            f"Workflow run concluded {conclusion!r} but registered no "
            "check-run — likely a workflow parse failure (invalid "
            f"uses:/YAML). {url}"
        )
    summary = summary.strip()
    if len(summary) > 2000:
        summary = summary[:1999] + "…"
    return summary


def _merge_workflow_run_failures(
    result: dict[str, Any],
    runs: list[dict[str, Any]],
    fetch_job_failures: Callable[
        [Any], tuple[list[dict[str, Any]], list[dict[str, Any]]]
    ]
    | None = None,
) -> None:
    """Fold Actions-API workflow-run failures into a check-runs *result*.

    A workflow that fails to PARSE never registers a check-run, so the
    check-runs/statuses aggregation reads ``"success"`` while the workflow
    ran zero jobs (live: cost-monitor 27a2, where ``ci.yml`` and
    ``release.yml`` both failed at parse yet the PR flagged CI-green +
    mergeable). This mutates *result* in place to fail the gate when a
    workflow run for the head SHA concluded failure/startup_failure but is
    not already reflected in the check-runs failure detail.

    When the check-runs aggregation ALREADY reads ``"failure"``, the concrete
    per-job detail there is richer, so *result* is left untouched. Otherwise
    a failing entry is synthesized per uncaught workflow (named by its
    ``name``, which for a parse failure is the workflow file path).

    *fetch_job_failures* (``run_id -> (failed_jobs, annotations)``) enriches
    each synthesized entry with the run's job annotations: a run whose jobs
    were refused before starting (GitHub billing block) carries its reason
    only there, and ``classify_ci_run`` needs it to detect
    ``INFRA_ACCOUNT_BLOCKED``.
    """
    failing_runs = _latest_failing_workflow_runs(runs)
    if not failing_runs:
        return
    if result.get("conclusion") == "failure":
        return
    existing = {(f.get("name") or "").strip() for f in result.get("failing", []) or []}
    for run in failing_runs:
        name = (run.get("name") or run.get("path") or "workflow").strip()
        if name in existing:
            continue
        existing.add(name)
        failed_jobs: list[dict[str, Any]] = []
        annotations: list[dict[str, Any]] = []
        if fetch_job_failures is not None:
            failed_jobs, annotations = fetch_job_failures(run.get("id"))
        result.setdefault("failing", []).append(
            {
                "name": name,
                "summary": _synthesized_run_summary(run, failed_jobs, annotations),
                "text": None,
                "annotations": annotations,
                "conclusion": run.get("conclusion"),
                # Actions jobs API (``actions: read``) — readable even when
                # check-runs are not; ``classify_ci_run`` keys on ``steps``.
                "jobs": [
                    {
                        "name": j.get("name"),
                        "conclusion": j.get("conclusion"),
                        "steps": j.get("steps"),
                        "runner_name": j.get("runner_name"),
                    }
                    for j in failed_jobs
                ],
            }
        )
    result["conclusion"] = "failure"
    result["pending"] = []
