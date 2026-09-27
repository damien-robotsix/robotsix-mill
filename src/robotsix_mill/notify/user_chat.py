"""Operator ``user_chat`` escalation primitive.

A small, ticket-agnostic operator-alert channel.  Where
:func:`..notify.send_notification` announces per-ticket human-attention
transitions, this routes a free-form fleet-wide operator alert (e.g. an
account-block escalation that spans many tickets across boards) to the
same real operator channel — the configured fleet notification endpoint
(``fleet_notify_url``, consumed by robotsix-chat / a fleet aggregator).

Delivery is best-effort and never raises.  The return value tells the
caller whether the alert actually reached a configured channel, so a
caller can fall back to a durable ``log.warning`` when no channel is
configured (or delivery failed).
"""

from __future__ import annotations

import logging
import time

import httpx

from ..config import get_secrets

log = logging.getLogger("robotsix_mill.notify.user_chat")


def escalate_to_user_chat(
    message: str,
    *,
    severity: str = "warning",
    category: str = "operator_alert",
    dedup_key: str | None = None,
) -> bool:
    """Route an operator alert to the ``user_chat`` (fleet) operator channel.

    Posts a structured JSON payload to ``fleet_notify_url``.  Returns
    ``True`` only when the alert was delivered to a configured channel;
    returns ``False`` when no channel is configured or delivery failed, so
    the caller can fall back to logging.  Never raises.
    """
    secrets = get_secrets()
    fleet_url = secrets.fleet_notify_url
    if not fleet_url:
        return False

    payload: dict[str, object] = {
        "source": "mill",
        "severity": severity,
        "category": category,
        "message": message,
        "dedup_key": dedup_key or category,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secrets.fleet_notify_token:
        headers["Authorization"] = f"Bearer {secrets.fleet_notify_token}"

    from ..agents.retry import call_with_retry

    def _post() -> None:
        r = httpx.post(
            fleet_url.rstrip("/"),
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(5.0, read=10.0),
        )
        r.raise_for_status()

    try:
        call_with_retry(_post, what="user-chat")
        log.debug("user_chat escalation sent (category=%s)", category)
        return True
    except Exception:
        log.warning(
            "user_chat escalation failed (category=%s)", category, exc_info=True
        )
        return False
