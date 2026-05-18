"""Best-effort ntfy notification on human-attention states.

Fires a push notification when the worker transitions a ticket into one
of the four human-attention states (``awaiting_approval``, ``in_review``,
``blocked``, ``errored``).  Network errors / timeouts are caught and
logged — the notification is fire-and-forget and never interferes with
ticket processing.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings
from .core.models import Ticket
from .core.states import State

log = logging.getLogger("robotsix_mill.notify")

_TIMEOUT = httpx.Timeout(5.0, read=10.0)

#: States whose worker-driven transitions trigger a notification.
_TRIGGER_STATES: set[State] = {
    State.AWAITING_APPROVAL,
    State.IN_REVIEW,
    State.BLOCKED,
    State.ERRORED,
}


def send_notification(
    ticket: Ticket,
    dst: State,
    note: str | None,
    settings: Settings,
) -> None:
    """Post an ntfy notification for a human-attention transition.

    No-op when ``settings.NTFY_URL`` is unset / empty.
    """
    url = settings.ntfy_url
    if not url:
        return

    headers: dict[str, str] = {
        "X-Title": f"mill: {dst.value} — {ticket.title}",
        "Content-Type": "text/plain",
    }
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"

    body = (
        f"Ticket: {ticket.id}\n"
        f"State: {dst.value}\n"
        f"Note: {note or '(none)'}\n"
        f"Board: {settings.api_url}"
    )

    from .agents.retry import call_with_retry

    def _post() -> None:
        r = httpx.post(url, headers=headers, content=body, timeout=_TIMEOUT)
        r.raise_for_status()

    try:
        # bounded retry on transient (429/5xx/timeout); still fully
        # best-effort — never raises out of here.
        call_with_retry(_post, settings=settings, what="ntfy")
        log.debug("ntfy notification sent for %s -> %s", ticket.id, dst.value)
    except Exception:
        log.warning("ntfy notification failed for %s -> %s", ticket.id, dst.value, exc_info=True)
