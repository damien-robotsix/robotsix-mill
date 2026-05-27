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



class ReviewAsk(BaseModel):
    """One actionable change request, classified against ticket scope.

    The review stage uses ``files_touched`` to split asks into
    in-scope vs out-of-scope (against the ticket's ``file_map.json``)
    and routes them differently: in-scope asks become a single
    review comment and bounce the ticket to READY for another
    implement pass; each out-of-scope ask is materialised as a fresh
    dependency ticket and the current ticket is parked on those
    deps. This prevents the loop where scope-triage rejects edits
    that review legitimately demands.
    """

    description: str = Field(
        description="The concrete change the implement agent must make. "
                    "One ask = one logical issue; split unrelated issues "
                    "across multiple ReviewAsk entries."
    )
    files_touched: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths the implement agent would need "
                    "to add/edit/delete to address this ask. Leave empty "
                    "only when the ask is genuinely file-less (e.g. "
                    "clarify a spec ambiguity). Include EVERY file the "
                    "change would touch, even peripheral ones like "
                    "`.gitignore` — that classification is what lets the "
                    "review stage spawn a dep ticket for out-of-scope work."
    )


class ReviewVerdict(BaseModel):
    """Structured output from the blind review agent."""

    verdict: Literal["APPROVE", "REQUEST_CHANGES", "NEEDS_DISCUSSION"]
    comments: str = Field(
        description="Detailed review feedback. For APPROVE, note any "
                    "minor observations. For REQUEST_CHANGES, summarise "
                    "the issues here AND populate ``request_changes`` "
                    "with one entry per actionable ask. For "
                    "NEEDS_DISCUSSION, explain what requires human "
                    "judgment."
    )
    request_changes: list[ReviewAsk] = Field(
        default_factory=list,
        description="Structured list of actionable change requests. "
                    "REQUIRED on REQUEST_CHANGES verdicts (one entry per "
                    "issue); leave empty for APPROVE / NEEDS_DISCUSSION. "
                    "Each ask names the files it would touch so the "
                    "stage can split in-scope vs out-of-scope work."
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
    reference_files: list[str] | None = None,
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
    shell is not read-only.

    When *reference_files* is provided (paths relative to *repo_dir*),
    each file's contents are preloaded as a synthetic read_file
    ToolCall / ToolReturn pair in the agent's ``message_history``. The
    reviewer "wakes up" with those files already in context — saving
    one LLM round-trip per file (the "decide to call read_file →
    consume the result" cycle). Pass the union of the implement
    stage's ``ImplementResult.reference_files`` and paths parsed from
    the diff so the common case (reviewer wants every modified file)
    skips all its read_file round-trips."""
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

    # Build the synthetic-message_history pre-seed when paths are
    # supplied. Same helper implement uses (fs_tools.build_preseed_history).
    message_history = None
    if reference_files and repo_dir is not None:
        from .fs_tools import build_preseed_history

        preseed = build_preseed_history(repo_dir, list(reference_files))
        if preseed:
            message_history = preseed

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
        run_kwargs: dict = {"usage_limits": limits}
        if message_history is not None:
            run_kwargs["message_history"] = message_history
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt, **run_kwargs),
            settings=settings, what="review",
        )
    finally:
        _safe_close(agent)
    return result.output
