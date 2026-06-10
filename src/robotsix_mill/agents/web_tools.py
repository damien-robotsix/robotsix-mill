"""web_fetch tool: GET a URL via the dedicated network-enabled fetch
sandbox (see sandbox.fetch — no repo/data mount, fixed curl).

Per-tool layering on top of the raw sandbox fetch:

1. Per-run URL dedupe — fetches whose canonical URL (scheme + netloc
   + path + query, fragment stripped) match a prior call return the
   cached response instantly. The trace that motivated this layer
   (d40e3c9d4fa5add80b2fe313c1d821f2) hit the same Python docs page
   twice in one refine pass, differing only in ``#fragment``.

2. HTML → text extraction — when the body looks like HTML the markup
   is stripped to whitespace-collapsed prose. A 315 KB docs page
   shrinks to ~80 KB of usable text. The agent gets the same
   information; the context costs ~1/4 as many tokens.

3. Post-extraction cap — separate from the curl ``--max-filesize``
   network cap, this bounds what the agent's context actually sees.

Operator can disable layers 1-2 by setting ``web.fetch_raw: true``
in the mill YAML config.
"""

from __future__ import annotations

import html
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

from ..config import Settings

log = logging.getLogger(__name__)

# HTML detection heuristic — match against the first 1 KB of the
# response so we don't scan a multi-MB payload to decide. Any of
# DOCTYPE / <html / <body / <head wins.
_HTML_SNIFF = re.compile(
    rb"<!doctype html|<html[\s>]|<body[\s>]|<head[\s>]",
    re.IGNORECASE,
)

# Tags whose entire content (open → close, inclusive) must be
# dropped before tag stripping. Script and style payloads are
# never useful for an LLM and they're often the bulk of a docs
# page's byte budget.
_BLOCK_TAGS = ("script", "style", "noscript", "svg")

_PER_RUN_CACHE_TTL_SECONDS = 30
# Per-process LRU cache keyed on canonical URL. One entry =
# (timestamp, (returncode, body_text_after_extraction)). Capped at
# ~1 MB total payload to avoid unbounded growth.
_cache: dict[str, tuple[float, tuple[int, str]]] = {}
_cache_max_total_bytes = 1_000_000

# Per-process web_fetch budget, reset once per ``ask_web_knowledge``
# consult (see web_knowledge.run_web_knowledge). The ``*_request_limit``
# knobs count model requests, not tool calls, so they can't bound the
# fetch fan-out; these counters do. Same single-threaded-use assumption
# as ``_cache`` — the tool runs inside the agent's synchronous loop.
_fetch_calls: int = 0
_fetch_bytes: int = 0


def reset_web_fetch_budget() -> None:
    """Zero the per-consult web_fetch budget counters. Called at the
    start of each web-knowledge consult so the budget scopes to one
    ``ask_web_knowledge`` call across every ``web_research`` sub-agent
    it spawns."""
    global _fetch_calls, _fetch_bytes
    _fetch_calls = 0
    _fetch_bytes = 0


def web_fetch_budget() -> tuple[int, int]:
    """Return the current ``(calls, bytes)`` consumed against the
    budget. Exposed for tests."""
    return _fetch_calls, _fetch_bytes


def _budget_sentinel(settings: Settings) -> str | None:
    """Return the budget-exhausted sentinel string when the per-consult
    fetch budget is spent, else ``None``. Checked before a real sandbox
    fetch (cache hits / raw-mode never reach here). A
    ``web_fetch_max_total_bytes`` of 0 disables the byte ceiling."""
    max_calls = settings.web_fetch_max_calls
    max_total_bytes = settings.web_fetch_max_total_bytes
    if _fetch_calls >= max_calls or (
        max_total_bytes > 0 and _fetch_bytes >= max_total_bytes
    ):
        log.info(
            "web_fetch: budget exhausted (%d calls / %d bytes)",
            _fetch_calls,
            _fetch_bytes,
        )
        return (
            "web_fetch budget exhausted for this consult "
            f"(cap: {max_calls} fetches / {max_total_bytes:,} bytes). "
            "Answer from already-fetched content; do not request more "
            "pages."
        )
    return None


