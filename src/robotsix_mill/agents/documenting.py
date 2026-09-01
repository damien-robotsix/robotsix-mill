"""Documentation agent: classifies diff impact and updates docs.

The agent reads the ticket spec + git diff, classifies the change as
user-facing or internal-only, and — for user-facing changes — surveys
the repo's existing docs and applies targeted surgical edits.

Returns a structured ``DocResult`` with ``user_facing`` and ``summary``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from robotsix_mill._resources import agent_definitions_dir

from ..config import Settings

log = logging.getLogger(__name__)


class _DocRateLimitCeiling(Exception):
    """Internal sentinel: the doc run hit a rate-limit / usage ceiling.

    Raised from inside the run callback so the shared ``run_agent`` retry
    machinery sees a non-transient, non-tier-unavailable error and re-raises
    it immediately — no transient backoff, no provider-failover re-runs. The doc
    run loop catches it and degrades to a recommendation-only deliverable
    instead of burning further cycles on a guaranteed-to-fail retry loop.
    """


def _is_rate_limit_ceiling(exc: BaseException) -> bool:
    """True when *exc* (or a cause/context in its chain) is a rate-limit /
    usage ceiling that further retries cannot clear.

    Recognises the pydantic-ai ``UsageLimitExceeded`` family
    (``is_rate_limited``), the Claude SDK session/weekly caps
    (``ClaudeSDKUsageExhaustedError`` via ``is_tier_unavailable``), and any
    "token limit exceeded"-style message the transports surface as plain
    text. Once any of these fires, the ceiling is real — the doc agent
    should degrade rather than retry.
    """
    from .retry import is_rate_limited, is_tier_unavailable

    if is_rate_limited(exc) or is_tier_unavailable(exc):
        return True
    seen: set[int] = set()
    cur: BaseException | None = exc
    for _ in range(10):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        msg = str(cur).lower()
        if (
            "token limit exceeded" in msg
            or "usagelimitexceeded" in msg
            or "usage limit" in msg
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class DocClassifierResult(BaseModel):
    """Structured output from the cheap doc-classifier gate.

    This is a separate type from ``DocResult`` — the classifier only
    classifies; it never edits docs.
    """

    user_facing: bool = Field(
        description="True when the diff introduces a user-facing change "
        "(new feature, API change, config key, CLI flag, "
        "behavioral change a user would notice). False for "
        "internal-only changes (refactor, bug-fix with no doc "
        "impact, test/CI-only, lint/format)."
    )
    classification: str = Field(
        min_length=1,
        description="One-line human-readable classification, e.g. "
        "'internal-only — model field rename' or "
        "'user-facing — new CLI flag'.",
    )


class DocResult(BaseModel):
    """Structured output from the documentation agent."""

    user_facing: bool = Field(
        description="True when the diff introduces a user-facing change "
        "(new feature, API change, config key, CLI flag, "
        "behavioral change a user would notice). False for "
        "internal-only changes (refactor, bug-fix with no doc "
        "impact, test/CI-only, lint/format)."
    )
    summary: str = Field(
        min_length=1,
        description="Summary of documentation changes made, or a note "
        "that no changes were needed.",
    )
    updated_memory: str = Field(
        default="",
        description="Updated memory ledger — record the repo's doc "
        "layout, README sections, doc subdirs, and any "
        "conventions discovered during this run. Subsequent "
        "doc agents read this ledger so they don't have to "
        "explore the structure from scratch. Empty = no "
        "updates (incoming memory was complete).",
    )
    degraded: bool = Field(
        default=False,
        description="Set by the harness (never the agent) when the doc "
        "run was cut short by a rate-limit ceiling and the "
        "result is a recommendation-only fallback rather than a "
        "completed pass. Distinguishes 'completed successfully' "
        "from 'degraded due to rate limit' for monitoring.",
    )


def run_doc_classifier(
    *,
    settings: Settings,
    diff: str,
    spec: str,
) -> DocClassifierResult:
    """Run the cheap doc-classifier gate.

    Loads ``agent_definitions/doc_classifier.yaml``, builds a zero-tool
    agent, and returns a ``DocClassifierResult`` classifying the change
    as user-facing or internal-only.  The classifier is purely
    diff-and-spec-driven — it receives no tools.

    Conservative bias: when uncertain, classifies as user-facing (the
    only real risk is a wrong "internal-only" that skips needed docs).
    """
    from pydantic_ai.usage import UsageLimits

    from .base import _safe_close, build_agent_from_definition
    from .retry import run_agent
    from .yaml_loader import load_agent_definition

    definition = load_agent_definition(agent_definitions_dir() / "doc_classifier.yaml")

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
    )
    try:
        from ..core.text_utils import truncate_at_boundary
        from .prompt_blocks import section

        # The classifier only needs enough diff to judge user-facing vs
        # internal-only; cap it (truncate_at_boundary is a no-op when the
        # diff is already at/under the cap, and appends a clear omission
        # marker otherwise). Safe: the classifier is biased toward
        # user_facing=True, so lost signal routes to the full doc agent.
        classifier_diff = truncate_at_boundary(
            diff, settings.doc_classifier_diff_max_chars
        )
        user_prompt = (
            section("ticket-spec", spec) + "\n\n" + section("git-diff", classifier_diff)
        )
        limits = UsageLimits(request_limit=settings.doc_classifier_request_limit)
        result = run_agent(
            agent,
            lambda h: h.run_sync(user_prompt, usage_limits=limits),
            what="doc classifier",
        )
        return result.output
    finally:
        _safe_close(agent)


def _build_doc_preseed(
    reference_files: list[str] | None,
    repo_dir: Path | None,
    user_prompt: str,
) -> tuple[dict[str, Any], str | None]:
    """Build preseed message history when reference files are available.

    Returns ``(run_kwargs, run_user_prompt)`` — the caller passes these
    directly into ``h.run_sync()``.  When no preseed is applicable the
    returned kwargs dict is empty and *run_user_prompt* is the original
    prompt unchanged.
    """
    if not reference_files or repo_dir is None:
        return {}, user_prompt
    from .fs_tools import build_preseed_history

    preseed = build_preseed_history(
        repo_dir,
        list(reference_files),
        user_prompt=user_prompt,
    )
    if preseed:
        return {"message_history": preseed}, None
    return {}, user_prompt


def run_doc_agent(
    *,
    settings: Settings,
    repo_dir,
    diff: str,
    spec: str,
    level: int | None = None,
    extra_roots: list[Path] | None = None,
    board_id: str = "",
    reference_files: list[str] | None = None,
) -> tuple[DocResult, bytes | None]:
    """Build a documentation agent, classify *diff* + *spec*, and update
    docs for user-facing changes.

    The agent receives the ticket spec and git diff. It surveys the
    repo's docs (README.md, docs/*, AGENT.md) and applies targeted
    edits for user-facing changes. Internal-only changes are a no-op.

    When *reference_files* is provided, those repo-relative paths are
    pre-loaded into the agent's context via the same
    parallel-read_file preseed used by implement/review — the
    documenter usually has to read README.md and every changed source
    file to decide what to update, so handing those over up front
    skips one ``read_file`` round-trip per file.

    A persistent memory ledger (``settings.memory_file_for("doc", board_id)``) records
    the repo's doc layout across runs so subsequent passes don't have
    to re-explore the structure from scratch.
    """
    from pydantic_ai.usage import UsageLimits

    from ..agents.runners.pass_runner import load_memory, persist_memory
    from .base import _safe_close, build_agent_from_definition
    from .explore import make_explore_tool, make_parallel_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import run_agent
    from .yaml_loader import load_agent_definition

    definition = load_agent_definition(agent_definitions_dir() / "document.yaml")

    # Load the doc memory ledger (empty string if unset / missing /
    # unreadable — first run starts a fresh ledger).  When board_id
    # is empty we skip the ledger entirely and emit a warning.
    doc_memory_path = settings.memory_file_for("doc", board_id) if board_id else None
    if doc_memory_path is None:
        log.warning("doc agent running without memory ledger: empty board_id")
    memory_text = (
        load_memory(doc_memory_path, max_chars=settings.max_memory_chars)
        if doc_memory_path is not None
        else ""
    )

    fs = build_fs_tools(
        repo_dir,
        settings,
        extra_roots=extra_roots,
        write_blocked_prefixes=["www/", "src/"],
    )
    overrides: dict[str, Any] = {}
    if level is not None:
        overrides["level"] = level

    # Inject the memory block into the agent's system prompt — the
    # YAML's static prompt + a dynamic ``memory`` fenced block at the
    # end. The same pattern implement/refine/retrospect already use.
    from .prompt_blocks import section as _section

    system_prompt = definition.system_prompt
    system_prompt += "\n\n" + _section(
        "memory",
        memory_text or "(empty — start a new ledger)",
    )

    doc_fs_tools = [
        t
        for t in fs
        if t.__name__ in ("read_file", "write_file", "list_dir", "edit_file")
    ]
    from ..core.tool_wrappers import wrap_read_tools_with_consecutive_error_guard

    doc_fs_tools = wrap_read_tools_with_consecutive_error_guard(doc_fs_tools)

    agent = build_agent_from_definition(
        settings,
        definition,
        repo_dir=repo_dir,  # confine SDK built-in edit tools to the clone
        board_id=board_id,  # so report_issue can file a blocker on the board
        system_prompt=system_prompt,
        tools=[
            make_explore_tool(
                settings,
                repo_dir,
                extra_roots=extra_roots,
                pre_seeded_paths=reference_files,
            ),
            make_parallel_explore_tool(
                settings,
                repo_dir,
                extra_roots=extra_roots,
            ),
            *doc_fs_tools,
        ],
        **overrides,
    )
    try:
        from .prompt_blocks import section

        user_prompt = section("ticket-spec", spec) + "\n\n" + section("git-diff", diff)
        limits = UsageLimits(request_limit=settings.doc_request_limit)
        run_kwargs: dict[str, Any] = {"usage_limits": limits}
        # Pre-load the modified files (and any docs the operator
        # supplied) into a single parallel-read_file turn, with the
        # user_prompt as the leading ModelRequest so the trace reads
        # system → user → preload-call → preload-return → response.
        preseed_kw, run_user_prompt = _build_doc_preseed(
            reference_files,
            repo_dir,
            user_prompt,
        )
        run_kwargs.update(preseed_kw)

        def _run(h: Any) -> Any:
            try:
                return h.run_sync(run_user_prompt, **run_kwargs)
            except Exception as e:
                # Convert a rate-limit ceiling into a sentinel BEFORE
                # run_agent's transient-retry / failover machinery can
                # act on it — re-running the whole doc agent against a hit
                # ceiling only burns more credits for the same failure.
                if _is_rate_limit_ceiling(e):
                    raise _DocRateLimitCeiling(str(e)) from e
                raise

        try:
            result = run_agent(agent, _run, what="document")
        except _DocRateLimitCeiling as e:
            # Degrade gracefully: finalize with a lightweight
            # recommendation-only deliverable instead of retrying through
            # exhaustion. The ``degraded`` flag keeps the pattern observable.
            log.warning(
                "document agent hit a rate-limit ceiling (%s) — degrading to "
                "a recommendation-only deliverable instead of retrying",
                e,
            )
            degraded = DocResult(
                user_facing=True,
                summary=(
                    "Documentation degraded due to rate limit: the doc agent "
                    "reached a rate-limit/usage ceiling before it could "
                    "generate documentation, so no file edits were applied. "
                    "Re-run the document stage once the limit resets to "
                    f"produce the docs. (ceiling: {e})"
                ),
                degraded=True,
            )
            return degraded, None
        output: DocResult = result.output
        try:
            new_msgs = result.new_messages_json()
        except AttributeError:
            # The Claude SDK tool loop returns an ``_SdkToolResult`` that
            # mirrors only part of pydantic-ai's ``AgentRunResult`` and has
            # no ``new_messages_json()``.  Match the coordinating/refining
            # agents: fall back to ``None`` so pause detection simply sees
            # no new messages instead of raising.
            new_msgs = None
        # Persist the agent's updated ledger; empty string = keep
        # existing memory unchanged.  Respect the board_id guard —
        # only persist when we actually have a ledger path.
        if output.updated_memory and doc_memory_path is not None:
            persist_memory(doc_memory_path, output.updated_memory)
        return output, new_msgs
    finally:
        _safe_close(agent)
