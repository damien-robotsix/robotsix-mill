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
    runs = []
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


def _conclusion_for_check(cr: dict[str, Any]) -> str:
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


def _merge_workflow_run_failures(
    result: dict[str, Any], runs: list[dict[str, Any]]
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
        result.setdefault("failing", []).append(
            {
                "name": name,
                "summary": (
                    f"Workflow run concluded {run.get('conclusion')!r} but "
                    "registered no check-run — likely a workflow parse "
                    "failure (invalid uses:/YAML). "
                    f"{run.get('html_url', '')}"
                ).strip(),
                "text": None,
                "annotations": [],
                "conclusion": run.get("conclusion"),
            }
        )
    result["conclusion"] = "failure"
    result["pending"] = []


class GitHubForgeCIMixin:
    """CI/checks operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo``, ``self.settings``,
    ``self._repo_config``, ``self._get_pr`` to exist on the final class.
    """

    def check_status(
        self, *, source_branch: str, require_checks: bool = False
    ) -> dict[str, Any] | None:
        """Return the aggregate CI check status for *source_branch*'s PR head.

        Returns a ``dict`` with ``conclusion`` (``"success"`` /
        ``"failure"`` / ``"pending"``) and a ``failing`` list of failing-
        check detail dicts, or ``None`` when there is no PR / head SHA to
        gate on. A repo with no CI configured reports ``"success"`` so the
        merge pipeline does not wait forever.

        When *require_checks* is ``True``, an empty check-run list (no CI
        registered yet for the commit) is classified as ``"pending"``
        rather than ``"success"`` — callers that know the repo MUST have CI
        (e.g. the ci-fix agent after a push) should pass ``True`` to avoid
        a false ``CI_PASSED`` before the first check run materialises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._check_status(
            owner=owner, repo=repo, head=source_branch, require_checks=require_checks
        )

    def commit_ci_conclusion(self, *, sha: str) -> dict[str, Any] | None:
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
    ) -> list[dict[str, Any]]:
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

    def rerun_workflow(self, *, run_id: int) -> dict[str, Any]:
        """Re-run a GitHub Actions workflow run by id.

        Returns ``{"rerun": True}`` on success, ``{"rerun": False,
        "reason": ...}`` on any failure.  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._rerun_workflow(owner=owner, repo=repo, run_id=run_id)
        except Exception as e:
            return {"rerun": False, "reason": str(e)}

    # --- HTTP seams (monkeypatched in tests) ---

    def _check_status(
        self,
        *,
        owner: str,
        repo: str,
        head: str,
        sha: str | None = None,
        require_checks: bool = False,
    ) -> dict[str, Any] | None:
        if sha is None:
            pr = self._get_pr(owner=owner, repo=repo, head=head)  # type: ignore[attr-defined]
            if pr is None:
                return None

            sha = pr.get("sha", "")
            if not sha:
                return None

        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            # 1. Fetch check runs (any status — completed, in_progress,
            # queued — so a brand-new SHA with a workflow that's been
            # queued but not started is correctly classified "pending"
            # rather than "no CI configured" below.
            #
            # A 403 here means the App installation lacks ``checks: read``
            # for this repo. That's a config gap, not a transient error
            # — treat it as "no check_runs visible" and fall through to
            # statuses + no-CI handling.
            check_runs: list[dict[str, Any]] = []
            cr_resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                headers=headers,
                params={"per_page": 100},
            )
            if cr_resp.status_code == 401:
                continue
            if cr_resp.status_code != 403:
                cr_resp.raise_for_status()
                check_runs = cr_resp.json().get("check_runs", [])

            # 2. Always probe combined statuses too. A repo without
            # any CI returns empty check_runs AND empty
            # statuses_data["statuses"] — we use that to distinguish
            # "no CI configured" (pass-through) from "CI pending"
            # (wait). 403 on statuses follows the same logic.
            status_runs: list[dict[str, Any]] = []
            st_resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/status",
                headers=headers,
            )
            if st_resp.status_code == 401:
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
            #
            # When *require_checks* is True the caller knows the repo
            # MUST have CI (e.g. ci-fix after a push).  An empty list
            # here means check runs haven't been registered YET, not
            # that the repo lacks CI — classify as "pending" so the
            # caller keeps waiting rather than false-passing.
            wf_runs: list[dict[str, Any]] | None = None
            if not check_runs and not status_runs:
                if require_checks:
                    result = {
                        "conclusion": "pending",
                        "failing": [],
                        "pending": [],
                        "jobs": [],
                        "_no_checks": True,
                    }
                else:
                    # Empty check-runs is NOT yet proof of "no CI": a
                    # just-pushed SHA has a window where its workflow RUN
                    # exists but no check-run is registered, and reading
                    # that window as no-CI green-lit red merges (hexarchy
                    # #286/#287, 2026-09-05 — the merge stage's own
                    # refresh push raced its CI scan). Probe the Actions
                    # API including in-flight runs; only a SHA with zero
                    # runs is treated as a repo without CI.
                    wf_runs = self._safe_list_workflow_runs(
                        owner, repo, sha, status=None
                    )
                    in_flight = [r for r in wf_runs if r.get("conclusion") is None]
                    if in_flight:
                        result = {
                            "conclusion": "pending",
                            "failing": [],
                            "pending": [
                                (r.get("name") or r.get("path") or "workflow")
                                for r in in_flight
                            ],
                            "jobs": [],
                            "_no_checks": True,
                        }
                    else:
                        result = {
                            "conclusion": "success",
                            "failing": [],
                            "pending": [],
                            "jobs": [],
                        }
            else:
                result = _derive_check_conclusion(
                    c, api, owner, repo, headers, check_runs
                )

            # A workflow that fails to PARSE (invalid ``uses:``, malformed
            # YAML) registers ZERO check-runs and posts NO commit status, so
            # its failure is invisible to the aggregation above — the gate
            # reads "success" while the workflow ran not one job (live:
            # cost-monitor 27a2). Cross-check the Actions API: any workflow
            # RUN for this head SHA that concluded failure/startup_failure
            # fails the gate, including runs whose name is the workflow file
            # path (the parse-failure signature).
            _merge_workflow_run_failures(
                result,
                wf_runs
                if wf_runs is not None
                else self._safe_list_workflow_runs(owner, repo, sha),
            )
            result["_sha"] = sha
            return result
        return None

    def _safe_list_workflow_runs(
        self, owner: str, repo: str, sha: str, status: str | None = "completed"
    ) -> list[dict[str, Any]]:
        """Best-effort ``_list_workflow_runs`` for the parse-failure gate.

        Any error (App missing ``actions: read``, transport failure, an
        empty SHA) resolves to an empty list, so a flaky Actions API can
        never *add* a false failure to the CI gate. ``status=None`` lists
        runs in ANY state (in-flight included) for the no-check-runs probe.
        """
        if not sha:
            return []
        try:
            return self._list_workflow_runs(
                owner=owner, repo=repo, branch=None, head_sha=sha, status=status
            )
        except Exception:
            return []

    def _list_workflow_runs(
        self,
        *,
        owner: str,
        repo: str,
        branch: str | None,
        head_sha: str | None,
        status: str | None = "completed",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": 30}
        if status is not None:
            params["status"] = status
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
        s = self.settings  # type: ignore[attr-defined]

        # 1. List jobs for the run (with 401 retry).
        jobs: list[Any] = []
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            jobs_resp = c.get(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                headers=headers,
                params={"status": "completed"},
            )
            if jobs_resp.status_code == 401:
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
        for j in failed_jobs:
            job_id = j["id"]
            job_name = j.get("name", f"job-{job_id}")
            # Each job-log fetch gets its own 401 retry.
            raw: str = ""
            for _retry, c, api, headers in self._http.retrying_client(  # type: ignore[attr-defined]
                max_retries=2,
            ):
                try:
                    log_resp = c.get(
                        f"{api}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                        headers=headers,
                        follow_redirects=True,
                    )
                    if log_resp.status_code == 401:
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
                    raw = f"[log fetch failed for job {job_id}: {type(exc).__name__}]"
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

    def _rerun_workflow(self, *, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        """POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun."""
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            r = c.post(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/rerun",
                headers=headers,
            )
            if r.status_code == 401:
                continue
            r.raise_for_status()
            return {"rerun": True}
        return {"rerun": False, "reason": "max retries exhausted (401 loop)"}
