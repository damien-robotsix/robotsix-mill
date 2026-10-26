"""GitHub branch-operations mixin — branch update, delete, listing.

Split from the former monolithic ``github_pr.py``.  Defines
``GitHubForgeBranchMixin`` that ``GitHubForge`` inherits from.  PR
lifecycle lives in ``github_pr.py`` and PR status queries in
``github_pr_status.py``.
"""

from __future__ import annotations

from typing import Any

from ._github_pagination import _paginated_get
from .base import BranchInfo, _parse_iso_utc


class GitHubForgeBranchMixin:
    """Branch operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo``, ``self._head_owner``,
    ``self.settings``, ``self._repo_config`` to exist on the final class.
    """

    def update_branch(self, *, source_branch: str) -> dict[str, Any]:
        """Update *source_branch*'s PR head with the latest base branch.

        Calls the GitHub ``update-branch`` API (merges the base into the PR
        head), mutating remote state. Returns a ``dict`` with ``updated``
        (bool) and a ``reason`` string — ``False``/"already up to date" when
        there is nothing to merge, ``False``/"PR not found" when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(  # type: ignore[attr-defined]
            owner=owner, repo=repo, head=source_branch
        )
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
        except Exception as e:
            return {"updated": False, "reason": str(e)}

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

                fork_owner, fork_name = _parse_owner_repo(cct.fork_remote_url)
                return self._delete_branch(
                    owner=fork_owner, repo=fork_name, branch=branch
                )
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._delete_branch(owner=owner, repo=repo, branch=branch)

    # --- HTTP seam (monkeypatched in tests) ---

    def _delete_branch(self, *, owner: str, repo: str, branch: str) -> bool:
        try:
            r = self._http.delete(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/git/refs/heads/{branch}"
            )
            # 204 = deleted; 404/422 = ref does not exist (already gone,
            # e.g. by GitHub auto-delete) — the branch is gone either way,
            # which is the desired end state.
            return r.status_code in (204, 404, 422)
        except Exception:
            return False

    def list_branches(self) -> list[BranchInfo]:
        """Return all branches of the repo as :class:`BranchInfo` entries.

        Paginates the GitHub branches API and returns a ``list[BranchInfo]``
        (``name``, ``last_commit_at``, ``is_protected``). Returns ``[]`` on
        any API failure.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_branches(owner=owner, repo=repo)

    def _list_branches(self, *, owner: str, repo: str) -> list[BranchInfo]:
        return _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/branches",
            item_fn=lambda b: BranchInfo(
                name=b["name"],
                last_commit_at=_parse_iso_utc(
                    (
                        ((b.get("commit") or {}).get("commit") or {}).get("committer")
                        or {}
                    ).get("date")
                ),
                is_protected=bool(b.get("protected")),
            ),
            fallback=[],
        )

    def list_open_pr_branches(self) -> set[str]:
        """Return the set of head branch names that have an open PR.

        Paginates the open-PRs API and collects each PR's head ref. Returns
        a ``set[str]`` of branch names (empty on any API failure).
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_open_pr_branches(owner=owner, repo=repo)

    def _list_open_pr_branches(self, *, owner: str, repo: str) -> set[str]:
        result = _paginated_get(
            self._http,  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open"},
            item_fn=lambda pr: (pr.get("head") or {}).get("ref", ""),
            fallback=[],
        )
        return {ref for ref in result if ref}
