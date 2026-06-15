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

import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

from robotsix_llmio.core import html_to_text

from ..config import Settings

log = logging.getLogger(__name__)

# HTML detection heuristic — match against the first 1 KB of the
# response so we don't scan a multi-MB payload to decide. Any of
# DOCTYPE / <html / <body / <head wins.
_HTML_SNIFF = re.compile(
    rb"<!doctype html|<html[\s>]|<body[\s>]|<head[\s>]",
    re.IGNORECASE,
)

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

# Per-survey-run web_fetch budget — a second tier that spans the
# entire survey run (not reset between ask_web_knowledge consults).
# Activated only when the survey runner calls reset_trace_web_fetch_budget
# with non-zero caps; otherwise a no-op so refine/implement/answer agents
# are unaffected.
_trace_fetch_calls: int = 0
_trace_fetch_bytes: int = 0
_trace_budget_max_calls: int = 0
_trace_budget_max_bytes: int = 0


def reset_web_fetch_budget() -> None:
    """Zero the per-consult web_fetch budget counters. Called at the
    start of each web-knowledge consult so the budget scopes to one
    ``ask_web_knowledge`` call across every ``web_research`` sub-agent
    it spawns."""
    global _fetch_calls, _fetch_bytes
    _fetch_calls = 0
    _fetch_bytes = 0


def reset_trace_web_fetch_budget(max_calls: int, max_bytes: int) -> None:
    """Zero the per-survey-run web_fetch budget counters and store new caps.

    Call with ``max_calls=0`` AND ``max_bytes=0`` to deactivate the trace
    budget (return to per-consult-only gating). The per-consult budget
    (``reset_web_fetch_budget``) is NOT affected by this call.
    """
    global _trace_fetch_calls, _trace_fetch_bytes
    global _trace_budget_max_calls, _trace_budget_max_bytes
    _trace_fetch_calls = 0
    _trace_fetch_bytes = 0
    _trace_budget_max_calls = max_calls
    _trace_budget_max_bytes = max_bytes


def _trace_budget_sentinel() -> str | None:
    """Return the budget-exhausted sentinel when the per-survey-run
    trace budget is spent, else ``None``. Returns ``None`` when the
    trace budget is inactive (both caps are 0)."""
    if _trace_budget_max_calls <= 0 and _trace_budget_max_bytes <= 0:
        return None
    if _trace_fetch_calls >= _trace_budget_max_calls or (
        _trace_budget_max_bytes > 0 and _trace_fetch_bytes >= _trace_budget_max_bytes
    ):
        log.info(
            "web_fetch: trace budget exhausted (%d calls / %d bytes)",
            _trace_fetch_calls,
            _trace_fetch_bytes,
        )
        return (
            "web_fetch trace budget exhausted for this survey run "
            f"(cap: {_trace_budget_max_calls} fetches / "
            f"{_trace_budget_max_bytes:,} bytes). "
            "Answer from already-retrieved information; do not request "
            "more pages."
        )
    return None


def web_fetch_budget() -> tuple[int, int]:
    """Return the current ``(calls, bytes)`` consumed against the
    budget. Exposed for tests."""
    return _fetch_calls, _fetch_bytes


def _budget_sentinel(settings: Settings) -> str | None:
    """Return the budget-exhausted sentinel string when either the
    per-consult fetch budget OR the per-survey-run trace budget is
    spent, else ``None``. Checked before a real sandbox fetch (cache
    hits / raw-mode never reach here). A ``web_fetch_max_total_bytes``
    of 0 disables the byte ceiling."""
    # Check the per-consult budget first.
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
    # Then check the trace-level (per-survey-run) budget.
    if (ts := _trace_budget_sentinel()) is not None:
        return ts
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
    text, using boundary-aware truncation that preserves sentence /
    paragraph boundaries instead of a hard byte cut."""
    encoded = body.encode("utf-8", errors="ignore")
    if len(encoded) <= max_bytes:
        return body
    from ..core.text_utils import truncate_at_boundary

    return truncate_at_boundary(body, max_bytes)


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

    def web_fetch(url: str) -> str:  # noqa: C901 — budget gates + cache + extract + cap pipeline
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

        # --- trace budget byte ceiling (post-fetch) -------------------
        # The per-survey-run trace budget checks cumulative bytes *after*
        # the body is processed so that the byte count reflects what the
        # agent's context actually receives.  If storing this body would
        # overshoot the trace byte cap, decline the fetch and keep the
        # counters unchanged.
        global _trace_fetch_calls, _trace_fetch_bytes
        if (
            _trace_budget_max_bytes > 0
            and _trace_fetch_bytes + len(body.encode("utf-8", errors="ignore"))
            > _trace_budget_max_bytes
        ):
            log.info(
                "web_fetch: trace byte budget would overshoot (%d + %d > %d)",
                _trace_fetch_bytes,
                len(body.encode("utf-8", errors="ignore")),
                _trace_budget_max_bytes,
            )
            return (
                "web_fetch trace budget exhausted for this survey run "
                f"(cap: {_trace_budget_max_calls} fetches / "
                f"{_trace_budget_max_bytes:,} bytes). "
                "Answer from already-retrieved information; do not "
                "request more fetches."
            )

        _cache[canonical] = (now, (rc, body))
        # Charge both the per-consult and the per-survey-run budgets for
        # this real (cache-miss, non-raw) fetch — at the same point the
        # result enters the cache, so the byte count reflects what the
        # agent's context actually receives.
        global _fetch_calls, _fetch_bytes
        _fetch_calls += 1
        _fetch_bytes += len(body.encode("utf-8", errors="ignore"))
        _trace_fetch_calls += 1
        _trace_fetch_bytes += len(body.encode("utf-8", errors="ignore"))
        return body

    return web_fetch
