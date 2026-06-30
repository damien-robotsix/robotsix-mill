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

from ..config import Settings

log = logging.getLogger(__name__)


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

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "doc_classifier.yaml"
    )

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
    )
    try:
        from .prompt_blocks import section
        from ..core.text_utils import truncate_at_boundary

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
) -> DocResult:
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
    to re-explore the structure from scratch."""
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .explore import make_explore_tool, make_parallel_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import run_agent
    from ..runners.pass_runner import load_memory, persist_memory

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "document.yaml"
    )

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

    fs = build_fs_tools(repo_dir, settings, extra_roots=extra_roots)
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
        run_user_prompt: str | None = user_prompt
        run_kwargs: dict = {"usage_limits": limits}
        # Pre-load the modified files (and any docs the operator
        # supplied) into a single parallel-read_file turn, with the
        # user_prompt as the leading ModelRequest so the trace reads
        # system → user → preload-call → preload-return → response.
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

        result = run_agent(
            agent,
            lambda h: h.run_sync(run_user_prompt, **run_kwargs),
            what="document",
        )
        output: DocResult = result.output
        # Persist the agent's updated ledger; empty string = keep
        # existing memory unchanged.  Respect the board_id guard —
        # only persist when we actually have a ledger path.
        if output.updated_memory and doc_memory_path is not None:
            persist_memory(doc_memory_path, output.updated_memory)
        return output
    finally:
        _safe_close(agent)