def _canonical_url(url: str) -> str:
    """Return the cache-keying form of *url*: scheme + netloc +
    path + query, fragment stripped. Trailing slash on the path
    is preserved (different pages on many servers).

    Returns the input verbatim if it doesn't parse — be permissive
    so a tool call never crashes the parent agent's loop on a
    weird URL.
    """
    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, ""),
    )


def _looks_like_html(body: bytes | str) -> bool:
    """``True`` when the first 1 KB of *body* contains an HTML
    structural marker. Robust against pages that don't start with
    DOCTYPE (e.g. a leading BOM or comment)."""
    if isinstance(body, str):
        body = body.encode("utf-8", errors="ignore")
    return _HTML_SNIFF.search(body[:1024]) is not None


def _strip_block_tag(body: str, tag: str) -> str:
    """Remove every ``<tag>...</tag>`` block (including content) from
    *body*. Case-insensitive; tolerant of attributes on the open tag.

    Implemented with a non-greedy regex rather than a real HTML
    parser — wrong for adversarial input, fine for the docs pages
    we routinely fetch. The LLM is the eventual consumer, not a
    browser, so layout fidelity doesn't matter."""
    pattern = re.compile(
        rf"<{tag}\b[^>]*>.*?</{tag}\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    return pattern.sub(" ", body)


def html_to_text(body: str) -> str:
    """Strip *body* of HTML markup, return whitespace-collapsed text.

    - drops ``<script>``, ``<style>``, ``<noscript>``, ``<svg>``
      blocks entirely (content + tags);
    - removes all remaining tags;
    - unescapes HTML entities (``&amp;`` → ``&``, ``&nbsp;`` → space);
    - collapses runs of whitespace to a single space, then squashes
      multiple blank lines back to one (preserve paragraph breaks).

    Dependency-free — regex + ``html.unescape``. Adequate for the
    docs pages mill fetches; a malicious page could trick this into
    leaking script content as text, but the agent's context is the
    only consumer and a script-tag dump is just noise to the LLM.
    """
    for tag in _BLOCK_TAGS:
        body = _strip_block_tag(body, tag)
    # Strip remaining tags.
    body = re.sub(r"<[^>]+>", " ", body)
    # Unescape entities.
    body = html.unescape(body)
    # Collapse whitespace: runs of horizontal whitespace → one space,
    # but keep newlines so the LLM still sees paragraph structure.
    body = re.sub(r"[ \t]+", " ", body)
    # Collapse 3+ consecutive newlines (created by the tag stripping)
    # to exactly two.
    body = re.sub(r"\n{3,}", "\n\n", body)
    # Strip leading/trailing whitespace on each line.
    body = "\n".join(line.strip() for line in body.split("\n"))
    # And drop pure-empty surrounding lines.
    return body.strip()


def _prune_cache(now: float) -> None:
    """Drop expired entries and shrink the cache to ≤ the byte
    budget. Called from inside the tool, single-threaded use only
    (the tool runs inside the agent's synchronous loop)."""
    expired = [
        k for k, (ts, _) in _cache.items() if now - ts > _PER_RUN_CACHE_TTL_SECONDS
    ]
    for k in expired:
        _cache.pop(k, None)
    # If still over budget, drop oldest until under.
    while sum(len(v[1][1]) for v in _cache.values()) > _cache_max_total_bytes:
        if not _cache:
            return
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)


def _apply_text_cap(body: str, max_bytes: int) -> str:
    """Truncate *body* if it exceeds *max_bytes* of UTF-8 encoded
    text, appending a clear truncation marker so the agent knows
    it isn't seeing the full payload."""
    encoded = body.encode("utf-8", errors="ignore")
    if len(encoded) <= max_bytes:
        return body
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return (
        f"{truncated}\n"
        f"... [truncated, fetched {len(encoded):,} chars total, "
        f"showing first {max_bytes:,}]"
    )


