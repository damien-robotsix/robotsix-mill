"""Pre-refine obsolescence gate.

A single cheap LLM call that re-evaluates whether a *spawned* follow-up
or corrective draft's cited gap still exists on HEAD.  When a draft is
spawned from a prior-stage recommendation (doc-agent note, code-review
suggestion) or a parent ticket's review, the cited gap (a missing doc
section, a still-listed dependency, a grep that should return nothing)
may already have been filled in place by a parallel/parent ticket before
the draft reaches refine.  This gate reads the cited files at HEAD and,
when the gap is clearly already resolved, short-circuits the draft
straight to ``DONE`` before the expensive refine agent runs.

``run_obsolescence_check`` is the mockable seam — tests monkeypatch it
just like ``run_dedup_check``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..config import Settings
from .prompt_blocks import section


class ObsolescenceResult(BaseModel):
    """Structured output from the obsolescence check agent."""

    model_config = ConfigDict(strict=True, extra="forbid")

    obsolete: bool = False
    reason: str = ""


log = logging.getLogger("robotsix_mill.agents.obsolescence")


def _build_prompt(
    *,
    draft_title: str,
    draft_body: str,
) -> str:
    draft_block = "\n".join(
        [
            section("title", draft_title),
            section("body", draft_body),
        ]
    )
    return section("draft", draft_block)


def run_obsolescence_check(
    *,
    settings: Settings,
    draft_title: str,
    draft_body: str,
    repo_dir: Path | None,
) -> dict:
    """Return ``{"obsolete": bool, "reason": str}``.

    Degrades gracefully: on any exception, returns ``obsolete=False``
    with a failure reason — the gate is best-effort and never blocks
    the pipeline.
    """
    from .yaml_loader import load_and_run_agent

    from pydantic_ai.usage import UsageLimits

    # Build filesystem tools when a repo_dir is available; the
    # obsolescence agent is read-only — only read_file and list_dir are
    # exposed so it can open the cited files and inspect their content
    # on HEAD.
    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        fs = build_fs_tools(repo_dir, settings)
        tools = [t for t in fs if t.__name__ in ("read_file", "list_dir")]

    try:
        result = load_and_run_agent(
            settings=settings,
            definition_name="obsolescence",
            tools=tools,
            prompt=_build_prompt(
                draft_title=draft_title,
                draft_body=draft_body,
            ),
            what="obsolescence check",
            run_kwargs={
                "usage_limits": UsageLimits(
                    request_limit=settings.obsolescence_request_limit
                )
            },
        )
        output = result.output
        if not isinstance(output, ObsolescenceResult):
            log.warning(
                "obsolescence check returned non-ObsolescenceResult: %s",
                type(output),
            )
            return {
                "obsolete": False,
                "reason": "obsolescence check returned unexpected type",
            }
        return {
            "obsolete": output.obsolete,
            "reason": output.reason,
        }
    except Exception:
        log.warning("obsolescence check failed, proceeding with refine", exc_info=True)
        return {
            "obsolete": False,
            "reason": "obsolescence check failed",
        }
