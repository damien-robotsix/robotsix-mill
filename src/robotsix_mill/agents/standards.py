"""Fetch and cache robotsix-standards content for injection into
agent prompts.

The refine stage calls :func:`fetch_standards_context` during
initialisation so the refine agent can ground its specs in the
fleet-wide conventions documented at
https://damien-robotsix.github.io/robotsix-standards/.

Cached on disk with a TTL (default 72 h, governed by
``web_knowledge_cache_ttl_hours``).  On fetch failure the caller
degrades gracefully — the refine agent is told to mark the spec as
"standards context unavailable."
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from ..config.settings import Settings

log = logging.getLogger(__name__)

# Key standards pages to fetch.  repo-baseline.md covers versioning/
# release, repo lifecycle, CI conventions — the domain where the
# most costly spec-vs-standard conflicts arise (e.g. towncrier vs
# release-please).  The README gives the high-level stack overview.
# distribution-packaging.md and free-tier-only.md carry the explicit
# PROHIBITIONS (no registry publishing, no paid GitHub features) that
# the refine-time standards gate enforces — a draft like "publish
# @robotsix/ui to npm" sailed through refine because neither rule was
# in the fetched context.
_STANDARDS_SOURCE_URLS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/damien-robotsix/robotsix-standards/main/README.md",
    "https://raw.githubusercontent.com/damien-robotsix/robotsix-standards/main/docs/repo-baseline.md",
    "https://raw.githubusercontent.com/damien-robotsix/robotsix-standards/main/docs/distribution-packaging.md",
    "https://raw.githubusercontent.com/damien-robotsix/robotsix-standards/main/docs/free-tier-only.md",
)

# v2: bumped when _STANDARDS_SOURCE_URLS changes so a warm TTL cache
# from the previous URL set cannot serve stale (narrower) content.
_STANDARDS_CACHE_FILENAME = "robotsix-standards-v2.md"


def _standards_cache_file(settings: Settings) -> Path:
    return settings.data_dir / "standards_cache" / _STANDARDS_CACHE_FILENAME


def fetch_standards_context(settings: Settings) -> str:
    """Return up-to-date robotsix-standards content ready for prompt injection.

    Returns the combined markdown of the key standards pages, or an
    empty string when the fetch fails (the caller should degrade by
    telling the agent the standards are unavailable).

    The result is cached on disk; re-fetches only happen when the
    cached copy is older than ``settings.web_knowledge_cache_ttl_hours``.
    """
    cache_file = _standards_cache_file(settings)
    ttl_hours = settings.web_knowledge_cache_ttl_hours

    if cache_file.exists():
        try:
            age_hours = (time.time() - cache_file.stat().st_mtime) / 3600.0
        except OSError:
            age_hours = float("inf")
        if age_hours < ttl_hours:
            try:
                content = cache_file.read_text()
                if content.strip():
                    log.info(
                        "standards: cache hit (%.1f h old, %.1f h ttl)",
                        age_hours,
                        ttl_hours,
                    )
                    return content
            except OSError:
                log.debug("standards: cache read failed, will re-fetch", exc_info=True)

    # Fetch from GitHub raw.
    parts: list[str] = []
    fetch_ok = False
    try:
        with httpx.Client(timeout=30) as client:
            for url in _STANDARDS_SOURCE_URLS:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    parts.append(resp.text)
                    fetch_ok = True
                except Exception:
                    log.warning("standards: failed to fetch %s", url, exc_info=True)
    except Exception:
        log.warning("standards: httpx client setup failed", exc_info=True)

    if not parts:
        if not fetch_ok:
            log.warning("standards: all fetches failed — returning empty")
        return ""

    combined = "\n\n---\n\n".join(parts)

    # Write-through cache.
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(combined)
        log.info("standards: cached %d bytes to %s", len(combined), cache_file)
    except OSError:
        log.debug("standards: cache write failed", exc_info=True)

    return combined
