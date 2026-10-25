"""GitHub PR lifecycle mixin — PR creation, merge, close, reopen, comment.

Split from the former monolithic ``github_pr.py``.  Defines
``GitHubForgePRMixin`` that ``GitHubForge`` inherits from.  PR status
queries live in ``github_pr_status.py`` and branch operations in
``github_branch.py``.
"""

from __future__ import annotations

from typing import Any


def _api_message(response: Any) -> str:
    """Return GitHub's human-readable ``message`` from an error response.

    The merge endpoint distinguishes a permanent refusal from a
    still-settling required check only in the body, so the caller needs
    it in the reason string. Falls back to the raw (truncated) text when
    the body is not the usual ``{"message": ...}`` envelope.
    """
    try:
        message = response.json().get("message")
    except Exception:
        message = None
    return str(message) if message else response.text[:200]


def _existing_pr_lookup_unavailable(response: Any) -> bool:
    """Return True when an existing-PR lookup failed for a retryable reason.

    A 401 (App-token replica flap), 403 (rate/abuse limit) or 429
    (throttle) means "ask again later", not "no such PR" — the caller
    must not conclude the PR is absent from an empty list it never got.
    """
    return getattr(response, "status_code", None) in (401, 403, 429)


class GitHubForgePRMixin:
    """PR lifecycle operations for GitHub — mixed into ``GitHubForge``.

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
        from ...config import target_branch_for  # lazy: avoid import cycle

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

        payload = {"title": title, "head": head, "base": base, "body": body}
        # GitHub sometimes takes a few seconds to index a freshly-
        # pushed ref before the pulls API can resolve it — the
        # symptom is a 422 with field=head, code=invalid even
        # though the branch is visible via git/refs. Retry the
        # create call a few times before giving up; existing-PR
        # detection runs each round so we don't double-open.
        for _retry, c, api, headers in self._http.retrying_client(  # type: ignore[attr-defined]
            max_retries=2,
        ):
            url = f"{api}/repos/{owner}/{repo}/pulls"
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
                    # head owner prefix. Use ``_head_owner`` (not the
                    # upstream ``owner``) so a cross-repo/fork target
                    # resolves the same way ``_get_pr`` does.
                    head_owner = getattr(self, "_head_owner", owner) or owner
                    head_param = head if ":" in head else f"{head_owner}:{head}"
                    q = c.get(
                        url,
                        headers=headers,
                        # ``state=all`` matches ``_get_pr``: a closed PR for
                        # this head still makes GitHub reject the create, and
                        # an ``open``-only filter cannot see it.
                        params={"head": head_param, "state": "all"},
                    )
                    if q.status_code == 200:
                        items = q.json()
                        if items:
                            return items[0]["html_url"]
                    elif _existing_pr_lookup_unavailable(q):
                        # The create says a PR already exists but the lookup
                        # that would recover its URL was itself refused
                        # (throttled / auth flap). Raising the 422 here is a
                        # lie — it reads as "PR create failed" and blocks the
                        # ticket for a human, when in truth the PR is sitting
                        # there and only the read failed. Surface the lookup
                        # failure instead so the stage-level classifier sees a
                        # transient error and retries with backoff.
                        q.raise_for_status()
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
                # (GitHub replica lag). Break out of the inner
                # attempt loop; the outer retrying_client loop
                # invalidates the cached token and retries with
                # fresh headers.
                if r.status_code == 401:
                    break
                # Non-422 / non-401 (or final attempt) — surface.
                break
            else:
                # Inner loop exhausted all 4 attempts (all 422s).
                break
            if r.status_code == 401:
                continue  # trigger retrying_client retry
            break  # success or non-401 failure
        raise RuntimeError(f"GitHub PR create failed: {r.status_code} {r.text[:300]}")

    def merge_pr(self, *, source_branch: str) -> dict[str, Any]:
        """Merge (squash) the PR whose head is *source_branch*.

        Returns a ``dict`` with ``merged`` (bool) and a ``reason`` string.
        Mutates remote state: squash-merges the PR via the GitHub API.
        Returns ``{"merged": False, "reason": "PR not found"}`` when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(  # type: ignore[attr-defined]
            owner=owner, repo=repo, head=source_branch
        )
        if pr is None:
            return {"merged": False, "reason": "PR not found"}
        return self._merge_pr(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def _merge_pr(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict[str, Any]:
        try:
            r = self._http.put(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/pulls/{pull_number}/merge",
                json={"merge_method": "squash"},
            )
            if r.status_code == 200:
                result: dict[str, Any] = {"merged": True, "reason": "merged"}
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        merge_sha = data.get("sha")
                        if merge_sha:
                            result["merge_commit_sha"] = merge_sha
                except ValueError, AttributeError:
                    pass
                return result
            if r.status_code == 405:
                # GitHub overloads 405 for both permanent refusals ("Merge
                # commits are not allowed on this repository") and the
                # transient "Required status check … is expected" that a
                # still-settling required gate produces. The two are only
                # distinguishable from the response body, so surface it
                # verbatim and mark the merge retryable — the caller
                # re-polls rather than blocking the ticket at the finish
                # line (observed: seven robotsix-llmio tickets BLOCKED by a
                # merge fired 24-68s before CodeQL/ci reported).
                return {
                    "merged": False,
                    "retryable": True,
                    "reason": f"merge not allowed: {_api_message(r)}",
                }
            if r.status_code == 409:
                return {
                    "merged": False,
                    "retryable": True,
                    "reason": f"PR is not mergeable: {_api_message(r)}",
                }
            return {
                "merged": False,
                "reason": f"HTTP {r.status_code}: {r.text[:200]}",
            }
        except Exception as e:
            import logging

            from .auth import classify_token_error

            transient = classify_token_error(e) == "transient"
            result = {"merged": False, "reason": str(e)}
            if transient:
                logging.getLogger(__name__).info(
                    "_merge_pr transient auth error for %s/%s PR #%d: %s",
                    owner,
                    repo,
                    pull_number,
                    e,
                )
                result["retryable"] = True
            return result

    def close_pr(self, *, source_branch: str) -> bool:
        """Close/decline the open PR for *source_branch* without merging.

        Returns ``True`` on success, ``False`` when the PR is not found
        or already closed.  Never raises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(  # type: ignore[attr-defined]
            owner=owner, repo=repo, head=source_branch
        )
        if pr is None:
            return False
        return self._close_pr(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

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

    def reopen_pr(self, *, source_branch: str) -> bool:
        """Reopen a closed PR for *source_branch*.

        Returns ``True`` on success, ``False`` when the PR is not found
        or already open.  Never raises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(  # type: ignore[attr-defined]
            owner=owner, repo=repo, head=source_branch
        )
        if pr is None:
            return False
        return self._reopen_pr(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def _reopen_pr(
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
                json={"state": "open"},
            )
            if r.status_code == 200:
                return True
            logger.info(
                "reopen_pr HTTP %s for %s/%s PR #%d: %s",
                r.status_code,
                owner,
                repo,
                pull_number,
                r.text[:200],
            )
            return False
        except Exception:
            logger.exception(
                "reopen_pr failed for %s/%s PR #%d",
                owner,
                repo,
                pull_number,
            )
            return False

    def post_pr_comment(self, *, source_branch: str, body: str) -> bool:
        """Post a plain comment on the open PR for *source_branch*.

        Returns ``True`` on success, ``False`` when the PR is not found.
        Never raises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        pr = self._get_pr(  # type: ignore[attr-defined]
            owner=owner, repo=repo, head=source_branch
        )
        if pr is None:
            return False
        return self._post_pr_comment(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
            body=body,
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
