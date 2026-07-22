"""Deploy-server integration: worker-image freshness checks.

The deploy server exposes a ``GET /services/mill`` endpoint that returns
the running and latest image digests for the mill worker.  When the two
diverge (a newer image is available but hasn't been deployed yet), a
retry of a previously-blocked ticket is likely to fail with the same
error — the fix hasn't reached the running worker.

This module provides a lightweight freshness check that the implement
preflight and resume-blocked paths gate on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeployStatus:
    """Result of a deploy-freshness query.

    Attributes:
        running_digest: SHA256 digest of the currently running image
            (e.g. ``sha256:97d4ba26…``).
        latest_digest: SHA256 digest of the latest-pushed image
            (e.g. ``sha256:320a7a9a…``).
        update_available: ``True`` when *running_digest* ≠ *latest_digest*.
    """

    running_digest: str
    latest_digest: str
    update_available: bool


def check_deploy_freshness(deploy_api_url: str | None) -> DeployStatus | None:
    """Query the deploy server for worker-image freshness.

    Returns ``None`` when *deploy_api_url* is unset (freshness gate
    disabled), or when the deploy server is unreachable / returns an
    unexpected response (transient infra failure — don't block the
    ticket on it).

    Returns a :class:`DeployStatus` when the server responds with the
    expected ``/services/mill`` payload.
    """
    if not deploy_api_url:
        return None

    # Normalize: prepend https:// when the URL lacks a scheme so that
    # downstream httpx calls (and any callers that construct URLs from
    # this base — e.g. self_restart, component update) don't fail with
    # UnsupportedProtocol.
    if "://" not in deploy_api_url:
        deploy_api_url = f"https://{deploy_api_url}"

    api_base = deploy_api_url.rstrip("/")
    url = f"{api_base}/services/mill"

    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(url)
            r.raise_for_status()
            data: dict[str, object] = r.json()
    except Exception:
        logger.warning(
            "deploy-freshness check failed for %s — treating as reachable "
            "(deploy server unreachable or unexpected response)",
            url,
            exc_info=True,
        )
        return None

    try:
        running = str(data["running_digest"])
        latest = str(data["latest_digest"])
        update_available = bool(data.get("update_available", running != latest))
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "deploy-freshness response from %s missing expected keys "
            "(running_digest, latest_digest): %s",
            url,
            exc,
        )
        return None

    return DeployStatus(
        running_digest=running,
        latest_digest=latest,
        update_available=update_available,
    )
