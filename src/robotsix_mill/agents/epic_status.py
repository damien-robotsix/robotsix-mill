"""Epic status agent: re-evaluates whether an epic's goal has been
achieved given the current state of all its child tickets.

Seam: tests monkeypatch ``run_epic_status_agent``.  The agent does NOT
get filesystem access — it only sees the structured data passed in by
the caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from .prompt_blocks import section


class EpicStatusResult(BaseModel):
    decision: Literal["close", "keep_open", "update_description", "update_deps"]
    note: str = ""
    dep_updates: dict[str, list[str] | None] | None = Field(default=None)
    new_children: list[dict[str, str]] | None = Field(default=None)
    child_rescopes: dict[str, dict[str, str]] | None = Field(default=None)
    child_closures: list[str] | None = Field(default=None)


def run_epic_status_agent(
    *,
    settings: Settings,
    epic_title: str,
    epic_description: str,
    children: list[dict],
) -> EpicStatusResult:
    """Evaluate whether an epic's goal has been achieved.

    The agent receives the epic title + description and a list of
    child ticket summaries (each with ``id``, ``title``, ``state``,
    and ``description``).  Returns a structured
    ``EpicStatusResult`` with a ``decision`` and a human-readable
    ``note``.

    The agent is constructed via :func:`~.base.build_agent` with
    ``PromptedOutput(EpicStatusResult)``, ``web=False``,
    ``report_issue=False``, and ``model_name=settings.audit_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry` for
    transient/rate-limit resilience.
    """
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "epic_status.yaml"
    )

    agent = build_agent_from_definition(
        settings, definition, tools=[],
        model_name=definition.model or settings.audit_model,
    )
    import json

    children_json = json.dumps(children, indent=2, default=str)
    prompt = (
        section("epic-title", epic_title) + "\n\n"
        + section("epic-description", epic_description) + "\n\n"
        + section("children", children_json) + "\n\n"
        + "Evaluate the epic's status and return your decision."
    )
    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt),
            settings=settings,
            what="epic-status",
        )
    finally:
        _safe_close(agent)
    return result.output
