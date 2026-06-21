"""Web-knowledge sub-agent — the single gateway to the internet.

A multi-turn flash agent that answers focused library / API / framework
questions. It owns a small mill-global knowledge base on disk:

  - ``<data_dir>/web_knowledge/<lib>.md`` — one Markdown file per
    library, frontmatter-stamped with ``last_updated:``.
  - ``<data_dir>/web_knowledge/_general.md`` — cross-library memory
    for cross-cutting notes that don't fit a single library (e.g.
    "OpenRouter caches prompts with N-minute TTL").

The cache is centralized (NOT per-board): library facts like
"imaplib.login raises this exception on Gmail" don't change between
repos, and partitioning them per-board fragmented identical knowledge
across every repo that asked the same question.

The agent is autonomous. It receives the caller's question, sees an
index of every existing knowledge file in its system prompt, and
decides for itself whether the cached files cover the question or
whether to ``web_search`` for fresh information. After answering it
typically updates its own knowledge file so future consults can
answer from cache.

This module replaces the deterministic
``consult_library``+``web_research`` split: the previous design
forced refreshes purely on mtime, which couldn't tell the difference
between "fresh file that covers the question" and "fresh file that
covers a different question on the same library." With a multi-turn
agent that owns its memory, that judgment lives in the model. The
trade-off is real — even the happy path costs 3-4 flash turns
instead of one — but the alternative (the cache returning "I don't
know" until the 30-day TTL expires) is worse.

All other agents lose direct ``web_research`` access; their only
route to the internet is the ``ask_web_knowledge`` tool this module
exposes. That makes the trace cost-attribution cleaner (every web
hit shows up under one agent name) and means the operator can audit
web traffic by reading exactly one place.

``run_web_knowledge`` is the single mockable seam for tests — same
pattern every other agent in this tree follows.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings, get_secrets

log = logging.getLogger(__name__)


_LIBRARY_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_FRONTMATTER_RE = re.compile(
    r"\A---\n(.*?)\n---\n(.*)\Z",
    re.DOTALL,
)
_LAST_UPDATED_RE = re.compile(
    r"^last_updated:\s*(\S+)\s*$",
    re.MULTILINE,
)
_GENERAL_FILENAME = "_general.md"

# Per-survey-run web_search budget — caps web_search invocations across
# all ask_web_knowledge consults in a single survey pass. Activated only
# when the survey runner calls reset_trace_web_search_budget with a
# non-zero cap; otherwise a no-op so other agents are unaffected.
_trace_search_calls: int = 0
_trace_search_max_calls: int = 0


def reset_trace_web_search_budget(max_calls: int) -> None:
    """Zero the per-survey-run web_search counter and set a new cap.

    Call with ``max_calls=0`` to deactivate the trace budget (return to
    unlimited searches across consults). The per-consult web_fetch budget
    is NOT affected by this call.
    """
    global _trace_search_calls, _trace_search_max_calls
    _trace_search_calls = 0
    _trace_search_max_calls = max_calls


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _slug(library: str) -> str:
    s = library.strip().lower()
    s = _LIBRARY_SLUG_RE.sub("-", s).strip("-")
    return s or "unknown"


def _knowledge_dir(settings: Settings) -> Path:
    """Mill-global cache directory. NOT per-board: library facts
    don't change between repos, so partitioning would just fragment
    identical knowledge across boards."""
    return settings.data_dir / "web_knowledge"


def _library_path(settings: Settings, library: str) -> Path:
    return _knowledge_dir(settings) / f"{_slug(library)}.md"


def _general_path(settings: Settings) -> Path:
    return _knowledge_dir(settings) / _GENERAL_FILENAME


def _parse_frontmatter(text: str) -> tuple[datetime | None, str]:
    """Return ``(last_updated, body)`` from a frontmatter-stamped
    knowledge file. Missing / unparseable frontmatter → ``(None, text)``."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    head, body = m.group(1), m.group(2)
    ts_match = _LAST_UPDATED_RE.search(head)
    if not ts_match:
        return None, body
    try:
        ts = datetime.fromisoformat(ts_match.group(1))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts, body
    except ValueError:
        return None, body


def _stamp_frontmatter(library: str, body: str) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return f"---\nlibrary: {library}\nlast_updated: {now}\n---\n{body}"


# ---------------------------------------------------------------------------
# Index of known knowledge — pre-loaded into the agent's system prompt
# ---------------------------------------------------------------------------


def _build_index(settings: Settings) -> str:
    """Render an inline index of every library file + the general
    memory size, so the agent sees what it already has without
    spending a turn on ``list_knowledge_files``. Returns "(empty)"
    when nothing has been cached yet."""
    d = _knowledge_dir(settings)
    if not d.is_dir():
        return "(empty)"
    rows: list[str] = []
    general = _general_path(settings)
    if general.is_file():
        size_kb = general.stat().st_size / 1024
        rows.append(f"- _general memory_ ({size_kb:.1f} KB)")
    for path in sorted(d.glob("*.md")):
        if path.name == _GENERAL_FILENAME:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            ts, _body = _parse_frontmatter(raw)
            size_kb = path.stat().st_size / 1024
            stamp = ts.isoformat() if ts else "(no timestamp)"
            rows.append(
                f"- {path.stem} — last_updated: {stamp}, size: {size_kb:.1f} KB"
            )
        except OSError:
            continue
    return "\n".join(rows) if rows else "(empty)"


