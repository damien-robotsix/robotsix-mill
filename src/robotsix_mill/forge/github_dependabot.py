"""GitHub Dependabot vulnerability-alert mixin — alert listing.

Split from ``github.py``.  Defines ``GitHubForgeDependabotMixin`` that
``GitHubForge`` inherits from.

Unlike code-scanning alerts (which are ref-scoped and distinguish a 403
"unreadable" state), Dependabot alerts are repo-level and consumed only by
the deterministic ingest poll loop.  Every failure mode — including a 403
from a token without the ``vulnerability-alerts`` read permission — degrades
to ``[]`` so the poll loop simply files nothing that pass.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class GitHubForgeDependabotMixin:
    """Dependabot vulnerability-alert operations — mixed into ``GitHubForge``.

    Expects ``self._http`` and ``self._owner_repo`` to exist on the final
    class.
    """

    def list_dependabot_alerts(self) -> list[dict[str, Any]]:
        """Return OPEN Dependabot vulnerability alerts for the repo.

        Each entry is a normalized ``dict`` with: ``number``, ``ghsa_id``,
        ``cve_id``, ``severity`` (``critical`` / ``high`` / ``medium`` /
        ``low``), ``package``, ``ecosystem``, ``manifest_path``, ``summary``,
        and ``url``.

        Degrades to ``[]`` on any error (404 = Dependabot alerts disabled,
        403 = token lacks the ``vulnerability-alerts`` read permission, or
        any transport failure).  Paginates up to a bounded number of pages.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        out: list[dict[str, Any]] = []
        # Bounded pagination — 100/page over up to 5 pages = 500 alerts,
        # far beyond any realistic open-alert count for a single repo.
        for page in range(1, 6):
            try:
                r = self._http.get(  # type: ignore[attr-defined]
                    f"/repos/{owner}/{repo}/dependabot/alerts",
                    params={"state": "open", "per_page": 100, "page": page},
                )
                if r.status_code in (403, 404):
                    # 404 = alerts disabled; 403 = token lacks permission.
                    # Both mean "nothing readable" for ingestion purposes.
                    return out
                r.raise_for_status()
                raw = r.json()
            except Exception:  # noqa: BLE001 — best-effort, never fatal
                return out

            if not isinstance(raw, list) or not raw:
                break

            for a in raw:
                if not isinstance(a, dict):
                    continue
                out.append(_normalize_alert(a))

            if len(raw) < 100:
                break  # last page

        return out


def _normalize_alert(a: dict[str, Any]) -> dict[str, Any]:
    """Flatten a raw GitHub Dependabot alert into the ingest dict shape."""
    vuln = a.get("security_vulnerability") or {}
    adv = a.get("security_advisory") or {}
    pkg = vuln.get("package") or {}
    dep = a.get("dependency") or {}

    # Prefer a CVE identifier when present in the advisory's identifier list.
    cve_id = ""
    for ident in adv.get("identifiers") or []:
        if isinstance(ident, dict) and ident.get("type") == "CVE":
            cve_id = ident.get("value", "") or ""
            break

    return {
        "number": a.get("number"),
        "ghsa_id": adv.get("ghsa_id", "") or "",
        "cve_id": cve_id,
        "severity": (vuln.get("severity") or adv.get("severity") or "").lower(),
        "package": pkg.get("name", "") or "",
        "ecosystem": pkg.get("ecosystem", "") or "",
        "manifest_path": dep.get("manifest_path", "") or "",
        "summary": adv.get("summary", "") or "",
        "url": a.get("html_url", "") or "",
    }
