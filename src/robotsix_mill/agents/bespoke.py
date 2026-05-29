"""Generic LLM runner for bespoke per-repo periodic agents.

A bespoke agent is operator-authored YAML committed to a managed
repo's source tree at ``<repo>/.robotsix-mill/agents/<name>.yaml``.
The YAML carries the entire prompt; everything else (tool palette,
output shape, retry/usage limits, memory plumbing) is fixed in code
so a managed repo can not turn arbitrary execution loose on mill.

Tool palette: ``explore``, ``read_file``, ``list_dir``, and
``web_research`` (when ``web=true`` on the YAML). No write/edit/run
tools. Drafts are emitted via structured output, not by a separate
ticket-emission tool — keeps the agent loop short and the contract
inspectable from outside.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from .bespoke_loader import BespokeAgentDefinition

log = logging.getLogger("robotsix_mill.bespoke")

# Mirrors the ``MAX_GAPS`` cap in ``auditing.py``: bespoke agents are
# narrow checkers; a pass that wants to emit ten things on one cycle
# almost always means the prompt is too broad. Cap and let the next
# cycle pick up the rest.
MAX_DRAFTS = 5


class BespokeResult(BaseModel):
    """Structured output from one bespoke pass.

    Shape intentionally mirrors :class:`~.auditing.AuditResult` so the
    shared :func:`~..pass_runner.run_agent_pass` boilerplate (memory
    persist + draft-ticket creation + dedup) accepts it without
    branching.
    """

    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_bespoke_agent(
    *,
    settings: Settings,
    definition: BespokeAgentDefinition,
    memory: str = "",
    recent_proposals: str = "",
    repo_dir: Path | None = None,
) -> BespokeResult:
    """Execute one bespoke-agent pass.

    Builds a pydantic-ai agent with:

    - the operator-authored ``definition.system_prompt`` as the entire
      system prompt (no shipped YAML merged in);
    - a read-only tool palette derived from *repo_dir* — ``explore``,
      ``read_file``, ``list_dir``, and (when ``definition.web`` is
      True) ``web_research``;
    - structured output of type :class:`BespokeResult`.

    Bespoke agents never get ``write_file``, ``edit_file``, or
    ``run_command`` — those would let any managed-repo committer turn
    mill into a mutation engine. Drafts are emitted via the structured
    output and surfaced to the operator as DRAFT tickets on the repo's
    board.
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close
    from .prompt_blocks import section
    from .retry import call_with_retry

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t
            for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    model_name = definition.model or settings.bespoke_default_model

    agent = build_agent(
        settings,
        name=f"bespoke:{definition.name}",
        system_prompt=definition.system_prompt,
        model_name=model_name,
        tools=tools,
        web=definition.web,
        report_issue=False,
        read_ticket=False,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
        output_type=PromptedOutput(BespokeResult),
        retries=2,
        skills=[],
    )

    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"{recent_proposals}"
        + section("forge-remote-url", forge_url)
        + "\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + "Perform the inspection and return your result."
    )

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt),
            settings=settings,
            what=f"bespoke:{definition.name}",
        )
    finally:
        _safe_close(agent)
    out = result.output
    out.draft_titles = out.draft_titles[:MAX_DRAFTS]
    out.draft_bodies = out.draft_bodies[:MAX_DRAFTS]
    out.gap_ids = out.gap_ids[:MAX_DRAFTS]
    return out