# ---------------------------------------------------------------------------
# System prompt — the agent's contract
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT_TEMPLATE = """\
You are the web-knowledge agent. You answer ONE focused question
about a library, framework, API, or technical fact, using a small
on-disk knowledge base plus a web-search tool.

## Your knowledge base

You see an index of files you have written below. Each per-library
file at ``<library>.md`` is a curated reference for that library.
The ``_general memory_`` is for cross-cutting notes that don't fit
a single library (rate-limit math, API quirks, operational facts
you've learned).

<knowledge-index>
{index}
</knowledge-index>

## Procedure

1. Look at the index. Decide what's already relevant:
   - If a library file exists for the topic AND its ``last_updated``
     is recent enough that the answer likely hasn't drifted, read
     it with ``read_library`` and try to answer from there.
   - If the relevant cached file is older than ~{stale_days} days OR doesn't
     exist OR you suspect the question is about something the file
     wouldn't cover (a new feature, an edge case), ``web_search``
     first and integrate.
   - The general memory is worth a glance for cross-cutting facts.
     Don't read it for every consult — only when the question
     plausibly matches its themes.

2. Answer the caller's question concisely. No preamble, no
   restating, no bullet-list of sources. ONE clear factual answer.
   Cite a URL inline (bare URL after the sentence) only when the
   reader will need to re-verify.

3. AFTER answering, if you learned something new (from web_search
   or by stitching together cached facts), persist it:
   - For library-specific facts, call ``update_library(library,
     content)`` with the FULL revised file body. The runner stamps
     the frontmatter for you — do NOT include ``---`` blocks.
   - For cross-cutting / general facts, call ``append_general(note)``.
     One short paragraph per call.

4. Stop. Your final reply to the caller is your answer, not a
   transcript of how you found it.

## Hard rules

- ONE web search per consult is the typical case. Multiple searches
  are fine if they're for distinctly different sub-topics; never
  search just to double-check a fact you already wrote down.
- NEVER answer from training-data memory alone if you can't ground
  the answer in cache or fresh web info — say so briefly and stop.
- Be CONCISE. The caller is another agent; it does not want a
  tutorial. One paragraph + one URL is usually right.
- Library names are normalised to lowercase kebab-case (e.g.
  ``imaplib``, ``fastapi``, ``pydantic-ai``). Use the same string
  the caller used so subsequent consults hit the same file.
"""


# ---------------------------------------------------------------------------
# Tools the web_knowledge agent has access to
# ---------------------------------------------------------------------------


