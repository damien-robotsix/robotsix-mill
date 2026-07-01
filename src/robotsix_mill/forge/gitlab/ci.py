"""GitLab CI pipeline mixin — pipeline status, job log retrieval,
failure analysis.

Split from the monolithic ``gitlab.py``.  Defines
``GitLabForgeCIMixin`` that ``GitLabForge`` inherits from.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

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


class GitLabForgeCIMixin:
    """CI operations for GitLab — mixed into ``GitLabForge``.

    Expects ``self._http``, ``self.settings``, ``self._repo_config``,
    ``self._remote_url``, and ``self._resolve_project_id`` to exist on
    the final class.
    """

    def check_status(self, *, source_branch: str) -> dict | None:
        try:
            from .core import _parse_gitlab_project_path

            project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)  # type: ignore[attr-defined]
            if mr is None:
                return None

            pipeline = self._get_latest_pipeline(project_path, mr["iid"])
            if pipeline is None:
                return {"conclusion": None, "failing": [], "pending": []}

            status = pipeline.get("status", "")
            conclusion = _map_pipeline_status(status)

            failing: list[dict] = []
            if conclusion == "failure":
                failing = self._get_failed_jobs(project_path, pipeline["id"])

            return {"conclusion": conclusion, "failing": failing, "pending": []}
        except Exception:
            return None

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        from .core import _parse_gitlab_project_path

        project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
        return self._list_pipelines(
            project_path=project_path, branch=branch, head_sha=head_sha
        )

    def fetch_workflow_job_logs(self, *, run_id: int, full_log: bool = False) -> str:
        from .core import _parse_gitlab_project_path

        project_path = _parse_gitlab_project_path(self._remote_url)  # type: ignore[attr-defined]
        return self._fetch_pipeline_job_logs(
            project_path=project_path, run_id=run_id, full_log=full_log
        )

    # -- HTTP seams (monkeypatched in tests) -------------------------------

    def _get_latest_pipeline(self, project_path: str, mr_iid: int) -> dict | None:
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

    def _get_failed_jobs(self, project_path: str, pipeline_id: int) -> list[dict]:
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
                    }
                )

        return failing

    def _list_pipelines(
        self,
        *,
        project_path: str,
        branch: str | None,
        head_sha: str | None,
    ) -> list[dict]:
        """GET /projects/:id/pipelines?ref=…&sha=…&per_page=30."""
        pid = self._resolve_project_id(project_path)  # type: ignore[attr-defined]
        params: dict = {"per_page": 30}
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
