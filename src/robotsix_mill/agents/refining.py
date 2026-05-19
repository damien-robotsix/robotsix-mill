"""The refine agent: a capable model that authors the spec, grounded
in the ACTUAL repo when a local clone is available.

When the refine stage has cloned the target repo it passes
``repo_dir``; the agent then gets the cheap ``explore`` scout +
read-only ``read_file``/``list_dir`` to ground the spec in real code
(instead of web-fetching the project's own files — slow & indirect).
``web_research`` stays for genuinely external lookups only. With no
repo (no forge configured) it falls back to draft-only as before.
``run_refine_agent`` is the seam tests monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings

SYSTEM_PROMPT = """\
You turn a rough ticket draft into a precise, self-contained
engineering spec an autonomous coder can implement without asking
questions.

- If a repo is available you have `explore` (a scout returning
  concise paths/symbols/line-ranges, never whole files) and
  `read_file`/`list_dir`. USE THEM to ground the spec in the ACTUAL
  codebase — real file paths, existing patterns/conventions, and
  constraints. Do NOT web-fetch the project's own files.
- Use `web_research` ONLY for things not in the repo (a
  library/API/standard/best practice). Skip it when unneeded.
- Output Markdown only, with these sections:
  ## Problem — what & why, one short paragraph.
  ## Scope — concrete changes, as bullets.
  ## Acceptance criteria — checklist an automated reviewer can verify.
  ## Out of scope / constraints — what NOT to do, assumptions.
- The <draft> section may be empty (the user may have only provided a
  title). In that case, derive the spec from the title's intent alone.
- Stay faithful to the draft's intent; invent nothing unrelated. Be
  concrete and testable. Output the spec only — no preamble, no fences.
"""


def run_refine_agent(
    *,
    settings: Settings,
    title: str,
    draft: str,
    repo_dir: Path | None = None,
    reviewer_comments: str | None = None,
) -> str:
    """Return the refined Markdown spec. When ``repo_dir`` is given the
    agent grounds the spec in that local clone via explore/read_file;
    otherwise it works draft-only. When ``reviewer_comments`` is given
    the agent incorporates the feedback into the refined spec. Raises
    RuntimeError if no OpenRouter key is configured (build_agent
    enforces this)."""
    from .base import build_agent
    from .kb import load_kb
    from .retry import call_with_retry

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    # Inject technology constraints KB so the refiner avoids prescribing
    # things that are impossible for the project's stack (e.g.
    # DateTime(timezone=True) on SQLite).
    kb_section = load_kb(settings.kb_dir)
    system_prompt = SYSTEM_PROMPT + kb_section

    agent = build_agent(
        settings,
        system_prompt=system_prompt,
        tools=tools,
        web=True,  # cheap web_research sub-agent (external lookups only)
        model_name=settings.refine_model,
        name="refine",
    )

    # Build user prompt: title, draft, and optionally reviewer feedback.
    user_prompt = f"<title>{title}</title>\n<draft>\n{draft}\n</draft>"
    if reviewer_comments:
        user_prompt += (
            "\n<reviewer_feedback>The reviewer sent this spec back "
            "with the following comments. Address each one in the "
            "revised spec:\n\n"
            f"{reviewer_comments}\n</reviewer_feedback>"
        )

    result = call_with_retry(
        lambda: agent.run_sync(user_prompt),
        settings=settings, what="refine",
    )
    return str(result.output).strip()
