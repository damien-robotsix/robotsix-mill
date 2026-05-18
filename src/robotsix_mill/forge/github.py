"""GitHub forge adapter — open a Pull Request for an already-pushed
branch via the GitHub REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.
"""

from __future__ import annotations

import re

from .base import Forge

_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


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

        s = self.settings
        api = s.github_api_url.rstrip("/")
        url = f"{api}/repos/{owner}/{repo}/pulls"
        headers = {
            "Authorization": f"Bearer {s.forge_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
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
