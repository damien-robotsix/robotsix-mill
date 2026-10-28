"""GitHub PR status / query mixin — PR status, files, open-PR listing,
required status contexts, authenticated-user lookup.

Split from the former monolithic ``github_pr.py``.  Defines
``GitHubForgePRStatusMixin`` that ``GitHubForge`` inherits from.  PR
lifecycle (create/merge/close) lives in ``github_pr.py`` and branch
operations in ``github_branch.py``.
"""

from __future__ import annotations

from typing import Any

from ._github_pagination import _paginated_get


def _parse_pr_detail(pr: dict[str, Any]) -> dict[str, Any]:
    """Normalize a GitHub PR detail dict into the standard status shape
    (the same dict ``_get_pr`` / ``pr_status`` return).

    GitHub computes mergeable asynchronously after every force-push.
    Until the computation finishes, mergeable_state is "unknown" and
    ``mergeable`` carries the STALE pre-push value — which the merge
    stage previously treated as a real conflict and bounced into
    REBASING. Surface "still computing" as ``None`` so the caller
    waits the next poll instead.
    """
    mergeable_state = pr.get("mergeable_state")
    mergeable = pr.get("mergeable")
    if mergeable_state in (None, "unknown"):
        mergeable = None
    return {
        "merged": bool(pr.get("merged")),
        "state": pr.get("state", "open"),
        "url": pr.get("html_url", ""),
        "mergeable": mergeable,  # True/False/None
        "mergeable_state": mergeable_state,
        "sha": (pr.get("head") or {}).get("sha", ""),
        "number": pr["number"],
        "author": (pr.get("user") or {}).get("login", ""),
    }


def _app_or_any(app_id: Any) -> int | None:
    """Normalize a protection ``app_id`` / ``integration_id``: ``-1``/null = any app."""
    return app_id if isinstance(app_id, int) and app_id > 0 else None


