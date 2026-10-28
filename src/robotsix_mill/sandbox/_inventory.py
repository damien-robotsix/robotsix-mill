"""Startup self-check: which tools the configured sandbox image provides.

Production pins ``sandbox_image`` to the mill image itself, so a tool added
only to ``sandbox/Dockerfile`` silently never reaches a live sandbox (#3499).
Logging the inventory and the pin at worker start makes that visible on the
first pass. Log-only — never changes the configured image.
"""

from __future__ import annotations

import logging
import subprocess

from ..config import Settings
from ._image import (
    _image_repository,
    local_image_id,
    resolve_sandbox_image,
    running_image_id,
)

log = logging.getLogger("robotsix_mill.sandbox")

# Tools the implement agent's gates rely on inside the sandbox.
SANDBOX_TOOLS = ("actionlint", "zizmor", "uvx", "uv", "gh", "git")

# Image repository ``sandbox/Dockerfile`` is published as (release.yml
# ``publish-sandbox``).
SANDBOX_DOCKERFILE_IMAGE = "ghcr.io/damien-robotsix/robotsix-mill-sandbox"

_PROBE = 'for t in "$@"; do if command -v "$t" >/dev/null 2>&1; then echo "+$t"; else echo "-$t"; fi; done'


def probe_sandbox_tools(image: str) -> tuple[list[str], list[str]] | None:
    """Return ``(present, missing)`` tools in *image*, or ``None`` on failure.

    Uses ``--pull=never`` so startup never blocks on a registry pull.
    """
    try:
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                image,
                "-c",
                _PROBE,
                "sh",
                *SANDBOX_TOOLS,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        log.warning(
            "sandbox image %s: tool probe failed: %s",
            image,
            proc.stderr.strip()[:200],
        )
        return None
    lines = proc.stdout.split()
    present = [t for t in SANDBOX_TOOLS if f"+{t}" in lines]
    missing = [t for t in SANDBOX_TOOLS if t not in present]
    return present, missing


def log_sandbox_tool_inventory(settings: Settings) -> None:
    """Log the configured sandbox image's tool inventory and pin drift."""
    image = settings.sandbox_image
    if _image_repository(image) != SANDBOX_DOCKERFILE_IMAGE:
        log.warning(
            "config-pin-drift: sandbox_image=%s is not the image "
            "sandbox/Dockerfile builds (%s); sandbox/Dockerfile changes "
            "do not reach live sandboxes",
            image,
            SANDBOX_DOCKERFILE_IMAGE,
        )
    # Name a stale tag explicitly: the host's cached tag vs the image the
    # running mill was deployed from (2026-10-28: :main was 37 h stale).
    running = running_image_id(image)
    tag_id = local_image_id(image) if running else None
    if running and tag_id and tag_id != running:
        log.warning(
            "sandbox image %s: sandbox tag digest %s != running digest %s",
            image,
            tag_id,
            running,
        )
    # Probe what sandboxes will actually run (digest-pinned or freshly
    # pulled by init_sandbox_image); the probe itself keeps --pull=never.
    image = resolve_sandbox_image(image)
    result = probe_sandbox_tools(image)
    if result is None:
        log.warning("sandbox image %s: tool inventory unavailable", image)
        return
    present, missing = result
    level = logging.WARNING if missing else logging.INFO
    log.log(
        level,
        "sandbox image %s: tools present %s missing %s",
        image,
        present,
        missing,
    )
