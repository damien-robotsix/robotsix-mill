"""GitLab forge adapter — open a Merge Request for an already-pushed
branch via the GitLab REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.
"""

from __future__ import annotations

import re

from .base import Forge


def _build_headers(token: str) -> dict:
    return {
        "PRIVATE-TOKEN": token,
    }


def _parse_gitlab_project_path(remote_url: str) -> str:
    """Extract namespace/project path from a GitLab remote URL.

    Accepts HTTPS (https://gitlab.com/ns/project.git) and
    SSH (git@gitlab.com:ns/project.git). Returns the path as-is
    (no URL encoding — callers encode when building URLs).
    """
    remote = remote_url or ""
    # HTTPS: https://gitlab.com/ns/project.git
    m = re.match(r"https://gitlab\.com/(?P<path>.+?)(?:\.git)?$", remote)
    if m:
        return m.group("path")
    # SSH: git@gitlab.com:ns/project.git
    m = re.match(r"git@gitlab\.com:(?P<path>.+?)(?:\.git)?$", remote)
    if m:
        return m.group("path")
    raise RuntimeError(f"cannot parse GitLab project path from {remote_url!r}")


class GitLabForge(Forge):
    # ------------------------------------------------------------------
    # Public methods mandated by Forge ABC
    # ------------------------------------------------------------------

    def open_merge_request(
        self, *, source_branch: str, title: str, body: str
    ) -> str:
        s = self.settings
        project_path = _parse_gitlab_project_path(s.forge_remote_url or "")
        return self._create_mr(
            project_path=project_path,
            source_branch=source_branch,
            target_branch=s.forge_target_branch,
            title=title,
            description=body,
        )

    def pr_status(self, *, source_branch: str) -> dict | None:
        s = self.settings
        project_path = _parse_gitlab_project_path(s.forge_remote_url or "")
        mr = self._find_mr(project_path=project_path, source_branch=source_branch)
        if mr is None:
            return None
        return {
            "merged": mr["state"] == "merged",
            "state": mr["state"],
            "url": mr["web_url"],
            "mergeable": _map_merge_status(mr.get("merge_status", "")),
            "sha": (mr.get("diff_refs") or {}).get("head_sha")
                   or mr.get("sha", ""),
            "number": mr["iid"],
        }

    def check_status(self, *, source_branch: str) -> dict | None:
        s = self.settings
        project_path = _parse_gitlab_project_path(s.forge_remote_url or "")
        mr = self._find_mr(project_path=project_path, source_branch=source_branch)
        if mr is None:
            return None

        pipeline = self._get_latest_pipeline(project_path, mr["iid"])
        if pipeline is None:
            return {"conclusion": None, "failing": []}

        status = pipeline.get("status", "")
        conclusion = _map_pipeline_status(status)

        failing: list[dict] = []
        if conclusion == "failure":
            failing = self._get_failed_jobs(project_path, pipeline["id"])

        return {"conclusion": conclusion, "failing": failing}

    def merge_pr(self, *, source_branch: str) -> dict:
        s = self.settings
        project_path = _parse_gitlab_project_path(s.forge_remote_url or "")
        mr = self._find_mr(project_path=project_path, source_branch=source_branch)
        if mr is None:
            return {"merged": False, "reason": "MR not found"}
        return self._merge_mr(project_path, mr["iid"])

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        raise NotImplementedError("GitLab forge adapter not implemented yet")

    def fetch_workflow_job_logs(self, *, run_id: int) -> str:
        raise NotImplementedError("GitLab forge adapter not implemented yet")

    # ------------------------------------------------------------------
    # HTTP seams (monkeypatched in tests)
    # ------------------------------------------------------------------

    def _resolve_project_id(self, project_path: str) -> int:
        """GET /projects/:encoded_path → project id."""
        import httpx

        from urllib.parse import quote

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(s.forge_token or "")
        encoded = quote(project_path, safe="")
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{api}/projects/{encoded}", headers=headers)
            if r.status_code == 200:
                return r.json()["id"]
            raise RuntimeError(
                f"GitLab project lookup failed: {r.status_code} "
                f"{r.text[:300]}"
            )

    def _find_mr(self, project_path: str, source_branch: str) -> dict | None:
        """GET /projects/:id/merge_requests?source_branch=…&state=all&per_page=1."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(s.forge_token or "")
        pid = self._resolve_project_id(project_path)
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/merge_requests",
                headers=headers,
                params={
                    "source_branch": source_branch,
                    "state": "all",
                    "per_page": 1,
                },
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                return None
            return items[0]

    def _get_latest_pipeline(
        self, project_path: str, mr_iid: int
    ) -> dict | None:
        """GET /projects/:id/merge_requests/:iid/pipelines?per_page=1."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(s.forge_token or "")
        pid = self._resolve_project_id(project_path)
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/merge_requests/{mr_iid}/pipelines",
                headers=headers,
                params={"per_page": 1},
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                return None
            return items[0]

    def _get_failed_jobs(
        self, project_path: str, pipeline_id: int
    ) -> list[dict]:
        """GET /projects/:id/pipelines/:pipeline_id/jobs?scope=failed&per_page=20."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(s.forge_token or "")
        pid = self._resolve_project_id(project_path)
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/pipelines/{pipeline_id}/jobs",
                headers=headers,
                params={"scope": "failed", "per_page": 20},
            )
            r.raise_for_status()
            jobs = r.json()
        return [
            {
                "name": j.get("name", ""),
                "summary": None,
                "text": None,
                "annotations": [],
            }
            for j in jobs
        ]

    def _create_mr(
        self,
        *,
        project_path: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> str:
        """POST /projects/:id/merge_requests → web_url. Falls back on 409."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(s.forge_token or "")
        pid = self._resolve_project_id(project_path)
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{api}/projects/{pid}/merge_requests",
                headers=headers,
                json=payload,
            )
            if r.status_code == 201:
                return r.json()["web_url"]
            if r.status_code == 409:
                # MR already exists — find it and return its web_url
                existing = self._find_mr(
                    project_path=project_path, source_branch=source_branch
                )
                if existing:
                    return existing["web_url"]
            raise RuntimeError(
                f"GitLab MR create failed: {r.status_code} "
                f"{r.text[:300]}"
            )

    def _merge_mr(self, project_path: str, mr_iid: int) -> dict:
        """PUT /projects/:id/merge_requests/:iid/merge with MWPS + squash."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(s.forge_token or "")
        pid = self._resolve_project_id(project_path)
        payload = {
            "merge_when_pipeline_succeeds": True,
            "squash": True,
            "should_remove_source_branch": False,
        }
        try:
            with httpx.Client(timeout=30) as c:
                r = c.put(
                    f"{api}/projects/{pid}/merge_requests/{mr_iid}/merge",
                    headers=headers,
                    json=payload,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("state") == "merged":
                        return {"merged": True, "reason": "merged"}
                    return {
                        "merged": False,
                        "reason": "merge_when_pipeline_succeeds set; awaiting pipeline",
                    }
                if r.status_code == 405:
                    return {
                        "merged": False,
                        "reason": "merge not allowed (branch protection?)",
                    }
                if r.status_code == 409:
                    return {"merged": False, "reason": "MR is not mergeable"}
                return {
                    "merged": False,
                    "reason": f"HTTP {r.status_code}: {r.text[:200]}",
                }
        except Exception as e:
            return {"merged": False, "reason": str(e)}


def _map_merge_status(merge_status: str) -> bool | None:
    """Map GitLab merge_status to the standard mergeable field."""
    if merge_status == "can_be_merged":
        return True
    if merge_status == "cannot_be_merged":
        return False
    # "checking", "unchecked" → None (treat as mergeable per base.py docstring)
    return None


def _map_pipeline_status(status: str) -> str | None:
    """Map GitLab pipeline status to standard conclusion."""
    if status == "success":
        return "success"
    if status in ("failed", "canceled"):
        return "failure"
    if status in (
        "pending", "running", "created", "waiting_for_resource",
        "preparing", "manual", "scheduled",
    ):
        return "pending"
    return None
