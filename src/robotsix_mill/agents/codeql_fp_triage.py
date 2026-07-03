"""CodeQL false-positive triage agent — conservative last-resort sub-agent.

Called from the ci_fix stage at the hard cycle ceiling when the ONLY
remaining red check is CodeQL code-scanning.  The agent evaluates each
eligible alert and returns a per-alert verdict (dismiss/abstain).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..config import Settings, get_secrets


class AlertVerdict(BaseModel):
    """Per-alert verdict from the codeql_fp_triage agent."""

    model_config = ConfigDict(strict=True, extra="forbid")

    alert_number: int
    verdict: Literal["dismiss", "abstain"]
    rationale: str = ""
    code_fix_possible: bool = False
    code_fix_description: str = ""


class CodeQLFpTriageResult(BaseModel):
    """Structured output from the codeql_fp_triage agent."""

    model_config = ConfigDict(strict=True, extra="forbid")

    verdicts: list[AlertVerdict] = []
    summary: str = ""


def run_codeql_fp_triage_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    alerts_json: str,
    ticket_id: str = "",
    board_id: str = "",
) -> CodeQLFpTriageResult:
    """Run the codeql_fp_triage agent on the given *alerts_json*.

    *alerts_json* is a compact JSON list of eligible alerts, each with
    ``number``, ``rule``, ``path``, ``line``, ``message``.  Only alerts
    that passed the deterministic guardrails (in-scope, non-security
    severity) are included — the agent receives a pre-filtered list.

    Returns a ``CodeQLFpTriageResult`` with per-alert verdicts.
    """
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .yaml_loader import load_and_run_agent
    from .fs_tools import build_fs_tools

    tools = build_fs_tools(repo_dir, settings)

    user_prompt = (
        "You are evaluating the following CodeQL alerts for false-positive "
        "dismissal.  Each alert has already passed deterministic guardrails: "
        "it is in a file changed by this PR and has NO security severity.\n\n"
        "```json\n" + alerts_json + "\n```\n\n"
        "For each alert, read the flagged file at the flagged line, trace "
        "the symbol, and return DISMISS (only with a concrete proof-style "
        "rationale) or ABSTAIN (the default for any uncertainty)."
    )

    result = load_and_run_agent(
        settings=settings,
        definition_name="codeql_fp_triage",
        tools=tools,
        prompt=user_prompt,
        what="codeql_fp_triage",
        repo_dir=repo_dir,
        board_id=board_id,
        system_prompt_format_kwargs={},
    )

    return CodeQLFpTriageResult.model_validate(result.output)
