"""Dual-model review agent: audits a git diff blind.

A second model (defaults to a different model than the implement agent)
reviews the implementation diff with no access to the implement agent's
context — only the diff and ticket spec.  Returns a structured verdict:
APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION.
"""

from __future__ import annotations

import json
import logging
import re
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
            "Set to true when verdict is APPROVE and your comments "
            "contain only minor or informational observations (style "
            "nits, non-blocking improvement suggestions, informational "
            "notes). Set to false ONLY when: (a) verdict is "
            "REQUEST_CHANGES or NEEDS_DISCUSSION; OR (b) you identified "
            "a genuine security risk, irreversible action, or "
            "correctness blocker — even if you approved overall — that "
            "a human should specifically review before the PR merges. "
            "Minor observations, style nits, and non-blocking notes do "
            "NOT justify false. When in doubt, set to true for "
            "APPROVE verdicts."
        ),
    )


# Substrings that identify a provider context-window / token-limit
# overflow (case-insensitive match on the exception message). These
# surface as unhandled exceptions from the model call — unlike transient
# rate-limit errors, retrying the same prompt won't help, so the review
# path catches them and degrades. Mirrors the precedent in
# ``trace_inspector.run_trace_inspector`` (the ``"maximum context length"``
# branch).
_TOKEN_LIMIT_SIGNALS = (
    "maximum context length",
    "context length",
    "token limit",
    "context_length_exceeded",
    "tokens requested",
)

_OUTPUT_TOKEN_EXHAUSTION_SIGNALS = ("before any response was generated",)


def _is_token_limit_error(exc: BaseException) -> bool:
    """True when *exc*'s message matches a known token-limit signal."""
    msg = str(exc).lower()
    return any(sig in msg for sig in _TOKEN_LIMIT_SIGNALS)


def _is_output_token_exhaustion(exc: BaseException) -> bool:
    """True when *exc* indicates output-token exhaustion (max_tokens too
    low for reasoning output), NOT input context overflow."""
    msg = str(exc).lower()
    return any(sig in msg for sig in _OUTPUT_TOKEN_EXHAUSTION_SIGNALS)


