"""Dependabot alert-ingest poll loop for the ``Worker`` — split out of
``poll_loops``.

Defines ``PollLoopsDependabotMixin``, which files one deduped draft per
open GitHub Dependabot alert, plus the draft formatting helpers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ...config import RepoConfig, get_repos_config
from ...core.models import SourceKind
from ._base import _WorkerBase

log = logging.getLogger("robotsix_mill.worker")


class PollLoopsDependabotMixin(_WorkerBase):
    """Dependabot alert-ingest poll loop, mixed into ``PollLoopsMixin``."""

    async def _poll_one_repo_dependabot(
        self,
        rc: RepoConfig,
        now: float,
        ttl_seconds: int,
        remaining_cap: int,
    ) -> int:
        """Ingest one repo's OPEN Dependabot alerts, filing deduped drafts.

        Returns the number of drafts created (so the caller can enforce a
        per-pass cap across repos).  *remaining_cap* is the number of drafts
        still allowed this pass (``<= 0`` disables filing but still refreshes
        the dedup state).
        """
        from ...core.service import TicketService
        from ...forge import get_forge

        settings = self.ctx.settings
        repo_label = rc.repo_id

        state_dir = settings.data_dir / rc.repo_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "dependabot_ingest_state.json"

        # 1. Load dedup state: {"seen": {alert_key: epoch}}.
        state: dict[str, Any] = {"seen": {}}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text("utf-8"))
            except json.JSONDecodeError, OSError:
                state = {"seen": {}}
        seen = state.setdefault("seen", {})

        # 2. Prune entries older than TTL (alert fixed/dismissed long ago).
        for key in [
            k
            for k, v in seen.items()
            if isinstance(v, (int, float)) and (now - v) > ttl_seconds
        ]:
            del seen[key]

        # 3. List OPEN Dependabot alerts (best-effort; [] on any error).
        forge = get_forge(settings, repo_config=rc)
        alerts = await asyncio.to_thread(forge.list_dependabot_alerts)

        created = 0
        if alerts:
            service = TicketService(settings, board_id=rc.board_id)
            for alert in alerts:
                # Dedup key: ghsa_id+package is stable across alert renumbering
                # (default-setup re-numbers alerts); fall back to the number.
                ghsa = alert.get("ghsa_id") or ""
                package = alert.get("package") or ""
                number = alert.get("number")
                key = f"{ghsa}:{package}" if ghsa else f"num:{number}"
                if key in seen:
                    continue

                if remaining_cap - created <= 0:
                    # Cap reached — do NOT mark seen, so it's filed next pass.
                    log.info(
                        "Dependabot ingest: per-pass cap reached, deferring "
                        "remaining alerts for repo %s",
                        repo_label,
                    )
                    break

                title = _dependabot_title(alert)
                body = _dependabot_body(alert)
                try:
                    await asyncio.to_thread(
                        service.create,
                        title=title,
                        description=body,
                        source=SourceKind.DEPENDABOT_ALERTS,
                    )
                except Exception:
                    log.exception(
                        "Dependabot ingest: failed to create draft for "
                        "alert %s in repo %s",
                        number,
                        repo_label,
                    )
                    continue

                seen[key] = now
                created += 1
                log.info(
                    "Dependabot ingest (%s): filed draft for %s (%s in %s)",
                    repo_label,
                    ghsa or f"#{number}",
                    alert.get("severity", "?"),
                    package or "?",
                )

        # 4. Persist state.
        state_path.write_text(json.dumps(state), "utf-8")
        return created

    async def _dependabot_ingest_poll_loop(self) -> None:
        """Periodic Dependabot vulnerability-alert ingest.

        Iterates every registered repo, lists its OPEN GitHub Dependabot
        alerts via the forge, and files one deduped ``source="dependabot_alerts"``
        draft per new alert so the normal pipeline picks up the dependency
        bump.  Deterministic — no LLM, no Langfuse tracing.

        Gated by ``dependabot_ingest_interval_seconds`` (0 = disabled); cadence by
        ``dependabot_ingest_interval_seconds`` (min 60 s); per-pass draft
        volume by ``dependabot_ingest_max_drafts_per_pass``.
        """
        settings = self.ctx.settings
        interval = max(60, settings.dependabot_ingest_interval_seconds)
        ttl_seconds = 90 * 86400  # 90 days — alerts are long-lived.

        await asyncio.sleep(self._initial_delay("dependabot-ingest", interval))
        while True:
            cap = settings.dependabot_ingest_max_drafts_per_pass
            # cap <= 0 means unlimited; track a large remaining budget.
            remaining = cap if cap > 0 else 1_000_000
            now = time.time()
            try:
                for rc in get_repos_config().repos.values():
                    try:
                        filed = await self._poll_one_repo_dependabot(
                            rc, now, ttl_seconds, remaining
                        )
                        remaining -= filed
                        if remaining <= 0 and cap > 0:
                            break
                    except Exception:
                        log.exception(
                            "Dependabot ingest poll failed for repo %s", rc.repo_id
                        )
            except Exception:
                log.exception("Dependabot ingest poll: could not enumerate repos")
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Dependabot draft formatting helpers
# ---------------------------------------------------------------------------


def _dependabot_title(alert: dict[str, Any]) -> str:
    """Build a concise ticket title for a Dependabot alert."""
    severity = (alert.get("severity") or "unknown").capitalize()
    package = alert.get("package") or "dependency"
    return f"Dependabot: {severity} vulnerability in {package}"


def _dependabot_body(alert: dict[str, Any]) -> str:
    """Build the draft body for a Dependabot alert."""
    lines = [
        f"**Package:** `{alert.get('package', '')}` ({alert.get('ecosystem', '')})",
        f"**Severity:** {alert.get('severity', '')}",
    ]
    if alert.get("ghsa_id"):
        lines.append(f"**Advisory:** {alert['ghsa_id']}")
    if alert.get("cve_id"):
        lines.append(f"**CVE:** {alert['cve_id']}")
    if alert.get("manifest_path"):
        lines.append(f"**Manifest:** `{alert['manifest_path']}`")
    if alert.get("url"):
        lines.append(f"**Alert:** {alert['url']}")
    lines.append("")
    if alert.get("summary"):
        lines.append(alert["summary"])
    lines.append("")
    lines.append(
        "Resolve by upgrading the affected dependency to a non-vulnerable "
        "version (update the lockfile / manifest), then verify the build and "
        "tests pass."
    )
    return "\n".join(lines)
