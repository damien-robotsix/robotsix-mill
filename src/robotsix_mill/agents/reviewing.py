"""Dual-model review agent: audits a git diff blind.

A second model (defaults to a different model than the implement agent)
reviews the implementation diff with no access to the implement agent's
context — only the diff and ticket spec.  Returns a structured verdict:
APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION.
"""

from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal

from ..config import Settings

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml
_SYSPROMPT_PATH = Path(__file__).parent.parent.parent.parent / "agent_definitions" / "review.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]



class ReviewVerdict(BaseModel):
    """Structured output from the blind review agent."""

    verdict: Literal["APPROVE", "REQUEST_CHANGES", "NEEDS_DISCUSSION"]
    comments: str = Field(
        description="Detailed review feedback. For APPROVE, note any "
                    "minor observations. For REQUEST_CHANGES, list "
                    "specific, actionable issues. For NEEDS_DISCUSSION, "
                    "explain what requires human judgment."
    )
    auto_merge_eligible: bool = Field(
        default=False,
        description=(
            "Default to true when verdict is APPROVE and you raised no "
            "specific concern in comments. Set to false only when you can "
            "name a concrete reason a human should still look — an "
            "architectural decision noticed in passing, an "
            "accepted-but-flagged risk, or a spec-vs-implementation gap "
            "below the REQUEST_CHANGES threshold. REQUEST_CHANGES and "
            "NEEDS_DISCUSSION verdicts always leave this false."
        ),
    )


def run_review_agent(
    *,
    settings: Settings,
    diff: str,
    spec: str,
    model_name: str | None = None,
    prior_context: str | None = None,
    repo_dir: Path | None = None,
) -> ReviewVerdict:
    """Run a blind review of *diff* against *spec*.

    The agent receives ONLY the diff and spec — no implementation
    context, no memory, no history. Uses *model_name* if given,
    otherwise falls back to ``settings.review_model``.

    When *prior_context* is provided (prior review comments and the
    implement agent's rebuttal from the last round), it is injected
    before the ticket spec so the reviewer can avoid re-raising
    resolved issues.

    When *repo_dir* is provided, the agent receives read-only
    filesystem tools (``read_file`` and ``list_dir``) sandboxed to
    that directory, allowing it to verify claims before raising them.
    ``run_command`` is deliberately excluded — even sandboxed, executing
    shell is not read-only."""
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "review.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        all_fs_tools = build_fs_tools(repo_dir, settings)
        # run_command is deliberately NOT included — even sandboxed, executing
        # shell is not read-only. The reviewer can verify file content via
        # read_file + list_dir without arbitrary command execution.
        readonly_names = {"read_file", "list_dir"}
        tools = [t for t in all_fs_tools if t.__name__ in readonly_names]

    overrides = {}
    if model_name is not None:
        overrides["model_name"] = model_name
    elif not definition.model:
        overrides["model_name"] = settings.review_model

    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        **overrides,
    )
    try:
        user_prompt = ""
        if prior_context is not None:
            user_prompt += f"{prior_context}\n\n"
        user_prompt += (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<git_diff>\n{diff}\n</git_diff>"
        )
        limits = UsageLimits(request_limit=settings.review_request_limit)
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt, usage_limits=limits),
            settings=settings, what="review",
        )
    finally:
        _safe_close(agent)
    return result.output
