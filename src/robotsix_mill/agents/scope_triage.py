"""Scope-violation triage agent: a cheap classifier that decides
whether changed files outside the ticket's declared scope are
legitimate expansions or scope creep.

This follows the same pattern as ``triage_refine`` and
``triage_auto_approve`` in :mod:`refining`: load the YAML definition,
build a no-tools agent, call with retry, and return a structured
Pydantic output.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..config import Settings
from .prompt_blocks import section


class ScopeTriageVerdict(BaseModel):
    """Classifier verdict on out-of-scope file changes.

    ``action`` is the triage decision — ``EXPAND`` (accept the changes
    as a legitimate scope expansion), ``REJECT`` (scope creep to
    revert), or ``ESCALATE`` (defer to a human). ``justification`` is
    the rationale, and ``expand_files`` lists the out-of-scope paths
    approved when ``action`` is ``EXPAND``.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

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
    """Triage whether out-of-scope file changes are legitimate.

    Builds a cheap no-tools classifier from the ``scope_triage`` YAML
    definition and runs it (with retry) over the ticket spec, declared
    file map, the offending out-of-scope files, and their diff
    summaries, returning a structured :class:`ScopeTriageVerdict`.

    Args:
        settings: Application configuration — model name
            (``scope_triage_model``) and retry parameters.
        ticket_spec: The ticket specification text.
        file_map: The files the ticket declared in scope.
        out_of_scope_files: Changed files not covered by ``file_map``.
        diff_summaries: Mapping of file path to a summary of its diff.

    Returns:
        A :class:`ScopeTriageVerdict` with the action, justification,
        and any approved expansion files.
    """
    from .yaml_loader import load_and_run_agent

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

    result = load_and_run_agent(
        settings=settings,
        definition_name="scope_triage",
        tools=[],
        prompt=user_prompt,
        what="scope triage",
    )
    return result.output
