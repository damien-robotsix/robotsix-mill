"""Operational-maintenance classifier for the ingest pipeline.

A cheap LLM classifier that decides whether an incoming report requires
code work or is a manual operational action (credential rotation,
redeploy, infra console change) that should not enter the implement
pipeline.

This follows the same pattern as :mod:`scope_triage`: load the YAML
definition, build a no-tools agent, call with retry, and return a
structured Pydantic output.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..config import Settings
from .prompt_blocks import section


class OpsClassifyVerdict(BaseModel):
    """Classifier verdict on whether a report is operational or code.

    ``classification`` is either ``"OPERATIONAL"`` (no code change
    required — manual human/deploy action) or ``"CODE"`` (requires
    editing tracked files).  ``reason`` is a one-line explanation
    suitable for diagnostic logging and operator review.
    """

    classification: Literal["OPERATIONAL", "CODE"]
    reason: str


def run_ops_classify_agent(
    *,
    settings: Settings,
    title: str,
    body: str,
) -> OpsClassifyVerdict:
    """Classify an incoming report as operational-maintenance or code.

    Builds a cheap no-tools classifier from the ``ops_classify`` YAML
    definition and runs it (with retry) over the report title and body,
    returning a structured :class:`OpsClassifyVerdict`.

    Args:
        settings: Application configuration — model name and retry
            parameters.
        title: The report title.
        body: The report body / description.

    Returns:
        An :class:`OpsClassifyVerdict` with the classification and
        reason.
    """
    from .yaml_loader import load_and_run_agent

    user_prompt = section("title", title) + "\n\n" + section("body", body)

    result = load_and_run_agent(
        settings=settings,
        definition_name="ops_classify",
        tools=[],
        prompt=user_prompt,
        what="ops-classify",
    )
    return OpsClassifyVerdict.model_validate(result.output)
