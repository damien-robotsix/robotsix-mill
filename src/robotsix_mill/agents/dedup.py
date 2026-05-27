"""Pre-refine dedup / already-done check.

A single cheap LLM call that inspects a draft against existing tickets
to decide whether it's a duplicate or already implemented.  The
refiner would be wasted on such drafts — this guard short-circuits
them straight to ``CLOSED`` before the expensive agent runs.

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


def _build_prompt(
    *,
    draft_title: str,
    draft_body: str,
    candidates_json: str,
) -> str:
    return "\n".join([
        "<draft>",
        f"<title>{draft_title}</title>",
        f"<body>{draft_body}</body>",
        "</draft>",
        "<candidates>",
        candidates_json,
        "</candidates>",
    ])


def run_dedup_check(
    *,
    settings: Settings,
    draft_title: str,
    draft_body: str,
    candidates_json: str,
    repo_dir: Path | None = None,
) -> dict:
    """Return ``{"duplicate_of": ..., "already_done": ..., "reason": ...}``.

    Degrades gracefully: on any exception, returns nulls with a failure
    reason — the guard is best-effort and never blocks the pipeline.
    """
    from .base import build_agent_from_definition, _safe_close
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "dedup.yaml"
    )

    # Build filesystem tools when a repo_dir is available; the dedup
    # agent is read-only — only read_file and list_dir are exposed.
    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        fs = build_fs_tools(repo_dir, settings)
        tools = [t for t in fs if t.__name__ in ("read_file", "list_dir")]

    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        model_name=definition.model or settings.dedup_model,
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