class GitHubForgePRStatusMixin:
    """PR status / query operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo``, ``self._head_owner``,
    ``self.settings``, ``self._repo_config`` to exist on the final class.
    """

    def pr_status(self, *, source_branch: str) -> dict[str, Any] | None:
        """Return the PR status for the PR whose head is *source_branch*.

        Looks the PR up by head branch and returns the normalized status
        ``dict`` (``merged``, ``state``, ``url``, ``mergeable``,
        ``mergeable_state``, ``sha``, ``number``), or ``None`` when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._get_pr(owner=owner, repo=repo, head=source_branch)

    def pr_status_by_url(self, *, url: str) -> dict[str, Any] | None:
        """Return the PR status resolved directly from a PR *url*.

        Parses the ``/pull/<number>`` segment out of *url* and fetches the
        PR by number, returning the same status ``dict`` shape as
        :meth:`pr_status`. Returns ``None`` when *url* has no PR number.
        Unlike :meth:`pr_status` this still resolves a merged PR whose head
        branch was auto-deleted on merge.
        """
        import re

        m = re.search(r"/pull/(\d+)", url or "")
        if not m:
            return None
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._get_pr_by_number(owner=owner, repo=repo, number=int(m.group(1)))

    def pr_files(self, *, source_branch: str) -> list[dict[str, Any]]:
        """Return the list of files changed in *source_branch*'s PR.

        Each entry is a ``dict`` with ``path``, ``status``, ``additions``,
        and ``deletions``. Returns ``[]`` when no PR exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return []
        return self._pr_files(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    # --- HTTP seam (monkeypatched in tests) ---

    def _pr_files(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict[str, Any]]:
        return _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
            item_fn=lambda item: {
                "path": item["filename"],
                "status": item.get("status", "modified"),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
            },
            fallback=[],
        )

    def list_open_prs(self) -> list[dict[str, Any]]:
        """Return [{branch, author_login}, ...] for all open PRs. Returns [] on failure."""
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_open_prs(owner=owner, repo=repo)

    def _list_open_prs(self, *, owner: str, repo: str) -> list[dict[str, Any]]:
        """Return per-PR metadata dicts for all open PRs.

        Each dict carries: ``branch`` (head ref), ``author_login``,
        ``number`` (PR number), ``url`` (html_url), and ``title``.

        Uses :func:`_paginated_get` for pagination and 401-retry.
        Returns [] on any failure (MUST NOT raise).
        """
        result = _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open"},
            item_fn=lambda pr: (
                (pr.get("head") or {}).get("ref"),
                (pr.get("user") or {}).get("login", ""),
                pr.get("number"),
                pr.get("html_url", ""),
                pr.get("title", ""),
            ),
            fallback=[],
        )
        return [
            {
                "branch": ref,
                "author_login": author,
                "number": number,
                "url": url,
                "title": title,
            }
            for ref, author, number, url, title in result
            if ref
        ]

    def _required_status_contexts(
        self, *, owner: str, repo: str, branch: str
    ) -> list[str]:
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            resp = c.get(
                f"{api}/repos/{owner}/{repo}/branches/{branch}/protection"
                "/required_status_checks",
                headers=headers,
            )
            if resp.status_code == 401:
                continue
            # 404 = unprotected branch or no required checks configured;
            # 403 = token cannot read protection. Both mean "unknown".
            if resp.status_code in (403, 404):
                return []
            resp.raise_for_status()
            body = resp.json()
            checks = body.get("checks")
            if isinstance(checks, list):
                return [
                    ctx
                    for ctx in (chk.get("context") for chk in checks)
                    if isinstance(ctx, str)
                ]
            return [ctx for ctx in body.get("contexts", []) if isinstance(ctx, str)]
        return []

    def _required_status_checks(
        self, *, owner: str, repo: str, branch: str
    ) -> list[dict[str, Any]]:
        """Classic-protection checks plus ruleset checks, with their app ids."""
        out: list[dict[str, Any]] = []
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            resp = c.get(
                f"{api}/repos/{owner}/{repo}/branches/{branch}/protection"
                "/required_status_checks",
                headers=headers,
            )
            if resp.status_code == 401:
                continue
            if resp.status_code not in (403, 404):
                resp.raise_for_status()
                body = resp.json()
                checks = body.get("checks")
                if isinstance(checks, list):
                    out += [
                        {
                            "context": chk["context"],
                            "app_id": _app_or_any(chk.get("app_id")),
                        }
                        for chk in checks
                        if isinstance(chk.get("context"), str)
                    ]
                else:
                    out += [
                        {"context": ctx, "app_id": None}
                        for ctx in body.get("contexts", [])
                        if isinstance(ctx, str)
                    ]
            rules = c.get(
                f"{api}/repos/{owner}/{repo}/rules/branches/{branch}",
                headers=headers,
            )
            if rules.status_code == 200 and isinstance(rules.json(), list):
                for rule in rules.json():
                    if rule.get("type") != "required_status_checks":
                        continue
                    params = rule.get("parameters") or {}
                    out += [
                        {
                            "context": chk["context"],
                            "app_id": _app_or_any(chk.get("integration_id")),
                        }
                        for chk in params.get("required_status_checks") or []
                        if isinstance(chk.get("context"), str)
                    ]
            return out
        return out

    def required_status_checks(self, *, target_branch: str) -> list[dict[str, Any]]:
        """Required checks on *target_branch* (classic protection + rulesets).

        Each entry is ``{"context": str, "app_id": int | None}``. Returns
        ``[]`` on any failure — callers read ``[]`` as "unknown".
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        try:
            return self._required_status_checks(
                owner=owner, repo=repo, branch=target_branch
            )
        except Exception:
            return []

    def _head_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, Any]]:
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                headers=headers,
                params={"per_page": 100},
            )
            if resp.status_code == 401:
                continue
            resp.raise_for_status()
            out = [
                {"name": cr["name"], "app_id": (cr.get("app") or {}).get("id")}
                for cr in resp.json().get("check_runs", [])
                if isinstance(cr.get("name"), str)
            ]
            st = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/status", headers=headers
            )
            if st.status_code == 200:
                out += [
                    {"name": s["context"], "app_id": None}
                    for s in st.json().get("statuses", [])
                    if isinstance(s.get("context"), str)
                ]
            return out
        return []

    def head_check_runs(self, *, sha: str) -> list[dict[str, Any]]:
        """Check-runs and commit statuses on *sha*, each with its app id.

        Returns ``[]`` on any failure (diagnosis only).
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        try:
            return self._head_check_runs(owner=owner, repo=repo, sha=sha)
        except Exception:
            return []

    def required_status_contexts(self, *, target_branch: str) -> list[str]:
        """Return the status contexts branch protection requires on *target_branch*.

        Returns ``[]`` when the branch is unprotected, when the token lacks
        ``administration: read`` (403), or on any other API failure — callers
        must read ``[]`` as "unknown", never as "nothing is required".
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        try:
            return self._required_status_contexts(
                owner=owner, repo=repo, branch=target_branch
            )
        except Exception:
            return []

    def get_authenticated_user_login(self) -> str:
        """Return the login of the GitHub user/app associated with the current token.

        Calls GET /user. For GitHub Apps installations the login appears as
        '<app-slug>[bot]'. Caches the result on the instance after the first call.
        Returns '' on any failure (MUST NOT raise).
        """
        cached = getattr(self, "_cached_bot_login", None)
        if cached is not None:
            return cached
        try:
            login = self._get_authenticated_user_login()
        except Exception:
            login = ""
        self._cached_bot_login = login
        return login

    def _get_authenticated_user_login(self) -> str:
        with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
            r = c.get(f"{api}/user", headers=headers)
            if r.status_code == 200:
                return r.json().get("login", "")
        return ""

    def _get_pr(self, *, owner: str, repo: str, head: str) -> dict[str, Any] | None:
        # For cross-repo targets the head branch lives on the fork,
        # so the head filter must use the fork owner (not the upstream
        # owner passed in *owner*).  _head_owner resolves accordingly.
        head_owner = self._head_owner  # type: ignore[attr-defined]
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            lst = c.get(
                f"{api}/repos/{owner}/{repo}/pulls",
                headers=headers,
                params={"head": f"{head_owner}:{head}", "state": "all"},
            )
            if lst.status_code == 401:
                continue
            lst.raise_for_status()
            items = lst.json()
            if not items:
                return None
            num = items[0]["number"]
            d = c.get(f"{api}/repos/{owner}/{repo}/pulls/{num}", headers=headers)
            if d.status_code == 401:
                continue
            d.raise_for_status()
            pr = d.json()
            return _parse_pr_detail(pr)
        return None

    def _get_pr_by_number(
        self, *, owner: str, repo: str, number: int
    ) -> dict[str, Any] | None:
        """Fetch a PR's status directly by number via a single
        ``GET /repos/{owner}/{repo}/pulls/{number}``.

        Returns the same dict shape as ``_get_pr`` (including the
        ``mergeable_state`` → ``mergeable`` normalization). Used by
        ``pr_status_by_url`` to resolve a recorded PR url even after the
        head branch was auto-deleted on merge (which makes the
        branch-keyed ``_get_pr`` list come back empty).
        """
        r = self._http.get(f"/repos/{owner}/{repo}/pulls/{number}")  # type: ignore[attr-defined]
        r.raise_for_status()
        pr = r.json()
        return _parse_pr_detail(pr)
