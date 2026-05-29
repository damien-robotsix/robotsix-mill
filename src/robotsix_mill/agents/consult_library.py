"""Library-knowledge consultation sub-agent.

The implement / refine coordinators ask focused questions about library
APIs (e.g. "how does imaplib raise on login failure?", "what's the
FastAPI dependency-injection pattern for a session-scoped DB?"). The
naive answer is ``web_research(...)`` — but that scrapes pages on every
call and burns hundreds of thousands of tokens per consult (the trace
that prompted this module spent $1.10+ on four ``imaplib`` queries the
model already half-knew).

``consult_library`` is a cached intermediate seam:

  1. The runner checks ``<data_dir>/<board>/library_knowledge/<lib>.md``
     for a frontmatter-stamped knowledge file.
  2. **Fresh cache + relevant**: one cheap flash call answers the
     question from the cached body.
  3. **Stale or missing**: one ``web_research`` call (still cheap on
     flash) fetches current info; the curator integrates it into the
     knowledge file (with bumped frontmatter) and answers the question
     in the same structured-output call.

So a stable library asked five times across five tickets costs **one**
web_research, not five — and most subsequent consults are pure-flash
reads from disk.

``run_consult_library`` is the single mockable seam: tests monkeypatch
it (no LLM, no network) exactly like every other agent seam.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

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


class _CuratorResult(BaseModel):
    """Structured output from the refresh-and-answer curator call."""

    answer: str = Field(
        description="Concise factual answer to the caller's question, "
        "directly addressing it. No restating, no preamble. Cite "
        "sources inline as bare URLs when the answer relies on a "
        "specific page.",
    )
    updated_knowledge: str = Field(
        description="The FULL revised knowledge file body — not just "
        "a diff. Merge new info from <fresh_info> into the existing "
        "<cached_knowledge>; keep sections that are still accurate, "
        "remove obsolete claims, add new findings. Do NOT include the "
        "YAML frontmatter (the runner re-stamps it). Markdown headings "
        "for organisation are encouraged.",
    )


_ANSWERER_SYSTEM_PROMPT = (
    "You answer one focused library-API question from a cached "
    "knowledge note. Read the <cached_knowledge> block carefully and "
    "return ONE concise factual answer — directly addressing the "
    "question, no preamble, no restating, no source list. If the "
    "cached note plainly does not cover the question, say so briefly "
    "and stop (do not invent). Cite a specific URL only if the cache "
    "lists one for the relevant section."
)

_CURATOR_SYSTEM_PROMPT = (
    "You are a library-knowledge curator. You receive: (1) a "
    "<cached_knowledge> note (may be empty for a first-time library), "
    "(2) <fresh_info> just retrieved from the web for a focused "
    "query, and (3) the caller's <question>. Your job is to (a) "
    "answer the question concisely using whichever source is more "
    "reliable for that specific point, and (b) emit the FULL revised "
    "knowledge note in ``updated_knowledge`` so future consults of "
    "this library can answer from cache.\n\n"
    "Curation rules:\n"
    "- Merge — do not overwrite. Keep cached sections that are still "
    "accurate; add or correct only what the fresh info changes.\n"
    "- Cite sources inline as bare URLs at the END of the relevant "
    "section, so a future consult can re-verify.\n"
    "- Organise by topic (Authentication, Errors, Common patterns, "
    "Gotchas) — not chronologically. Future questions will be "
    "different from this one; structure for retrieval.\n"
    "- Be concise; this file is loaded into every future consult's "
    "context. Aim for a tight reference, not a tutorial.\n"
    "- Never include the YAML frontmatter in ``updated_knowledge``. "
    "The runner stamps it for you."
)


def _slug(library: str) -> str:
    """Normalize the library name to a filesystem-safe slug."""
    s = library.strip().lower()
    s = _LIBRARY_SLUG_RE.sub("-", s).strip("-")
    return s or "unknown"


def _knowledge_dir(settings: Settings, board_id: str) -> Path:
    if board_id:
        return settings.data_dir / board_id / "library_knowledge"
    return settings.data_dir / "library_knowledge"


def _knowledge_path(settings: Settings, board_id: str, library: str) -> Path:
    return _knowledge_dir(settings, board_id) / f"{_slug(library)}.md"


def _parse_cache(text: str) -> tuple[datetime | None, str]:
    """Return ``(last_updated_utc, body_without_frontmatter)``. When the
    file has no recognised frontmatter the whole text is treated as
    body and ``last_updated`` is ``None`` (forcing a refresh)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    frontmatter, body = m.group(1), m.group(2)
    ts_m = _LAST_UPDATED_RE.search(frontmatter)
    if not ts_m:
        return None, body
    try:
        ts = datetime.fromisoformat(ts_m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None, body
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts, body


def _is_fresh(last_updated: datetime | None, stale_days: int) -> bool:
    if last_updated is None:
        return False
    age = datetime.now(timezone.utc) - last_updated
    return age.days < stale_days


def _stamp_frontmatter(
    library: str,
    body: str,
    sources: list[str] | None = None,
) -> str:
    """Render the frontmatter + body for a knowledge file."""
    now = datetime.now(timezone.utc).isoformat()
    parts = [
        "---",
        f"library: {library}",
        f"last_updated: {now}",
    ]
    if sources:
        parts.append("sources:")
        for src in sources[:10]:
            parts.append(f"  - {src}")
    parts.append("---")
    parts.append("")
    parts.append(body.strip())
    parts.append("")
    return "\n".join(parts)


def run_consult_library(
    *,
    settings: Settings,
    library: str,
    question: str,
    board_id: str = "",
) -> str:
    """Answer *question* about *library*, using a per-library knowledge
    file at ``<data_dir>/<board>/library_knowledge/<lib>.md`` as the
    cache. Refreshes from ``web_research`` only when the file is missing
    or older than ``settings.library_knowledge_stale_days``.

    Never raises — degrades to a short error string so the calling
    coordinator can carry on.
    """
    if not get_secrets().openrouter_api_key:
        return "consult_library unavailable: OPENROUTER_API_KEY is not set"

    path = _knowledge_path(settings, board_id, library)
    cached_body = ""
    last_updated: datetime | None = None
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            last_updated, cached_body = _parse_cache(raw)
        except OSError:
            log.warning(
                "consult_library: %s exists but is unreadable",
                path,
            )

    fresh = _is_fresh(last_updated, settings.library_knowledge_stale_days)

    if fresh and cached_body.strip():
        return _answer_from_cache(
            settings=settings,
            cached_body=cached_body,
            question=question,
        )

    return _refresh_and_answer(
        settings=settings,
        library=library,
        question=question,
        cached_body=cached_body,
        knowledge_path=path,
    )


# ---------------------------------------------------------------------------
# Internal: the two LLM call paths
# ---------------------------------------------------------------------------


def _answer_from_cache(
    *,
    settings: Settings,
    cached_body: str,
    question: str,
) -> str:
    """Single cheap flash call — answer purely from the cached note."""
    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .base import _close_async_client, timeout_http_client
    from .openrouter_cost import CostInstrumentedOpenRouterModel
    from .prompt_blocks import section
    from .retry import call_with_retry

    client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        settings.library_knowledge_model,
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=client,
        ),
    )
    agent = Agent(
        model=model,
        system_prompt=_ANSWERER_SYSTEM_PROMPT,
        output_type=str,
        name="library_answerer",
    )
    prompt = (
        section("cached-knowledge", cached_body)
        + "\n\n"
        + section("question", question)
    )
    limits = UsageLimits(
        request_limit=settings.consult_library_request_limit,
    )
    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt, usage_limits=limits),
            settings=settings,
            what="consult_library:answer",
        )
    except Exception as e:  # noqa: BLE001 — degrade, never break the caller
        return f"consult_library failed: {e}"
    finally:
        _close_async_client(client)
    return str(result.output)


