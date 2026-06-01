"""Meta repo-triage agent.

Given a cross-repo meta-board proposal, decides which registered
repositories the work touches so the meta workspace is built by cloning
only those repos (see :mod:`robotsix_mill.meta_workspace`).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml as _yaml
from pydantic import BaseModel, Field

from ..config import Settings, get_repos_config
from .prompt_blocks import section

log = logging.getLogger("robotsix_mill.agents.meta_triage")

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "pipeline"
    / "meta_triage.yaml"
)
# Re-exported for tests (loaded from YAML without env-var resolution).
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


class RequiredReposResult(BaseModel):
    """Triage output: the registered repos a meta proposal requires."""

    repo_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


def _registered_repos_block() -> str:
    """A ``<registered-repos>`` block: one line per repo with a forge URL."""
    repos_config = get_repos_config()
    lines: list[str] = []
    for repo_id, rc in repos_config.repos.items():
        if not rc.forge_remote_url:
            continue  # only clonable repos are triage candidates
        lines.append(f"- {repo_id}: {rc.forge_remote_url}")
    return "\n".join(lines)


def required_repos_for(*, settings: Settings, spec: str) -> list[str]:
    """Return the registered repo ids a meta proposal requires.

    Runs the triage agent over *spec* + the registered-repo list, validates
    the result against the registry (drops unknown ids), and falls back to
    ALL clonable registered repos when the agent returns nothing usable.
    """
    repos_config = get_repos_config()
    clonable = {rid for rid, rc in repos_config.repos.items() if rc.forge_remote_url}
    if not clonable:
        return []

    from .base import _safe_close, build_agent_from_definition
    from .retry import run_agent
    from .yaml_loader import load_agent_definition

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "pipeline"
        / "meta_triage.yaml"
    )
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        model_name=definition.model or settings.module_curator_model,
    )
    prompt = section("registered-repos", _registered_repos_block()) + section(
        "proposal", spec
    )
    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            settings=settings,
            what="meta-triage",
        )
    finally:
        _safe_close(agent)

    out: RequiredReposResult = result.output
    # Validate against the registry; keep order, drop unknowns/dups.
    seen: set[str] = set()
    valid: list[str] = []
    for rid in out.repo_ids:
        if rid in clonable and rid not in seen:
            valid.append(rid)
            seen.add(rid)
    if not valid:
        # Safe fallback: clone everything so the work is at least possible.
        log.info(
            "meta-triage: no usable repo_ids (%r) — falling back to all "
            "clonable repos %s",
            out.repo_ids,
            sorted(clonable),
        )
        return sorted(clonable)
    return valid
