"""Best-effort notification on human-attention states.

Dispatches to the fleet notification endpoint when the worker transitions
a ticket into one of the four human-attention states (``human_issue_approval``,
``human_mr_approval``, ``blocked``, ``errored``).  Network errors / timeouts
are caught and logged — the notification is fire-and-forget and never
interferes with ticket processing.
"""

from __future__ import annotations

import logging

from ..config import Settings, get_secrets
from ..core.models import Ticket
from ..core.states import State

log = logging.getLogger("robotsix_mill.notify")

#: States whose worker-driven transitions trigger a notification.
_TRIGGER_STATES: set[State] = {
    State.HUMAN_ISSUE_APPROVAL,
    State.HUMAN_MR_APPROVAL,
    State.BLOCKED,
    State.ERRORED,
}


def send_notification(
    ticket: Ticket,
    dst: State,
    note: str | None,
    settings: Settings,
) -> None:
    """Post a fleet notification for a human-attention transition.

    No-op when ``fleet_notify_url`` is unset / empty.  Falls back to the
    legacy ntfy channel when fleet is not configured and ntfy is.
    """
    secrets = get_secrets()
    fleet_url = secrets.fleet_notify_url
    if fleet_url:
        from .fleet import get_fleet_notifier

        notifier = get_fleet_notifier(fleet_url, secrets.fleet_notify_token)
        notifier.notify(ticket, dst, note, str(settings.api_url))
        return

    # Legacy ntfy fallback — kept for existing deployments that have
    # ntfy configured but not yet the fleet endpoint.
    ntfy_url = secrets.ntfy_url
    if ntfy_url:
        _send_ntfy(ticket, dst, note, settings, ntfy_url)
        return

    # Neither fleet nor ntfy configured — silent no-op.
    log.debug("no notification channel configured for %s -> %s", ticket.id, dst.value)


# ---------------------------------------------------------------------------
# Legacy ntfy delivery (kept as fallback)
# ---------------------------------------------------------------------------


def _send_ntfy(
    ticket: Ticket,
    dst: State,
    note: str | None,
    settings: Settings,
    ntfy_url: str,
) -> None:
    """Post an ntfy notification for a human-attention transition.

    Kept as a fallback for deployments that have ntfy configured but not
    yet the fleet notification endpoint.
    """
    import httpx

    # HTTP headers must be ASCII/latin-1; an em-dash (or any non-ASCII
    # in the ticket title) makes httpx raise UnicodeEncodeError and the
    # whole notification fails. Use a plain hyphen and coerce the title
    # to ASCII (ntfy shows '?' for stripped chars — far better than no
    # push). The UTF-8 message body is unaffected.
    title = f"mill: {dst.value} - {ticket.title}".encode("ascii", "replace").decode(
        "ascii"
    )
    headers: dict[str, str] = {
        "X-Title": title,
        "Content-Type": "text/plain",
    }
    if get_secrets().ntfy_token:
        headers["Authorization"] = f"Bearer {get_secrets().ntfy_token}"

    body = (
        f"Ticket: {ticket.id}\n"
        f"State: {dst.value}\n"
        f"Note: {note or '(none)'}\n"
        f"Board: {settings.api_url}"
    )

    from ..agents.retry import call_with_retry

    def _post() -> None:
        r = httpx.post(
            ntfy_url,
            headers=headers,
            content=body,
            timeout=httpx.Timeout(5.0, read=10.0),
        )
        r.raise_for_status()

    try:
        call_with_retry(_post, what="ntfy")
        log.debug("ntfy notification sent for %s -> %s", ticket.id, dst.value)
    except Exception:
        log.warning(
            "ntfy notification failed for %s -> %s", ticket.id, dst.value, exc_info=True
        )
