"""HTTP artefact retrieval in a sandboxed container."""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import uuid
from urllib.parse import urlsplit

from ..config import Settings

# Redirect hops followed (each one re-validated) before giving up.
_MAX_REDIRECTS = 5

# Write-out marker curl prints to stderr after the transfer so the
# redirect target can be re-validated here instead of curl following it.
_REDIRECT_MARKER = "__mill_redirect__:"

# Hostnames refused before any resolution (cloud metadata aliases).
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "metadata", "metadata.google.internal", "metadata.goog"}
)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _is_blocked(ip: IPAddress) -> bool:
    """True for any address a model-driven fetch must never reach.

    Denies private (incl. 10/8, 172.16/12, 192.168/16, fc00::/7),
    loopback, link-local (incl. 169.254.169.254 metadata), multicast,
    unspecified, reserved, CGNAT, and IPv6 forms embedding a blocked
    IPv4 address (mapped, 6to4, Teredo, NAT64).
    """
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(_is_blocked(e) for e in embedded):
            return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _validate(url: str) -> tuple[str, str | None]:
    """Resolve and vet ``url``; return ``(error, None)`` or ``("", resolve)``.

    ``resolve`` is a curl ``--resolve host:port:addr`` entry pinning the
    connection to the validated address (``""`` for an IP-literal host),
    so curl cannot re-resolve to a different (internal) address.
    """
    if not url.startswith(("http://", "https://")):
        return f"refused: only http(s) URLs allowed: {url!r}", None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return f"refused: malformed URL: {url!r}", None
    if not host:
        return f"refused: URL has no host: {url!r}", None
    host = host.rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        return f"refused: blocked host {host!r}", None
    try:
        literal: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked(literal):
            return f"refused: blocked address {host!r}", None
        return "", ""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        return f"refused: cannot resolve {host!r}: {e}", None
    addrs = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    if not addrs:
        return f"refused: cannot resolve {host!r}", None
    blocked = [str(a) for a in addrs if _is_blocked(a)]
    if blocked:
        return f"refused: {host!r} resolves to blocked address {blocked[0]}", None
    pinned = addrs[0]
    pin = f"[{pinned}]" if pinned.version == 6 else str(pinned)
    return "", f"{host}:{port}:{pin}"


def fetch(url: str, *, settings: Settings) -> tuple[int, str]:
    """HTTP(S) GET ``url`` in a dedicated, network-ENABLED container.

    Deliberately weaker isolation than :func:`run` (network is on), so
    it is locked down the other way: NO repo/data mount (nothing local
    to exfiltrate), non-root, read-only, caps dropped, no-new-privs,
    fixed ``curl`` (not a shell — the URL is a plain argv item, no
    injection), size/time capped. http(s) only.

    SSRF guard (robotsix-standards ssrf-hardened-fetchers.md): the host
    is resolved here and every address checked by :func:`_is_blocked`;
    curl is pinned to the vetted address (``--resolve``) and does NOT
    follow redirects — each ``Location`` hop is re-validated here, up to
    ``_MAX_REDIRECTS``. Residual risk: an agent can encode data into the
    URL it asks to fetch.
    """
    for _hop in range(_MAX_REDIRECTS + 1):
        error, resolve = _validate(url)
        if resolve is None:
            return 1, error
        rc, body, location = _fetch_once(url, resolve, settings=settings)
        if not location:
            return rc, body
        url = location
    return 1, f"refused: more than {_MAX_REDIRECTS} redirects"


def _fetch_once(url: str, resolve: str, *, settings: Settings) -> tuple[int, str, str]:
    """Run one curl transfer (no redirect following).

    Returns ``(returncode, body, redirect_url)``; ``redirect_url`` is
    ``""`` unless the response was a redirect with a ``Location``.
    """
    # Lazy import to avoid circular dependency with __init__.py.
    from robotsix_mill.sandbox import SandboxError

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
        "-sS",
        "--proto",
        "=http,https",
        "--max-redirs",
        "0",
        "--max-time",
        str(settings.web_fetch_timeout),
        "--max-filesize",
        str(settings.web_fetch_max_bytes),
        "-A",
        "robotsix-mill-fetch",
        "-w",
        f"%{{stderr}}\n{_REDIRECT_MARKER}%{{redirect_url}}\n",
    ]
    if resolve:
        argv += ["--resolve", resolve]
    argv += ["--", url]
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
        return 124, f"fetch timed out after {settings.web_fetch_timeout}s", ""

    # Decode stdout/stderr with replacement for non-UTF-8 bytes
    stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
    body = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""

    location = ""
    kept: list[str] = []
    for line in stderr.splitlines():
        if line.startswith(_REDIRECT_MARKER):
            location = line[len(_REDIRECT_MARKER) :].strip()
        else:
            kept.append(line)
    stderr = "\n".join(kept)

    if r.returncode == 125:
        raise SandboxError(f"docker run failed: {stderr.strip()[:300]}")
    if r.returncode == 0 and location:
        return 0, "", location
    if len(body) > settings.web_fetch_max_bytes:
        body = body[: settings.web_fetch_max_bytes] + "\n... [truncated]"
    if r.returncode != 0:
        body = f"(curl exit {r.returncode}) {stderr.strip()[:300]}\n{body}"
    return r.returncode, body, ""
