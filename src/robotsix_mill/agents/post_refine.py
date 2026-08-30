"""Post-refine check — single LLM call replacing spec-review +
reviewer-agreement + auto-approve.

Combines three serial post-refine gates into one structured-output
call.  ``run_post_refine_check`` is the mockable seam.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from robotsix_mill._resources import agent_definitions_dir

from ..config import Settings
from .prompt_blocks import section

log = logging.getLogger("robotsix_mill.agents.post_refine")


class PostRefineResult(BaseModel):
    """Structured output from the post-refine check."""

    # Spec conciseness
    concise_spec: str = ""
    stripped_summary: str = ""

    # Auto-approve
    auto_approve: Literal["APPROVE", "NEEDS_APPROVAL"] = "NEEDS_APPROVAL"
    auto_approve_reason: str = ""

    # Reviewer agreement (conditional — null when no reviewer feedback)
    reviewer_agreement: Literal["AGREE", "DISAGREE", "ADMIN_ONLY"] | None = None
    reviewer_agreement_reason: str = ""


def _build_prompt(
    *,
    spec: str,
    reviewer_comments: str | None = None,
) -> str:
    """Build the combined prompt for the post-refine check."""
    parts = [
        section("spec", spec),
    ]
    if reviewer_comments:
        parts.append(section("reviewer_feedback", reviewer_comments))
    return "\n".join(parts)


def run_post_refine_check(
    *,
    settings: Settings,
    spec: str,
    reviewer_comments: str | None = None,
) -> PostRefineResult:
    """Return a ``PostRefineResult`` from a single cheap LLM call.

    Combines spec conciseness review, auto-approve decision, and
    reviewer-agreement check into one structured-output call.
    Degrades gracefully: on any failure, returns a result that
    preserves the original spec and routes to human approval.
    """
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_and_run_agent

    prompt = _build_prompt(
        spec=spec,
        reviewer_comments=reviewer_comments,
    )

    try:
        result = load_and_run_agent(
            settings=settings,
            definition_name="post-refine",
            tools=[],
            prompt=prompt,
            what="post-refine check",
            run_kwargs={"usage_limits": UsageLimits(request_limit=4)},
        )
        output = result.output
        if not isinstance(output, PostRefineResult):
            log.warning(
                "post-refine check returned non-PostRefineResult: %s",
                type(output),
            )
            return PostRefineResult(
                concise_spec=spec,
                stripped_summary="classifier returned unexpected type — using original spec",
                auto_approve="NEEDS_APPROVAL",
                auto_approve_reason="classifier returned unexpected type",
            )
        # Guard: if the classifier returned an empty/placeholder concise_spec,
        # fall back to the original spec.
        if not output.concise_spec or not output.concise_spec.strip():
            output.concise_spec = spec
            output.stripped_summary = "classifier returned empty spec — using original"
        return output
    except Exception:
        log.warning(
            "post-refine check failed, using original spec and human approval",
            exc_info=True,
        )
        return PostRefineResult(
            concise_spec=spec,
            stripped_summary="post-refine check failed — using original spec",
            auto_approve="NEEDS_APPROVAL",
            auto_approve_reason="post-refine check failed",
        )
