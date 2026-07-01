"""The implement agent.

A capable model that reads and edits the repo ITSELF to satisfy ONE
ticket. Each invocation is a single explore→read→edit pass — the
implement *stage* owns the deterministic test→retry→escalate loop and
re-invokes this agent with a distilled failure diagnosis when the suite
fails. No separate implement sub-agent — that layer just re-explored
everything and never converged.

``run_coordinator`` is the seam ``coding.run_implement_agent`` drives
(name kept for the stage/tests).
"""

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from pydantic import BaseModel, model_validator

from ..config import Settings

_T = TypeVar("_T")


log = logging.getLogger(__name__)


class ImplementResult(BaseModel):
    """Structured output from the implement (coordinator) agent."""

    summary: str
    updated_memory: str = ""
    reference_files: list[str] = []
    # Full transcript (``all_messages_json``) — saved by the stage
    # runner for resume.
    conversation_state: bytes | None = None
    # Only messages added during THIS run (``new_messages_json``) —
    # used by ``check_for_pause`` so an old ask_user sentinel from a
    # prior turn doesn't re-trigger after resume.
    new_messages: bytes | None = None
    # When True the agent concluded the ticket's intent is already
    # satisfied by the codebase (e.g. a "remove dead ``hasattr``
    # guard" cleanup that has already been removed by a sibling
    # ticket). The stage routes such tickets to DONE with
    # ``no_change_rationale`` as the closing note — same shape as
    # refine's ``no_change_needed`` mode — instead of BLOCKING with a
    # generic "no changes produced" error. Default False keeps the
    # existing BLOCK-on-silent-no-changes behaviour.
    no_change_needed: bool = False
    no_change_rationale: str = ""

    @model_validator(mode="before")
    @classmethod
    def _absorb_summary_typos(cls, data):
        """deepseek-v4-pro repeatedly mis-keys the required ``summary``
        field. pydantic-ai's strict validation then exceeds output
        retries, the implement stage blocks the ticket with "Exceeded
        maximum output retries", and the user pays $1+ in coordinator
        cost per retry.

        Two-tier absorption:
        1. Preferred: a known near-miss key (``summary_text``, ``text``,
           ``result``, etc.).
        2. Fallback: any non-``updated_memory`` string value in the
           dict — the schema has only two string fields, so anything
           else the model emitted under a different name is almost
           certainly the intended summary.

        Only kicks in when canonical ``summary`` is missing/empty —
        correctly-keyed output passes straight through. Empty values
        are NOT absorbed (a genuinely-empty summary still surfaces
        downstream).
        """
        if not isinstance(data, dict):
            return data
        if data.get("summary"):
            return data
        # Tier 1: known near-misses in priority order.
        for k in (
            "summary_text",
            "summary_str",
            "summaryText",
            "result_summary",
            "text",
            "result",
            "output",
        ):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                data["summary"] = v
                return data
        # Tier 2: any non-updated_memory string value. Pick the
        # longest — heuristically the most likely candidate for a
        # multi-sentence summary.
        candidates = [
            (k, v)
            for k, v in data.items()
            if k not in ("summary", "updated_memory")
            and isinstance(v, str)
            and v.strip()
        ]
        if candidates:
            best_k, best_v = max(candidates, key=lambda kv: len(kv[1]))
            data["summary"] = best_v
        return data


class ValidationResult(BaseModel):
    """Deterministic routing decision for one implement iteration.

    Produced by the implement *stage* after each test-gate run (NOT by
    the model). It is the single routing authority — it decides whether
    to deliver (``proceed``), re-invoke the coordinator (``retry``), or
    block the ticket (``escalate``).
    """

    passed: bool
    next_action: Literal["proceed", "retry", "escalate"]
    failure_summary: str = ""
    iterations_used: int = 0

    @classmethod
    def decide(
        cls,
        *,
        passed: bool,
        iterations: int,
        max_iters: int,
        feedback: str = "",
    ) -> "ValidationResult":
        """Route deterministically from a test-gate outcome.

        ``passed`` → ``proceed``; a failure with attempts remaining →
        ``retry``; a failure on the last allowed attempt → ``escalate``.
        No LLM is involved — for any ``(passed, iterations, max_iters)``
        triple the result is fixed.
        """
        if passed:
            next_action: Literal["proceed", "retry", "escalate"] = "proceed"
        elif iterations < max_iters:
            next_action = "retry"
        else:
            next_action = "escalate"
        return cls(
            passed=passed,
            next_action=next_action,
            failure_summary="" if passed else feedback,
            iterations_used=iterations,
        )


