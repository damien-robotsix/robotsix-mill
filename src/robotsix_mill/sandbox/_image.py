"""Resolve the sandbox image to what the deploy actually shipped.

central-deploy deploys the mill by DIGEST, but ``sandbox_image`` names a
TAG (``…/robotsix-mill:main``) that nothing re-pulled, so sandboxes ran a
days-old image invisibly (2026-10-28, found by the startup inventory).

* When ``sandbox_image`` names the running mill's own repository, every
  sandbox create uses the running container's image (digest-pinned).
* Otherwise the configured tag is ``docker pull``-ed at startup and at most
  once per hour before a sandbox create.

Inert until :func:`init_sandbox_image` runs at worker startup, so callers
(and tests) that never initialise it keep the configured image unchanged.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time

from ..config import Settings

log = logging.getLogger("robotsix_mill.sandbox")

PULL_INTERVAL_SECONDS = 3600.0

_lock = threading.Lock()
_initialized = False
# (repository, digest-pinned reference, local image id) of the running mill
# container, or None outside a container / when docker cannot inspect it.
_running: tuple[str, str, str] | None = None
_last_pull: dict[str, float] = {}


def _image_repository(image: str) -> str:
    """Strip the ``@digest`` and ``:tag`` from an image reference."""
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    return name


def _inspect_running_image() -> tuple[str, str, str] | None:
    """Return ``(repository, pinned_ref, image_id)`` of this container.

    Inside a container the hostname is the container id. ``Config.Image`` is
    the reference the container was created from (a ``repo@sha256:…`` under
    central-deploy); ``Image`` is the local image id, always runnable here.
    """
    try:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Config.Image}} {{.Image}}",
                socket.gethostname(),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return None
    parts = proc.stdout.split() if proc.returncode == 0 else []
    if len(parts) != 2:
        return None
    config_image, image_id = parts
    pinned = config_image if "@sha256:" in config_image else image_id
    return _image_repository(config_image), pinned, image_id


def local_image_id(image: str) -> str | None:
    """Return the local image id *image* resolves to, or ``None``."""
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def running_image_id(image: str) -> str | None:
    """Running mill's local image id when *image* names its repository."""
    if _running is None or _image_repository(image) != _running[0]:
        return None
    return _running[2]


def _pull(image: str) -> None:
    try:
        proc = subprocess.run(
            ["docker", "pull", "--quiet", image],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("sandbox image %s: pull failed: %s", image, e)
        return
    if proc.returncode != 0:
        log.warning(
            "sandbox image %s: pull failed: %s", image, proc.stderr.strip()[:200]
        )
    else:
        log.info("sandbox image %s: pulled", image)


def _maybe_pull(image: str) -> None:
    """Pull *image* unless it was pulled (or attempted) within the hour."""
    with _lock:
        now = time.monotonic()
        last = _last_pull.get(image)
        if last is not None and now - last < PULL_INTERVAL_SECONDS:
            return
        _last_pull[image] = now
        _pull(image)


def resolve_sandbox_image(image: str) -> str:
    """Return the reference a sandbox create should use for *image*."""
    if not _initialized:
        return image
    if _running is not None and _image_repository(image) == _running[0]:
        return _running[1]
    _maybe_pull(image)
    return image


def init_sandbox_image(settings: Settings) -> str:
    """Detect the running mill image and resolve ``settings.sandbox_image``.

    Called once at worker startup; logs the reference sandboxes will run.
    """
    global _initialized, _running
    _running = _inspect_running_image()
    _initialized = True
    resolved = resolve_sandbox_image(settings.sandbox_image)
    log.info(
        "sandbox image resolved to %s (configured %s)",
        resolved,
        settings.sandbox_image,
    )
    return resolved


def _reset() -> None:
    """Test hook: return the module to its uninitialised state."""
    global _initialized, _running
    _initialized = False
    _running = None
    _last_pull.clear()