def _refresh_and_answer(
    *,
    settings: Settings,
    library: str,
    question: str,
    cached_body: str,
    knowledge_path: Path,
) -> str:
    """Two-step: web_research → curator merge & answer; persist file."""
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .base import _close_async_client, timeout_http_client
    from .openrouter_cost import CostInstrumentedOpenRouterModel
    from .prompt_blocks import section
    from .retry import call_with_retry
    from .web_research import run_web_research

    # Tighter query helps the web_research sub-agent stay focused on
    # the library rather than the calling ticket's context.
    web_query = f"{library} library: {question}"
    fresh_info = run_web_research(settings=settings, query=web_query)

    client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        settings.library_knowledge_model,
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=client,
        ),
    )
    agent = Agent(
        model=model,
        system_prompt=_CURATOR_SYSTEM_PROMPT,
        output_type=PromptedOutput(_CuratorResult),
        name="library_curator",
    )
    prompt = (
        section("cached-knowledge", cached_body or "(empty — first lookup)")
        + "\n\n"
        + section("fresh-info", fresh_info)
        + "\n\n"
        + section("question", question)
    )
    limits = UsageLimits(
        request_limit=settings.consult_library_request_limit,
    )
    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt, usage_limits=limits),
            settings=settings,
            what="consult_library:curate",
        )
    except Exception as e:  # noqa: BLE001 — degrade
        log.warning("consult_library curator failed: %s", e)
        return f"consult_library failed: {e}"
    finally:
        _close_async_client(client)

    output = result.output
    if not isinstance(output, _CuratorResult):
        # Belt-and-braces: a misconfigured model could return a raw
        # string. Return what's there and skip persistence.
        return str(output)

    # Persist the new cache atomically.
    if output.updated_knowledge and output.updated_knowledge.strip():
        try:
            stamped = _stamp_frontmatter(library, output.updated_knowledge)
            knowledge_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = knowledge_path.with_suffix(".md.tmp")
            tmp.write_text(stamped, encoding="utf-8")
            tmp.replace(knowledge_path)
        except Exception:  # noqa: BLE001 — best-effort
            log.warning(
                "consult_library: could not persist %s",
                knowledge_path,
                exc_info=True,
            )

    return output.answer


