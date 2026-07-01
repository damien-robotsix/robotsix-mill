"""GitLab forge adapter — open a Merge Request for an already-pushed
branch via the GitLab REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import httpx

from ._http import _ApiClient
from ._log_utils import _capture_failure_window, _strip_runner_noise
from .auth import gitlab_token
from .base import BranchInfo, Forge, NotConfiguredError, RepoInfo
from .github import _parse_iso_utc
from .github_ci import _ANSI_RE, _MAX_FAILED_JOBS

T = TypeVar("T")

# Earliest-failure markers in a GitLab CI job log.  GitLab jobs don't
# emit GitHub Actions–style ``##[error]`` lines; instead we match the
# patterns that GitLab Runner / common build tools emit on failure.
_LOG_FAILURE_RE = re.compile(
    r"(?:^ERROR:\s|^ERROR\[|Job failed|exit code [1-9]|"
    r"^\s*FAIL\b|FAILED\s|fatal:)",
    re.MULTILINE,
)


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
        self._http = _ApiClient(
            settings,
            repo_config,
            "gitlab_api_url",
            lambda s, rc: _build_headers(gitlab_token()),
        )

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

    def open_merge_request(
        self,
        *,
        source_branch: str,
        title: str,
        body: str,
        head_repo: str | None = None,
    ) -> str:
        if head_repo is not None:
            raise NotImplementedError(
                "cross-fork merge requests are not supported by the GitLab "
                "adapter; cross_repo_target is GitHub-only"
            )
        s = self.settings
        from ..config import target_branch_for  # lazy: avoid import cycle

        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._create_mr(
            project_path=project_path,
            source_branch=source_branch,
            target_branch=target_branch_for(s, self._repo_config),
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
                return {"conclusion": None, "failing": [], "pending": []}

            status = pipeline.get("status", "")
            conclusion = _map_pipeline_status(status)

            failing: list[dict] = []
            if conclusion == "failure":
                failing = self._get_failed_jobs(project_path, pipeline["id"])

            return {"conclusion": conclusion, "failing": failing, "pending": []}
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

    def close_pr(self, *, source_branch: str) -> bool:
        """Close/decline the open MR for *source_branch* without merging.

        Returns ``True`` on success, ``False`` when the MR is not found
        or already closed.  Never raises.
        """
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)
            if mr is None:
                return False
            return self._close_mr(project_path, mr["iid"])
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "close_pr failed for branch %s", source_branch
            )
            return False

    def post_pr_comment(self, *, source_branch: str, body: str) -> bool:
        """Post a plain comment on the open MR for *source_branch*.

        Returns ``True`` on success, ``False`` when the MR is not found.
        Never raises.
        """
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)
            if mr is None:
                return False
            return self._post_mr_note(project_path, mr["iid"], body)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "post_pr_comment failed for branch %s", source_branch
            )
            return False

    def update_branch(self, *, source_branch: str) -> dict:
        try:
            project_path = _parse_gitlab_project_path(self._remote_url)
            mr = self._find_mr(project_path=project_path, source_branch=source_branch)
            if mr is None:
                return {"updated": False, "reason": "MR not found"}
            return self._rebase_mr(project_path, mr["iid"])
        except Exception as e:
            return {"updated": False, "reason": str(e)}

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

    def fetch_workflow_job_logs(self, *, run_id: int, full_log: bool = False) -> str:
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._fetch_pipeline_job_logs(
            project_path=project_path, run_id=run_id, full_log=full_log
        )

    def create_repo(
        self, *, name: str, owner: str, private: bool | None = None, description: str
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

    def fork_repo(
        self,
        *,
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> RepoInfo:
        if not self.settings.enable_repo_creation:
            raise NotConfiguredError(
                "Repo creation is disabled. Set enable_repo_creation=True "
                "and verify the GitLab token has api scope with permission to "
                "create projects in the target namespace."
            )
        return self._fork_repo(
            source_owner=source_owner,
            source_repo=source_repo,
            target_namespace=target_namespace,
        )

    def delete_branch(self, *, branch: str) -> bool:
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._delete_branch(project_path, branch)

    def list_branches(self) -> list[BranchInfo]:
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._list_branches(project_path)

    def list_open_pr_branches(self) -> set[str]:
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._list_open_pr_branches(project_path)

    def list_open_prs(self) -> list[dict[str, Any]]:
        project_path = _parse_gitlab_project_path(self._remote_url)
        return self._list_open_prs(project_path)

    # ------------------------------------------------------------------
    # HTTP seams (monkeypatched in tests)
    # ------------------------------------------------------------------

    def _resolve_project_id(self, project_path: str) -> int:
        """GET /projects/:encoded_path → project id."""
        from urllib.parse import quote

        encoded = quote(project_path, safe="")
        r = self._http.get(f"/projects/{encoded}")
        if r.status_code == 200:
            return r.json()["id"]
        raise RuntimeError(
            f"GitLab project lookup failed: {r.status_code} {r.text[:300]}"
        )

    def _find_mr(
        self, project_path: str, source_branch: str, state: str = "all"
    ) -> dict | None:
        """GET /projects/:id/merge_requests?source_branch=…&state=…&per_page=1."""
        pid = self._resolve_project_id(project_path)
        r = self._http.get(
            f"/projects/{pid}/merge_requests",
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
        pid = self._resolve_project_id(project_path)
        r = self._http.get(
            f"/projects/{pid}/merge_requests/{mr_iid}",
        )
        r.raise_for_status()
        return r.json()

    def _get_latest_pipeline(self, project_path: str, mr_iid: int) -> dict | None:
        """GET /projects/:id/merge_requests/:iid/pipelines?per_page=1."""
        pid = self._resolve_project_id(project_path)
        r = self._http.get(
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
        pid = self._resolve_project_id(project_path)
        log_max = self.settings.ci_log_max_bytes

        with self._http.client() as (c, api, headers):
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

    def _mr_notes(self, *, project_path: str, mr_iid: int) -> list[dict]:
        """GET /projects/:id/merge_requests/:iid/notes?per_page=100."""
        pid = self._resolve_project_id(project_path)
        r = self._http.get(
            f"/projects/{pid}/merge_requests/{mr_iid}/notes",
            params={"per_page": 100},
        )
        r.raise_for_status()
        return r.json()

    def _pr_review_status(self, *, project_path: str, mr_iid: int) -> dict:
        """Aggregate review state from MR approvals + general/inline notes.

        Five-state heuristic derived from the approvals object plus the
        notes already fetched by :meth:`_mr_notes` (no extra HTTP call).
        Precedence (first match wins): an unresolved blocking discussion
        (a resolvable note that is not resolved) → ``"CHANGES_REQUESTED"``
        regardless of approval; else ``"APPROVED"`` when the approvals
        object reports ``approved``; else ``"DISMISSED"`` when a system
        note records a revoked approval (best-effort, from the note body,
        e.g. "unapproved this merge request"); else ``"COMMENTED"`` when
        any non-system notes exist; else ``"PENDING"``.
        """
        pid = self._resolve_project_id(project_path)

        with self._http.client() as (c, api, headers):
            r = c.get(
                f"{api}/projects/{pid}/merge_requests/{mr_iid}/approvals",
                headers=headers,
            )
            approved = bool(r.json().get("approved")) if r.status_code == 200 else False

        notes = self._mr_notes(project_path=project_path, mr_iid=mr_iid)
        relevant = [n for n in notes if n.get("system") is False]

        # An unresolved blocking discussion → reviewer wants changes.
        unresolved = any(n.get("resolvable") and not n.get("resolved") for n in notes)
        # A previously-granted approval that was later revoked (best-effort,
        # from GitLab's system note body, e.g. "unapproved this merge request").
        unapproved = any(
            n.get("system") is True and "unapproved" in (n.get("body") or "").lower()
            for n in notes
        )

        if unresolved:
            state = "CHANGES_REQUESTED"
        elif approved:
            state = "APPROVED"
        elif unapproved:
            state = "DISMISSED"
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
        pid = self._resolve_project_id(project_path)
        params: dict = {"per_page": 30}
        if branch is not None:
            params["ref"] = branch
        if head_sha is not None:
            params["sha"] = head_sha

        r = self._http.get(
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
        s = self.settings
        pid = self._resolve_project_id(project_path)

        with self._http.client() as (c, api, headers):
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

        with self._http.client() as (c, api, headers):
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

    # -- shared helpers ---------------------------------------------------

    @staticmethod
    def _to_repo_info(data: dict) -> RepoInfo:
        """Build a RepoInfo from a GitLab 201 project-creation response."""
        return RepoInfo(
            id=data["id"],
            name=data["path"] or data["name"],
            clone_url=data["http_url_to_repo"],
            html_url=data["web_url"],
        )

    def _create_project(
        self,
        *,
        name: str,
        owner: str,
        private: bool | None = None,
        description: str,
    ) -> RepoInfo:
        """POST /projects → RepoInfo. Resolves *owner* to a namespace id."""
        from urllib.parse import quote

        if private is None:
            private = self.settings.repo_visibility_default == "private"

        from ..config import get_secrets

        # Prefer a dedicated repo-creation token when configured; fall
        # back to the normal forge token otherwise.  Mirrors the GitHub
        # pattern where App installation tokens cannot create repos.
        token = get_secrets().forge_repo_create_token or gitlab_token()
        custom_headers = _build_headers(token)

        payload: dict = {
            "name": name,
            "visibility": "private" if private else "public",
            "description": description,
        }

        with self._http.client() as (c, api, _headers):
            if owner:
                ns = c.get(
                    f"{api}/namespaces/{quote(owner, safe='')}",
                    headers=custom_headers,
                )
                if ns.status_code != 200:
                    raise RuntimeError(
                        f"GitLab namespace lookup for {owner!r} failed: "
                        f"{ns.status_code} {ns.text[:300]}"
                    )
                payload["namespace_id"] = ns.json()["id"]

            r = c.post(f"{api}/projects", headers=custom_headers, json=payload)
            if r.status_code == 201:
                return self._to_repo_info(r.json())
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

    def _fork_repo(
        self,
        *,
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> RepoInfo:
        """POST /projects/:id/fork → RepoInfo."""
        from ..config import get_secrets

        # Prefer a dedicated repo-creation token when configured; fall
        # back to the normal forge token otherwise.
        token = get_secrets().forge_repo_create_token or gitlab_token()
        custom_headers = _build_headers(token)

        source_path = f"{source_owner}/{source_repo}"
        pid = self._resolve_project_id(source_path)
        payload: dict = {}
        if target_namespace is not None:
            payload["namespace"] = target_namespace

        with self._http.client() as (c, api, _headers):
            r = c.post(
                f"{api}/projects/{pid}/fork",
                headers=custom_headers,
                json=payload,
            )
            if r.status_code == 201:
                return self._to_repo_info(r.json())
            if r.status_code == 409 and (
                "already been taken" in (r.text or "").lower()
                or "already exists" in (r.text or "").lower()
            ):
                raise RuntimeError(
                    f"GitLab fork failed: a fork of '{source_path}' already "
                    f"exists in the target namespace: {r.text[:300]}"
                )
            raise RuntimeError(f"GitLab fork failed: {r.status_code} {r.text[:300]}")

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
        pid = self._resolve_project_id(project_path)
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        r = self._http.post(
            f"/projects/{pid}/merge_requests",
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
        raise RuntimeError(f"GitLab MR create failed: {r.status_code} {r.text[:300]}")

    def _mr_changes(
        self,
        project_path: str,
        mr_iid: int,
    ) -> list[dict]:
        """GET /projects/:id/merge_requests/:iid/changes → normalized file list."""
        pid = self._resolve_project_id(project_path)
        try:
            r = self._http.get(
                f"/projects/{pid}/merge_requests/{mr_iid}/changes",
            )
            r.raise_for_status()
            changes = r.json().get("changes", [])
        except Exception:
            return []

        result: list[dict[str, Any]] = []
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

    def _merge_mr(self, project_path: str, mr_iid: int) -> dict[str, Any]:
        """PUT /projects/:id/merge_requests/:iid/merge with MWPS + squash."""
        pid = self._resolve_project_id(project_path)
        payload = {
            "merge_when_pipeline_succeeds": True,
            "squash": True,
            "should_remove_source_branch": False,
        }
        try:
            r = self._http.put(
                f"/projects/{pid}/merge_requests/{mr_iid}/merge",
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

    def _close_mr(self, project_path: str, mr_iid: int) -> bool:
        """PUT /projects/:id/merge_requests/:iid with state_event=close."""
        import logging

        logger = logging.getLogger(__name__)
        try:
            pid = self._resolve_project_id(project_path)
            r = self._http.put(
                f"/projects/{pid}/merge_requests/{mr_iid}",
                json={"state_event": "close"},
            )
            if r.status_code == 200:
                return True
            logger.info(
                "close_pr HTTP %s for %s MR !%d: %s",
                r.status_code,
                project_path,
                mr_iid,
                r.text[:200],
            )
            return False
        except Exception:
            logger.exception(
                "close_pr failed for %s MR !%d",
                project_path,
                mr_iid,
            )
            return False

    def _post_mr_note(self, project_path: str, mr_iid: int, body: str) -> bool:
        """POST /projects/:id/merge_requests/:iid/notes with body."""
        import logging

        logger = logging.getLogger(__name__)
        try:
            pid = self._resolve_project_id(project_path)
            r = self._http.post(
                f"/projects/{pid}/merge_requests/{mr_iid}/notes",
                json={"body": body},
            )
            if r.status_code == 201:
                return True
            logger.info(
                "post_pr_comment HTTP %s for %s MR !%d: %s",
                r.status_code,
                project_path,
                mr_iid,
                r.text[:200],
            )
            return False
        except Exception:
            logger.exception(
                "post_pr_comment failed for %s MR !%d",
                project_path,
                mr_iid,
            )
            return False

    def _rebase_mr(self, project_path: str, mr_iid: int) -> dict[str, Any]:
        """PUT /projects/:id/merge_requests/:iid/rebase to merge the target
        branch tip into the MR branch so its pipeline re-runs against the
        current base."""
        pid = self._resolve_project_id(project_path)
        try:
            r = self._http.put(
                f"/projects/{pid}/merge_requests/{mr_iid}/rebase",
            )
            if r.status_code == 202:
                return {"updated": True, "reason": "rebase accepted"}
            if r.status_code == 403:
                return {
                    "updated": False,
                    "reason": "rebase forbidden (insufficient permissions?)",
                }
            if r.status_code == 409:
                return {"updated": False, "reason": "MR is not mergeable"}
            return {
                "updated": False,
                "reason": f"HTTP {r.status_code}: {r.text[:200]}",
            }
        except Exception as e:
            return {"updated": False, "reason": str(e)}

    def _delete_branch(self, project_path: str, branch: str) -> bool:
        """DELETE /projects/:id/repository/branches/:branch."""
        from urllib.parse import quote

        try:
            pid = self._resolve_project_id(project_path)
            encoded = quote(branch, safe="")
            r = self._http.delete(
                f"/projects/{pid}/repository/branches/{encoded}",
            )
            # 204 = deleted; 404 = branch already gone — desired end state.
            if r.status_code in (204, 404):
                return True
            return False
        except Exception:
            return False

    def _paginated_get(
        self,
        url_suffix: str,
        *,
        params: dict[str, Any],
        item_fn: Callable[[dict[str, Any]], T],
    ) -> Iterator[T]:
        """Paginate through a GitLab API endpoint, yielding items via *item_fn*.

        Each dict from the JSON array response is passed to *item_fn*; the
        result is yielded.  Pagination stops when fewer than 100 items are
        returned (last page).
        """
        with self._http.client() as (c, api, headers):
            page = 1
            while True:
                r = c.get(
                    f"{api}{url_suffix}",
                    headers=headers,
                    params={"per_page": 100, "page": page, **params},
                )
                r.raise_for_status()
                items: list[dict[str, Any]] = r.json()
                for item in items:
                    yield item_fn(item)
                if len(items) < 100:
                    break
                page += 1

    def _list_branches(self, project_path: str) -> list[BranchInfo]:
        """GET /projects/:id/repository/branches?per_page=100 (paginated)."""
        out: list[BranchInfo] = []
        try:
            pid = self._resolve_project_id(project_path)

            def _mk(b: dict[str, Any]) -> BranchInfo:
                date = (b.get("commit") or {}).get("committed_date")
                return BranchInfo(
                    name=b["name"],
                    last_commit_at=_parse_iso_utc(date),
                    is_protected=bool(b.get("protected")),
                )

            for bi in self._paginated_get(
                f"/projects/{pid}/repository/branches",
                params={},
                item_fn=_mk,
            ):
                out.append(bi)
        except Exception:
            return []
        return out

    def _list_open_pr_branches(self, project_path: str) -> set[str]:
        """GET /projects/:id/merge_requests?state=opened (paginated)."""
        out: set[str] = set()
        try:
            pid = self._resolve_project_id(project_path)

            def _src_branch(mr: dict[str, Any]) -> str | None:
                return mr.get("source_branch")

            for ref in self._paginated_get(
                f"/projects/{pid}/merge_requests",
                params={"state": "opened"},
                item_fn=_src_branch,
            ):
                if ref:
                    out.add(ref)
        except Exception:
            return set()
        return out

    def _list_open_prs(self, project_path: str) -> list[dict[str, Any]]:
        """GET /projects/:id/merge_requests?state=opened → per-MR metadata.

        Returns [{'branch', 'author_login', 'number', 'url', 'title'}, ...].
        Returns [] on any failure (MUST NOT raise).
        """
        out: list[dict[str, Any]] = []
        try:
            pid = self._resolve_project_id(project_path)

            def _identity(mr: dict[str, Any]) -> dict[str, Any]:
                return mr

            for mr in self._paginated_get(
                f"/projects/{pid}/merge_requests",
                params={"state": "opened"},
                item_fn=_identity,
            ):
                ref = mr.get("source_branch")
                if not ref:
                    continue
                out.append(
                    {
                        "branch": ref,
                        "author_login": (mr.get("author") or {}).get("username", ""),
                        "number": mr.get("iid"),
                        "url": mr.get("web_url", ""),
                        "title": mr.get("title", ""),
                    }
                )
        except Exception:
            return []
        return out


def _map_merge_status(merge_status: str) -> bool | None:
    """Map GitLab merge_status to the standard mergeable field."""
    if merge_status == "can_be_merged":
        return True
    if merge_status == "cannot_be_merged":
        return False
    # "checking", "unchecked" → None (treat as mergeable per base.py docstring)
    return None


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
