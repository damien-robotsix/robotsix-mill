"""Dual-model review agent: audits a git diff blind.

A second model (defaults to a different model than the implement agent)
reviews the implementation diff with no access to the implement agent's
context — only the diff and ticket spec.  Returns a structured verdict:
APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal

from ..config import Settings


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
            "Set to True ONLY when you are highly confident the change is "
            "safe to auto-merge without human review. The change must be "
            "small, self-contained, well-tested by existing tests, and "
            "carry no architectural risk. When in ANY doubt, leave False — "
            "the PR will wait for a human merge."
        ),
    )


SYSTEM_PROMPT = """\
You are a senior code reviewer conducting a blind audit of a git diff.
You have NO access to the implementation agent's context, memory, or
reasoning — you see only the diff and the ticket spec below.

Audit for:
- Security vulnerabilities (injection, auth bypass, unsafe defaults, etc.)
- Logical errors (off-by-one, inverted conditions, missing guards)
- Design problems (tight coupling, leaky abstractions, wrong layer)
- Missing edge cases (null/empty inputs, error paths, race conditions)
- Style violations relative to the existing codebase conventions visible
  in the diff (inconsistent naming, formatting, patterns)

Do NOT:
- Critique the *idea* or re-litigate the ticket spec — audit the
  *implementation*.
- Suggest major architectural rewrites unless the implementation
  introduces a genuine risk.
- Nitpick trivial style preferences (trailing whitespace, optional
  commas) unless they violate a clear convention shown in the diff.

Be specific: cite file paths and line-level issues where possible.
Group related issues together.

Return your verdict:
- APPROVE: the implementation is correct, safe, and ready to deliver.
  Use the comments field for minor observations only.
- REQUEST_CHANGES: there are specific, actionable issues that must be
  addressed before delivery. List them clearly in comments.
- NEEDS_DISCUSSION: you see something that requires human judgment
  (ambiguous design trade-off, unclear spec interpretation). Explain
  what needs discussion in comments.

The ``auto_merge_eligible`` field:
Set this to ``true`` ONLY when the change meets ALL of these criteria:
- The diff is small and focused (single concern, few files).
- Existing tests cover the changed code paths (no new untested logic).
- No new infrastructure, framework, or architectural pattern is introduced.
- You see zero risk of regression or unintended side-effects.

Default to ``false`` — when uncertain, choose the human path. Even an
APPROVE verdict may have ``auto_merge_eligible: false`` if the change is
large but correct.
"""


def run_review_agent(
    *,
    settings: Settings,
    diff: str,
    spec: str,
    model_name: str | None = None,
) -> ReviewVerdict:
    """Run a blind review of *diff* against *spec*.

    The agent receives ONLY the diff and spec — no implementation
    context, no memory, no history. Uses *model_name* if given,
    otherwise falls back to ``settings.review_model``."""
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent, _safe_close
    from .retry import call_with_retry

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(ReviewVerdict),
        tools=[],
        web=False,
        report_issue=False,
        model_name=model_name if model_name is not None else settings.review_model,
        name="review",
    )
    try:
        user_prompt = (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<git_diff>\n{diff}\n</git_diff>"
        )
        limits = UsageLimits(request_limit=4)
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt, usage_limits=limits),
            settings=settings, what="review",
        )
    finally:
        _safe_close(agent)
    return result.output
