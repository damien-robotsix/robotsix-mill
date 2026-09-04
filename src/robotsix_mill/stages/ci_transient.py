"""Transient CI failure classifier and automatic re-run logic.

When CI fails, the ci-fix agent may produce an OUT_OF_SCOPE verdict.
Before spawning a blocking ``ci_fix_dependency`` ticket, we classify
the failure as transient (infrastructure flake: network reset,
buildkit boot timeout, runner shutdown, etc.) or deterministic
(lint/test/type error).  Transient failures get automatic CI re-runs
(up to a configurable limit) instead of spawning a fix ticket.
"""

from __future__ import annotations

import re
from typing import Any

# Compiled regex patterns that identify transient / infrastructure CI
# failures.  These are applied against the full failing_summary text,
# which includes check names, annotations, and job logs.
TRANSIENT_PATTERNS: list[re.Pattern[str]] = [
    # Network-level failures
    re.compile(r"ECONNRESET|ECONNREFUSED|econnreset|econnrefused", re.IGNORECASE),
    re.compile(r"connection reset by peer|connection refused", re.IGNORECASE),
    re.compile(r"TLS? handshake.*timeout|SSL.*error", re.IGNORECASE),
    # buildx / Docker infrastructure flakes
    re.compile(r"booting buildkit|cannot boot buildkit", re.IGNORECASE),
    re.compile(r"error pulling.*docker.*timeout", re.IGNORECASE),
    re.compile(
        r"Error response from daemon.*(?:pull|timeout|connection refused)",
        re.IGNORECASE,
    ),
    # Tool / action fetcher failures
    re.compile(r"setup-uv.*failed|astral-sh/setup-uv.*error", re.IGNORECASE),
    re.compile(
        r"Failed to download action|unable to download|download.*failed.*action",
        re.IGNORECASE,
    ),
    # GitHub Actions runner infrastructure failures
    re.compile(
        r"The runner.*has received a shutdown signal|runner.*lost communication",
        re.IGNORECASE,
    ),
    re.compile(r"runner.*is not healthy|runner.*unavailable", re.IGNORECASE),
    re.compile(
        r"The job was canceled|The operation was canceled|job was abandoned",
        re.IGNORECASE,
    ),
    # API rate limiting / server errors
    re.compile(r"API rate limit exceeded|secondary rate limit", re.IGNORECASE),
    re.compile(r"HTTP 5\d\d|502 Bad Gateway|503 Service", re.IGNORECASE),
    re.compile(r"500 Internal Server Error", re.IGNORECASE),
    # Transient package-manager fetch failures
    re.compile(
        r"(?:npm|pip|uv|apt-get|apk).*network.*unreachable",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"Temporary failure resolving|Could not resolve host",
        re.IGNORECASE,
    ),
    # Runner / action-setup infra flakes.  A GitHub action-setup step that
    # dies fetching its version manifest or binary emits a bare
    # ``##[error]fetch failed`` marker, often on a different log line from
    # the ``Run <action>@<ref>`` group header — so a single-line
    # ``setup-uv.*failed`` pattern misses it.  Match the marker directly.
    re.compile(r"##\[error\][^\n]*fetch failed", re.IGNORECASE),
    re.compile(
        r"(?:astral-sh/setup-uv|actions/setup-python|actions/checkout)"
        r"[^\n]*(?:fetch failed|failed to download|unable to download)",
        re.IGNORECASE,
    ),
    # Action reference cannot be resolved (registry/manifest fetch flake).
    re.compile(r"Unable to resolve action", re.IGNORECASE),
    # The hosted runner lost communication with the GitHub Actions service.
    re.compile(r"lost communication with the server", re.IGNORECASE),
    # Rate limiting surfaced as an HttpError from the API/registry.
    re.compile(r"HttpError:\s*rate limit", re.IGNORECASE),
    # Docker Hub throttled / 5xx image pulls.
    re.compile(
        r"toomanyrequests|429 Too Many Requests|"
        r"received unexpected HTTP status:\s*5\d\d",
        re.IGNORECASE,
    ),
    # apt / PyPI mirror 5xx while installing packages.
    re.compile(
        r"(?:apt-get|apt|pip|uv|pypi|pythonhosted)[^\n]*"
        r"(?:HTTP error 5\d\d|5\d\d\s+(?:Server Error|Bad Gateway|"
        r"Service Unavailable))",
        re.IGNORECASE,
    ),
    # External link-checker (mkdocs htmlproofer / lychee) reporting an
    # upstream 5xx for a link OUTSIDE the ticket's diff — e.g. htmlproofer
    # prints "response code 504 means something's wrong" when slsa.dev
    # 504s.  A self-clearing upstream outage, not a diffable failure.
    re.compile(r"response code 5\d\d", re.IGNORECASE),
    # Generic upstream 5xx status phrase (e.g. a bare "504 Gateway Timeout")
    # not already anchored by the HTTP-5xx / "502 Bad Gateway" / "503
    # Service" patterns above.
    re.compile(
        r"\b5\d\d\s+(?:Bad Gateway|Service Unavailable|Gateway Time-?out)\b",
        re.IGNORECASE,
    ),
    # `npm audit` / `npm install` hitting a registry.npmjs.org 5xx — the
    # JS lint job surfaces this as "npm error code E503" or a bare 503 from
    # the registry host.  Throttling/outage on npm's side, self-clearing.
    re.compile(r"npm (?:error|warn)\b[^\n]*\bE?5\d\d\b", re.IGNORECASE),
    re.compile(r"registry\.npmjs\.org[^\n]*\b5\d\d\b", re.IGNORECASE),
]


def is_transient_ci_failure(
    failing_summary: str,
    failing: list[dict[str, Any]] | None = None,
) -> bool:
    """Return ``True`` when *failing_summary* matches a known transient pattern.

    The full *failing_summary* string (check names, annotations, and job
    logs) is searched; *failing* (the raw check dicts) is accepted for
    future use but currently ignored.
    """
    return any(pattern.search(failing_summary) for pattern in TRANSIENT_PATTERNS)
