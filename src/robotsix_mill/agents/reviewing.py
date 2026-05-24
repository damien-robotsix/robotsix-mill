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

When the user prompt contains a ``<prior_context>`` block:
- Check it for comments you already raised in earlier rounds — do NOT
  re-raise the same issue unless the implement agent has *not* addressed
  it.  If you recognise a contradiction across your own prior rounds,
  resolve it and explain your final position.
- The ``<implement_rebuttal>`` section (if present) contains the implement
  agent's summary of what it did in the last round.  If it convincingly
  demonstrates that a prior comment was a false positive (e.g. a claim
  you made based on incomplete diff context), explicitly acknowledge the
  withdrawal and drop that comment.  Do NOT re-raise withdrawn issues.

You have access to read-only filesystem tools (``read_file`` and
``list_dir``) on the real repo clone.  Use them:
- Verify "missing import", "undefined symbol", "duplicated code", or
  similar claims against the real file content *before* raising them.
- If you cannot verify a claim from the diff alone, either verify it
  with a tool or explicitly flag it as unverified ("I cannot confirm X
  from the diff — consider checking manually").

The ``auto_merge_eligible`` field:
Set this to ``true`` when your verdict is ``APPROVE`` and you raised no
specific concern in ``comments`` — if the implementation is correct and
you have nothing concrete to flag, a human doesn't need to look.

Set this to ``false`` only when you can articulate a *specific* reason a
human should still look, even if it's not blocking — for example, an
architectural choice you noticed in passing, an accepted-but-flagged risk,
or a gap between the spec and the implementation that didn't meet the
REQUEST_CHANGES threshold.

``REQUEST_CHANGES`` and ``NEEDS_DISCUSSION`` verdicts always leave this
``false``.

When unsure whether a genuine human-judgment concern is present, default
to ``false`` (the PR will wait for a human).
"""


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
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent, _safe_close
    from .retry import call_with_retry

    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        all_fs_tools = build_fs_tools(repo_dir, settings)
        # run_command is deliberately NOT included — even sandboxed, executing
        # shell is not read-only. The reviewer can verify file content via
        # read_file + list_dir without arbitrary command execution.
        readonly_names = {"read_file", "list_dir"}
        tools = [t for t in all_fs_tools if t.__name__ in readonly_names]

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(ReviewVerdict),
        tools=tools,
        web=False,
        report_issue=False,
        model_name=model_name if model_name is not None else settings.review_model,
        name="review",
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
