"""Meta repo-triage agent.

Given a cross-repo meta-board proposal, decides which registered
repositories the work touches so the meta workspace is built by cloning
only those repos (see :mod:`robotsix_mill.meta.workspace`).
"""

from __future__ import annotations

import logging

import yaml as _yaml
from pydantic import BaseModel, Field, ConfigDict

from robotsix_mill._resources import agent_definitions_dir
from ..config import Settings, get_repos_config
from ..agents.prompt_blocks import section

log = logging.getLogger("robotsix_mill.meta.triage")

_SYSPROMPT_PATH = agent_definitions_dir() / "pipeline" / "meta_triage.yaml"
# Re-exported for tests (loaded from YAML without env-var resolution).
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


class RequiredReposResult(BaseModel):
    """Triage output: the registered repos a meta proposal requires."""

    model_config = ConfigDict(strict=True, extra="forbid")

    repo_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class TriagedRepos(list[str]):
    """The clonable repo ids a meta proposal requires.

    A plain ``list[str]`` subclass so existing callers/tests that treat
    the result as a list (equality, iteration, ``", ".join(...)``) keep
    working unchanged — with one extra bit: :attr:`fallback` is ``True``
    only when triage could NOT confidently match any repo and fell back
    to cloning *every* clonable repo.

    Deliver consults that bit to refuse merging brand-new top-level files
    into an arbitrarily-chosen primary repo (see
    :mod:`robotsix_mill.stages.deliver`).  ``fallback`` stays ``False``
    for both a confident match and a genuine all-repos ticket (the agent
    explicitly named every repo), so legitimate cross-repo audits still
    proceed.
    """

    fallback: bool = False


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

    from ..agents.yaml_loader import load_and_run_agent

    prompt = section("registered-repos", _registered_repos_block()) + section(
        "proposal", spec
    )
    result = load_and_run_agent(
        settings=settings,
        definition_name="pipeline/meta_triage",
        tools=[],
        prompt=prompt,
        what="meta-triage",
    )

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
        # Flag it so deliver can refuse to merge brand-new top-level files
        # into an arbitrarily-chosen primary repo (the work may target a
        # not-yet-created repo).
        log.info(
            "meta-triage: no usable repo_ids (%r) — falling back to all "
            "clonable repos %s",
            out.repo_ids,
            sorted(clonable),
        )
        fell_back = TriagedRepos(sorted(clonable))
        fell_back.fallback = True
        return fell_back
    return TriagedRepos(valid)
