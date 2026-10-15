"""GitLab CI pipeline mixin — pipeline status, job log retrieval,
failure analysis.

Split from the monolithic ``gitlab.py``.  Defines
``GitLabForgeCIMixin`` that ``GitLabForge`` inherits from.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import yaml

from .._log_utils import _capture_failure_window, _strip_runner_noise
from ..github_ci import _ANSI_RE, _MAX_FAILED_JOBS

# Earliest-failure markers in a GitLab CI job log.  GitLab jobs don't
# emit GitHub Actions–style ``##[error]`` lines; instead we match the
# patterns that GitLab Runner / common build tools emit on failure.
_LOG_FAILURE_RE = re.compile(
    r"(?:^ERROR:\s|^ERROR\[|Job failed|exit code [1-9]|"
    r"^\s*FAIL\b|FAILED\s|fatal:)",
    re.MULTILINE,
)

# Direct mapping for _list_pipelines — preserves terminal granularity
# (canceled, skipped, etc.) that _map_pipeline_status collapses to "pending".
_PIPELINE_CONCLUSION_MAP: dict[str, str | None] = {
    "success": "success",
    "failed": "failure",
    "canceled": "cancelled",
    "skipped": "skipped",
}


def _map_pipeline_status(status: str) -> str | None:
    """Map GitLab pipeline status to standard conclusion (for check_status)."""
    if status == "success":
        return "success"
    if status == "failed":
        return "failure"
    if status in (
        "pending",
        "running",
        "created",
        "waiting_for_resource",
        "preparing",
        "manual",
        "scheduled",
        # Canceled / superseded pipelines don't represent a real verdict;
        # treating them as failure triggers the same false-fix loop that
        # GitHub's _INCONCLUSIVE_CONCLUSIONS guards against.
        "canceled",
        "skipped",
    ):
        return "pending"
    return None


def _gitlab_ci_triggers(ci_text: str, *, has_active_schedule: bool) -> list[str] | None:
    """Derive GitHub-style trigger event names from a GitLab CI config.

    GitLab has no per-workflow ``on:`` block — a single ``.gitlab-ci.yml``
    plus zero or more pipeline schedules governs when a pipeline runs.  We
    normalise that into the same event vocabulary the forge-agnostic
    ci-auto-close runner expects (``schedule``, ``workflow_dispatch``,
    ``push``, ``pull_request``), so :func:`_is_cron_only` can detect a
    GitLab pipeline that only ever runs on a schedule.

    Mapping, derived from the top-level ``workflow:rules`` block (GitLab's
    ``$CI_PIPELINE_SOURCE`` discriminator):

    - ``schedule`` → ``schedule`` (also added when an active pipeline
      schedule exists)
    - ``web`` / ``api`` / ``trigger`` → ``workflow_dispatch`` (GitLab also
      always permits an API-triggered pipeline, so this is always present)
    - ``push`` / ``$CI_COMMIT_BRANCH`` / ``$CI_COMMIT_TAG`` → ``push``
    - ``merge_request_event`` / ``external_pull_request_event`` →
      ``pull_request``

    When there is no ``workflow:rules`` block, GitLab runs on every push by
    default, so ``push`` is reported.  Returns ``None`` only when nothing
    could be derived.
    """
    events: set[str] = {"workflow_dispatch"}
    if has_active_schedule:
        events.add("schedule")

    data: Any = None
    if ci_text:
        try:
            data = yaml.safe_load(ci_text)
        except yaml.YAMLError:
            data = None

    rules: Any = None
    if isinstance(data, dict):
        workflow = data.get("workflow")
        if isinstance(workflow, dict):
            rules = workflow.get("rules")

    if isinstance(rules, list):
        for rule in rules:
            cond = str(rule.get("if", "")) if isinstance(rule, dict) else str(rule)
            events.update(_rule_events(cond))
    else:
        # No workflow:rules → GitLab runs on every push by default.
        events.add("push")

    return sorted(events) if events else None


def _rule_events(cond: str) -> set[str]:
    """Map a single GitLab ``workflow:rules`` ``if:`` condition to event names.

    Helper for :func:`_gitlab_ci_triggers` — keeps the per-rule source-match
    logic out of the loop so the caller stays within complexity limits.
    """
    events: set[str] = set()
    if '"schedule"' in cond or "'schedule'" in cond:
        events.add("schedule")
    if (
        '"push"' in cond
        or "'push'" in cond
        or "CI_COMMIT_BRANCH" in cond
        or "CI_COMMIT_TAG" in cond
    ):
        events.add("push")
    if "merge_request" in cond or "external_pull_request" in cond:
        events.add("pull_request")
    if (
        '"web"' in cond
        or "'web'" in cond
        or '"api"' in cond
        or "'api'" in cond
        or '"trigger"' in cond
        or "'trigger'" in cond
    ):
        events.add("workflow_dispatch")
    return events


class GitLabForgeCIMixin:
    """CI operations for GitLab — mixed into ``GitLabForge``.

    Expects ``self._http``, ``self.settings``, ``self._repo_config``,
    ``self._remote_url``, and ``self._resolve_project_id`` to exist on
    the final class.
    """

    def check_status(
        self, *, source_branch: str, require_checks: bool = False
    ) -> dict[str, Any] | None:
        """Return pipeline conclusion + failed job details for the MR of *source_branch*.

        Returns a dict with ``conclusion``, ``failing``, ``pending``, and
        ``jobs`` keys, or ``None`` when the MR or pipeline is not found.
        Never raises.
        """
        try:
            from .core import _parse_gitlab_project_path

            project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)  # type: ignore[attr-defined]
            if mr is None:
                return None

            pipeline = self._get_latest_pipeline(project_path, mr["iid"])
            if pipeline is None:
                return {"conclusion": None, "failing": [], "pending": [], "jobs": []}

            status = pipeline.get("status", "")
            conclusion = _map_pipeline_status(status)
            pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]

            failing: list[dict[str, Any]] = []
            if conclusion == "failure":
                failing = self._get_failed_jobs(project_path, pipeline["id"])

            # Lightweight per-job list — name + conclusion only, no trace fetch.
            jobs = self._get_pipeline_job_summaries(pid, pipeline["id"])

            return {
                "conclusion": conclusion,
                "failing": failing,
                "pending": [],
                "jobs": jobs,
            }
        except Exception:
            return None

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict[str, Any]]:
        """List pipeline runs for *branch* and/or *head_sha*, newest first.

        Returns a list of terminal pipeline dicts with ``id``, ``name``,
        ``head_sha``, ``conclusion``, ``html_url``, and ``created_at`` keys.
        """
        from .core import _parse_gitlab_project_path

        project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
        return self._list_pipelines(
            project_path=project_path, branch=branch, head_sha=head_sha
        )

    def fetch_workflow_job_logs(self, *, run_id: int, full_log: bool = False) -> str:
        """Fetch concatenated failed-job logs from a pipeline by *run_id*.

        Returns ANSI-stripped, size-capped log text, or ``""`` when there
        are no failed jobs.
        """
        from .core import _parse_gitlab_project_path

        project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
        return self._fetch_pipeline_job_logs(
            project_path=project_path, run_id=run_id, full_log=full_log
        )

    def rerun_workflow(self, *, run_id: int) -> dict[str, Any]:
        """Re-run a GitLab pipeline by id.

        Returns ``{"rerun": True}`` on success, ``{"rerun": False,
        "reason": ...}`` on any failure.  Must NEVER raise.
        """
        try:
            from .core import _parse_gitlab_project_path

            project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
            return self._rerun_workflow(project_path=project_path, run_id=run_id)
        except Exception as e:
            return {"rerun": False, "reason": str(e)}

    def get_workflow_run(self, *, run_id: int) -> dict[str, Any] | None:
        """Return a single GitLab pipeline by *run_id* (a pipeline id).

        Best-effort: returns a dict with ``id``, ``name`` (the pipeline
        ref — GitLab pipelines have no name), ``path`` (always ``""`` —
        GitLab has no per-workflow file), ``head_branch``, ``head_sha``,
        ``conclusion``, ``run_attempt`` (always ``None``), ``html_url``,
        and ``created_at``, or ``None`` on any error.  Lets the
        ci-auto-close runner recover anchors from a pipeline URL.
        """
        try:
            from .core import _parse_gitlab_project_path

            project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
            return self._get_workflow_run(project_path=project_path, run_id=run_id)
        except Exception:
            return None

    def list_workflow_run_jobs(self, *, run_id: int) -> list[dict[str, Any]]:
        """List the jobs of pipeline *run_id*.

        Returns a list of ``{"id", "name", "status", "conclusion"}`` dicts
        (empty when the pipeline started no job — the queue-timeout
        signature).  May raise on transport error so a caller can tell
        "no jobs" apart from "could not tell" (mirrors the GitHub seam).
        """
        from .core import _parse_gitlab_project_path

        project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
        return self._list_workflow_run_jobs(project_path=project_path, run_id=run_id)

    def has_online_runner(self) -> bool:
        """True when any runner available to the project is online.

        Best-effort: any lookup error returns ``False``.  Must NEVER raise.
        """
        try:
            from .core import _parse_gitlab_project_path

            project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
            return self._has_online_runner(project_path=project_path)
        except Exception:
            return False

    def get_workflow_triggers(self, *, path: str) -> list[str] | None:
        """Return the trigger event names governing the project's pipeline.

        GitLab has no per-workflow file, so *path* is accepted for ABC
        parity but ignored — triggers are derived from the project's
        pipeline schedules and the top-level ``workflow:rules`` block of
        ``.gitlab-ci.yml`` (see :func:`_gitlab_ci_triggers`).  Returns a
        list such as ``["schedule", "workflow_dispatch"]``, or ``None`` on
        any error.  Must NEVER raise.
        """
        try:
            from .core import _parse_gitlab_project_path

            project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
            return self._get_workflow_triggers(project_path=project_path, path=path)
        except Exception:
            return None

    def dispatch_workflow(
        self,
        *,
        workflow_id: int | None = None,
        workflow_file: str | None = None,
        ref: str = "main",
    ) -> dict[str, Any]:
        """Trigger a new pipeline on *ref*.

        GitLab has a single ``.gitlab-ci.yml`` rather than selectable
        workflows, so *workflow_id* / *workflow_file* are accepted for ABC
        parity but ignored — every dispatch creates a fresh pipeline via
        ``POST /projects/:id/pipeline``.  Returns ``{"dispatched": True}``
        on success, ``{"dispatched": False, "reason": ...}`` otherwise.
        Must NEVER raise.
        """
        try:
            from .core import _parse_gitlab_project_path

            project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
            return self._dispatch_workflow(project_path=project_path, ref=ref)
        except Exception as e:
            return {"dispatched": False, "reason": str(e)}

    # -- HTTP seams (monkeypatched in tests) -------------------------------

    def _get_latest_pipeline(
        self, project_path: str, mr_iid: int
    ) -> dict[str, Any] | None:
        """GET /projects/:id/merge_requests/:iid/pipelines?per_page=1."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        r = self._http.get(  # type: ignore[attr-defined]
            f"/projects/{pid}/merge_requests/{mr_iid}/pipelines",
            params={"per_page": 1},
        )
        r.raise_for_status()
        items = r.json()
        if not items:
            return None
        return items[0]

    def _get_pipeline_job_summaries(
        self, pid: str, pipeline_id: int
    ) -> list[dict[str, Any]]:
        """GET /projects/:id/pipelines/:pipeline_id/jobs (all jobs, no trace).

        Returns a lightweight list of ``{"name": str, "conclusion": str}``
        dicts — one per job in the pipeline.  This is intentionally cheap
        (no trace fetch, no ANSI stripping) so callers can get a complete
        per-job picture without the overhead of ``_get_failed_jobs``.
        """
        try:
            r = self._http.get(  # type: ignore[attr-defined]
                f"/projects/{pid}/pipelines/{pipeline_id}/jobs",
                params={"per_page": 100},
            )
            r.raise_for_status()
            jobs = r.json()
        except Exception:
            return []
        return [
            {"name": j.get("name", ""), "conclusion": j.get("status")} for j in jobs
        ]

    def _get_failed_jobs(
        self, project_path: str, pipeline_id: int
    ) -> list[dict[str, Any]]:
        """GET /projects/:id/pipelines/:pipeline_id/jobs?scope=failed&per_page=20.

        Each failed job's trace (``GET /projects/:id/jobs/:job_id/trace``) is
        fetched and ANSI-stripped / failure-windowed via the shared
        ``_capture_failure_window`` helper so ``check_status`` returns failure
        detail (``summary``/``text``) comparable to the GitHub adapter. GitLab
        exposes no per-line annotations, so ``annotations`` stays ``[]``.
        """
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        log_max = self.settings.ci_log_max_bytes  # type: ignore[attr-defined]

        with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
            jobs_resp = c.get(
                f"{api}/projects/{pid}/pipelines/{pipeline_id}/jobs",
                headers=headers,
                params={"scope": "failed", "per_page": 20},
            )
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json()

            failing: list[dict[str, Any]] = []
            for j in jobs:
                job_id = j["id"]
                try:
                    log_resp = c.get(
                        f"{api}/projects/{pid}/jobs/{job_id}/trace",
                        headers=headers,
                    )
                    log_resp.raise_for_status()
                    raw = log_resp.text
                except Exception:
                    raw = ""

                clean = _capture_failure_window(
                    _strip_runner_noise(_ANSI_RE.sub("", raw)),
                    log_max,
                    failure_re=_LOG_FAILURE_RE,
                )
                summary = clean or None
                text = clean or None
                if summary and len(summary) > 2000:
                    summary = summary[:1999] + "…"
                if text and len(text) > 4000:
                    text = text[:3999] + "…"

                failing.append(
                    {
                        "name": j.get("name", ""),
                        "summary": summary,
                        "text": text,
                        "annotations": [],
                        "conclusion": j.get("status", "failed"),
                    }
                )

        return failing

    def _list_pipelines(
        self,
        *,
        project_path: str,
        branch: str | None,
        head_sha: str | None,
    ) -> list[dict[str, Any]]:
        """GET /projects/:id/pipelines?ref=…&sha=…&per_page=30."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        params: dict[str, Any] = {"per_page": 30}
        if branch is not None:
            params["ref"] = branch
        if head_sha is not None:
            params["sha"] = head_sha

        r = self._http.get(  # type: ignore[attr-defined]
            f"/projects/{pid}/pipelines",
            params=params,
        )
        r.raise_for_status()
        raw = r.json()
        terminal = {"success", "failed", "canceled", "skipped"}
        return [
            {
                "id": p["id"],
                # GitLab pipelines have no name; surface the ref instead.
                "name": p.get("ref", ""),
                "workflow_id": None,
                "head_sha": p.get("sha", ""),
                "conclusion": _PIPELINE_CONCLUSION_MAP.get(p.get("status", "")),
                # GitLab pipelines have no re-run/attempt counter on the list
                # endpoint (a retry creates a fresh pipeline id) — always None.
                "run_attempt": None,
                "html_url": p.get("web_url", ""),
                "created_at": p.get("created_at", ""),
                "path": "",
            }
            for p in raw
            if p.get("status") in terminal
        ]

    def _fetch_pipeline_job_logs(
        self, *, project_path: str, run_id: int, full_log: bool = False
    ) -> str:
        """Concatenate ANSI-stripped, size-capped traces of failed jobs in a
        pipeline.  *run_id* is a GitLab pipeline id.  Returns ``""`` when there
        are no failed jobs.
        """
        s = self.settings  # type: ignore[attr-defined]
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]

        with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
            jobs_resp = c.get(
                f"{api}/projects/{pid}/pipelines/{run_id}/jobs",
                headers=headers,
                params={"scope": "failed", "per_page": 20},
            )
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json()

        failed_jobs = jobs[:_MAX_FAILED_JOBS]
        if not failed_jobs:
            return ""

        parts: list[str] = []
        log_max = s.ci_log_max_bytes

        with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
            for j in failed_jobs:
                job_id = j["id"]
                job_name = j.get("name", f"job-{job_id}")
                try:
                    log_resp = c.get(
                        f"{api}/projects/{pid}/jobs/{job_id}/trace",
                        headers=headers,
                    )
                    log_resp.raise_for_status()
                    raw = log_resp.text
                except httpx.HTTPStatusError:
                    raw = f"[log fetch failed for job {job_id}: HTTP {log_resp.status_code}]"
                except Exception as exc:
                    raw = f"[log fetch failed for job {job_id}: {type(exc).__name__}]"
                else:
                    if not raw:
                        raw = f"[log fetch returned empty body for job {job_id}]"

                clean = _ANSI_RE.sub("", raw)
                clean = _strip_runner_noise(clean)
                if not full_log:
                    clean = _capture_failure_window(
                        clean,
                        log_max,
                        failure_re=_LOG_FAILURE_RE,
                    )

                parts.append(f"### Job: {job_name} (id={job_id})\n")
                parts.append(clean)
                parts.append("\n")

        return "\n".join(parts)

    def _rerun_workflow(self, *, project_path: str, run_id: int) -> dict[str, Any]:
        """POST /projects/:id/pipelines/:pipeline_id/retry."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        try:
            r = self._http.post(  # type: ignore[attr-defined]
                f"/projects/{pid}/pipelines/{run_id}/retry",
            )
            if r.status_code == 201:
                return {"rerun": True}
            return {
                "rerun": False,
                "reason": f"retry endpoint returned HTTP {r.status_code}",
            }
        except Exception as e:
            return {"rerun": False, "reason": str(e)}

    def _get_workflow_run(
        self, *, project_path: str, run_id: int
    ) -> dict[str, Any] | None:
        """GET /projects/:id/pipelines/:pipeline_id → canonical run dict."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        r = self._http.get(f"/projects/{pid}/pipelines/{run_id}")  # type: ignore[attr-defined]
        r.raise_for_status()
        p = r.json()
        return {
            "id": p.get("id"),
            # GitLab pipelines have no name; surface the ref instead.
            "name": p.get("ref", ""),
            # GitLab has no per-workflow file path.
            "path": "",
            "head_branch": p.get("ref"),
            "head_sha": p.get("sha", ""),
            "conclusion": _PIPELINE_CONCLUSION_MAP.get(p.get("status", "")),
            # A GitLab retry yields a fresh pipeline id, not an attempt count.
            "run_attempt": None,
            "html_url": p.get("web_url", ""),
            "created_at": p.get("created_at", ""),
        }

    def _list_workflow_run_jobs(
        self, *, project_path: str, run_id: int
    ) -> list[dict[str, Any]]:
        """GET /projects/:id/pipelines/:pipeline_id/jobs."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        r = self._http.get(  # type: ignore[attr-defined]
            f"/projects/{pid}/pipelines/{run_id}/jobs",
            params={"per_page": 100},
        )
        r.raise_for_status()
        jobs = r.json()
        return [
            {
                "id": j.get("id"),
                "name": j.get("name", ""),
                "status": j.get("status"),
                "conclusion": _PIPELINE_CONCLUSION_MAP.get(j.get("status", "")),
            }
            for j in jobs
        ]

    def _has_online_runner(self, *, project_path: str) -> bool:
        """GET /projects/:id/runners?status=online → any runner returned."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        r = self._http.get(  # type: ignore[attr-defined]
            f"/projects/{pid}/runners",
            params={"status": "online"},
        )
        r.raise_for_status()
        return bool(r.json())

    def _dispatch_workflow(self, *, project_path: str, ref: str) -> dict[str, Any]:
        """POST /projects/:id/pipeline → create (dispatch) a pipeline on *ref*."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        r = self._http.post(  # type: ignore[attr-defined]
            f"/projects/{pid}/pipeline",
            json={"ref": ref},
        )
        if r.status_code in (200, 201):
            return {"dispatched": True}
        return {
            "dispatched": False,
            "reason": f"pipeline endpoint returned HTTP {r.status_code}",
        }

    def _get_workflow_triggers(
        self, *, project_path: str, path: str
    ) -> list[str] | None:
        """Derive trigger event names from schedules + ``.gitlab-ci.yml``.

        *path* is ignored (GitLab has no per-workflow file). Fetches the
        project's active pipeline schedules and the raw ``.gitlab-ci.yml``
        on the configured target branch, then normalises them via
        :func:`_gitlab_ci_triggers`.
        """
        from urllib.parse import quote

        from ...config import target_branch_for

        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        ref = target_branch_for(self.settings, self._repo_config)  # type: ignore[attr-defined]

        sched = self._http.get(  # type: ignore[attr-defined]
            f"/projects/{pid}/pipeline_schedules",
            params={"scope": "active"},
        )
        sched.raise_for_status()
        has_active_schedule = bool(sched.json())

        ci_text = ""
        encoded = quote(".gitlab-ci.yml", safe="")
        f = self._http.get(  # type: ignore[attr-defined]
            f"/projects/{pid}/repository/files/{encoded}/raw",
            params={"ref": ref},
        )
        if f.status_code == 200:
            ci_text = f.text

        return _gitlab_ci_triggers(ci_text, has_active_schedule=has_active_schedule)