def _make_tools(settings: Settings) -> list:
    """Build the closures the agent calls during a consult."""
    from .web_research import run_web_research

    def list_knowledge_files() -> str:
        """List every library file + general-memory size you have
        written so far. You also see this index in your system
        prompt; call this tool only if you want a refresh after a
        write."""
        return _build_index(settings)

    def read_library(library: str) -> str:
        """Read the cached knowledge file for *library*. Returns the
        full body (no frontmatter) plus the last_updated stamp on the
        first line. Returns ``(not found)`` if the file does not
        exist."""
        path = _library_path(settings, library)
        if not path.is_file():
            return "(not found)"
        try:
            raw = path.read_text(encoding="utf-8")
            ts, body = _parse_frontmatter(raw)
            stamp = ts.isoformat() if ts else "(no timestamp)"
            return f"last_updated: {stamp}\n\n{body}"
        except OSError as e:
            return f"(read error: {e!s})"

    def read_general_memory() -> str:
        """Read the cross-library general memory file. Returns
        ``(empty)`` when nothing has been written yet."""
        path = _general_path(settings)
        if not path.is_file():
            return "(empty)"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            return f"(read error: {e!s})"

    def update_library(library: str, content: str) -> str:
        """Atomically write the full revised body of ``<library>.md``.
        The runner stamps frontmatter (``last_updated:``) for you —
        do NOT include ``---`` blocks in *content*. Replaces the
        file outright; pass the FULL revised note, not a delta."""
        if not content or not content.strip():
            return "skipped: empty content"
        path = _library_path(settings, library)
        try:
            stamped = _stamp_frontmatter(library, content)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(stamped, encoding="utf-8")
            tmp.replace(path)
            return f"updated: {path.name}"
        except OSError as e:
            log.warning("web_knowledge update_library failed: %s", e)
            return f"(write error: {e!s})"

    def append_general_memory(note: str) -> str:
        """Append a short note to the general memory. Use for cross-
        library facts that don't fit a single ``<library>.md``."""
        if not note or not note.strip():
            return "skipped: empty note"
        path = _general_path(settings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            entry = f"\n## {now}\n\n{note.strip()}\n"
            with path.open("a", encoding="utf-8") as f:
                f.write(entry)
            return "appended"
        except OSError as e:
            log.warning("web_knowledge append_general failed: %s", e)
            return f"(write error: {e!s})"

    async def web_search(query: str) -> str:
        """Search the web for *query*. Use sparingly — prefer the
        cached files when they cover the question. Returns the
        web-research sub-agent's distilled conclusion (it has already
        read pages for you)."""
        global _trace_search_calls
        if (
            _trace_search_max_calls > 0
            and _trace_search_calls >= _trace_search_max_calls
        ):
            return (
                "web_search trace budget exhausted for this survey run "
                f"(cap: {_trace_search_max_calls} searches). "
                "Answer from already-retrieved information; do not request "
                "more searches."
            )
        _trace_search_calls += 1
        return await run_web_research(settings=settings, query=query)

    return [
        list_knowledge_files,
        read_library,
        read_general_memory,
        update_library,
        append_general_memory,
        web_search,
    ]


# ---------------------------------------------------------------------------
# Runner — the single mockable seam
# ---------------------------------------------------------------------------


async def run_web_knowledge(
    *,
    settings: Settings,
    question: str,
) -> str:
    """Run one multi-turn consult and return the agent's answer.

    Degrades to a short error string on any failure — never raises;
    the calling coordinator can carry on.
    """
    if not get_secrets().openrouter_api_key:
        return "web_knowledge unavailable: OPENROUTER_API_KEY is not set"

    # Reset the per-consult web_fetch budget so this consult — and every
    # web_research sub-agent it fans out to — shares one fresh fetch/byte
    # allowance. The counter is a process-global with the same single-
    # threaded-use assumption as web_tools._cache (the tool runs inside
    # the agent's synchronous loop); concurrent consults in one process
    # would interleave, an accepted limitation consistent with _cache.
    from .web_tools import reset_web_fetch_budget

    reset_web_fetch_budget()

    # Lazy: keep the test suite hermetic, the core import-light.
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    from .base import _aclose_async_client, build_openrouter_model
    from .retry import acall_with_retry

    model, client = build_openrouter_model(settings.web_knowledge_model)

    index = _build_index(settings)
    agent = Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT_TEMPLATE.format(
            index=index, stale_days=settings.web_knowledge_stale_days
        ),
        output_type=str,
        tools=_make_tools(settings),
        name="web_knowledge",
        retries=2,
    )

    limits = UsageLimits(request_limit=settings.web_knowledge_request_limit)
    try:
        result = await acall_with_retry(
            lambda: agent.run(question, usage_limits=limits),
            what="web_knowledge",
        )
        return str(result.output)
    except Exception as e:
        log.warning("web_knowledge failed: %s", e)
        return f"web_knowledge failed: {e}"
    finally:
        await _aclose_async_client(client)


# ---------------------------------------------------------------------------
# Tool wrapper — exposed to refine / implement / answer / etc.
# ---------------------------------------------------------------------------


def make_ask_web_knowledge_tool(settings: Settings):
    """Build the ``ask_web_knowledge`` tool that calling agents use as
    their single gateway to the internet. Other agents do NOT receive
    a direct ``web_search`` tool — only this gateway."""

    async def ask_web_knowledge(question: str) -> str:
        """Ask the web-knowledge agent ONE focused question about a
        library, framework, API, or technical fact.

        The web-knowledge agent owns a mill-global Markdown knowledge
        base (one file per library + a general-memory file). It
        decides whether the cached files cover your question or
        whether it needs to web-search for fresh information. After
        answering it typically updates its files so the next consult
        can answer from cache.

        Prefer ONE focused question per call (e.g. "what exceptions
        does imaplib.login raise on Gmail?"). Avoid stuffing multiple
        topics into one question — the cached note is then harder
        to retrieve from.

        BUDGET: Each ask_web_knowledge call has a limited request
        budget (8 model turns) and a shared 15-page fetch budget
        (2 MB total text body) per consult. Prefer explore() for
        repo-local questions — ask_web_knowledge is for external
        APIs/frameworks. If the question is about code in a private
        org repo, skip it — web search cannot reach private repos.

        Args:
            question: A focused question. The web-knowledge agent
                figures out which library file (if any) to read and
                whether to refresh from the web.
        """
        return await run_web_knowledge(settings=settings, question=question)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="ask_web_knowledge",
            description=(
                "Ask the web-knowledge agent ONE focused question about a "
                "library, framework, API, or technical fact. The agent "
                "owns a per-repo knowledge base and decides whether to "
                "answer from cache or web-search. This is your ONLY route "
                "to the internet — there is no direct web_search tool. "
                "BUDGET: 8 model turns + 15-page fetch budget per consult. "
                "Prefer explore() for repo-local questions."
            ),
            category="exploration",
            parameters={
                "question": "str (one focused factual question)",
            },
        )
    )

    return ask_web_knowledge


__all__ = [
    "run_web_knowledge",
    "make_ask_web_knowledge_tool",
    "reset_trace_web_search_budget",
]
