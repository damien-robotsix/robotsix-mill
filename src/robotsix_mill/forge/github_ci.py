"""GitHub CI/checks mixin — workflow runs, job log fetching, check status.

Split from ``github.py``.  Defines ``GitHubForgeCIMixin`` that
``GitHubForge`` inherits from.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ._log_utils import _capture_failure_window, _strip_runner_noise

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


def _statuses_to_check_runs(statuses_data: dict) -> list[dict]:
    """Convert combined statuses response into check-run–shaped dicts."""
    statuses = statuses_data.get("statuses", [])
    if not statuses:
        return []
    # Collapse per-context into a single item.
    by_context: dict[str, list[dict]] = {}
    for st in statuses:
        ctx = st.get("context", "")
        by_context.setdefault(ctx, []).append(st)
    runs = []
    for ctx, _items in by_context.items():
        # overall state: "success", "failure", "pending"
        state = statuses_data.get("state", "success")
        conclusion = state if state != "pending" else None
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


def _conclusion_for_check(cr: dict) -> str:
    """Classify a single check run as 'pending', 'failure', or 'neutral'."""
    if cr.get("status", "") in _PENDING_STATUSES:
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
            if r.get("status", "") not in _PENDING_STATUSES
            and (r.get("conclusion") or "") not in _INCONCLUSIVE_CONCLUSIONS
        ]
        reps.append(definitive[-1] if definitive else runs_sorted[-1])
    return reps


def _extract_annotations(
    client,
    api: str,
    owner: str,
    repo: str,
    headers: dict,
    cr: dict,
) -> dict:
    """Fetch and parse annotations for a failing check run (best-effort)."""
    cr_id = cr.get("id")
    name = cr.get("name", "unknown")
    summary = None
    text = None
    annotations: list[dict] = []

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
    }


def _derive_check_conclusion(
    client,
    api: str,
    owner: str,
    repo: str,
    headers: dict,
    check_runs: list[dict],
) -> dict:
    """Derive the overall conclusion and build the failing/pending lists."""
    if not check_runs:
        return {"conclusion": None, "failing": [], "pending": []}

    # Collapse same-name reruns so a superseded ``cancelled`` run doesn't
    # mask the authoritative ``success`` and pin the PR at pending forever.
    check_runs = _latest_definitive_runs(check_runs)

    has_pending = False
    has_failure = False
    failing: list[dict[str, Any]] = []
    pending: list[str] = []

    for cr in check_runs:
        cat = _conclusion_for_check(cr)
        if cat == "pending":
            has_pending = True
            pending.append(cr.get("name", "unknown"))
        elif cat == "failure":
            has_failure = True
            failing.append(_extract_annotations(client, api, owner, repo, headers, cr))

    if has_failure:
        return {"conclusion": "failure", "failing": failing, "pending": pending}
    if has_pending:
        return {"conclusion": "pending", "failing": [], "pending": pending}
    return {"conclusion": "success", "failing": [], "pending": []}


class GitHubForgeCIMixin:
    """CI/checks operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo``, ``self.settings``,
    ``self._repo_config``, ``self._get_pr`` to exist on the final class.
    """

    def check_status(self, *, source_branch: str) -> dict | None:
        """Return the aggregate CI check status for *source_branch*'s PR head.

        Returns a ``dict`` with ``conclusion`` (``"success"`` /
        ``"failure"`` / ``"pending"``) and a ``failing`` list of failing-
        check detail dicts, or ``None`` when there is no PR / head SHA to
        gate on. A repo with no CI configured reports ``"success"`` so the
        merge pipeline does not wait forever.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._check_status(owner=owner, repo=repo, head=source_branch)

    def commit_ci_conclusion(self, *, sha: str) -> dict | None:
        """Aggregate CI conclusion for an arbitrary commit SHA (no PR).

        Same return shape as check_status: {"conclusion": "success"|"failure"|
        "pending"|None, "failing": [...], "pending": [...]} or None when the
        status cannot be determined (auth/permission/transport error).
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._check_status(owner=owner, repo=repo, head="", sha=sha)
        except Exception:
            return None

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        """Return completed GitHub Actions workflow runs.

        :param branch: when set, filter runs to this branch.
        :param head_sha: when set, filter runs to this head commit SHA.
        Returns a ``list[dict]`` (one per run) with ``id``, ``name``,
        ``workflow_id``, ``head_sha``, ``conclusion``, ``html_url``,
        ``created_at``, ``event``, and ``head_branch``.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_workflow_runs(
            owner=owner,
            repo=repo,
            branch=branch,
            head_sha=head_sha,
        )

    def fetch_workflow_job_logs(self, *, run_id: int, full_log: bool = False) -> str:
        """Return the logs of the failed jobs in workflow run *run_id*.

        :param run_id: GitHub Actions workflow-run id whose jobs to fetch.
        :param full_log: when ``False`` (default), size-caps and windows
            the log around the first failure marker; ``True`` returns the
            complete job logs (still ANSI-stripped and runner-noise-stripped).
        Concatenates logs for up to the first few failed-like jobs of the run
        into a single string; returns ``""`` when the run has no failed jobs.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._fetch_workflow_job_logs(
            owner=owner,
            repo=repo,
            run_id=run_id,
            full_log=full_log,
        )

    # --- HTTP seams (monkeypatched in tests) ---

    def _check_status(
        self, *, owner: str, repo: str, head: str, sha: str | None = None
    ) -> dict | None:
        from .auth import invalidate_and_backoff  # lazy: avoid import cycle

        if sha is None:
            pr = self._get_pr(owner=owner, repo=repo, head=head)  # type: ignore[attr-defined]
            if pr is None:
                return None

            sha = pr.get("sha", "")
            if not sha:
                return None

        for retry in range(2):
            with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
                # 1. Fetch check runs (any status — completed, in_progress,
                # queued — so a brand-new SHA with a workflow that's been
                # queued but not started is correctly classified "pending"
                # rather than "no CI configured" below.
                #
                # A 403 here means the App installation lacks ``checks: read``
                # for this repo. That's a config gap, not a transient error
                # — treat it as "no check_runs visible" and fall through to
                # statuses + no-CI handling.
                check_runs: list[dict] = []
                cr_resp = c.get(
                    f"{api}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                    headers=headers,
                    params={"per_page": 100},
                )
                if cr_resp.status_code == 401 and retry == 0:
                    invalidate_and_backoff(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    continue
                if cr_resp.status_code != 403:
                    cr_resp.raise_for_status()
                    check_runs = cr_resp.json().get("check_runs", [])

                # 2. Always probe combined statuses too. A repo without
                # any CI returns empty check_runs AND empty
                # statuses_data["statuses"] — we use that to distinguish
                # "no CI configured" (pass-through) from "CI pending"
                # (wait). 403 on statuses follows the same logic.
                status_runs: list[dict] = []
                st_resp = c.get(
                    f"{api}/repos/{owner}/{repo}/commits/{sha}/status",
                    headers=headers,
                )
                if st_resp.status_code == 401 and retry == 0:
                    invalidate_and_backoff(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    continue
                if st_resp.status_code != 403:
                    st_resp.raise_for_status()
                    statuses_data = st_resp.json()
                    status_runs = _statuses_to_check_runs(statuses_data)
                if not check_runs:
                    check_runs = status_runs

                # No checks AND no statuses (either truly empty or the
                # App lacks read permission for both endpoints) → there
                # is nothing meaningful to gate on. Treat as success so
                # the merge stage doesn't loop forever.
                if not check_runs and not status_runs:
                    return {"conclusion": "success", "failing": [], "pending": []}

                return _derive_check_conclusion(
                    c, api, owner, repo, headers, check_runs
                )
        return None

    def _list_workflow_runs(
        self,
        *,
        owner: str,
        repo: str,
        branch: str | None,
        head_sha: str | None,
    ) -> list[dict]:
        params: dict = {"status": "completed", "per_page": 30}
        if branch is not None:
            params["branch"] = branch
        if head_sha is not None:
            params["head_sha"] = head_sha

        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/actions/runs",
            params=params,
        )
        r.raise_for_status()
        raw = r.json().get("workflow_runs", [])
        return [
            {
                "id": run["id"],
                "name": run.get("name", ""),
                "workflow_id": run.get("workflow_id"),
                "head_sha": run.get("head_sha", ""),
                "conclusion": run.get("conclusion"),
                "html_url": run.get("html_url", ""),
                "created_at": run.get("created_at", ""),
                "event": run.get("event", ""),
                "head_branch": run.get("head_branch"),
                "path": run.get("path", ""),
            }
            for run in raw
        ]

    def _fetch_workflow_job_logs(
        self,
        *,
        owner: str,
        repo: str,
        run_id: int,
        full_log: bool = False,
    ) -> str:
        from .auth import invalidate_and_backoff  # lazy: avoid import cycle

        s = self.settings  # type: ignore[attr-defined]

        # 1. List jobs for the run (with 401 retry).
        # Defensive init: the loop sets `jobs` on every non-exception path,
        # but CodeQL's py/uninitialized-local-variable can't prove it through
        # the retry/continue/break flow — initialise so the analysis is clean
        # without changing behaviour (a double-401 still raises before use).
        jobs: list[Any] = []
        for retry in range(2):
            with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
                jobs_resp = c.get(
                    f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                    headers=headers,
                    params={"status": "completed"},
                )
                if jobs_resp.status_code == 401 and retry == 0:
                    invalidate_and_backoff(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    continue
                jobs_resp.raise_for_status()
                jobs = jobs_resp.json().get("jobs", [])
            break

        # 2. Filter to failed-like jobs.
        failed_conclusions = frozenset(
            {
                "failure",
                "cancelled",
                "timed_out",
                "action_required",
            }
        )
        failed_jobs = [j for j in jobs if j.get("conclusion") in failed_conclusions][
            :_MAX_FAILED_JOBS
        ]

        if not failed_jobs:
            return ""

        parts: list[str] = []
        log_max = s.ci_log_max_bytes

        # 3. Fetch logs for each failed job (with 401 retry per fetch).
        with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
            for j in failed_jobs:
                job_id = j["id"]
                job_name = j.get("name", f"job-{job_id}")
                # Each job-log fetch gets its own 401 retry.
                raw: str = ""
                for log_retry in range(2):
                    try:
                        log_resp = c.get(
                            f"{api}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                            headers=headers,
                            follow_redirects=True,
                        )
                        if log_resp.status_code == 401 and log_retry == 0:
                            invalidate_and_backoff(self.settings, self._repo_config)  # type: ignore[attr-defined]
                            headers = self._http.regenerate_headers()  # type: ignore[attr-defined]
                            continue
                        log_resp.raise_for_status()
                        raw = log_resp.text
                    except httpx.HTTPStatusError:
                        sc = log_resp.status_code
                        if sc == 403:
                            raw = f"[log fetch failed for job {job_id}: HTTP 403 — App likely missing Actions:Read permission]"
                        else:
                            raw = f"[log fetch failed for job {job_id}: HTTP {sc}]"
                    except Exception as exc:
                        raw = (
                            f"[log fetch failed for job {job_id}: {type(exc).__name__}]"
                        )
                    else:
                        if not raw:
                            raw = f"[log fetch returned empty body for job {job_id}]"
                    break  # success or final attempt

                # Strip ANSI.
                clean = _ANSI_RE.sub("", raw)
                # Strip runner preamble boilerplate (OS version, runner
                # image, git config, etc.) — pure token saving with zero
                # diagnostic loss.
                clean = _strip_runner_noise(clean)
                # Capture the window around the FIRST failure marker (not a
                # blind tail-cap) so an ``if: always()`` cascade — where a
                # downstream always-step re-errors with misleading input —
                # can't mask the step that actually failed first.
                if not full_log:
                    clean = _capture_failure_window(
                        clean,
                        log_max,
                        failure_re=_LOG_FAILURE_RE,
                        tail_context=_LOG_FAILURE_TAIL_CONTEXT,
                    )

                parts.append(f"### Job: {job_name} (id={job_id})\n")
                parts.append(clean)
                parts.append("\n")

        return "\n".join(parts)