def _review_attempt(
    *,
    diff_text: str,
    use_preseed: bool,
    note: str | None,
    max_tokens_override: int | None = None,
    spec: str,
    prior_context: str | None,
    reference_files: list[str] | None,
    repo_dir: Path | None,
    screenshot_path: Path | None,
    agent: Any,
    level: int,
    settings: Settings,
    limits: Any,
    claude_sdk_supports_inline_image: Any,
) -> object:
    """Build the prompt and run one review pass, returning the
    agent's (possibly re-prompted) output. Raises on token-limit
    overflow so the caller can decide whether to degrade."""
    from .prompt_blocks import section
    from .structured_output_guard import reprompt_if_unstructured
    from .retry import run_agent
    from .base import level_uses_claude

    user_prompt = ""
    if note:
        user_prompt += f"{note}\n\n"
    if prior_context is not None:
        user_prompt += f"{prior_context}\n\n"
    user_prompt += (
        section("ticket-spec", spec) + "\n\n" + section("git-diff", diff_text)
    )
    run_kwargs: dict[str, Any] = {"usage_limits": limits}
    if max_tokens_override is not None:
        from pydantic_ai.settings import ModelSettings

        run_kwargs["model_settings"] = ModelSettings(max_tokens=max_tokens_override)
    run_user_prompt: str | list[Any] | None = user_prompt
    # Build the synthetic message_history AFTER the user_prompt is
    # finalized so the prompt can be prepended cleanly BEFORE the
    # preload tool calls; see fs_tools.build_preseed_history.
    if use_preseed and reference_files and repo_dir is not None:
        from .fs_tools import build_preseed_history

        preseed = build_preseed_history(
            repo_dir,
            list(reference_files),
            user_prompt=user_prompt,
        )
        if preseed:
            run_kwargs["message_history"] = preseed
            run_user_prompt = None
    # Attach a board screenshot as a vision image ONLY when the
    # review agent is routed to the Claude SDK backend AND that
    # backend can actually view inline images (the capability gate
    # — default OFF, because the installed llmio bridge silently
    # mishandles BinaryContent and stalls the CLI for 1200s).
    # DeepSeek has no vision either. A missing/unreadable file
    # degrades silently to the text-only path — never crash review.
    if (
        screenshot_path is not None
        and level_uses_claude(level)
        and claude_sdk_supports_inline_image(settings)
    ):
        run_user_prompt = _maybe_attach_screenshot(run_user_prompt, screenshot_path)
    result = run_agent(
        agent,
        lambda h: h.run_sync(run_user_prompt, **run_kwargs),
        what="review",
    )
    schema_json = json.dumps(ReviewVerdict.model_json_schema(), indent=2)
    example_json = json.dumps(
        {
            "verdict": "APPROVE",
            "comments": (
                "The diff looks correct — all files match the spec. "
                "Minor note: consider adding a docstring to the new function."
            ),
            "request_changes": [],
            "auto_merge_eligible": True,
        },
        indent=2,
        ensure_ascii=False,
    )
    reprompt_message = (
        "Your last response did not produce a structured "
        "ReviewVerdict. Reply now with a valid JSON object matching "
        "the expected schema below.\n\n"
        "Expected JSON schema:\n"
        f"{schema_json}\n\n"
        "Valid example:\n"
        f"{example_json}\n\n"
        "CRITICAL: all string values must be valid JSON — escape any "
        'inner double quotes with backslash (\\"). '
        "Do not include raw newlines inside string values; use \\n instead."
    )
    result = reprompt_if_unstructured(
        result=result,
        agent=agent,
        expected_type=ReviewVerdict,
        reprompt_message=reprompt_message,
        settings=settings,
        what="review (re-prompt after prose-only)",
        run_kwargs={"usage_limits": limits},
        require_no_tool_calls=False,
    )
    return result.output


