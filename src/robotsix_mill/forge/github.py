"""GitHub forge adapter — open a Pull Request for an already-pushed
branch via the GitHub REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.
"""

from __future__ import annotations

import re

from .base import Forge

# Regex for stripping ANSI escape sequences (CSI / SGR).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Maximum number of failed jobs whose logs are fetched per run.
_MAX_FAILED_JOBS = 10

_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# Check-run conclusions that are not "success"-like.
_FAILING_CONCLUSIONS = frozenset({
    "failure", "timed_out", "action_required", "cancelled",
    "startup_failure", "stale",
})

# Statuses that mean the check is still in-flight.
_PENDING_STATUSES = frozenset({
    "in_progress", "queued", "waiting", "requested", "pending",
})


def _build_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_owner_repo(remote_url: str) -> tuple[str, str]:
    m = _REMOTE_RE.search(remote_url or "")
    if not m:
        raise RuntimeError(f"cannot parse owner/repo from {remote_url!r}")
    return m.group("owner"), m.group("repo")


class GitHubForge(Forge):
    def open_merge_request(
        self, *, source_branch: str, title: str, body: str
    ) -> str:
        s = self.settings
        owner, repo = _parse_owner_repo(s.forge_remote_url or "")
        return self._create_pr(
            owner=owner,
            repo=repo,
            head=source_branch,
            base=s.forge_target_branch,
            title=title,
            body=body,
        )

    # --- HTTP seam (monkeypatched in tests) ---
    def _create_pr(
        self, *, owner: str, repo: str, head: str, base: str,
        title: str, body: str,
    ) -> str:
        import httpx

        from .auth import github_token  # lazy: avoid import cycle

        s = self.settings
        api = s.github_api_url.rstrip("/")
        url = f"{api}/repos/{owner}/{repo}/pulls"
        headers = _build_headers(github_token(s))
        payload = {"title": title, "head": head, "base": base, "body": body}
        with httpx.Client(timeout=30) as c:
            r = c.post(url, headers=headers, json=payload)
            if r.status_code == 201:
                return r.json()["html_url"]
            # already exists → return the open PR for this head branch
            if r.status_code == 422:
                q = c.get(
                    url,
                    headers=headers,
                    params={"head": f"{owner}:{head}", "state": "open"},
                )
                items = q.json() if q.status_code == 200 else []
                if items:
                    return items[0]["html_url"]
            raise RuntimeError(
                f"GitHub PR create failed: {r.status_code} "
                f"{r.text[:300]}"
            )

    def pr_status(self, *, source_branch: str) -> dict | None:
        s = self.settings
        owner, repo = _parse_owner_repo(s.forge_remote_url or "")
        return self._get_pr(owner=owner, repo=repo, head=source_branch)

    def check_status(self, *, source_branch: str) -> dict | None:
        s = self.settings
        owner, repo = _parse_owner_repo(s.forge_remote_url or "")
        return self._check_status(owner=owner, repo=repo, head=source_branch)

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        s = self.settings
        owner, repo = _parse_owner_repo(s.forge_remote_url or "")
        return self._list_workflow_runs(
            owner=owner, repo=repo, branch=branch, head_sha=head_sha,
        )

    def fetch_workflow_job_logs(self, *, run_id: int) -> str:
        s = self.settings
        owner, repo = _parse_owner_repo(s.forge_remote_url or "")
        return self._fetch_workflow_job_logs(
            owner=owner, repo=repo, run_id=run_id,
        )

    # --- HTTP seamm (monkeypatched in tests) ---
    def _get_pr(self, *, owner: str, repo: str, head: str) -> dict | None:
        import httpx

        from .auth import github_token  # lazy: avoid import cycle

        s = self.settings
        api = s.github_api_url.rstrip("/")
        headers = _build_headers(github_token(s))
        with httpx.Client(timeout=30) as c:
            lst = c.get(
                f"{api}/repos/{owner}/{repo}/pulls",
                headers=headers,
                params={"head": f"{owner}:{head}", "state": "all"},
            )
            lst.raise_for_status()
            items = lst.json()
            if not items:
                return None
            num = items[0]["number"]
            d = c.get(
                f"{api}/repos/{owner}/{repo}/pulls/{num}", headers=headers
            )
            d.raise_for_status()
            pr = d.json()
        return {
            "merged": bool(pr.get("merged")),
            "state": pr.get("state", "open"),
            "url": pr.get("html_url", ""),
            "mergeable": pr.get("mergeable"),  # True/False/None
            "sha": (pr.get("head") or {}).get("sha", ""),
        }

    # --- HTTP seam (monkeypatched in tests) ---
    def _check_status(
        self, *, owner: str, repo: str, head: str
    ) -> dict | None:
        import httpx

        from .auth import github_token  # lazy: avoid import cycle

        s = self.settings
        api = s.github_api_url.rstrip("/")
        headers = _build_headers(github_token(s))

        pr = self._get_pr(owner=owner, repo=repo, head=head)
        if pr is None:
            return None

        sha = pr.get("sha", "")
        if not sha:
            return None

        with httpx.Client(timeout=30) as c:
            # 1. Fetch check runs (completed).
            cr_resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                headers=headers,
                params={"per_page": 100, "status": "completed"},
            )
            cr_resp.raise_for_status()
            check_runs = cr_resp.json().get("check_runs", [])

            if not check_runs:
                # 2. Fallback: combined statuses API.
                st_resp = c.get(
                    f"{api}/repos/{owner}/{repo}/commits/{sha}/status",
                    headers=headers,
                )
                st_resp.raise_for_status()
                statuses_data = st_resp.json()
                check_runs = _statuses_to_check_runs(statuses_data)

            return _derive_check_conclusion(
                c, api, owner, repo, headers, check_runs
            )

    # --- HTTP seam (monkeypatched in tests) ---
    def _list_workflow_runs(
        self, *, owner: str, repo: str,
        branch: str | None, head_sha: str | None,
    ) -> list[dict]:
        import httpx

        from .auth import github_token

        s = self.settings
        api = s.github_api_url.rstrip("/")
        headers = _build_headers(github_token(s))
        params: dict = {"status": "completed", "per_page": 30}
        if branch is not None:
            params["branch"] = branch
        if head_sha is not None:
            params["head_sha"] = head_sha

        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{api}/repos/{owner}/{repo}/actions/runs",
                headers=headers, params=params,
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
            }
            for run in raw
        ]

    # --- HTTP seam (monkeypatched in tests) ---
    def _fetch_workflow_job_logs(
        self, *, owner: str, repo: str, run_id: int,
    ) -> str:
        import httpx

        from .auth import github_token

        s = self.settings
        api = s.github_api_url.rstrip("/")
        headers = _build_headers(github_token(s))

        with httpx.Client(timeout=30) as c:
            # 1. List jobs for the run.
            jobs_resp = c.get(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                headers=headers,
                params={"status": "completed"},
            )
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json().get("jobs", [])

        # 2. Filter to failed-like jobs.
        failed_conclusions = frozenset({
            "failure", "cancelled", "timed_out", "action_required",
        })
        failed_jobs = [
            j for j in jobs
            if j.get("conclusion") in failed_conclusions
        ][:_MAX_FAILED_JOBS]

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
                        f"{api}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                        headers=headers,
                        follow_redirects=True,
                    )
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

                # Strip ANSI.
                clean = _ANSI_RE.sub("", raw)
                # Tail-cap: keep the last N bytes.
                if len(clean) > log_max:
                    clean = clean[-log_max:]

                parts.append(f"### Job: {job_name} (id={job_id})\n")
                parts.append(clean)
                parts.append("\n")

        return "\n".join(parts)


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
    for ctx, items in by_context.items():
        # overall state: "success", "failure", "pending"
        state = statuses_data.get("state", "success")
        conclusion = state if state != "pending" else None
        runs.append({
            "id": None,  # no detail fetch for statuses
            "name": ctx,
            "status": "completed" if state != "pending" else "in_progress",
            "conclusion": conclusion,
            "output": {
                "summary": None,
                "text": None,
                "annotations": [],
            },
        })
    return runs


def _conclusion_for_check(cr: dict) -> str:
    """Classify a single check run as 'pending', 'failure', or 'neutral'."""
    if cr.get("status", "") in _PENDING_STATUSES:
        return "pending"
    if cr.get("conclusion") in _FAILING_CONCLUSIONS:
        return "failure"
    return "neutral"


def _extract_annotations(
    client, api: str, owner: str, repo: str, headers: dict, cr: dict,
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
    client, api: str, owner: str, repo: str, headers: dict,
    check_runs: list[dict],
) -> dict:
    """Derive the overall conclusion and build the failing list."""
    if not check_runs:
        return {"conclusion": None, "failing": []}

    has_pending = False
    has_failure = False
    failing: list[dict] = []

    for cr in check_runs:
        cat = _conclusion_for_check(cr)
        if cat == "pending":
            has_pending = True
        elif cat == "failure":
            has_failure = True
            failing.append(
                _extract_annotations(client, api, owner, repo, headers, cr)
            )

    if has_failure:
        return {"conclusion": "failure", "failing": failing}
    if has_pending:
        return {"conclusion": "pending", "failing": []}
    return {"conclusion": "success", "failing": []}
