"""Deploy-server integration: worker-image freshness checks.

The deploy server exposes a ``GET /services/mill`` endpoint that returns
the running and latest image digests for the mill worker.  When the two
diverge (a newer image is available but hasn't been deployed yet), a
retry of a previously-blocked ticket is likely to fail with the same
error — the fix hasn't reached the running worker.

This module provides a lightweight freshness check that the implement
preflight and resume-blocked paths gate on.

Config-standard footprint validation
------------------------------------

The module also provides :func:`validate_config_standard_footprint`,
a deploy-time gate that rejects deployments carrying a stray local
``_standards/`` copy of the standards contract.  The canonical
standard/doc sources are ``robotsix-config`` and ``robotsix-standards``
only — individual repos must **NOT** carry local ``_standards/`` copies.

Note: this gate operates on a checked-out repo tree, so — unlike the
pre-merge ``scripts/check_config_standard_footprint.py`` diff check — it
cannot tell whether a change is a config-standard-compliance PR.  It
therefore only flags the one artifact that is *never* legitimate (a
``_standards/`` copy) and leaves ordinary repo yaml alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

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


def validate_config_standard_footprint(
    repo_dir: str | Path, diff_files: set[str] | None = None
) -> list[str]:
    """Validate the config-standard file footprint in *repo_dir*.

    Returns a list of stray config-standard artifacts (a local
    ``_standards/`` copy) found in *repo_dir*.  An empty list means the
    footprint is clean.

    When *diff_files* is provided, only paths that appear in that set
    (or whose parent directories appear) are reported — pre-existing
    files that the ticket branch never touched are silently allowed.
    This prevents the gate from blocking a ticket for fleet-standard
    files like ``.pre-commit-config.yaml`` that live in the repo but
    are not part of the branch's change set.

    This is a deploy-time gate — call it before pushing to catch a stray
    ``_standards/`` copy that would otherwise reach the target repo.
    Ordinary repo yaml (``config/default.yaml``, ``docker-compose.yml``,
    ``.pre-commit-config.yaml``, ``mkdocs.yml``, …) is intentionally left
    alone; enforcing the four-file footprint against a full checkout would
    block every normal delivery.
    """
    repo = Path(repo_dir)
    violations: list[str] = []

    # Only flag a genuine stray config-standard artifact: a local
    # ``_standards/`` copy of the standards contract that must not ship
    # to the target repo.
    #
    # We deliberately do NOT glob every ``*.yaml`` / ``*.yml`` file and
    # flag those outside the four-file footprint: ordinary repos legitimately
    # carry many unrelated yaml files (``config/default.yaml``, a root
    # ``docker-compose.yml``, ``.pre-commit-config.yaml``, ``mkdocs.yml``,
    # GitHub workflow yaml, …). That over-broad rule treated every such file
    # as an out-of-footprint config-standard artifact and blocked deploys
    # fleet-wide.
    for suspect in ("_standards", "_standards/"):
        if (repo / suspect).exists():
            if _is_in_diff(suspect, diff_files):
                violations.append(suspect)

    return violations


def _is_in_diff(path: str, diff_files: set[str] | None) -> bool:
    """Return True when *path* should be checked against the footprint.

    When *diff_files* is ``None``, all paths pass (backward-compatible
    behaviour — no diff filtering).  When *diff_files* is provided,
    *path* passes only when it (or any file under it, for directory
    paths) appears in the diff set.
    """
    if diff_files is None:
        return True

    # Direct match: the exact file path was in the diff.
    if path in diff_files:
        return True

    # Directory match: a file inside the directory was in the diff.
    # e.g. path="_standards" matches diff_files entry
    # "_standards/foo.yaml".
    prefix = path.rstrip("/") + "/"
    for df in diff_files:
        if df == path or df.startswith(prefix):
            return True

    return False
