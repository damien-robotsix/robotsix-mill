"""GitHub code-scanning alerts mixin — alert listing and dismissal.

Split from ``github.py``.  Defines ``GitHubForgeCodeScanningMixin`` that
``GitHubForge`` inherits from.
"""

from __future__ import annotations


class CodeScanningAlertsUnavailable(Exception):
    """Raised when the code-scanning alerts API returns 403.

    This means the App/token lacks the ``security-events`` /
    Code-scanning-alerts read permission — alerts exist but are
    unreadable, which is NOT the same as "no alerts".
    """


class GitHubForgeCodeScanningMixin:
    """Code-scanning (CodeQL) alert operations — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo``, ``self._get_pr`` to
    exist on the final class.
    """

    # --- HTTP seam (monkeypatched in tests) ---
    def _fetch_alerts_for_ref(self, *, owner: str, repo: str, ref: str) -> list[dict]:
        """Fetch raw open code-scanning alerts for a single *ref* (best-effort).

        Returns ``[]`` on 404 (code-scanning not enabled) or any other
        non-403 error.  Raises :exc:`CodeScanningAlertsUnavailable` on 403
        (token lacks the ``security-events`` / Code-scanning-alerts read
        permission) — alerts exist but are unreadable.
        """
        try:
            r = self._http.get(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/code-scanning/alerts",
                params={"ref": ref, "state": "open", "per_page": 50},
            )
            if r.status_code == 403:
                raise CodeScanningAlertsUnavailable(
                    f"Code-scanning alerts API returned 403 for ref {ref} — "
                    "the token lacks the 'security-events' / Code-scanning "
                    "alerts read permission."
                )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            raw = r.json()
        except CodeScanningAlertsUnavailable:
            raise
        except Exception:  # noqa: BLE001 — best-effort enrichment, never fatal
            return []
        return raw if isinstance(raw, list) else []

    def _wait_for_code_scanning_analysis(
        self, *, owner: str, repo: str, pr_ref: str
    ) -> list[dict]:
        """Poll code-scanning analyses for *pr_ref* and retry the alert fetch.

        When default-setup CodeQL analysis has completed but its alerts
        haven't been indexed yet (eventual-consistency timing gap), the
        merge-ref query returns ``[]``.  This polls the analyses endpoint
        with bounded exponential backoff (2s / 4s / 8s / 16s / 30s ≈ 60s
        window) and re-queries alerts once an analysis is visible.

        Returns the raw alert list (may be empty when still unavailable
        after the backoff window).  Raises :exc:`CodeScanningAlertsUnavailable`
        on 403 at either endpoint.
        """
        import time

        # Check whether any analysis exists on this ref at all.
        try:
            r = self._http.get(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/code-scanning/analyses",
                params={"ref": pr_ref, "per_page": 1},
            )
            if r.status_code == 403:
                raise CodeScanningAlertsUnavailable(
                    f"Code-scanning analyses API returned 403 for ref {pr_ref} — "
                    "the token lacks the 'security-events' / Code-scanning "
                    "alerts read permission."
                )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            raw_analyses = r.json()
            analyses = raw_analyses if isinstance(raw_analyses, list) else []
        except CodeScanningAlertsUnavailable:
            raise
        except Exception:  # noqa: BLE001 — best-effort
            return []

        if not analyses:
            # No analysis exists on this ref → alerts are genuinely absent.
            return []

        # An analysis exists — poll for alerts with exponential backoff.
        for delay in (2, 4, 8, 16, 30):
            time.sleep(delay)
            try:
                r2 = self._http.get(  # type: ignore[attr-defined]
                    f"/repos/{owner}/{repo}/code-scanning/alerts",
                    params={"ref": pr_ref, "state": "open", "per_page": 50},
                )
                if r2.status_code == 403:
                    raise CodeScanningAlertsUnavailable(
                        f"Code-scanning alerts API returned 403 for ref {pr_ref} — "
                        "the token lacks the 'security-events' / Code-scanning "
                        "alerts read permission."
                    )
                if r2.status_code == 404:
                    return []
                r2.raise_for_status()
                raw_alerts = r2.json()
                if isinstance(raw_alerts, list) and raw_alerts:
                    return raw_alerts
            except CodeScanningAlertsUnavailable:
                raise
            except Exception:  # noqa: BLE001 — best-effort
                pass

        # Exhausted backoff window — alerts still unavailable.
        return []

    def list_code_scanning_alerts(self, *, source_branch: str) -> list[dict]:
        """Return open code-scanning (CodeQL) alerts for *source_branch*.

        Queries both the PR merge ref and the branch ref, unioning the
        results (de-duped on the raw alert number) so both CodeQL workflow
        shapes are covered. Each entry is a ``dict`` with ``rule``,
        ``severity``, ``path``, ``line``, ``message``, and ``url``.

        Degrades to ``[]`` when code-scanning is off (404) or any other
        non-403 error occurs.  Raises :exc:`CodeScanningAlertsUnavailable`
        on 403 — the token lacks the ``security-events`` / Code-scanning
        alerts read permission; alerts exist but are unreadable.

        When the merge-ref query returns empty but a PR exists and a recent
        analysis is visible on the analyses endpoint, the method polls with
        exponential backoff (≈60s window) before giving up — this covers the
        eventual-consistency timing gap where default-setup CodeQL analysis
        has completed but its alerts haven't been indexed yet.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        # A CodeQL workflow that only triggers on ``pull_request`` (the common
        # case) files its alerts under the PR merge ref ``refs/pull/{N}/merge``,
        # NOT ``refs/heads/{branch}`` — a feature-branch push never runs that
        # analysis. Resolve the PR for this branch and query BOTH the merge ref
        # and the branch ref, unioning the results (de-duped on the raw alert
        # ``number``) so both workflow shapes are covered. Resolving the PR is
        # best-effort: any failure degrades to the branch-ref-only query.
        try:
            pr = self._get_pr(owner=owner, repo=repo, head=source_branch)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — best-effort; fall back to branch ref
            pr = None

        merge_ref = f"refs/pull/{pr['number']}/merge" if pr is not None else None
        branch_ref = f"refs/heads/{source_branch}"

        seen: set[int] = set()
        raw_alerts: list[dict] = []

        # Merge ref first (PR-triggered CodeQL).  When the initial query
        # returns empty but a PR exists, poll for eventual consistency.
        if merge_ref is not None:
            merge_alerts = self._fetch_alerts_for_ref(
                owner=owner, repo=repo, ref=merge_ref
            )
            if not merge_alerts:
                merge_alerts = self._wait_for_code_scanning_analysis(
                    owner=owner, repo=repo, pr_ref=merge_ref
                )
            for a in merge_alerts:
                num = a.get("number") if isinstance(a, dict) else None
                if num is not None:
                    if num in seen:
                        continue
                    seen.add(num)
                raw_alerts.append(a)

        # Branch ref (push-triggered CodeQL).
        for a in self._fetch_alerts_for_ref(owner=owner, repo=repo, ref=branch_ref):
            num = a.get("number") if isinstance(a, dict) else None
            if num is not None:
                if num in seen:
                    continue
                seen.add(num)
            raw_alerts.append(a)

        out: list[dict] = []
        for a in raw_alerts:
            rule = a.get("rule") or {}
            inst = a.get("most_recent_instance") or {}
            loc = inst.get("location") or {}
            out.append(
                {
                    "number": a.get("number"),
                    "rule": rule.get("id", ""),
                    "severity": rule.get("security_severity_level")
                    or rule.get("severity", ""),
                    "security_severity_level": rule.get("security_severity_level"),
                    "path": loc.get("path", ""),
                    "line": loc.get("start_line"),
                    "message": (inst.get("message") or {}).get("text", "")
                    or rule.get("description", ""),
                    "url": a.get("html_url", ""),
                }
            )
        return out

    def dismiss_code_scanning_alert(
        self, *, number: int, reason: str, comment: str
    ) -> bool:
        """Dismiss a single code-scanning alert by its *number*.

        *reason* must be one of ``"false positive"``, ``"won't fix"``,
        or ``"used in tests"`` (GitHub's required enum — note the spaces,
        not underscores).  *comment* is an optional dismissal note.

        Returns ``True`` on success, ``False`` on any failure.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        try:
            r = self._http.patch(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/code-scanning/alerts/{number}",
                json={
                    "state": "dismissed",
                    "dismissed_reason": reason,
                    "dismissed_comment": comment,
                },
            )
            r.raise_for_status()
            return True
        except Exception:  # noqa: BLE001 — best-effort
            return False