def make_consult_library_tool(
    settings: Settings,
    *,
    board_id: str = "",
):
    """Build the ``consult_library`` tool exposed to a coordinator.

    The closure delegates to :func:`run_consult_library` so tests can
    monkeypatch that single seam without touching the registry."""

    def consult_library(library: str, question: str) -> str:
        """Look up information about a Python library or framework.

        Prefer this over ``web_research`` for library API questions —
        it's cheap (cached) and stays current. The first lookup of a
        library does one ``web_research`` and persists the answer
        so future calls about the same library hit the cache.

        Args:
            library: The import name (e.g. ``'imaplib'``, ``'fastapi'``,
                ``'sqlmodel'``). Use the same string for every consult
                so subsequent calls land in the same cache file.
            question: A focused question — one topic per consult
                (e.g. "what exceptions does .login() raise?"). The
                cached file accumulates topics over time.
        """
        return run_consult_library(
            settings=settings,
            library=library,
            question=question,
            board_id=board_id,
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="consult_library",
            description=(
                "Look up information about a library or framework. "
                "Cached per-library; refreshes from the web only when the "
                "cache is stale or doesn't cover the question. Prefer this "
                "over web_research for library API questions."
            ),
            category="exploration",
            parameters={
                "library": "str (import name, e.g. 'imaplib')",
                "question": "str (one focused question)",
            },
        )
    )

    return consult_library


__all__ = [
    "run_consult_library",
    "make_consult_library_tool",
]
