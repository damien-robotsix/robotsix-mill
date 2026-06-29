"""GitHub PR / branch-operations mixin — PR creation, status, reviews, merge.

Split from ``github.py``.  Defines ``GitHubForgePRMixin`` that
``GitHubForge`` inherits from.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import BranchInfo


def _parse_iso_utc(value: str | None) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Accepts a trailing ``Z`` (GitHub's UTC marker). Naive timestamps are
    assumed UTC; aware ones are converted to UTC. Returns the Unix epoch
    (UTC) when *value* is missing or unparseable.
    """
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_pr_detail(pr: dict) -> dict:
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
    }


class GitHubForgePRMixin:
    """PR / branch operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo``, ``self._head_owner``,
    ``self.settings``, ``self._repo_config`` to exist on the final class.
    """

    def open_merge_request(
        self,
        *,
        source_branch: str,
        title: str,
        body: str,
        head_repo: str | None = None,
    ) -> str:
        """Open a Pull Request for the already-pushed *source_branch*.

        :param source_branch: head branch to open the PR from.
        :param title: PR title.
        :param body: PR description body.
        :param head_repo: when set (``owner/repo``), a cross-fork PR whose
            head lives on the fork; the head is qualified ``owner:branch``
            and the base resolves to the upstream ``base_branch``.
        Returns the new (or already-existing) PR's ``html_url``. Calls the
        GitHub API to create the PR (idempotent: reuses an open PR for the
        same head instead of double-opening). Raises ``RuntimeError`` on a
        non-recoverable create failure.
        """
        s = self.settings  # type: ignore[attr-defined]
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        from ..config import target_branch_for  # lazy: avoid import cycle

        base = target_branch_for(s, self._repo_config)  # type: ignore[attr-defined]
        head = source_branch
        if head_repo is not None:
            # Cross-fork PR: head lives in the fork (``owner:branch``),
            # base on the upstream repo / ``base_branch``. ``_owner_repo``
            # already resolves to upstream via ``cross_repo_target``.
            fork_owner = head_repo.split("/", 1)[0]
            head = f"{fork_owner}:{source_branch}"
            cct = getattr(self._repo_config, "cross_repo_target", None)  # type: ignore[attr-defined]
            if cct is not None:
                base = cct.base_branch
        return self._create_pr(
            owner=owner,
            repo=repo,
            head=head,
            base=base,
            title=title,
            body=body,
        )

    # --- HTTP seam (monkeypatched in tests) ---
    def _create_pr(
        self,
        *,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        payload = {"title": title, "head": head, "base": base, "body": body}
        # GitHub sometimes takes a few seconds to index a freshly-
        # pushed ref before the pulls API can resolve it — the
        # symptom is a 422 with field=head, code=invalid even
        # though the branch is visible via git/refs. Retry the
        # create call a few times before giving up; existing-PR
        # detection runs each round so we don't double-open.
        with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
            url = f"{api}/repos/{owner}/{repo}/pulls"
            already_retried_401 = False
            for attempt in range(4):
                r = c.post(url, headers=headers, json=payload)
                if r.status_code == 201:
                    return r.json()["html_url"]
                # 422 — either "already exists" or a transient
                # post-push indexing race.
                if r.status_code == 422:
                    # head is already fully qualified for cross-fork
                    # PRs (e.g. "fork-owner:branch"); for same-repo
                    # PRs it's just the branch name and needs the
                    # owner prefix.
                    head_param = head if ":" in head else f"{owner}:{head}"
                    q = c.get(
                        url,
                        headers=headers,
                        params={"head": head_param, "state": "open"},
                    )
                    items = q.json() if q.status_code == 200 else []
                    if items:
                        return items[0]["html_url"]
                    # No existing PR; treat as a transient "head
                    # invalid" race when the error body says so,
                    # back off and retry. Final attempt falls
                    # through to RuntimeError below.
                    err_text = r.text or ""
                    if (
                        attempt < 3
                        and '"field":"head"' in err_text
                        and '"code":"invalid"' in err_text
                    ):
                        time.sleep(2**attempt)  # 1s, 2s, 4s
                        continue
                # 401 — intermittent App-token write auth flap
                # (GitHub replica lag). Invalidate cached token,
                # back off, regenerate headers, retry once.
                if r.status_code == 401 and not already_retried_401:
                    invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    time.sleep(2)
                    headers = self._http.regenerate_headers()  # type: ignore[attr-defined]
                    already_retried_401 = True
                    continue
                # Non-422 / non-401 (or final attempt) — surface.
                break
            raise RuntimeError(
                f"GitHub PR create failed: {r.status_code} {r.text[:300]}"
            )

    def pr_status(self, *, source_branch: str) -> dict | None:
        """Return the PR status for the PR whose head is *source_branch*.

        Looks the PR up by head branch and returns the normalized status
        ``dict`` (``merged``, ``state``, ``url``, ``mergeable``,
        ``mergeable_state``, ``sha``, ``number``), or ``None`` when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._get_pr(owner=owner, repo=repo, head=source_branch)

    def pr_status_by_url(self, *, url: str) -> dict | None:
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

    def pr_files(self, *, source_branch: str) -> list[dict]:
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

    def merge_pr(self, *, source_branch: str) -> dict:
        """Merge (squash) the PR whose head is *source_branch*.

        Returns a ``dict`` with ``merged`` (bool) and a ``reason`` string.
        Mutates remote state: squash-merges the PR via the GitHub API.
        Returns ``{"merged": False, "reason": "PR not found"}`` when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return {"merged": False, "reason": "PR not found"}
        return self._merge_pr(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def close_pr(self, *, source_branch: str) -> bool:
        """Close/decline the open PR for *source_branch* without merging.

        Returns ``True`` on success, ``False`` when the PR is not found
        or already closed.  Never raises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return False
        return self._close_pr(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def post_pr_comment(self, *, source_branch: str, body: str) -> bool:
        """Post a plain comment on the open PR for *source_branch*.

        Returns ``True`` on success, ``False`` when the PR is not found.
        Never raises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return False
        return self._post_pr_comment(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
            body=body,
        )

    def update_branch(self, *, source_branch: str) -> dict:
        """Update *source_branch*'s PR head with the latest base branch.

        Calls the GitHub ``update-branch`` API (merges the base into the PR
        head), mutating remote state. Returns a ``dict`` with ``updated``
        (bool) and a ``reason`` string — ``False``/"already up to date" when
        there is nothing to merge, ``False``/"PR not found" when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return {"updated": False, "reason": "PR not found"}
        try:
            r = self._http.put(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/pulls/{pr['number']}/update-branch"
            )
            if r.status_code == 202:
                return {"updated": True, "reason": "update-branch accepted"}
            if r.status_code == 422:
                # branch already up to date — nothing to do
                return {"updated": False, "reason": "already up to date"}
            return {"updated": False, "reason": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:  # noqa: BLE001
            return {"updated": False, "reason": str(e)}

    def list_pr_reviews(self, *, source_branch: str) -> list[dict]:
        """Return the reviews submitted on *source_branch*'s PR.

        Each entry is a ``dict`` with ``id``, ``author``, ``created_at``,
        and ``body``. Returns ``[]`` when no PR exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return []
        return self._list_pr_reviews(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def list_review_comments(self, *, source_branch: str) -> list[dict]:
        """Return the inline review comments on *source_branch*'s PR.

        Each entry is a ``dict`` with ``id``, ``author``, ``created_at``,
        ``body``, ``file_path``, ``line``, and ``diff_hunk``. Returns ``[]``
        when no PR exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return []
        return self._list_review_comments(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def pr_review_status(self, *, source_branch: str) -> dict | None:
        """Return the aggregate review status for *source_branch*'s PR.

        Returns a ``dict`` with ``state`` (the latest non-dismissed review
        state, e.g. ``"CHANGES_REQUESTED"`` / ``"APPROVED"`` / ``"PENDING"``),
        a ``comments`` list (review bodies + inline comments, each carrying
        its ``review_state``), and ``files`` (changed file paths). Returns
        ``None`` when no PR exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return None
        return self._pr_review_status(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def delete_branch(self, *, branch: str) -> bool:
        """Delete remote *branch*, returning ``True`` once it is gone.

        :param branch: branch name to delete.
        Mutates remote state: issues a DELETE on the branch ref (resolved to
        the fork for cross-repo targets). Returns ``True`` when the branch is
        deleted or already absent, ``False`` on any other failure.
        """
        # For cross-repo targets the head branch lives on the fork,
        # not the upstream repo.  Resolve the fork owner/repo so the
        # DELETE goes to the right place instead of 404'ing on
        # upstream.
        if self._repo_config is not None:  # type: ignore[attr-defined]
            cct = getattr(self._repo_config, "cross_repo_target", None)  # type: ignore[attr-defined]
            if cct is not None and cct.fork_remote_url:
                from .github import _parse_owner_repo

                fork_owner, fork_repo = _parse_owner_repo(cct.fork_remote_url)
                return self._delete_branch(
                    owner=fork_owner, repo=fork_repo, branch=branch
                )
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._delete_branch(owner=owner, repo=repo, branch=branch)

    def list_branches(self) -> list[BranchInfo]:
        """Return all branches of the repo as :class:`BranchInfo` entries.

        Paginates the GitHub branches API and returns a ``list[BranchInfo]``
        (``name``, ``last_commit_at``, ``is_protected``). Returns ``[]`` on
        any API failure.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_branches(owner=owner, repo=repo)

    def list_open_pr_branches(self) -> set[str]:
        """Return the set of head branch names that have an open PR.

        Paginates the open-PRs API and collects each PR's head ref. Returns
        a ``set[str]`` of branch names (empty on any API failure).
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_open_pr_branches(owner=owner, repo=repo)

    def list_open_prs(self) -> list[dict]:
        """Return [{branch, author_login}, ...] for all open PRs. Returns [] on failure."""
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_open_prs(owner=owner, repo=repo)

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

    # --- HTTP seams (monkeypatched in tests) ---

    def _get_pr(self, *, owner: str, repo: str, head: str) -> dict | None:
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        # For cross-repo targets the head branch lives on the fork,
        # so the head filter must use the fork owner (not the upstream
        # owner passed in *owner*).  _head_owner resolves accordingly.
        head_owner = self._head_owner  # type: ignore[attr-defined]
        for retry in range(2):
            with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
                lst = c.get(
                    f"{api}/repos/{owner}/{repo}/pulls",
                    headers=headers,
                    params={"head": f"{head_owner}:{head}", "state": "all"},
                )
                if lst.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    time.sleep(2)
                    continue
                lst.raise_for_status()
                items = lst.json()
                if not items:
                    return None
                num = items[0]["number"]
                d = c.get(f"{api}/repos/{owner}/{repo}/pulls/{num}", headers=headers)
                if d.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    time.sleep(2)
                    continue
                d.raise_for_status()
                pr = d.json()
            return _parse_pr_detail(pr)
        return None

    def _get_pr_by_number(self, *, owner: str, repo: str, number: int) -> dict | None:
        """Fetch a PR's status directly by number via a single
        ``GET /repos/{owner}/{repo}/pulls/{number}``.

        Returns the same dict shape as ``_get_pr`` (including the
        ``mergeable_state`` → ``mergeable`` normalization). Used by
        ``pr_status_by_url`` to resolve a recorded PR url even after the
        head branch was auto-deleted on merge (which makes the
        branch-keyed ``_get_pr`` list come back empty)."""
        r = self._http.get(f"/repos/{owner}/{repo}/pulls/{number}")  # type: ignore[attr-defined]
        r.raise_for_status()
        pr = r.json()
        return _parse_pr_detail(pr)

    def _pr_files(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict]:
        try:
            r = self._http.get(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
                params={"per_page": 100},
            )
            r.raise_for_status()
            items = r.json()
        except Exception:
            return []
        return [
            {
                "path": item["filename"],
                "status": item.get("status", "modified"),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
            }
            for item in items
        ]

    def _merge_pr(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict:
        try:
            r = self._http.put(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/pulls/{pull_number}/merge",
                json={"merge_method": "squash"},
            )
            if r.status_code == 200:
                return {"merged": True, "reason": "merged"}
            if r.status_code == 405:
                return {
                    "merged": False,
                    "reason": "merge not allowed (branch protection?)",
                }
            if r.status_code == 409:
                return {"merged": False, "reason": "PR is not mergeable"}
            return {
                "merged": False,
                "reason": f"HTTP {r.status_code}: {r.text[:200]}",
            }
        except Exception as e:
            return {"merged": False, "reason": str(e)}

    def _close_pr(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> bool:
        import logging

        logger = logging.getLogger(__name__)
        try:
            r = self._http.patch(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/pulls/{pull_number}",
                json={"state": "closed"},
            )
            if r.status_code == 200:
                return True
            logger.info(
                "close_pr HTTP %s for %s/%s PR #%d: %s",
                r.status_code,
                owner,
                repo,
                pull_number,
                r.text[:200],
            )
            return False
        except Exception:
            logger.exception(
                "close_pr failed for %s/%s PR #%d",
                owner,
                repo,
                pull_number,
            )
            return False

    def _post_pr_comment(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
    ) -> bool:
        import logging

        logger = logging.getLogger(__name__)
        try:
            r = self._http.post(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/issues/{pull_number}/comments",
                json={"body": body},
            )
            if r.status_code == 201:
                return True
            logger.info(
                "post_pr_comment HTTP %s for %s/%s PR #%d: %s",
                r.status_code,
                owner,
                repo,
                pull_number,
                r.text[:200],
            )
            return False
        except Exception:
            logger.exception(
                "post_pr_comment failed for %s/%s PR #%d",
                owner,
                repo,
                pull_number,
            )
            return False

    def _delete_branch(self, *, owner: str, repo: str, branch: str) -> bool:
        try:
            r = self._http.delete(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/git/refs/heads/{branch}"
            )
            # 204 = deleted; 404/422 = ref does not exist (already gone,
            # e.g. by GitHub auto-delete) — the branch is gone either way,
            # which is the desired end state.
            if r.status_code in (204, 404, 422):
                return True
            return False
        except Exception:
            return False

    def _list_branches(self, *, owner: str, repo: str) -> list[BranchInfo]:
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        out: list[BranchInfo] = []
        for retry in range(2):
            hit_401 = False
            try:
                with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
                    url = f"{api}/repos/{owner}/{repo}/branches"
                    page = 1
                    while True:
                        r = c.get(
                            url,
                            headers=headers,
                            params={"per_page": 100, "page": page},
                        )
                        if r.status_code == 401 and retry == 0:
                            invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                            time.sleep(2)
                            hit_401 = True
                            break
                        r.raise_for_status()
                        items = r.json()
                        for b in items:
                            date = (
                                ((b.get("commit") or {}).get("commit") or {}).get(
                                    "committer"
                                )
                                or {}
                            ).get("date")
                            out.append(
                                BranchInfo(
                                    name=b["name"],
                                    last_commit_at=_parse_iso_utc(date),
                                    is_protected=bool(b.get("protected")),
                                )
                            )
                        if len(items) < 100:
                            break
                        page += 1
                if hit_401:
                    out.clear()
                    continue
                break  # success
            except Exception:
                return []
        return out

    def _list_open_pr_branches(self, *, owner: str, repo: str) -> set[str]:
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        out: set[str] = set()
        for retry in range(2):
            hit_401 = False
            try:
                with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
                    url = f"{api}/repos/{owner}/{repo}/pulls"
                    page = 1
                    while True:
                        r = c.get(
                            url,
                            headers=headers,
                            params={
                                "state": "open",
                                "per_page": 100,
                                "page": page,
                            },
                        )
                        if r.status_code == 401 and retry == 0:
                            invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                            time.sleep(2)
                            hit_401 = True
                            break
                        r.raise_for_status()
                        items = r.json()
                        for pr in items:
                            ref = (pr.get("head") or {}).get("ref")
                            if ref:
                                out.add(ref)
                        if len(items) < 100:
                            break
                        page += 1
                if hit_401:
                    out.clear()
                    continue
                break  # success
            except Exception:
                return set()
        return out

    def _list_open_prs(self, *, owner: str, repo: str) -> list[dict]:
        """Return [{'branch': str, 'author_login': str}, ...] for all open PRs.

        Uses the same pagination and 401-retry logic as _list_open_pr_branches().
        Returns [] on any failure (MUST NOT raise).
        """
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        out: list[dict] = []
        for retry in range(2):
            hit_401 = False
            try:
                with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
                    url = f"{api}/repos/{owner}/{repo}/pulls"
                    page = 1
                    while True:
                        r = c.get(
                            url,
                            headers=headers,
                            params={
                                "state": "open",
                                "per_page": 100,
                                "page": page,
                            },
                        )
                        if r.status_code == 401 and retry == 0:
                            invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                            time.sleep(2)
                            hit_401 = True
                            break
                        r.raise_for_status()
                        items = r.json()
                        for pr in items:
                            ref = (pr.get("head") or {}).get("ref")
                            author = (pr.get("user") or {}).get("login", "")
                            if ref:
                                out.append({"branch": ref, "author_login": author})
                        if len(items) < 100:
                            break
                        page += 1
                if hit_401:
                    out.clear()
                    continue
                break  # success
            except Exception:
                return []
        return out

    def _get_authenticated_user_login(self) -> str:
        _owner, _repo = self._owner_repo  # type: ignore[attr-defined]
        # unused but makes the HTTP seam consistent; actual call uses root /user
        with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
            r = c.get(f"{api}/user", headers=headers)
            if r.status_code == 200:
                return r.json().get("login", "")
        return ""

    def _list_pr_reviews(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict]:
        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            params={"per_page": 100},
        )
        r.raise_for_status()
        items = r.json()
        return [
            {
                "id": item["id"],
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("submitted_at", ""),
                "body": item.get("body") or "",
            }
            for item in items
        ]

    def _list_review_comments(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict]:
        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls/{pull_number}/comments",
            params={"per_page": 100},
        )
        r.raise_for_status()
        items = r.json()
        return [
            {
                "id": item["id"],
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("created_at", ""),
                "body": item.get("body") or "",
                "file_path": item.get("path", ""),
                "line": item.get("line") or item.get("original_line"),
                "diff_hunk": item.get("diff_hunk", ""),
            }
            for item in items
        ]

    def _pr_review_status(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict:
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        # Defensive init: the loop sets these on every non-exception path, but
        # CodeQL's py/uninitialized-local-variable can't prove it through the
        # retry/continue/break flow — initialise so the analysis is clean
        # without changing behaviour (a double-401 still raises before use).
        reviews_raw: list[Any] = []
        comments_raw: list[Any] = []
        files: list[Any] = []
        for retry in range(2):
            with self._http.client() as (c, api, headers):  # type: ignore[attr-defined]
                # 1. Fetch reviews (includes state field that list_pr_reviews drops).
                r = c.get(
                    f"{api}/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
                    headers=headers,
                    params={"per_page": 100},
                )
                if r.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    time.sleep(2)
                    continue
                r.raise_for_status()
                reviews_raw = r.json()

                # 2. Fetch inline review comments.
                r2 = c.get(
                    f"{api}/repos/{owner}/{repo}/pulls/{pull_number}/comments",
                    headers=headers,
                    params={"per_page": 100},
                )
                if r2.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)  # type: ignore[attr-defined]
                    time.sleep(2)
                    continue
                r2.raise_for_status()
                comments_raw = r2.json()

                # 3. Fetch changed files.
                files = self._pr_files(
                    owner=owner,
                    repo=repo,
                    pull_number=pull_number,
                )

            # If we get here the client block succeeded.
            break

        # Determine aggregate review state from the latest non-dismissed
        # review.  GitHub returns reviews oldest-first; iterate reversed.
        state = "PENDING"
        for rev in reversed(reviews_raw):
            rev_state = rev.get("state", "COMMENTED")
            if rev_state != "DISMISSED":
                state = rev_state
                break
        else:
            # All reviews are DISMISSED — use the latest one.
            if reviews_raw:
                state = reviews_raw[-1].get("state", "DISMISSED")

        # Build a review_state lookup: review_id -> state.
        review_state_map: dict[int, str] = {}
        for rev in reviews_raw:
            review_state_map[rev["id"]] = rev.get("state", "COMMENTED")

        # Merge review body comments + inline comments into one list.
        comments: list[dict] = []
        for rev in reviews_raw:
            body = rev.get("body")
            if body and body.strip():
                comments.append(
                    {
                        "body": body,
                        "path": "",
                        "line": None,
                        "review_state": rev.get("state", "COMMENTED"),
                    }
                )
        for c in comments_raw:
            comments.append(
                {
                    "body": c.get("body") or "",
                    "path": c.get("path", ""),
                    "line": c.get("line") or c.get("original_line"),
                    "review_state": review_state_map.get(
                        c.get("pull_request_review_id"), "COMMENTED"
                    ),
                }
            )

        return {
            "state": state,
            "comments": comments,
            "files": [f["path"] for f in files],
        }
