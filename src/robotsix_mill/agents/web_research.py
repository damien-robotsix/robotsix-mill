"""Web research sub-agent.

The main (expensive) implement/refine agent must NOT run with
OpenRouter's ``:online`` suffix — that bills a web-search surcharge on
*every* request of a pricey model (a ~$3 ticket was traced to it). It
also bloats the main context with raw pages.

Instead the main agent gets a single ``web_research(query)`` tool. Its
body runs THIS small, cheap, bounded sub-agent — the only place
``:online`` + ``web_fetch`` live — and returns just a tight factual
conclusion. Raw search results / pages never reach the main agent.

``run_web_research`` is the single mockable seam: tests monkeypatch it
(no real LLM/network), exactly like the other agent seams.
"""

from __future__ import annotations

from ..config import Settings, get_secrets

_SYSTEM_PROMPT = """\
You are a focused web research assistant. Given a single query, search
the web and read sources as needed, then return ONE concise factual
conclusion that directly answers it. Include essential specifics
(versions, API names, exact flags) and cite sources inline as bare
URLs. No preamble, no restating the question, no step log — just the
answer. If you cannot find a reliable answer, say so briefly.
When fetching source files from GitHub, GitLab, or Bitbucket, use the
raw content URL (e.g. https://raw.githubusercontent.com/... for GitHub,
or append ?raw=true) to get plain text without navigation HTML.

When checking whether a file exists in a GitHub repo, prefer the GitHub
Contents API (GET /repos/<org>/<repo>/contents/<path>) — it works
across all branches and returns 404 reliably for missing files without
needing to enumerate branch names. If you do use raw URLs: check the
default branch (main/master) first. A 404 there means the file does not
exist — do NOT probe every branch. One 404 on the default branch is
sufficient.
"""


async def run_web_research(*, settings: Settings, query: str) -> str:
    """Run the cheap research sub-agent for ``query`` and return only
    its conclusion string. Bounded by ``web_research_request_limit``.
    Never raises out — research failure degrades to a short message so
    the main agent can carry on."""
    if not get_secrets().openrouter_api_key:
        return "web research unavailable: OPENROUTER_API_KEY is not set"

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    from .base import _aclose_async_client, build_openrouter_model
    from .web_tools import make_web_fetch

    online = ":online" if settings.web_search else ""
    model, client = build_openrouter_model(
        settings, f"{settings.web_research_model}{online}"
    )
    agent = Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        output_type=str,
        tools=[make_web_fetch(settings)],
        name="web_research",
    )
    limits = UsageLimits(request_limit=settings.web_research_request_limit)
    try:
        from .retry import acall_with_retry

        result = await acall_with_retry(
            lambda: agent.run(query, usage_limits=limits),
            what="web_research",
        )
    except Exception as e:  # noqa: BLE001 — degrade, never break the caller
        return f"web research failed: {e}"
    finally:
        await _aclose_async_client(client)
    return str(result.output)
