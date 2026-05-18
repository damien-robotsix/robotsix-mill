"""The refine agent: a capable model that authors the spec directly,
delegating only external lookups to the cheap web_research sub-agent.

Refine runs before the repo is cloned, so it has no `explore` (no
repo yet) — it gets `web_research` to resolve anything the draft
references that it can't from the draft text alone. ``run_refine_agent``
is the seam tests monkeypatch to avoid the network/LLM.
"""

from __future__ import annotations

from ..config import Settings

SYSTEM_PROMPT = """\
You turn a rough ticket draft into a precise, self-contained
engineering spec an autonomous coder can implement without asking
questions.

- If the draft references something you cannot resolve from its own
  text (a library/API/standard/best practice), use `web_research` to
  clarify. Skip it when the draft is self-contained.
- Output Markdown only, with these sections:
  ## Problem — what & why, one short paragraph.
  ## Scope — concrete changes, as bullets.
  ## Acceptance criteria — checklist an automated reviewer can verify.
  ## Out of scope / constraints — what NOT to do, assumptions.
- Stay faithful to the draft's intent; invent nothing unrelated. Be
  concrete and testable. Output the spec only — no preamble, no fences.
"""


def run_refine_agent(*, settings: Settings, title: str, draft: str) -> str:
    """Return the refined Markdown spec. Raises RuntimeError if no
    OpenRouter key is configured (build_agent enforces this)."""
    from .base import build_agent
    from .retry import call_with_retry

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        web=True,  # cheap web_research sub-agent only
        model_name=settings.refine_model,
    )
    result = call_with_retry(
        lambda: agent.run_sync(
            f"<title>{title}</title>\n<draft>\n{draft}\n</draft>"
        ),
        settings=settings, what="refine",
    )
    return str(result.output).strip()