def make_web_fetch(settings: Settings):  # noqa: C901 — adds a per-consult fetch budget gate to the existing cache/extract/cap pipeline
    """Build the ``web_fetch`` tool exposed to web-knowledge agents.

    The returned callable performs an http(s) GET via the dedicated
    network-enabled fetch sandbox (:func:`sandbox.fetch`) and layers
    per-run URL deduplication, HTML→text extraction, a post-extraction
    size cap, and a per-consult fetch budget on top of the raw fetch.
    Setting ``web.fetch_raw: true`` in the YAML config disables the
    dedup/extraction layers and returns the verbatim curl body.

    Args:
        settings: Application configuration — controls ``web_fetch_raw``,
            the byte/call budgets (``web_fetch_max_calls``,
            ``web_fetch_max_total_bytes``), and the text cap
            (``web_fetch_max_text_bytes``).

    Returns:
        A ``web_fetch(url)`` closure returning the (possibly
        extracted, capped, or cached) text content, or an error /
        budget-exhausted sentinel string.
    """

    def web_fetch(url: str) -> str:
        """Fetch an http(s) URL and return its text content (size
        capped). Use for official docs, source files, package
        metadata. Runs in an isolated, no-local-access network
        container.

        By default the response is processed before being returned to
        the agent:
        - HTML pages are stripped to whitespace-collapsed text;
        - fragment-only URL variants reuse a prior fetch's result
          within the same agent run (~30 s window);
        - the returned text is capped at the YAML config
          ``web.fetch_max_text_bytes`` (default 200 KB).

        Set ``web.fetch_raw: true`` in the YAML config to disable
        extraction + deduplication and get the verbatim curl body
        back.
        """
        from .. import sandbox

        raw_mode = settings.web_fetch_raw
        # Look up cache before the sandbox spawn. Raw-mode skips the
        # cache so the operator-set escape hatch never returns
        # processed bytes.
        canonical = _canonical_url(url)
        now = time.monotonic()
        if not raw_mode:
            _prune_cache(now)
            entry = _cache.get(canonical)
            if entry is not None:
                ts, (rc, body) = entry
                if now - ts <= _PER_RUN_CACHE_TTL_SECONDS:
                    log.info(
                        "web_fetch: cache hit %r (canonical %r)",
                        url,
                        canonical,
                    )
                    return body if rc == 0 else f"fetch failed: {body}"

        # Budget gate — real sandbox fetches only. Cache hits (returned
        # above) and raw_mode (operator escape hatch, below) do NOT
        # count. ``*_request_limit`` bounds model requests, not fetches,
        # so this is the only thing that caps the fetch fan-out across a
        # consult's web_research sub-agents.
        if not raw_mode and (sentinel := _budget_sentinel(settings)) is not None:
            return sentinel

        try:
            rc, body = sandbox.fetch(url, settings=settings)
        except sandbox.SandboxError as e:
            return f"fetch error: {e}"

        if rc != 0:
            return f"fetch failed: {body}"

        if raw_mode:
            return body

        if _looks_like_html(body):
            try:
                body = html_to_text(body)
            except Exception:  # noqa: BLE001 — never crash the tool
                log.warning(
                    "web_fetch: html_to_text failed on %r — returning raw",
                    url,
                    exc_info=True,
                )
        body = _apply_text_cap(body, settings.web_fetch_max_text_bytes)
        _cache[canonical] = (now, (rc, body))
        # Charge the budget for this real (cache-miss, non-raw) fetch —
        # at the same point the result enters the cache, so the byte
        # count reflects what the agent's context actually receives.
        global _fetch_calls, _fetch_bytes
        _fetch_calls += 1
        _fetch_bytes += len(body.encode("utf-8", errors="ignore"))
        return body

    return web_fetch
