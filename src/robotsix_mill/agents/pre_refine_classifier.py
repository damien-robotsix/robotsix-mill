"""Pre-refine classifier — single LLM call replacing standards_gate +
triage + dedup.

Combines three serial gates into one structured-output call so the
refine stage can short-circuit on any terminal verdict without burning
additional LLM budget.  ``run_pre_refine_classifier`` is the mockable
seam — tests monkeypatch it just like ``run_refine_agent``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from robotsix_mill._resources import agent_definitions_dir

from ..config import Settings
from .prompt_blocks import section

log = logging.getLogger("robotsix_mill.agents.pre_refine_classifier")


class PreRefineClassifierResult(BaseModel):
    """Structured output from the pre-refine classifier."""

    # Standards gate
    standards_violation: bool = False
    standards_standard: str = ""
    standards_reason: str = ""

    # Triage
    triage_decision: Literal["REFINE", "SKIP", "NO_CHANGE", "MIGRATE"] = "REFINE"
    triage_reason: str = ""
    target_board: str | None = None
    complexity: Literal["simple", "needs-exploration"] | None = None
    trivial_scope: bool | None = None
    exploration_findings: str | None = None

    # Dedup
    duplicate_of: str | None = None
    already_done: str | None = None
    dedup_reason: str = ""

    # Reviewer agreement (conditional — null when no reviewer feedback)
    reviewer_agreement: Literal["AGREE", "DISAGREE", "ADMIN_ONLY"] | None = None
    reviewer_agreement_reason: str = ""


def _build_prompt(
    *,
    title: str,
    draft: str,
    standards_context: str = "",
    candidates_json: str = "",
    reviewer_comments: str | None = None,
) -> str:
    """Build the combined prompt for the pre-refine classifier."""
    parts = [
        section("title", title),
        section("body", draft),
    ]
    if standards_context:
        parts.append(section("robotsix-standards", standards_context))
    if candidates_json:
        parts.append(section("candidates", candidates_json))
    if reviewer_comments:
        parts.append(section("reviewer_feedback", reviewer_comments))
    return "\n".join(parts)


def run_pre_refine_classifier(
    *,
    settings: Settings,
    title: str,
    draft: str,
    standards_context: str = "",
    candidates_json: str = "",
    reviewer_comments: str | None = None,
) -> PreRefineClassifierResult:
    """Return a ``PreRefineClassifierResult`` from a single cheap LLM call.

    Combines standards gate, triage classification, dedup check, and
    reviewer-agreement into one structured-output call.  Degrades
    gracefully: on any failure, returns a result that lets refine proceed
    normally (no violation, REFINE, no duplicate).
    """
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_and_run_agent

    prompt = _build_prompt(
        title=title,
        draft=draft,
        standards_context=standards_context,
        candidates_json=candidates_json,
        reviewer_comments=reviewer_comments,
    )

    try:
        result = load_and_run_agent(
            settings=settings,
            definition_name="pre-refine-classifier",
            tools=[],
            prompt=prompt,
            what="pre-refine classifier",
            run_kwargs={"usage_limits": UsageLimits(request_limit=6)},
        )
        output = result.output
        if not isinstance(output, PreRefineClassifierResult):
            log.warning(
                "pre-refine classifier returned non-PreRefineClassifierResult: %s",
                type(output),
            )
            return PreRefineClassifierResult(
                triage_decision="REFINE",
                triage_reason="classifier returned unexpected type",
            )
        return output
    except Exception:
        log.warning(
            "pre-refine classifier failed, proceeding with refine",
            exc_info=True,
        )
        return PreRefineClassifierResult(
            triage_decision="REFINE",
            triage_reason="classifier failed — proceeding with refine",
        )
