"""GitLab forge adapter — open a Merge Request for an already-pushed
branch via the GitLab REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.
"""

from __future__ import annotations

import re

from ..config import get_secrets
from .base import Forge, NotConfiguredError, RepoInfo
from .github import _ANSI_RE, _MAX_FAILED_JOBS, _capture_failure_window


def _build_headers(token: str) -> dict:
    return {
        "PRIVATE-TOKEN": token,
    }


def _parse_gitlab_project_path(remote_url: str) -> str:
    """Extract namespace/project path from a GitLab remote URL.

    Supports any GitLab host (gitlab.com, self-hosted instances, etc.).
    Accepts HTTPS (https://<host>/ns/project.git) and
    SSH (git@<host>:ns/project.git). Returns the path as-is
    (no URL encoding — callers encode when building URLs).
    """
    remote = remote_url or ""
    # HTTPS: https://<host>/ns/project.git
    m = re.match(r"https://(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?$", remote)
    if m:
        return m.group("path")
    # SSH: git@<host>:ns/project.git
    m = re.match(r"git@(?P<host>[^:]+):(?P<path>.+?)(?:\.git)?$", remote)
    if m:
        return m.group("path")
    raise RuntimeError(f"cannot parse GitLab project path from {remote_url!r}")


class GitLabForge(Forge):
    """GitLab adapter — opens MRs, queries pipeline status, and merges via the GitLab API."""

    def __init__(self, settings, repo_config=None):
        super().__init__(settings)
        self._repo_config = repo_config

    @property
    def _remote_url(self) -> str:
        """Effective remote URL: per-repo override, else global setting."""
        if self._repo_config is not None:
            remote = getattr(self._repo_config, "forge_remote_url", None)
            if remote:
                return remote
        return self.settings.forge_remote_url or ""

    # ------------------------------------------------------------------
    # Public methods mandated by Forge ABC
    # ------------------------------------------------------------------

    def open_merge_request(self, *, source_branch: str, title: str, body: str) -> str:
        s = self.settings
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._create_mr(
            project_path=project_path,
            source_branch=source_branch,
            target_branch=s.forge_target_branch,
            title=title,
            description=body,
        )

    def pr_status(self, *, source_branch: str) -> dict | None:
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)
            if mr is None:
                return None
            return {
                "merged": mr["state"] == "merged",
                "state": mr["state"],
                "url": mr["web_url"],
                "mergeable": _map_merge_status(mr.get("merge_status", "")),
                "sha": (mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha", ""),
                "number": mr["iid"],
            }
        except Exception:
            return None

    def pr_status_by_url(self, *, url: str) -> dict | None:
        m = re.search(r"merge_requests/(\d+)", url or "")
        if not m:
            return None
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
            mr = self._get_mr_by_iid(project_path=project_path, mr_iid=int(m.group(1)))
            if mr is None:
                return None
            return {
                "merged": mr["state"] == "merged",
                "state": mr["state"],
                "url": mr["web_url"],
                "mergeable": _map_merge_status(mr.get("merge_status", "")),
                "sha": (mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha", ""),
                "number": mr["iid"],
            }
        except Exception:
            return None

    def check_status(self, *, source_branch: str) -> dict | None:
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
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
        except Exception:
            return None

    def pr_files(self, *, source_branch: str) -> list[dict]:
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)
            if mr is None:
                return []
            return self._mr_changes(
                project_path=project_path,
                mr_iid=mr["iid"],
            )
        except Exception:
            return []

    def merge_pr(self, *, source_branch: str) -> dict:
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)
            if mr is None:
                return {"merged": False, "reason": "MR not found"}
            return self._merge_mr(project_path, mr["iid"])
        except Exception as e:
            return {"merged": False, "reason": str(e)}

    def list_pr_reviews(self, *, source_branch: str) -> list[dict]:
        project_path = _parse_gitlab_project_path(self._remote_url)
        mr = self._find_mr(project_path=project_path, source_branch=source_branch)
        if mr is None:
            return []
        notes = self._mr_notes(project_path=project_path, mr_iid=mr["iid"])
        # GitLab has no GitHub-style review object; the faithful mapping is the
        # MR's general (non-system) notes WITHOUT a position (inline comments —
        # which carry a position — are handled by list_review_comments).
        return [
            {
                "id": n["id"],
                "author": (n.get("author") or {}).get("username", ""),
                "created_at": n.get("created_at", ""),
                "body": n.get("body") or "",
            }
            for n in notes
            if n.get("system") is False and not n.get("position")
        ]

    def list_review_comments(self, *, source_branch: str) -> list[dict]:
        project_path = _parse_gitlab_project_path(self._remote_url)
        mr = self._find_mr(project_path=project_path, source_branch=source_branch)
        if mr is None:
            return []
        notes = self._mr_notes(project_path=project_path, mr_iid=mr["iid"])
        result: list[dict] = []
        for n in notes:
            position = n.get("position")
            if not position:
                continue
            result.append(
                {
                    "id": n["id"],
                    "author": (n.get("author") or {}).get("username", ""),
                    "created_at": n.get("created_at", ""),
                    "body": n.get("body") or "",
                    "file_path": position.get("new_path")
                    or position.get("old_path", ""),
                    "line": position.get("new_line"),
                    # GitLab notes don't carry a diff hunk.
                    "diff_hunk": "",
                }
            )
        return result

    def pr_review_status(self, *, source_branch: str) -> dict | None:
        project_path = _parse_gitlab_project_path(self._remote_url)
        mr = self._find_mr(project_path=project_path, source_branch=source_branch)
        if mr is None:
            return None
        return self._pr_review_status(project_path=project_path, mr_iid=mr["iid"])

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._list_pipelines(
            project_path=project_path, branch=branch, head_sha=head_sha
        )

    def fetch_workflow_job_logs(self, *, run_id: int) -> str:
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._fetch_pipeline_job_logs(project_path=project_path, run_id=run_id)

    def create_repo(
        self, *, name: str, owner: str, private: bool, description: str
    ) -> RepoInfo:
        if not self.settings.enable_repo_creation:
            raise NotConfiguredError(
                "Repo creation is disabled. Set enable_repo_creation=True "
                "and verify the GitLab token has api scope with permission to "
                "create projects in the target namespace."
            )
        return self._create_project(
            name=name,
            owner=owner,
            private=private,
            description=description,
        )

    # ------------------------------------------------------------------
    # HTTP seams (monkeypatched in tests)
    # ------------------------------------------------------------------

    def _resolve_project_id(self, project_path: str) -> int:
        """GET /projects/:encoded_path → project id."""
        import httpx

        from urllib.parse import quote

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        encoded = quote(project_path, safe="")
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{api}/projects/{encoded}", headers=headers)
            if r.status_code == 200:
                return r.json()["id"]
            raise RuntimeError(
                f"GitLab project lookup failed: {r.status_code} {r.text[:300]}"
            )

    def _find_mr(
        self, project_path: str, source_branch: str, state: str = "all"
    ) -> dict | None:
        """GET /projects/:id/merge_requests?source_branch=…&state=…&per_page=1."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        pid = self._resolve_project_id(project_path)
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/merge_requests",
                headers=headers,
                params={
                    "source_branch": source_branch,
                    "state": state,
                    "per_page": 1,
                },
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                return None
            return items[0]

    def _get_mr_by_iid(self, *, project_path: str, mr_iid: int) -> dict | None:
        """GET /projects/:id/merge_requests/:iid → MR dict (by IID).

        Resolves a recorded MR web url to its current status independent
        of whether the source branch still exists, mirroring the GitHub
        ``_get_pr_by_number`` seam."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        pid = self._resolve_project_id(project_path)
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/merge_requests/{mr_iid}",
                headers=headers,
            )
            r.raise_for_status()
            return r.json()

    def _get_latest_pipeline(self, project_path: str, mr_iid: int) -> dict | None:
        """GET /projects/:id/merge_requests/:iid/pipelines?per_page=1."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
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

    def _get_failed_jobs(self, project_path: str, pipeline_id: int) -> list[dict]:
        """GET /projects/:id/pipelines/:pipeline_id/jobs?scope=failed&per_page=20."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
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

    def _mr_notes(self, *, project_path: str, mr_iid: int) -> list[dict]:
        """GET /projects/:id/merge_requests/:iid/notes?per_page=100."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        pid = self._resolve_project_id(project_path)
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/merge_requests/{mr_iid}/notes",
                headers=headers,
                params={"per_page": 100},
            )
            r.raise_for_status()
            return r.json()

    def _pr_review_status(self, *, project_path: str, mr_iid: int) -> dict:
        """Aggregate review state from MR approvals + general/inline notes.

        State heuristic (the simpler documented option): ``"APPROVED"`` when
        the MR approvals object reports ``approved``, ``"COMMENTED"`` when any
        non-system notes exist, else ``"PENDING"``.
        """
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        pid = self._resolve_project_id(project_path)

        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/merge_requests/{mr_iid}/approvals",
                headers=headers,
            )
            approved = bool(r.json().get("approved")) if r.status_code == 200 else False

        notes = self._mr_notes(project_path=project_path, mr_iid=mr_iid)
        relevant = [n for n in notes if n.get("system") is False]

        if approved:
            state = "APPROVED"
        elif relevant:
            state = "COMMENTED"
        else:
            state = "PENDING"

        comments: list[dict] = []
        for n in relevant:
            position = n.get("position")
            comments.append(
                {
                    "body": n.get("body") or "",
                    "path": (position.get("new_path") or position.get("old_path", ""))
                    if position
                    else "",
                    "line": position.get("new_line") if position else None,
                    "review_state": state,
                }
            )

        files = self._mr_changes(project_path=project_path, mr_iid=mr_iid)
        return {
            "state": state,
            "comments": comments,
            "files": [f["path"] for f in files],
        }

    def _list_pipelines(
        self,
        *,
        project_path: str,
        branch: str | None,
        head_sha: str | None,
    ) -> list[dict]:
        """GET /projects/:id/pipelines?ref=…&sha=…&per_page=30."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        pid = self._resolve_project_id(project_path)
        params: dict = {"per_page": 30}
        if branch is not None:
            params["ref"] = branch
        if head_sha is not None:
            params["sha"] = head_sha

        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/projects/{pid}/pipelines",
                headers=headers,
                params=params,
            )
            r.raise_for_status()
            raw = r.json()
        return [
            {
                "id": p["id"],
                # GitLab pipelines have no name; surface the ref instead.
                "name": p.get("ref", ""),
                "workflow_id": None,
                "head_sha": p.get("sha", ""),
                "conclusion": _map_pipeline_status(p.get("status", "")),
                "html_url": p.get("web_url", ""),
                "created_at": p.get("created_at", ""),
            }
            for p in raw
        ]

    def _fetch_pipeline_job_logs(self, *, project_path: str, run_id: int) -> str:
        """Concatenate ANSI-stripped, size-capped traces of failed jobs in a
        pipeline.  *run_id* is a GitLab pipeline id.  Returns ``""`` when there
        are no failed jobs.
        """
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        pid = self._resolve_project_id(project_path)

        with httpx.Client(timeout=30) as c:
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

        with httpx.Client(timeout=30) as c:
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
                clean = _capture_failure_window(clean, log_max)

                parts.append(f"### Job: {job_name} (id={job_id})\n")
                parts.append(clean)
                parts.append("\n")

        return "\n".join(parts)

    def _create_project(
        self,
        *,
        name: str,
        owner: str,
        private: bool,
        description: str,
    ) -> RepoInfo:
        """POST /projects → RepoInfo. Resolves *owner* to a namespace id."""
        import httpx

        from urllib.parse import quote

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        payload: dict = {
            "name": name,
            "visibility": "private" if private else "public",
            "description": description,
        }

        with httpx.Client(timeout=30) as c:
            if owner:
                ns = c.get(
                    f"{api}/namespaces/{quote(owner, safe='')}",
                    headers=headers,
                )
                if ns.status_code != 200:
                    raise RuntimeError(
                        f"GitLab namespace lookup for {owner!r} failed: "
                        f"{ns.status_code} {ns.text[:300]}"
                    )
                payload["namespace_id"] = ns.json()["id"]

            r = c.post(f"{api}/projects", headers=headers, json=payload)
            if r.status_code == 201:
                data = r.json()
                return RepoInfo(
                    id=data["id"],
                    name=data["path"] or data["name"],
                    clone_url=data["http_url_to_repo"],
                    html_url=data["web_url"],
                )
            if r.status_code in (400, 409) and (
                "already been taken" in (r.text or "").lower()
                or "already exists" in (r.text or "").lower()
            ):
                raise RuntimeError(
                    f"GitLab project '{name}' already exists under "
                    f"namespace '{owner}': {r.text[:300]}"
                )
            raise RuntimeError(
                f"GitLab repo create failed: {r.status_code} {r.text[:300]}"
            )

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
        headers = _build_headers(get_secrets().forge_token or "")
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
                try:
                    existing = self._find_mr(
                        project_path=project_path,
                        source_branch=source_branch,
                        state="opened",
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"GitLab MR create failed: 409 (conflict); "
                        f"lookup for existing MR also failed: {exc}"
                    ) from exc
                if existing:
                    return existing["web_url"]
            raise RuntimeError(
                f"GitLab MR create failed: {r.status_code} {r.text[:300]}"
            )

    def _mr_changes(
        self,
        project_path: str,
        mr_iid: int,
    ) -> list[dict]:
        """GET /projects/:id/merge_requests/:iid/changes → normalized file list."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
        pid = self._resolve_project_id(project_path)
        try:
            with httpx.Client(timeout=30) as c:
                r = c.get(
                    f"{api}/projects/{pid}/merge_requests/{mr_iid}/changes",
                    headers=headers,
                )
                r.raise_for_status()
                changes = r.json().get("changes", [])
        except Exception:
            return []

        result: list[dict] = []
        for ch in changes:
            path = ch.get("new_path", ch.get("old_path", ""))
            if ch.get("new_file"):
                status = "added"
            elif ch.get("deleted_file"):
                status = "removed"
            elif ch.get("renamed_file"):
                status = "renamed"
            else:
                status = "modified"

            diff = ch.get("diff", "")
            additions = 0
            deletions = 0
            if diff:
                for line in diff.split("\n"):
                    if line.startswith("+") and not line.startswith("+++"):
                        additions += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        deletions += 1

            result.append(
                {
                    "path": path,
                    "status": status,
                    "additions": additions,
                    "deletions": deletions,
                }
            )
        return result

    def _merge_mr(self, project_path: str, mr_iid: int) -> dict:
        """PUT /projects/:id/merge_requests/:iid/merge with MWPS + squash."""
        import httpx

        s = self.settings
        api = s.gitlab_api_url.rstrip("/")
        headers = _build_headers(get_secrets().forge_token or "")
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
        "pending",
        "running",
        "created",
        "waiting_for_resource",
        "preparing",
        "manual",
        "scheduled",
    ):
        return "pending"
    return None
