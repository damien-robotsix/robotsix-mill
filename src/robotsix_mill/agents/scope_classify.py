"""Scope-breadth classifier for the ingest pipeline.

A cheap LLM classifier that decides whether an incoming report is a
single focused task or multi-concern work that spans several
deliverables and should be promoted to an epic (then decomposed into
targeted child tickets via the existing epic-breakdown machinery).

This follows the same pattern as :mod:`ops_classify`: load the YAML
definition, build a no-tools agent, call with retry, and return a
structured Pydantic output.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from .prompt_blocks import section


class ScopeVerdict(BaseModel):
    """Classifier verdict on whether a report is a single task or an epic.

    ``classification`` is either ``"TASK"`` (a single focused change —
    proceed as today) or ``"EPIC"`` (multi-concern work that should be
    broken into dependency-ordered child tickets).  ``confidence`` is
    the classifier's confidence in an ``EPIC`` verdict in ``[0, 1]`` —
    the ingest gate only promotes when it clears the configured
    threshold, so borderline reports stay single tasks.  ``reason`` is a
    one-line explanation recorded on the ticket history for auditability.
    """

    classification: Literal["TASK", "EPIC"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


def run_scope_classify_agent(
    *,
    settings: Settings,
    title: str,
    body: str,
) -> ScopeVerdict:
    """Classify an incoming report as a single task or a broad epic.

    Builds a cheap no-tools classifier from the ``scope_classify`` YAML
    definition and runs it (with retry) over the report title and body,
    returning a structured :class:`ScopeVerdict`.

    Args:
        settings: Application configuration — model name and retry
            parameters.
        title: The report title.
        body: The report body / description.

    Returns:
        A :class:`ScopeVerdict` with the classification, confidence, and
        reason.
    """
    from .yaml_loader import load_and_run_agent

    user_prompt = section("title", title) + "\n\n" + section("body", body)

    result = load_and_run_agent(
        settings=settings,
        definition_name="scope_classify",
        tools=[],
        prompt=user_prompt,
        what="scope-classify",
    )
    return ScopeVerdict.model_validate(result.output)
