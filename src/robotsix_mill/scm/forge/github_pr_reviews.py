"""GitHub PR review & comment handling — reviews, dismissals, comments.

Split from ``github_pr.py``.  Defines ``GitHubForgePRReviewsMixin`` that
``GitHubForge`` inherits from (alongside ``GitHubForgePRMixin``).

Expects ``self._http``, ``self._owner_repo`` to exist on the final class,
plus the ``_get_pr`` / ``_pr_files`` seams provided by
``GitHubForgePRMixin``.
"""

from __future__ import annotations

from typing import Any

from ._github_pagination import _paginated_get


class GitHubForgePRReviewsMixin:
    """PR review & comment operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http`` and ``self._owner_repo`` on the final class, and
    relies on ``_get_pr`` / ``_pr_files`` from ``GitHubForgePRMixin``.
    """

    def list_pr_reviews(self, *, source_branch: str) -> list[dict[str, Any]]:
        """Return the reviews submitted on *source_branch*'s PR.

        Each entry is a ``dict`` with ``id``, ``author``, ``created_at``,
        and ``body``. Returns ``[]`` when no PR exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)  # type: ignore[attr-defined]
        if pr is None:
            return []
        return self._list_pr_reviews(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def dismiss_review(self, *, source_branch: str, review_id: int) -> bool:
        """Dismiss a single PR review by its *review_id*.

        Returns ``True`` on success, ``False`` when the PR or review is
        not found (or on any API failure). Must NEVER raise.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)  # type: ignore[attr-defined]
        if pr is None:
            return False
        return self._dismiss_review(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
            review_id=review_id,
        )

    def list_review_comments(self, *, source_branch: str) -> list[dict[str, Any]]:
        """Return the inline review comments on *source_branch*'s PR.

        Each entry is a ``dict`` with ``id``, ``author``, ``created_at``,
        ``body``, ``file_path``, ``line``, and ``diff_hunk``. Returns ``[]``
        when no PR exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)  # type: ignore[attr-defined]
        if pr is None:
            return []
        return self._list_review_comments(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def list_pr_comments(self, *, source_branch: str) -> list[dict[str, Any]]:
        """Return the plain (issue) comments on *source_branch*'s PR.

        Each entry is a ``dict`` with ``id``, ``author``, ``created_at``, and
        ``body``.  Returns ``[]`` when no PR exists for the branch.  These are
        the conversation-level comments posted by :meth:`post_pr_comment`
        (GitHub's issue-comments endpoint), not inline review comments.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)  # type: ignore[attr-defined]
        if pr is None:
            return []
        return self._list_pr_comments(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def pr_review_status(self, *, source_branch: str) -> dict[str, Any] | None:
        """Return the aggregate review status for *source_branch*'s PR.

        Returns a ``dict`` with ``state`` (the latest non-dismissed review
        state, e.g. ``"CHANGES_REQUESTED"`` / ``"APPROVED"`` / ``"PENDING"``),
        a ``comments`` list (review bodies + inline comments, each carrying
        its ``review_state``), and ``files`` (changed file paths). Returns
        ``None`` when no PR exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)  # type: ignore[attr-defined]
        if pr is None:
            return None
        return self._pr_review_status(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    # --- HTTP seams (monkeypatched in tests) ---

    def _list_pr_reviews(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict[str, Any]]:
        return _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            item_fn=lambda item: {
                "id": item["id"],
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("submitted_at", ""),
                "body": item.get("body") or "",
            },
            fallback=[],
        )

    def _list_review_comments(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict[str, Any]]:
        return _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls/{pull_number}/comments",
            item_fn=lambda item: {
                "id": item["id"],
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("created_at", ""),
                "body": item.get("body") or "",
                "file_path": item.get("path", ""),
                "line": item.get("line") or item.get("original_line"),
                "diff_hunk": item.get("diff_hunk", ""),
            },
            fallback=[],
        )

    def _list_pr_comments(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict[str, Any]]:
        # Plain PR comments live on the issue-comments endpoint (a PR is an
        # issue in GitHub's data model), distinct from the pulls/.../comments
        # review-comments endpoint used by ``_list_review_comments``.
        return _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/issues/{pull_number}/comments",
            item_fn=lambda item: {
                "id": item["id"],
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("created_at", ""),
                "body": item.get("body") or "",
            },
            fallback=[],
        )

    def _dismiss_review(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
        review_id: int,
    ) -> bool:
        import logging

        logger = logging.getLogger(__name__)
        try:
            r = self._http.put(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/dismissals",
                json={
                    "message": "Stale review — PR head has changed since this review was submitted."
                },
            )
            if r.status_code == 200:
                return True
            logger.info(
                "dismiss_review HTTP %s for %s/%s PR #%d review %d: %s",
                r.status_code,
                owner,
                repo,
                pull_number,
                review_id,
                r.text[:200],
            )
            return False
        except Exception:
            logger.exception(
                "dismiss_review failed for %s/%s PR #%d review %d",
                owner,
                repo,
                pull_number,
                review_id,
            )
            return False

    def _pr_review_status(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict[str, Any]:
        # Fetch reviews (raw dicts — need state field that _list_pr_reviews
        # drops) and inline comments with full pagination.  Each call
        # independently retries on 401 via _paginated_get's retrying_client.
        reviews_raw: list[Any] = _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            item_fn=lambda x: x,
            fallback=[],
        )
        comments_raw: list[Any] = _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls/{pull_number}/comments",
            item_fn=lambda x: x,
            fallback=[],
        )
        files = self._pr_files(  # type: ignore[attr-defined]
            owner=owner,
            repo=repo,
            pull_number=pull_number,
        )

        # Determine aggregate review state from the latest non-dismissed
        # review.  GitHub returns reviews oldest-first; iterate reversed.
        state = "PENDING"
        review_commit_id = ""
        review_id: int | None = None
        for rev in reversed(reviews_raw):
            rev_state = rev.get("state", "COMMENTED")
            if rev_state != "DISMISSED":
                state = rev_state
                review_commit_id = rev.get("commit_id", "")
                review_id = rev.get("id")
                break
        else:
            # All reviews are DISMISSED — use the latest one.
            if reviews_raw:
                state = reviews_raw[-1].get("state", "DISMISSED")
                review_commit_id = reviews_raw[-1].get("commit_id", "")
                review_id = reviews_raw[-1].get("id")

        # Build a review_state lookup: review_id -> state.
        review_state_map: dict[int, str] = {}
        for rev in reviews_raw:
            review_state_map[rev["id"]] = rev.get("state", "COMMENTED")

        # Merge review body comments + inline comments into one list.
        comments: list[dict[str, Any]] = []
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
            "commit_id": review_commit_id,
            "review_id": review_id,
        }
