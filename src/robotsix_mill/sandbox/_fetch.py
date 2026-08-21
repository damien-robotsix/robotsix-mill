"""Sandbox fetch: HTTP GET in a dedicated network-enabled container."""

from __future__ import annotations

import logging
import re
import subprocess
import uuid
from datetime import datetime

from ..config import Settings

log = logging.getLogger("robotsix_mill.sandbox._fetch")

# Name prefixes of the disposable sibling containers this module spawns:
# ``run()`` uses ``mill-sbx-*`` and ``fetch()`` uses ``mill-fetch-*``.
_SANDBOX_CONTAINER_PREFIXES = ("mill-sbx-", "mill-fetch-")


class SandboxError(RuntimeError):
    """Infrastructure failure (no Docker, daemon/image error) — distinct
    from the command itself exiting non-zero.
    """


def fetch(url: str, *, settings: Settings) -> tuple[int, str]:
    """HTTP(S) GET ``url`` in a dedicated, network-ENABLED container.

    Deliberately weaker isolation than :func:`run` (network is on), so
    it is locked down the other way: NO repo/data mount (nothing local
    to exfiltrate), non-root, read-only, caps dropped, no-new-privs,
    fixed ``curl`` (not a shell — the URL is a plain argv item, no
    injection), size/time capped. Residual risk: an agent can encode
    data into the URL it asks to fetch. http(s) only.
    """
    if not (url.startswith(("http://", "https://"))):
        return 1, f"refused: only http(s) URLs allowed: {url!r}"

    name = f"mill-fetch-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--read-only",
        "--tmpfs",
        "/tmp",  # nosec B108 — Docker tmpfs INSIDE the sandbox container, not host /tmp
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(settings.sandbox_pids_limit),
        "--memory",
        settings.sandbox_memory,
        settings.fetch_image,
        "-sSL",
        "--max-time",
        str(settings.web_fetch_timeout),
        "--max-filesize",
        str(settings.web_fetch_max_bytes),
        "-A",
        "robotsix-mill-fetch",
        "--",
        url,
    ]
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=False,  # was text=True — avoid UnicodeDecodeError
            timeout=settings.web_fetch_timeout + 15,
        )
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found in the mill image") from e
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        return 124, f"fetch timed out after {settings.web_fetch_timeout}s"

    # Decode stdout/stderr with replacement for non-UTF-8 bytes
    stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
    body = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""

    if r.returncode == 125:
        raise SandboxError(f"docker run failed: {stderr.strip()[:300]}")
    if len(body) > settings.web_fetch_max_bytes:
        body = body[: settings.web_fetch_max_bytes] + "\n... [truncated]"
    if r.returncode != 0:
        body = f"(curl exit {r.returncode}) {stderr.strip()[:300]}\n{body}"
    return r.returncode, body


def _parse_docker_started_at(value: str) -> datetime | None:
    """Parse Docker's ``State.StartedAt`` into an aware ``datetime``.

    Docker emits RFC3339 with up to 9 fractional digits and a ``Z`` suffix
    (e.g. ``2026-06-18T20:34:45.483641388Z``).  Returns ``None`` for the
    zero value (a container that never started) or anything unparseable —
    callers treat ``None`` as "leave it alone".
    """
    value = value.strip()
    if not value or value.startswith("0001-01-01"):
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    # ``datetime.fromisoformat`` accepts at most 6 fractional digits;
    # Docker emits 9, so truncate the fractional part to microseconds.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _list_sandbox_containers() -> list[tuple[str, str]]:
    """Return ``(id, name)`` for every ``mill-sbx-*``/``mill-fetch-*``
    container in ANY state (``docker ps -a``), not just running ones.

    Restarting the mill mid-run kills its in-flight ``docker run`` children,
    leaving their containers stuck in the ``Created`` state (never started).
    Those are invisible to a plain ``docker ps`` (running only), so a
    running-only reaper left them to accumulate and (when the worker blocked
    on the hung ``docker run``) stall the pipeline. Listing all states lets
    the startup reaper (which removes everything, since nothing is legitimately
    running at boot) sweep these leftovers. The age-gated periodic reaper
    still skips ``Created`` containers (they have no StartedAt → treated as
    "leave alone"), so it can't race a sandbox the worker just created.
    Best-effort: an empty list on any Docker CLI failure.
    """
    filters: list[str] = []
    for prefix in _SANDBOX_CONTAINER_PREFIXES:
        filters += ["--filter", f"name={prefix}"]
    try:
        listing = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--no-trunc",
                "--format",
                "{{.ID}}\t{{.Names}}",
                *filters,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return []
    if listing.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in listing.stdout.splitlines():
        cid, _, name = line.partition("\t")
        cid = cid.strip()
        if cid:
            out.append((cid, name.strip() or cid))
    return out
