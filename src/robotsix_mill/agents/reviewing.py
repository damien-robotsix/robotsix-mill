"""Dual-model review agent: audits a git diff blind.

A second model (defaults to a different model than the implement agent)
reviews the implementation diff with no access to the implement agent's
context — only the diff and ticket spec.  Returns a structured verdict:
APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION.
"""

from __future__ import annotations

import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Any, Literal

from ..config import Settings

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

log = logging.getLogger(__name__)

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent / "agent_definitions" / "review.yaml"
)
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

    title: str = Field(
        default="",
        description="Short, imperative title (≤80 chars) suitable as a "
        "ticket title if this ask becomes a dependency. "
        "Should name the *action*, e.g. 'add __pycache__ to "
        ".gitignore' — NOT the symptom ('remove "
        "__pycache__/foo.pyc'). When empty the review stage "
        "derives one from the description.",
    )
    description: str = Field(
        description="A self-contained work item, framed as the proper "
        "fix (NOT the observed symptom). Read your text "
        "imagining it will be the body of a brand-new "
        "ticket — describe the underlying problem AND the "
        "right way to address it. "
        "Bad: 'remove __pycache__/foo.pyc'. "
        "Good: '__pycache__ files are tracked because the "
        "repo has no .gitignore — add an entry for "
        "__pycache__/ to .gitignore to prevent it.' "
        "One ask = one logical issue; split unrelated "
        "issues across multiple ReviewAsk entries."
    )
    files_touched: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths the *proper fix* you "
        "described above would touch — NOT the symptom "
        "files visible in the current diff. For the "
        ".gitignore example: ``['.gitignore']``, not "
        "``['__pycache__/foo.pyc']``. The review stage uses "
        "this list to decide if the ask is in-scope for "
        "the current ticket or needs a dependency ticket. "
        "Leave empty only when the ask is genuinely "
        "file-less (e.g. clarify a spec ambiguity).",
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
        "stage can split in-scope vs out-of-scope work.",
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
    screenshot_path: Path | None = None,
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
    skips all its read_file round-trips.

    When *screenshot_path* is provided AND the file exists AND the review
    agent is routed to the Claude SDK backend (vision-capable), the PNG
    is read and attached as a ``pydantic_ai.BinaryContent`` image on the
    FINAL user turn so the model sees the rendered board alongside the
    diff. On the default DeepSeek path (no Claude SDK routing) the image
    is never attached — DeepSeek has no vision and would reject an image
    block. A missing/unreadable screenshot degrades silently to the
    text-only path; it never alters routing or crashes review."""
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close, _use_claude_sdk
    from .retry import run_agent

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
        settings,
        definition,
        # Confine the SDK's built-in Read/Bash to the clone so review reasons
        # about the ticket's code, not the worker's own /app source.
        repo_dir=repo_dir,
        tools=tools,
        **overrides,
    )
    try:
        from .prompt_blocks import section

        user_prompt = ""
        if prior_context is not None:
            user_prompt += f"{prior_context}\n\n"
        user_prompt += section("ticket-spec", spec) + "\n\n" + section("git-diff", diff)
        limits = UsageLimits(request_limit=settings.review_request_limit)
        run_kwargs: dict = {"usage_limits": limits}
        run_user_prompt: str | list[Any] | None = user_prompt
        # Build the synthetic message_history AFTER the user_prompt is
        # finalized so the prompt can be prepended cleanly BEFORE the
        # preload tool calls; see fs_tools.build_preseed_history.
        if reference_files and repo_dir is not None:
            from .fs_tools import build_preseed_history

            preseed = build_preseed_history(
                repo_dir,
                list(reference_files),
                user_prompt=user_prompt,
            )
            if preseed:
                run_kwargs["message_history"] = preseed
                run_user_prompt = None
        # Attach a board screenshot as a vision image ONLY when the review
        # agent is routed to the Claude SDK backend (DeepSeek has no vision
        # and rejects image blocks). A missing/unreadable file degrades
        # silently to the text-only path — never crash review.
        if screenshot_path is not None and _use_claude_sdk(settings, definition.name):
            run_user_prompt = _maybe_attach_screenshot(run_user_prompt, screenshot_path)
        result = run_agent(
            agent,
            lambda h: h.run_sync(run_user_prompt, **run_kwargs),
            settings=settings,
            what="review",
        )
        from .structured_output_guard import reprompt_if_unstructured

        result = reprompt_if_unstructured(
            result=result,
            agent=agent,
            expected_type=ReviewVerdict,
            reprompt_message=(
                "Your last response did not produce a structured ReviewVerdict. "
                "Reply now with a JSON object containing the required fields: "
                "verdict (one of APPROVE, REQUEST_CHANGES, NEEDS_DISCUSSION), "
                "comments, and request_changes."
            ),
            settings=settings,
            what="review (re-prompt after prose-only)",
            run_kwargs={"usage_limits": limits},
            require_no_tool_calls=False,
        )
    finally:
        _safe_close(agent)
    return _coerce_verdict(result.output)


def _maybe_attach_screenshot(
    run_user_prompt: str | list[Any] | None,
    screenshot_path: Path,
) -> str | list[Any] | None:
    """Return *run_user_prompt* with the board PNG attached as a vision
    image, or unchanged when the file is missing/unreadable.

    Caller has already confirmed the agent is routed to a vision-capable
    backend. A missing/unreadable file degrades silently to the text-only
    path — review must never crash on a bad screenshot.
    """
    if not screenshot_path.exists():
        return run_user_prompt
    try:
        png_bytes = screenshot_path.read_bytes()
    except OSError:
        return run_user_prompt
    if not png_bytes:
        return run_user_prompt

    from pydantic_ai import BinaryContent

    image = BinaryContent(data=png_bytes, media_type="image/png")
    if run_user_prompt is None:
        # Pre-seed active: the text prompt is in message_history; give the
        # run a short instruction plus the image as the final user turn.
        return [
            "A full-page screenshot of the rendered kanban board is "
            "attached. Assess its visual appearance (columns, seeded "
            "tickets, layout) alongside the diff.",
            image,
        ]
    return [run_user_prompt, image]


def _coerce_verdict(output: object) -> ReviewVerdict:
    """Return *output* as a :class:`ReviewVerdict`, degrading safely.

    pydantic-ai can fall back to raw text when the structured-output parse
    fails even after its output retries. Returning that bare str would crash
    the review STAGE on ``verdict.verdict`` ("'str' object has no attribute
    'verdict'") and hard-BLOCK the ticket with a Fatal — even though implement
    already succeeded and reached review. Degrade an unparseable review to
    NEEDS_DISCUSSION (never APPROVE — that could auto-merge unreviewed code):
    the stage routes it to AWAITING_USER_REPLY so a human makes the call,
    instead of a crash that needs a manual unblock.
    """
    if isinstance(output, ReviewVerdict):
        return output
    log.warning(
        "review: agent returned non-structured output (%s); "
        "degrading to NEEDS_DISCUSSION for human review",
        type(output).__name__,
    )
    text = output if isinstance(output, str) else ""
    return ReviewVerdict(
        verdict="NEEDS_DISCUSSION",
        comments=(
            "The review agent's structured output could not be parsed, so an "
            "automated verdict is unavailable — a human should review this PR "
            "directly. Raw model output (truncated):\n\n" + text[:1500]
        ),
        auto_merge_eligible=False,
    )
