"""HTTP artefact retrieval in a sandboxed container."""

from __future__ import annotations

import subprocess
import uuid

from ..config import Settings


def fetch(url: str, *, settings: Settings) -> tuple[int, str]:
    """HTTP(S) GET ``url`` in a dedicated, network-ENABLED container.

    Deliberately weaker isolation than :func:`run` (network is on), so
    it is locked down the other way: NO repo/data mount (nothing local
    to exfiltrate), non-root, read-only, caps dropped, no-new-privs,
    fixed ``curl`` (not a shell — the URL is a plain argv item, no
    injection), size/time capped. Residual risk: an agent can encode
    data into the URL it asks to fetch. http(s) only.
    """
    # Lazy import to avoid circular dependency with __init__.py.
    from robotsix_mill.sandbox import SandboxError

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
