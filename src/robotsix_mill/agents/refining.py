"""The refine agent: a cheap driver that researches as needed and
delegates the actual spec authoring to the strong model.

The driver (``settings.model``, small context) may ``web_research``
unknowns, then hands a complete context to ``deep_refine`` (the strong
model) and returns the spec it produces verbatim. ``run_refine_agent``
is the seam tests monkeypatch to avoid the network/LLM.
"""

from __future__ import annotations

from ..config import Settings
from .deep import make_deep_refine_tool

SYSTEM_PROMPT = """\
You are an orchestrator on a SMALL-context model. You do NOT write the
spec yourself. Steps:
1. If the draft references anything you cannot resolve from its own
   text (a library, an API, a standard), use `web_research` to clarify.
   Skip this if the draft is self-contained.
2. Assemble a COMPLETE context — the ticket title, the full draft
   verbatim, and any research findings — and pass it to `deep_refine`.
   The strong model returns the finished Markdown spec.
3. Reply with that spec EXACTLY as deep_refine returned it — no
   preamble, no fences, no edits of your own.
"""


def run_refine_agent(*, settings: Settings, title: str, draft: str) -> str:
    """Return the refined Markdown spec. Raises RuntimeError if no
    OpenRouter key is configured (build_agent enforces this)."""
    from .base import build_agent

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        tools=[make_deep_refine_tool(settings)],
        web=True,
    )
    from .retry import call_with_retry

    result = call_with_retry(
        lambda: agent.run_sync(
            f"<title>{title}</title>\n<draft>\n{draft}\n</draft>"
        ),
        settings=settings, what="refine",
    )
    return str(result.output).strip()
