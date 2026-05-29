"""Scope-violation triage agent: a cheap classifier that decides
whether changed files outside the ticket's declared scope are
legitimate expansions or scope creep.

This follows the same pattern as ``triage_refine`` and
``triage_auto_approve`` in :mod:`refining`: load the YAML definition,
build a no-tools agent, call with retry, and return a structured
Pydantic output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import Settings
from .prompt_blocks import section


class ScopeTriageVerdict(BaseModel):
    action: Literal["EXPAND", "REJECT", "ESCALATE"]
    justification: str
    expand_files: list[str] = []


def run_scope_triage_agent(
    *,
    settings: Settings,
    ticket_spec: str,
    file_map: list[str],
    out_of_scope_files: list[str],
    diff_summaries: dict[str, str],
) -> ScopeTriageVerdict:
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "scope_triage.yaml",
    )
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        model_name=definition.model or settings.scope_triage_model,
    )

    file_map_body = "\n".join(f"- {f}" for f in file_map)
    out_of_scope_body = "\n".join(f"- {f}" for f in out_of_scope_files)
    diff_body = "\n\n".join(
        f"--- {path} ---\n{summary}" for path, summary in diff_summaries.items()
    )
    user_prompt = (
        section("ticket-spec", ticket_spec)
        + "\n\n"
        + section("file-map", file_map_body)
        + "\n\n"
        + section("out-of-scope-files", out_of_scope_body)
        + "\n\n"
        + section("diff-summaries", diff_body)
    )

    try:
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt),
            settings=settings,
            what="scope triage",
        )
    finally:
        _safe_close(agent)

    return result.output
