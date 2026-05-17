"""The refine agent: turn a rough draft into an actionable spec.

Pure text transformation — no tools, no repo. This is the seam tests
monkeypatch to avoid the network/LLM.
"""

from __future__ import annotations

from ..config import Settings

SYSTEM_PROMPT = """\
You turn a rough ticket draft into a precise, self-contained engineering
spec for an autonomous coding agent that will implement it without
asking questions.

Output Markdown only, with these sections:
- ## Problem — what and why, in one short paragraph.
- ## Scope — bullet list of concrete changes to make.
- ## Acceptance criteria — checklist an automated reviewer can verify.
- ## Out of scope / constraints — what NOT to do, assumptions.

Stay faithful to the draft's intent; do not invent unrelated features.
Be concrete and testable. Output the spec only — no preamble.
"""


def run_refine_agent(*, settings: Settings, title: str, draft: str) -> str:
    """Return the refined Markdown spec. Raises RuntimeError if no
    OpenRouter key is configured (build_agent enforces this)."""
    from .base import build_agent

    agent = build_agent(settings, system_prompt=SYSTEM_PROMPT)
    result = agent.run_sync(
        f"<title>{title}</title>\n<draft>\n{draft}\n</draft>"
    )
    return str(result.output).strip()
