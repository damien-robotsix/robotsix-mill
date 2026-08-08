"""Outbound event notifier — pushes ticket state-change events to
configured subscriber URLs via HTTP POST.

Delivery is best-effort and asynchronous: a dead or slow subscriber
must never block or fail a ticket state transition.  Each delivery
spawns a daemon thread that makes up to 3 attempts with exponential
backoff, then drops the event (logging the failure at warning level).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import httpx

from .models import Ticket

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 1.0  # seconds
_MAX_BACKOFF = 10.0  # seconds
_REQUEST_TIMEOUT = 10.0  # seconds


class EventNotifier:
    """Delivers ticket state-change events to configured subscribers.

    Instantiated once at startup and wired into the ``_on_transition``
    hook.  ``notify()`` is thread-safe — it can be called from any
    thread (the worker threadpool or the event-loop thread).
    """

    def __init__(
        self,
        subscriber_urls: list[str],
        shared_secret: str | None = None,
    ) -> None:
        self._urls = list(subscriber_urls)
        self._secret = shared_secret

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify(self, ticket: Ticket, old_state: str) -> None:
        """Fire a state-change event to every configured subscriber.

        Spawns one daemon thread per subscriber URL.  Returns
        immediately — each thread owns its own retry loop and
        ``httpx.Client``.
        """
        if not self._urls:
            return
        payload = _build_payload(ticket, old_state)
        data = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Mill-Event-Secret"] = self._secret
        for url in self._urls:
            t = threading.Thread(
                target=_deliver,
                args=(url, data, headers),
                daemon=True,
                name=f"event-notifier-{url[-40:]}",
            )
            t.start()

    def notify_sync(self, ticket: Ticket, old_state: str) -> None:
        """Deliver synchronously — used only in tests."""
        if not self._urls:
            return
        payload = _build_payload(ticket, old_state)
        data = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Mill-Event-Secret"] = self._secret
        for url in self._urls:
            _deliver(url, data, headers)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _build_payload(ticket: Ticket, old_state: str) -> dict[str, object]:
    """Build the JSON payload for a state-change event."""
    return {
        "ticket_id": ticket.id,
        "board_id": ticket.board_id,
        "old_state": old_state,
        "new_state": ticket.state.value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _deliver(url: str, data: str, headers: dict[str, str]) -> None:
    """POST *data* to *url* with up to ``_MAX_ATTEMPTS`` retries.

    Exceptions and non-2xx responses are logged and retried; after the
    last attempt the event is dropped with a warning.  Never raises.
    """
    last_status: int | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                resp = client.post(url, content=data, headers=headers)
            if resp.is_success:
                log.debug(
                    "event-notifier: delivered %s → %s (attempt %d, HTTP %d)",
                    _ticket_id_from(data),
                    url,
                    attempt,
                    resp.status_code,
                )
                return
            last_status = resp.status_code
        except Exception:
            log.debug(
                "event-notifier: delivery attempt %d to %s failed",
                attempt,
                url,
                exc_info=True,
            )
        if attempt < _MAX_ATTEMPTS:
            backoff = min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_BACKOFF)
            time.sleep(backoff)
    log.warning(
        "event-notifier: dropped event %s → %s after %d attempts (last HTTP %s)",
        _ticket_id_from(data),
        url,
        _MAX_ATTEMPTS,
        last_status if last_status is not None else "error",
    )


def _ticket_id_from(data: str) -> str:
    """Extract the ticket_id from a JSON payload string for logging."""
    try:
        obj: dict[str, object] = json.loads(data)
        return str(obj.get("ticket_id", "?"))
    except Exception:
        return "?"