def run_review_agent(
    *,
    settings: Settings,
    diff: str,
    spec: str,
    level: int | None = None,
    prior_context: str | None = None,
    repo_dir: Path | None = None,
    reference_files: list[str] | None = None,
    screenshot_path: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> ReviewVerdict:
    """Run a blind review of *diff* against *spec*.

    The agent receives ONLY the diff and spec — no implementation
    context, no memory, no history. Uses *level* if given,
    otherwise the level declared in ``review.yaml``.

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

    When *extra_roots* is provided, those directories are added as
    secondary sandbox roots so the agent can also ``read_file`` and
    ``list_dir`` inside cloned sibling repositories (e.g. to verify
    reusable-workflow interfaces referenced in the diff). Paths outside
    *repo_dir* and outside every *extra_roots* entry are still rejected.

    When *screenshot_path* is provided AND the file exists AND the review
    agent is routed to the Claude SDK backend (vision-capable), the PNG
    is read and attached as a ``pydantic_ai.BinaryContent`` image on the
    FINAL user turn so the model sees the rendered board alongside the
    diff. On the default DeepSeek path (no Claude SDK routing) the image
    is never attached — DeepSeek has no vision and would reject an image
    block. A missing/unreadable screenshot degrades silently to the
    text-only path; it never alters routing or crashes review."""
    import functools

    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import (
        build_agent_from_definition,
        _safe_close,
        claude_sdk_supports_inline_image,
    )

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "review.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        all_fs_tools = build_fs_tools(repo_dir, settings, extra_roots=extra_roots)
        # run_command is deliberately NOT included — even sandboxed, executing
        # shell is not read-only. The reviewer can verify file content via
        # read_file + list_dir without arbitrary command execution.
        readonly_names = {"read_file", "list_dir"}
        tools = [t for t in all_fs_tools if t.__name__ in readonly_names]

        from ..core.tool_wrappers import wrap_read_tools_with_consecutive_error_guard

        tools = wrap_read_tools_with_consecutive_error_guard(tools)

    overrides: dict[str, Any] = {}
    if level is not None:
        overrides["level"] = level

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
        limits = UsageLimits(request_limit=settings.review_request_limit)

        _attempt = functools.partial(
            _review_attempt,
            spec=spec,
            prior_context=prior_context,
            reference_files=reference_files,
            repo_dir=repo_dir,
            screenshot_path=screenshot_path,
            agent=agent,
            level=level if level is not None else definition.level,
            settings=settings,
            limits=limits,
            claude_sdk_supports_inline_image=claude_sdk_supports_inline_image,
        )

        output = _run_with_degraded_retry(_attempt, diff=diff, settings=settings)
    finally:
        _safe_close(agent)
    return _coerce_verdict(output)


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified git diff into per-file ``(path, chunk_text)`` pairs.

    Splits on ``^diff --git `` boundaries and extracts the file path from
    the ``+++ b/<path>`` line in each chunk.  Empty chunks and deletion-only
    chunks (``+++ /dev/null``) are skipped.  Pairs are returned in the same
    order as the original diff.
    """
    chunks = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    result: list[tuple[str, str]] = []
    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped:
            continue
        path_match = re.search(r"^\+\+\+ b/(.+)$", chunk, re.MULTILINE)
        if not path_match:
            continue
        path = path_match.group(1)
        if path == "/dev/null":
            # Deletion-only chunk — no file to review
            continue
        result.append((path, stripped))
    return result


def _synthesize_chunk_verdicts(
    attempt: Any,
    per_chunk: list[tuple[str, ReviewVerdict]],
    settings: Settings,
) -> ReviewVerdict:
    """Run a synthesis pass over per-file chunk verdicts.

    Builds a *diff_text* from the per-chunk summaries (one Markdown
    section per file: verdict, comments, and any ``request_changes``
    asks), then calls *attempt* for a final consolidated verdict.  The
    returned verdict's ``comments`` field is prefixed with a
    machine-readable ``[Chunked review: …]`` marker.
    """
    sections: list[str] = []
    for path, verdict in per_chunk:
        sec = f"## {path}\n\n**Verdict:** {verdict.verdict}\n\n{verdict.comments}"
        if verdict.request_changes:
            asks = "\n".join(
                f"- **{a.title}**: {a.description}" for a in verdict.request_changes
            )
            sec += f"\n\n**Requested changes:**\n{asks}"
        sections.append(sec)

    synthesis_text = "\n\n".join(sections)
    synthesis_note = (
        f"Synthesis pass: you previously reviewed {len(per_chunk)} files "
        f"independently (chunked review due to diff size). Below are the "
        f"per-file summaries. Produce a single consolidated ReviewVerdict "
        f"covering ALL files."
    )
    output = attempt(diff_text=synthesis_text, use_preseed=False, note=synthesis_note)
    verdict = _coerce_verdict(output)

    # Deterministic severity floor: the synthesis LLM re-decides the
    # verdict from prose summaries, so a chunk's REQUEST_CHANGES could
    # otherwise be silently dropped — or worse, come back APPROVE with
    # auto_merge_eligible=True (unreviewed auto-merge). Floor the final
    # verdict at REQUEST_CHANGES when any chunk requested changes, union
    # the asks the synthesis pass dropped, and never allow auto-merge
    # from a chunked review (no single pass ever saw the whole diff).
    chunk_asks = [a for _, v in per_chunk for a in v.request_changes]
    any_request_changes = any(v.verdict == "REQUEST_CHANGES" for _, v in per_chunk)
    if any_request_changes and verdict.verdict != "REQUEST_CHANGES":
        verdict.verdict = "REQUEST_CHANGES"
        seen = {(a.title, a.description) for a in verdict.request_changes}
        verdict.request_changes.extend(
            a for a in chunk_asks if (a.title, a.description) not in seen
        )
    verdict.auto_merge_eligible = False

    verdict.comments = (
        f"[Chunked review: {len(per_chunk)} files reviewed in "
        f"{len(per_chunk)} chunks due to diff size]\n\n" + verdict.comments
    )
    return verdict


def _run_chunked_review(
    attempt: Any,
    diff: str,
    settings: Settings,
) -> ReviewVerdict | None:
    """Review each file's diff independently, then synthesise a consolidated verdict.

    Returns a synthesized :class:`ReviewVerdict` on success, or ``None``
    when a single file exceeds the per-chunk budget (caller falls through
    to the existing degraded single-pass).

    Steps:

    1. Split *diff* into per-file chunks via :func:`_split_diff_by_file`.
    2. If any single chunk exceeds the per-file budget (max of
       ``settings.review_diff_max_chars`` and 40 000), log a warning and
       return ``None`` immediately.
    3. Review each chunk independently, collecting ``(path, verdict)``
       pairs.
    4. Run a synthesis pass via :func:`_synthesize_chunk_verdicts` to
       produce the consolidated verdict.
    """
    chunks = _split_diff_by_file(diff)
    if not chunks:
        log.warning("chunked review: no per-file chunks extracted from diff")
        return None

    per_file_budget = max(settings.review_diff_max_chars, 40_000)

    # Single-file overflow guard: if any one file's diff is still too
    # large, chunked review cannot help — bail so the caller falls
    # through to the degraded single-pass.
    for path, chunk_text in chunks:
        if len(chunk_text) > per_file_budget:
            log.warning(
                "chunked review: single file %r diff (%d chars) exceeds "
                "per-chunk budget (%d chars); falling through to degraded "
                "single-pass",
                path,
                len(chunk_text),
                per_file_budget,
            )
            return None

    n = len(chunks)
    per_chunk_verdicts: list[tuple[str, ReviewVerdict]] = []

    for i, (path, chunk_text) in enumerate(chunks, start=1):
        note = (
            f"Reviewing file {i}/{n}: {path}. "
            f"This is part {i} of {n} in a chunked review — "
            f"focus on this file's changes. "
            f"Cross-file concerns will be assessed in a synthesis pass."
        )
        try:
            output = attempt(diff_text=chunk_text, use_preseed=False, note=note)
        except Exception as exc:
            if _is_token_limit_error(exc):
                log.warning(
                    "chunked review: single file %r still overflows (%s); "
                    "falling through to degraded single-pass",
                    path,
                    exc,
                )
                return None
            raise
        verdict = _coerce_verdict(output)
        per_chunk_verdicts.append((path, verdict))

    try:
        return _synthesize_chunk_verdicts(attempt, per_chunk_verdicts, settings)
    except Exception as exc:
        if _is_token_limit_error(exc):
            # The synthesis prompt itself hit the model's token limit
            # (with output-token exhaustion this can happen regardless of
            # prompt size). Preserve the graceful-degradation contract:
            # fall through to Tier 3 instead of crashing the stage.
            log.warning(
                "chunked review: synthesis pass hit token limit (%s); "
                "falling through to degraded single-pass",
                exc,
            )
            return None
        raise


def _run_with_degraded_retry(
    attempt: Any,
    *,
    diff: str,
    settings: Settings,
) -> object:
    """Run *attempt* (one review pass), degrading on token-limit overflow.

    Calls ``attempt(diff_text=, use_preseed=, note=)``.  Degradation
    follows a three-tier fallback:

    Tier 1 — full diff + preseed (reference files preloaded).
        On token-limit error: log warning, advance to Tier 2.
        On other error: re-raise (unchanged).

    Tier 2 — chunked per-file review via :func:`_run_chunked_review`.
        Each file's diff is reviewed independently, then a synthesis
        pass produces a consolidated verdict.
        Returns ``ReviewVerdict`` → use it (success).
        Returns ``None`` (single file too big) → fall through to Tier 3.

    Tier 3 — degraded single-pass: NO preseed, hard-truncated diff.
        On token-limit error: return best-effort ``NEEDS_DISCUSSION``.
        On success: return verdict (unchanged).

    Non token-limit exceptions propagate unchanged, so ``run_agent``'s
    transient retry and the caller's handling apply exactly as before.
    """
    # Tier 1: full diff + preseed -------------------------------------------
    try:
        return attempt(diff_text=diff, use_preseed=True, note=None)
    except Exception as exc:
        if not _is_token_limit_error(exc):
            raise
        # Output-token exhaustion: the model burned its entire max_tokens
        # budget on reasoning before emitting a verdict.  Retry with
        # increased max_tokens (same untruncated diff, same preseed).
        if _is_output_token_exhaustion(exc):
            output_budget = settings.review_output_token_budget
            if output_budget > 0:
                log.warning(
                    "review: output-token exhaustion (%s); retrying with "
                    "increased max_tokens=%d (diff unchanged)",
                    exc,
                    output_budget,
                )
                try:
                    return attempt(
                        diff_text=diff,
                        use_preseed=True,
                        note=None,
                        max_tokens_override=output_budget,
                    )
                except Exception as exc2:
                    if not _is_token_limit_error(exc2):
                        raise
                    log.warning(
                        "review: output-token exhaustion persists after "
                        "budget increase (%s); returning NEEDS_DISCUSSION",
                        exc2,
                    )
            else:
                log.warning(
                    "review: output-token exhaustion (%s) but "
                    "review_output_token_budget=0; returning NEEDS_DISCUSSION",
                    exc,
                )
            return ReviewVerdict(
                verdict="NEEDS_DISCUSSION",
                comments=(
                    "The review model exhausted its output token budget "
                    f"(max_tokens={output_budget or 'agent-default'}) before "
                    "generating a verdict — its reasoning output consumed the "
                    "entire budget. This is NOT a context-window overflow (the "
                    "diff was within limits). An automated verdict is unavailable "
                    "— a human should review this PR directly, or the review "
                    "model's max_tokens should be raised further."
                ),
                auto_merge_eligible=False,
            )
        log.warning(
            "review: context-window/token-limit error (%s); retrying with "
            "chunked per-file review",
            exc,
        )

    # Tier 2: chunked per-file review ---------------------------------------
    chunked_result = _run_chunked_review(attempt, diff, settings)
    if chunked_result is not None:
        return chunked_result

    # Tier 3: degraded single-pass — hard-truncated diff, no preseed --------
    from ..core.text_utils import head_tail_keep

    # Hard-truncate to a small fixed budget. ``or 40_000`` keeps the cap
    # real when review_diff_max_chars is 0 (uncapped).
    degraded_budget = min(settings.review_diff_max_chars or 40_000, 40_000)
    degraded_diff = head_tail_keep(diff, degraded_budget, label="git-diff")
    note = (
        "NOTE: the git diff below was heavily truncated due to its size — "
        "base your verdict on the visible portion and flag uncertainty "
        "rather than assuming the omitted middle is fine."
    )
    try:
        return attempt(diff_text=degraded_diff, use_preseed=False, note=note)
    except Exception as exc2:
        if not _is_token_limit_error(exc2):
            raise
        # Still overflowing after aggressive truncation — do NOT crash the
        # stage. Return a best-effort NEEDS_DISCUSSION so review completes
        # and the ticket isn't silently blocked.
        log.warning(
            "review: token-limit error persists after degraded retry (%s); "
            "returning NEEDS_DISCUSSION",
            exc2,
        )
        return ReviewVerdict(
            verdict="NEEDS_DISCUSSION",
            comments=(
                "The git diff exceeded the review model's context window even "
                f"after truncation (~{len(diff)} chars). An automated verdict "
                "is unavailable — a human should review this PR directly, or "
                "the change should be split into smaller diffs."
            ),
            auto_merge_eligible=False,
        )


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
