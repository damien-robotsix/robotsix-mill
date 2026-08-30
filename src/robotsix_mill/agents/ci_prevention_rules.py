"""CI prevention-rules reasoning core.

Turns a deterministic digest of a board's recent CI failures (grouped by
semantic bucket, with root causes and how ci_fix resolved them) into a
handful of short, imperative prevention rules for the implement agent.

The agent does NOT read the event store itself — the digest is built by
``runners.ci_prevention_rules_runner``, which also owns writing the rules
into the implement memory ledger. Built from the periodic YAML definition
(``agent_definitions/periodic/ci_prevention_rules.yaml``) so a per-repo
presence file can overlay the prompt; the YAML pins a small tier because
the task is compression of an already-structured digest, not judgement.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings


class CiPreventionRulesResult(BaseModel):
    """Structured output from the ci_prevention_rules pass.

    ``rules`` are short imperative sentences ("Run `ruff format` on changed
    files before stopping"), most impactful first. The runner clips the
    list to ``settings.ci_prevention_max_rules`` and drops blanks.
    """

    rules: list[str] = Field(default_factory=list)


def run_ci_prevention_rules_agent(
    *,
    settings: Settings,
    digest: str,
    definition: Any,
) -> CiPreventionRulesResult:
    """Run the prevention-rules agent over a pre-built *digest*.

    Args:
        settings: Application configuration.
        digest: The deterministic per-bucket CI failure digest built by the
            runner.
        definition: The (possibly overlaid) periodic agent definition.

    Returns:
        A ``CiPreventionRulesResult``; an infrastructure failure that yields
        no output raises ``RuntimeError`` so the pass records an error run
        instead of silently wiping the ledger section.
    """
    from .base import _safe_close, build_agent_from_definition
    from .retry import run_agent

    agent = build_agent_from_definition(settings, definition, tools=[])

    prompt = (
        digest + "\n\nDistil the digest above into prevention rules and emit your "
        "CiPreventionRulesResult. Fewer, sharper rules beat many vague ones; "
        "return an empty list when nothing recurs."
    )

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            what="ci-prevention-rules",
        )
    finally:
        _safe_close(agent)

    if result is None or getattr(result, "output", None) is None:
        raise RuntimeError(
            "ci_prevention_rules agent produced null output — "
            "likely an infrastructure failure (CLI crash, timeout, or "
            "fallback exhaustion)"
        )
    out: CiPreventionRulesResult = result.output
    return out
