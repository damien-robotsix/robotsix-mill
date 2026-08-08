"""Pre-refine standards gate.

A single cheap LLM call that checks whether a draft's GOAL conflicts
with an explicit robotsix-standards prohibition — e.g. "publish
@robotsix/ui to npm" against distribution-packaging.md's no-registry
rule, or "enable GHAS code scanning" on a private repo against
free-tier-only.md.  Only repos that follow the fleet conventions
(:meth:`RepoConfig.follows_robotsix_standards`) are gated; the check
judges the ticket's objective, not its style — a draft that merely
*phrases* things unconventionally is refine's job to fix, not this
gate's job to discard.

``run_standards_gate_check`` is the mockable seam — tests monkeypatch
it just like ``run_obsolescence_check``.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from ..config import Settings
from .prompt_blocks import section
from .standards import fetch_standards_context


class StandardsGateResult(BaseModel):
    """Structured output from the standards gate agent."""

    violates: bool = False
    standard: str = ""
    reason: str = ""


log = logging.getLogger("robotsix_mill.agents.standards_gate")


def _build_prompt(
    *,
    draft_title: str,
    draft_body: str,
    standards_context: str,
) -> str:
    draft_block = "\n".join(
        [
            section("title", draft_title),
            section("body", draft_body),
        ]
    )
    return "\n".join(
        [
            section("robotsix-standards", standards_context),
            section("draft", draft_block),
        ]
    )


def run_standards_gate_check(
    *,
    settings: Settings,
    draft_title: str,
    draft_body: str,
) -> dict[str, bool | str]:
    """Return ``{"violates": bool, "standard": str, "reason": str}``.

    Degrades gracefully: when the standards context cannot be fetched
    or the agent call fails, returns ``violates=False`` with a failure
    reason — the gate is best-effort and never blocks the pipeline.
    """
    from .yaml_loader import load_and_run_agent

    from pydantic_ai.usage import UsageLimits

    standards_ctx = fetch_standards_context(settings)
    if not standards_ctx:
        log.warning("standards gate: standards context unavailable, skipping")
        return {
            "violates": False,
            "standard": "",
            "reason": "standards context unavailable",
        }

    try:
        result = load_and_run_agent(
            settings=settings,
            definition_name="standards_gate",
            tools=[],
            prompt=_build_prompt(
                draft_title=draft_title,
                draft_body=draft_body,
                standards_context=standards_ctx,
            ),
            what="standards gate check",
            run_kwargs={"usage_limits": UsageLimits(request_limit=2)},
        )
        output = result.output
        if not isinstance(output, StandardsGateResult):
            log.warning(
                "standards gate returned non-StandardsGateResult: %s",
                type(output),
            )
            return {
                "violates": False,
                "standard": "",
                "reason": "standards gate returned unexpected type",
            }
        return {
            "violates": output.violates,
            "standard": output.standard,
            "reason": output.reason,
        }
    except Exception:
        log.warning("standards gate check failed, proceeding with refine", exc_info=True)
        return {
            "violates": False,
            "standard": "",
            "reason": "standards gate check failed",
        }
