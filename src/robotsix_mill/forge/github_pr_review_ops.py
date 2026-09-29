"""GitHub PR review / comment-operations mixin — reviews, dismissals,
review comments, and plain PR comments.

Split from ``github_pr.py``.  Defines ``GitHubForgePRReviewOpsMixin`` that
``GitHubForge`` inherits from.  The shared members these methods call
(``self._owner_repo``, ``self._get_pr``, ``self._http`` and the imported
``_paginated_get``) live on the sibling ``GitHubForgePRMixin`` / base and
resolve at runtime because both mixins are combined into ``GitHubForge``.
"""

from __future__ import annotations

from typing import Any

from ._github_pagination import _paginated_get


class GitHubForgePRReviewOpsMixin:
    """PR review / comment operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo`` and ``self._get_pr`` to
    exist on the final class (provided by ``GitHubForgePRMixin`` / base).
    """

    def post_pr_comment(self, *, source_branch: str, body: str) -> bool:
        """Post a plain comment on the open PR for *source_branch*.

        Returns ``True`` on success, ``False`` when the PR is not found.
        Never raises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)  # type: ignore[attr-defined]
        if pr is None:
            return False
        return self._post_pr_comment(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
            body=body,
        )

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
