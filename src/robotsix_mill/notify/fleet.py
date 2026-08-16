"""Fleet-wide notification publisher — the replacement for per-component ntfy.

Posts structured JSON notifications to a fleet notification endpoint
(robotsix-chat or a fleet aggregator).  In-memory suppression prevents
notification storms: when the same (state, board) fires repeatedly within
a short window, only the first notification is sent immediately, and the
next one after the cooldown includes a ``suppressed_count``.

Delivery is best-effort and never raises — the mill pipeline must never
block on notification delivery.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from ..core.models import Ticket
from ..core.states import State

log = logging.getLogger("robotsix_mill.notify.fleet")

# Map mill states to fleet severity levels.
_SEVERITY_MAP: dict[State, str] = {
    State.BLOCKED: "critical",
    State.ERRORED: "warning",
    State.HUMAN_ISSUE_APPROVAL: "info",
    State.HUMAN_MR_APPROVAL: "info",
}

# Map mill states to fleet categories.
_CATEGORY_MAP: dict[State, str] = {
    State.BLOCKED: "blocked",
    State.ERRORED: "errored",
    State.HUMAN_ISSUE_APPROVAL: "human_approval",
    State.HUMAN_MR_APPROVAL: "human_approval",
    State.AWAITING_USER_REPLY: "awaiting_user_reply",
}

# Window (seconds) during which duplicate notifications for the same
# (state, board_url) are suppressed rather than sent individually.
_DEDUP_WINDOW: float = 60.0


@dataclass
class _DedupEntry:
    """State for a single dedup key."""

    last_sent: float = 0.0
    suppressed_count: int = 0


class FleetNotifier:
    """Fire-and-forget publisher to the fleet notification endpoint.

    Thread-safe.  A single module-level instance (``_fleet_notifier``)
    is used by ``send_notification``.
    """

    def __init__(self, fleet_url: str, fleet_token: str | None = None):
        self._fleet_url: str = fleet_url.rstrip("/")
        self._fleet_token: str | None = fleet_token
        self._lock: threading.Lock = threading.Lock()
        self._dedup: dict[str, _DedupEntry] = {}
        self._client: httpx.Client | None = None

    @property
    def url(self) -> str:
        """The configured fleet notification endpoint URL."""
        return self._fleet_url

    def notify(
        self,
        ticket: Ticket,
        dst: State,
        note: str | None,
        board_url: str,
    ) -> None:
        """Post a notification to the fleet endpoint (best-effort).

        Suppresses duplicates within the dedup window — the first
        notification fires immediately; subsequent ones for the same
        (state, board) are counted and summarised after the window
        expires.
        """
        dedup_key = f"{dst.value}:{board_url}"

        with self._lock:
            entry = self._dedup.get(dedup_key)
            now = time.monotonic()

            if entry is not None and (now - entry.last_sent) < _DEDUP_WINDOW:
                entry.suppressed_count += 1
                log.debug(
                    "fleet notify suppressed %r (count=%d, dedup_key=%r)",
                    dst.value,
                    entry.suppressed_count,
                    dedup_key,
                )
                return

            # Either first time or cooldown expired — capture suppressed
            # count before resetting.
            suppressed = entry.suppressed_count if entry else 0
            self._dedup[dedup_key] = _DedupEntry(last_sent=now, suppressed_count=0)

        severity = _SEVERITY_MAP.get(dst, "info")
        category = _CATEGORY_MAP.get(dst, "unknown")

        payload: dict[str, object] = {
            "source": "mill",
            "severity": severity,
            "category": category,
            "ticket_id": ticket.id,
            "ticket_title": ticket.title,
            "state": dst.value,
            "note": note or "",
            "board_url": board_url,
            "dedup_key": dedup_key,
            "suppressed_count": suppressed,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        self._post(payload)

    def _post(self, payload: dict[str, object]) -> None:
        """POST the payload to the fleet endpoint.  Never raises."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._fleet_token:
            headers["Authorization"] = f"Bearer {self._fleet_token}"

        from ..agents.retry import call_with_retry

        def _do_post() -> None:
            client = self._client or httpx
            r = client.post(
                self._fleet_url,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(5.0, read=10.0),
            )
            r.raise_for_status()

        try:
            call_with_retry(_do_post, what="fleet-notify")
            log.debug(
                "fleet notify sent: %s -> %s", payload["ticket_id"], payload["state"]
            )
        except Exception:
            log.warning(
                "fleet notify failed for %s -> %s",
                payload["ticket_id"],
                payload["state"],
                exc_info=True,
            )


# Module-level singleton — initialised when fleet_notify_url is configured.
_fleet_notifier: FleetNotifier | None = None
_fleet_notifier_lock: threading.Lock = threading.Lock()


def get_fleet_notifier(fleet_url: str, fleet_token: str | None = None) -> FleetNotifier:
    """Return (or create) the module-level FleetNotifier singleton."""
    global _fleet_notifier
    key = (fleet_url, fleet_token)
    if (
        _fleet_notifier is None
        or (_fleet_notifier.url, _fleet_notifier._fleet_token) != key
    ):
        with _fleet_notifier_lock:
            if (
                _fleet_notifier is None
                or (_fleet_notifier.url, _fleet_notifier._fleet_token) != key
            ):
                _fleet_notifier = FleetNotifier(fleet_url, fleet_token)
    return _fleet_notifier


def _reset_fleet_notifier() -> None:
    """Reset the module-level singleton (test helper)."""
    global _fleet_notifier
    with _fleet_notifier_lock:
        _fleet_notifier = None
