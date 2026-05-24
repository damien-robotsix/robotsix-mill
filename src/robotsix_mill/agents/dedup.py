"""Pre-refine dedup / already-done check.

A single cheap LLM call that inspects a draft against existing tickets
and recent commits to decide whether it's a duplicate or already
implemented.  The refiner would be wasted on such drafts — this guard
short-circuits them straight to ``CLOSED`` before the expensive agent
runs.

``run_dedup_check`` is the mockable seam — tests monkeypatch it just
like ``run_refine_agent``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings


class DedupResult(BaseModel):
    """Structured output from the dedup check agent."""
    duplicate_of: str | None = None
    already_done: str | None = None
    reason: str = ""

log = logging.getLogger("robotsix_mill.agents.dedup")

SYSTEM_PROMPT = """\
You are a conservative duplicate detector for an engineering ticket
pipeline.  Your job is to decide — cheaply — whether a draft ticket is
(a) a duplicate of an existing ticket, or (b) already implemented in a
recent commit.  You must be **conservative**: only flag a CLEAR match.
When unsure, return nulls.

## Rules

1. Compare the draft's **intent / change**, not just its area.  Two
   tickets in the same file/feature are NOT duplicates unless they
   describe the exact same change.
2. The draft's own ticket is NEVER a valid match — ignore it even if it
   were to appear in the candidates (it won't, but double-check).
3. For commits: only flag when the commit subject clearly describes the
   same change the draft is asking for.  Vague subjects (e.g. "fix",
   "cleanup") are NOT a match.
4. When you are less than ~90% confident, return null for both fields.

## Ticket verification (primary)

Each entry in `<candidates>` now includes a `body` field — the full
specification that was (or is being) implemented.  Use this as your
primary signal for `already_done`:

1. Scan candidate titles first.  If a CLOSED ticket's title suggests
   overlapping intent, read its `body` and compare the described
   change against the draft's `<title>` and `<body>`.
2. Only flag `already_done` when the completed ticket's body describes
   the same concrete change — not just the same area or component.
3. Non-terminal candidates (DRAFT, READY, ...) are for `duplicate_of`
   detection; their bodies help confirm that two open tickets ask for
   the identical change.

## Commit verification (supplementary)

When `<recent_commits>` is available, use commit subjects as a
supplementary signal — e.g. a commit whose subject clearly matches
the draft AND whose author references a recently-CLOSED ticket.
Without a ticket body to anchor the comparison, commit subjects
alone are too vague to warrant `already_done` on their own.
"""


def _build_prompt(
    *,
    draft_title: str,
    draft_body: str,
    candidates_json: str,
    recent_commits_json: str | None,
) -> str:
    parts = [
        "<draft>",
        f"<title>{draft_title}</title>",
        f"<body>{draft_body}</body>",
        "</draft>",
        "<candidates>",
        candidates_json,
        "</candidates>",
    ]
    if recent_commits_json is not None:
        parts.extend([
            "<recent_commits>",
            recent_commits_json,
            "</recent_commits>",
        ])
    else:
        parts.append("<recent_commits>not available (no repo clone)</recent_commits>")
    return "\n".join(parts)


def run_dedup_check(
    *,
    settings: Settings,
    draft_title: str,
    draft_body: str,
    candidates_json: str,
    recent_commits_json: str | None,
    repo_dir: Path | None = None,
) -> dict:
    """Return ``{"duplicate_of": ..., "already_done": ..., "reason": ...}``.

    Degrades gracefully: on any exception, returns nulls with a failure
    reason — the guard is best-effort and never blocks the pipeline.
    """
    from .base import build_agent, _safe_close
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .retry import call_with_retry

    # Build filesystem tools when a repo_dir is available; the dedup
    # agent is read-only — only read_file and list_dir are exposed.
    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        fs = build_fs_tools(repo_dir, settings)
        tools = [t for t in fs if t.__name__ in ("read_file", "list_dir")]

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(DedupResult),
        model_name=settings.dedup_model,
        name="dedup",
        tools=tools,
    )
    # request_limit must be passed via usage_limits=UsageLimits(...),
    # NOT as a bare run_sync kwarg — the bare kwarg raises
    # UserError("Unknown keyword arguments: request_limit"), which made
    # the dedup check ALWAYS fail (caught best-effort), so dd73 never
    # actually deduped — hence the overlapping-PR churn it was meant to
    # stop. Mirrors coordinating/explore/testing.
    limits = UsageLimits(request_limit=settings.dedup_request_limit)
    try:
        result = call_with_retry(
            lambda: agent.run_sync(
                _build_prompt(
                    draft_title=draft_title,
                    draft_body=draft_body,
                    candidates_json=candidates_json,
                    recent_commits_json=recent_commits_json,
                ),
                usage_limits=limits,
            ),
            settings=settings,
            what="dedup check",
        )
        output = result.output
        if not isinstance(output, DedupResult):
            log.warning("dedup check returned non-DedupResult: %s", type(output))
            return {
                "duplicate_of": None,
                "already_done": None,
                "reason": "dedup check returned unexpected type",
            }
        return {
            "duplicate_of": output.duplicate_of,
            "already_done": output.already_done,
            "reason": output.reason,
        }
    except Exception:
        log.warning("dedup check failed, proceeding with refine", exc_info=True)
        return {
            "duplicate_of": None,
            "already_done": None,
            "reason": "dedup check failed",
        }
    finally:
        _safe_close(agent)
