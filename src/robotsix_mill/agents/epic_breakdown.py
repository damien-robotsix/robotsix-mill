"""Epic breakdown agent: reads an epic description and produces a
list of well-scoped child tickets.

Seam: tests monkeypatch ``run_epic_breakdown_agent``.  The agent does
NOT get filesystem access — it only sees the epic title + description.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "epic_breakdown.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


class EpicBreakdownResult(BaseModel):
    child_titles: list[str] = Field(default_factory=list)
    child_bodies: list[str] = Field(default_factory=list)
    epic_body: str | None = None


def run_epic_breakdown_agent(
    *,
    settings: Settings,
    epic_title: str,
    epic_description: str,
    comments: str = "",
) -> EpicBreakdownResult:
    """Break an epic into well-scoped child tickets.

    The agent receives only the epic title + description — no
    filesystem access.  Returns a structured ``EpicBreakdownResult``
    with parallel ``child_titles`` and ``child_bodies`` lists, and
    an optional ``epic_body`` field with a revised epic description.

    When *comments* is non-empty, the operator's comment history is
    appended to the prompt in an ``<operator_comments>`` block so the
    agent can follow the operator's explicit direction.

    The agent is constructed via :func:`~.base.build_agent` with
    ``PromptedOutput(EpicBreakdownResult)``, ``web=False``,
    ``report_issue=False``, and ``model_name=settings.audit_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry` for
    transient/rate-limit resilience.
    """
    from .yaml_loader import load_and_run_agent

    prompt = (
        section("epic-title", epic_title)
        + "\n\n"
        + section("epic-description", epic_description)
    )
    if comments:
        prompt += "\n\n" + section("operator-comments", comments)
    prompt += "\n\nBreak this epic into well-scoped child tickets."
    result = load_and_run_agent(
        settings=settings,
        definition_name="epic_breakdown",
        tools=[],
        model_name=settings.audit_model,
        prompt=prompt,
        what="epic-breakdown",
    )
    return result.output