def _call_with_timeout(
    fn: Callable[[], _T],
    timeout_seconds: int,
    what: str = "agent run",
) -> _T:
    """Run *fn* in a thread, return its result or raise TimeoutError.

    Uses a single-thread executor so the stage can reclaim control when
    the agent pass exceeds its wall-clock budget.  The abandoned thread
    will eventually complete (or be cleaned up at process exit); the
    stage retries with a fresh agent context on the next pass.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"{what} timed out after {timeout_seconds}s") from None


def run_coordinator(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    memory: str = "",
    level: int | None = None,
    feedback: str | None = None,
    epic_context: str = "",
    reference_files: list[dict] | None = None,
    message_history: list | None = None,
    previous_attempt_summary: str | None = None,
    board_id: str = "",
    current_ticket_id: str = "",
    language_instructions: str = "",
    extra_roots: list[Path] | None = None,
    sandbox_image: str | None = None,
) -> ImplementResult:
    """Run ONE explore→read→edit pass for the ticket and return the
    structured result.

    The implement *stage* owns the deterministic test→retry→escalate
    loop; when it re-invokes after a failed test gate it passes
    ``feedback`` — a distilled diagnosis of the previous run's failure —
    which is appended to the prompt as a ``<test_failure>`` block. The
    partial edits from earlier passes persist on disk in ``repo_dir``,
    so a retry continues from the current working tree. The seam tests
    monkeypatch this."""
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .explore import make_explore_tool, make_parallel_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import run_agent
    from .changelog_tool import make_insert_changelog_entry_tool

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "implement.yaml"
    )

    # Pre-seed fs_tools cache and build synthetic message_history when
    # reference files are provided (first invocation only, not a retry).
    pre_seeded: dict[str, str] | None = None
    final_message_history: list | None = message_history

    # Paths-only list (relative ``rf["path"]`` strings) of the reference
    # files the parent pre-seeds into its own context. Forwarded to the
    # context-isolated explore scout so it does NOT re-read files the
    # coordinator already has loaded. None on the resume path
    # (message_history provided) where nothing is pre-seeded.
    pre_seeded_paths: list[str] | None = (
        [rf["path"] for rf in reference_files]
        if (reference_files and message_history is None)
        else None
    )

    if reference_files and message_history is None:
        # Build pre_seeded mapping for _file_cache seeding (resolved Paths).
        # Read fresh from disk every time — the artifact is paths-only.
        pre_seeded = {}
        for rf in reference_files:
            file_path = repo_dir / rf["path"]
            try:
                pre_seeded[file_path.resolve()] = file_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                log.warning(
                    "reference_files: %s not found on disk, skipping",
                    rf["path"],
                )

    fs = build_fs_tools(
        repo_dir,
        settings,
        pre_seeded=pre_seeded,
        extra_roots=extra_roots,
        sandbox_image=sandbox_image,
    )
    # the main agent reads + writes itself and includes run_command for
    # focused diagnosis (re-run a single failing test, run a linter,
    # inspect git diff, etc.). The full suite is run by the stage.
    fs_tools = [
        t
        for t in fs
        if t.__name__
        in (
            "read_file",
            "write_file",
            "list_dir",
            "edit_file",
            "delete_file",
            "run_command",
        )
    ]

    from ..core.tool_wrappers import wrap_read_tools_with_consecutive_error_guard

    fs_tools = wrap_read_tools_with_consecutive_error_guard(fs_tools)

    overrides: dict[str, Any] = {}
    if level is not None:
        overrides["level"] = level

    from .consult_expert import make_consult_expert_tool
    from .post_comment import make_post_comment_tool
    from .spawn_subtask import make_spawn_subtask_tool

    agent = build_agent_from_definition(
        settings,
        definition,
        # Confine the Claude SDK's built-in Edit/Write/Bash to the ticket
        # clone. Without this the SDK runs them with cwd=the worker's own
        # source tree (/app, no .git): the agent edits /app and runs tests
        # there, the clone stays pristine, and the ticket blocks with "no
        # changes produced" while the agent reports success. (#578 wired
        # workspace_root into build_agent; the caller must feed it repo_dir.)
        repo_dir=repo_dir,
        # Thread the board so the report_issue tool can file a blocker/
        # dependency ticket. Without it report_issue is built with board_id=""
        # and fails at call time ("board_id is required"), so an agent that
        # legitimately cannot proceed (e.g. its target file is created by an
        # unmerged parent ticket) can't record WHY — it just surfaces as a
        # generic "no changes produced" block.
        board_id=board_id,
        current_ticket_id=current_ticket_id,
        tools=[
            make_explore_tool(
                settings,
                repo_dir,
                extra_roots=extra_roots,
                pre_seeded_paths=pre_seeded_paths,
            ),
            make_parallel_explore_tool(
                settings,
                repo_dir,
                extra_roots=extra_roots,
            ),
            make_consult_expert_tool(settings, repo_dir, board_id=board_id),
            make_spawn_subtask_tool(settings, repo_dir),
            make_post_comment_tool(settings, agent_name="implement"),
            make_insert_changelog_entry_tool(repo_dir),
            *fs_tools,
        ],
        **overrides,
    )
    try:
        from .prompt_blocks import section

        limits = UsageLimits(
            request_limit=settings.coordinator_request_limit,
            tool_calls_limit=settings.coordinator_max_tool_calls,
        )

        # -- delta-context trimming: retry/audit/re-refine passes ----------
        # When the stage re-invokes us with a failure diagnosis (feedback),
        # this is a retry pass — the model already saw the full spec, epic
        # context, and memory ledger on the first pass.  Re-sending them in
        # full inflates every call by 20-40%+ with no marginal value.
        # Instead, pass only the delta: a minimal spec reminder plus the
        # feedback block itself.  Gated by delta_context_retry_enabled
        # (default True) so operators can revert to full-context passes if
        # a particular ticket class needs them.
        _retry_spec = spec
        _retry_epic = epic_context
        _retry_memory = memory
        if feedback and settings.delta_context_retry_enabled:
            from ..core.delta_context import trim_spec_for_retry

            _retry_spec = trim_spec_for_retry(spec)
            _retry_epic = ""  # already injected on first pass
            _retry_memory = ""  # board conventions unchanged since first pass

        user_prompt = ""
        if language_instructions:
            user_prompt += (
                "## Language conventions\n\n" + language_instructions + "\n\n"
            )
        user_prompt += (
            f"The repository root (CWD for all run_command calls) is: {repo_dir}\n\n"
        )
        if _retry_epic:
            user_prompt += f"{_retry_epic}\n\n"
        user_prompt += (
            section("ticket-spec", _retry_spec)
            + "\n\n"
            + section("memory", _retry_memory or "(empty — start a new ledger)")
        )
        if previous_attempt_summary:
            # Inject prior summary before the feedback block so the
            # model doesn't undo its prior correct work.
            user_prompt = (
                section(
                    "previous-attempt",
                    "Your previous edit pass produced this summary "
                    "(already on disk):\n"
                    f"{previous_attempt_summary}",
                )
                + "\n\n"
            ) + user_prompt
        if feedback:
            if feedback.startswith("[REVIEW"):
                # Review feedback — prepend to the spec so the coordinator
                # addresses the flagged issues first.
                user_prompt = (
                    section(
                        "review-feedback",
                        "The code review flagged issues. Address these review "
                        "comments before proceeding.\n"
                        "For each comment, call `reply_to_thread(thread_id, body)` "
                        "to explain how you addressed it (or to ask a clarifying "
                        "question). Closing review threads is the reviewer's "
                        "responsibility, not yours.\n"
                        f"{feedback}",
                    )
                    + "\n\n"
                ) + user_prompt
            elif feedback.startswith("[SCOPE"):
                user_prompt += (
                    "\n\n"
                    + section(
                        "scope-violation",
                        "Your previous edit pass is already on disk, but it "
                        "modified files outside the ticket's stated scope. "
                        "The ticket spec is the source of truth for what is "
                        "in scope.\n"
                        f"{feedback}",
                    )
                    + "\n\nRevert the out-of-scope changes and stop."
                )
            else:
                user_prompt += (
                    "\n\n"
                    + section(
                        "test-failure",
                        "Your previous edit pass is already on disk, but the test "
                        "suite then failed. Diagnosis:\n"
                        f"{feedback}",
                    )
                    + "\n\nFix exactly this failure and stop."
                )
        # Build the synthetic message_history AFTER the user_prompt is
        # finalized so the prompt can be prepended as a clean
        # ModelRequest(UserPromptPart) BEFORE the preload tool calls.
        # Trace ordering becomes: system → user (real prompt) →
        # assistant (preload tool_calls) → user (tool returns) → model
        # response. Without this, pydantic-ai bundles the new
        # user_prompt as a trailing TextPart in the same ModelRequest
        # as the tool returns, which the Langfuse Formatted view hides
        # and which makes the model's own request invisible until it's
        # already seen the tool returns.
        run_user_prompt: str | None = user_prompt
        if reference_files and message_history is None:
            from .fs_tools import build_preseed_history

            final_message_history = build_preseed_history(
                repo_dir,
                [rf["path"] for rf in reference_files],
                user_prompt=user_prompt,
            )
            if final_message_history:
                # Prompt is already in the history; pass None so
                # pydantic-ai doesn't append a duplicate.
                run_user_prompt = None

        # History is passed through verbatim — NO compression. Dropping messages
        # from the front orphans a tool_call/tool_return pair or leaves a pending
        # tool-result the model must continue from without its reasoning_content
        # — both 400 on the DeepSeek capable tier. pydantic-ai round-trips
        # reasoning natively when the history is left intact.

        from .structured_output_guard import reprompt_if_unstructured

        result = _call_with_timeout(
            lambda: run_agent(
                agent,
                lambda h: h.run_sync(
                    run_user_prompt,
                    message_history=final_message_history,
                    usage_limits=limits,
                ),
                what="implement",
            ),
            timeout_seconds=settings.coordinator_timeout_seconds,
            what="implement agent",
        )
        result = reprompt_if_unstructured(
            result=result,
            agent=agent,
            expected_type=ImplementResult,
            reprompt_message=(
                "Your last response was all prose and no tool calls. Pick the "
                "first file change and use edit_file or write_file now."
            ),
            settings=settings,
            what="implement (re-prompt after prose-only)",
            run_kwargs={"usage_limits": limits},
            require_no_tool_calls=True,
        )
        output: ImplementResult = result.output
        if not isinstance(output, ImplementResult):
            # The model's final message didn't parse as ImplementResult JSON, so
            # llmio's structured-output path returned raw text (more likely on
            # the claude_sdk backend, which parses output itself). Coerce the
            # text into a result — without this, the setattr below raises
            # "'str' object has no attribute 'conversation_state'" AND the
            # except branch re-raises it, blocking the ticket.
            log.warning(
                "implement: output did not parse as ImplementResult (got %s); "
                "coercing raw text into summary",
                type(output).__name__,
            )
            output = ImplementResult(summary=str(output).strip() or "(no summary)")
        try:
            output.conversation_state = result.all_messages_json()
        except AttributeError:
            output.conversation_state = None
        try:
            output.new_messages = result.new_messages_json()
        except AttributeError:
            output.new_messages = None
    finally:
        _safe_close(agent)
    return output
