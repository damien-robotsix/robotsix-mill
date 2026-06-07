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

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from ..config import Settings


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


def make_run_tests_tool(settings: Settings, repo_dir: Path):
    def run_tests() -> str:
        """Run the project's test suite (isolated sandbox) via the test
        sub-agent. Returns 'PASS' or 'FAIL' followed by a short,
        actionable diagnosis — never the raw log."""
        from .testing import run_test_agent

        passed, feedback = run_test_agent(settings=settings, repo_dir=repo_dir)
        return f"{'PASS' if passed else 'FAIL'}: {feedback}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="run_tests",
            description="Run the project's test suite (isolated sandbox) via the test sub-agent.",
            category="testing",
            parameters={},
        )
    )

    return run_tests


def run_coordinator(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    memory: str = "",
    model_name: str | None = None,
    feedback: str | None = None,
    epic_context: str = "",
    reference_files: list[dict] | None = None,
    message_history: list | None = None,
    previous_attempt_summary: str | None = None,
    board_id: str = "",
    language_instructions: str = "",
    extra_roots: list[Path] | None = None,
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
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import run_agent

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "implement.yaml"
    )

    # Pre-seed fs_tools cache and build synthetic message_history when
    # reference files are provided (first invocation only, not a retry).
    pre_seeded: dict[str, str] | None = None
    final_message_history: list | None = message_history

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
        repo_dir, settings, pre_seeded=pre_seeded, extra_roots=extra_roots
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

    overrides = {}
    if model_name is not None:
        overrides["model_name"] = model_name
    elif not definition.model:
        overrides["model_name"] = settings.model

    prompt = definition.system_prompt
    if language_instructions:
        prompt += "\n\n## Language conventions\n\n" + language_instructions
    overrides["system_prompt"] = prompt

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
        tools=[
            make_explore_tool(settings, repo_dir, extra_roots=extra_roots),
            make_consult_expert_tool(settings, repo_dir, board_id=board_id),
            make_spawn_subtask_tool(settings, repo_dir),
            make_post_comment_tool(settings, agent_name="implement"),
            *fs_tools,
        ],
        **overrides,
    )
    try:
        from .prompt_blocks import section

        limits = UsageLimits(request_limit=settings.coordinator_request_limit)
        user_prompt = ""
        if epic_context:
            user_prompt += f"{epic_context}\n\n"
        user_prompt += (
            section("ticket-spec", spec)
            + "\n\n"
            + section("memory", memory or "(empty — start a new ledger)")
        )
        if feedback:
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

        result = run_agent(
            agent,
            lambda h: h.run_sync(
                run_user_prompt,
                message_history=final_message_history,
                usage_limits=limits,
            ),
            settings=settings,
            what="implement",
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


# ──────────────────────────────────────────────────────────────────────
# Expert-aware coordinator (ticket 0e3e)
#
# `run_coordinator_with_experts` is the seam `coding.run_implement_agent`
# *would* drive when expert definitions exist. It:
#
#   1. Loads all expert definitions; falls back to `run_coordinator`
#      if the definitions dir is missing, empty, or fails to parse.
#   2. Routes the work to one-or-more experts:
#       - With `file_map`: match each domain's `module_paths` glob
#         against every file in scope. The set of domains with ≥1
#         matching file are the active experts.
#       - Without `file_map`: a lightweight routing LLM call picks
#         domains by name from the spec. (Future work; for the first
#         cut we fall back to `run_coordinator` when file_map is None.)
#   3. Invokes each active expert sequentially with structured
#      `ImplementResult` output, its own per-domain memory ledger
#      injected via `memory_text=`, and a `<domain_context>` block
#      naming the other active domains.
#   4. Persists each expert's `updated_memory` to its memory file.
#   5. Aggregates the per-expert summaries + reference_files into
#      a single ImplementResult and returns it.
#
# Failure modes are caught — UsageLimitExceeded / UnexpectedModelBehavior
# in one expert is logged, that expert is skipped, others still run.
# If ALL experts fail (or zero matched), falls back to `run_coordinator`.
# ──────────────────────────────────────────────────────────────────────


def _resolve_expert_memory_path(
    settings: "Settings",
    definition,
    board_id: str = "",
) -> Path:
    """Resolve the on-disk memory ledger path for an expert.

    Prefers ``definition.memory.memory_path`` when explicitly set;
    otherwise routes via ``settings.memory_file_for`` so the file
    lives under the per-repo subtree
    (``{data_dir}/{board_id}/expert_{domain}_memory.md``). Empty
    ``board_id`` falls back to the legacy root path for callers
    that don't yet carry repo context.
    """
    explicit = definition.memory.memory_path if definition.memory else None
    if explicit:
        return Path(explicit)
    return settings.memory_file_for(f"expert_{definition.domain}", board_id)


def _build_expert_prompt(
    *,
    spec: str,
    domain: str,
    matched_files: list[str],
    other_domains: list[str],
    feedback: str | None,
    previous_attempt_summary: str | None,
    epic_context: str,
) -> str:
    """Build the user prompt for one expert agent.

    Mirrors `run_coordinator`'s prompt structure but adds a
    `<domain_context>` block scoping the expert to its files.
    Memory is NOT injected here — `create_expert(memory_text=…)`
    puts it in the system prompt instead.
    """
    from .prompt_blocks import section

    parts: list[str] = []
    if epic_context:
        parts.append(epic_context)
    parts.append(section("ticket-spec", spec))
    other_line = (
        f"Other experts also working this ticket: {', '.join(other_domains)}."
        if other_domains
        else "You are the only expert assigned to this ticket."
    )
    files_block = (
        "\n".join(f"  - {p}" for p in matched_files)
        if matched_files
        else "  (no in-scope files passed; fall back to module_paths in your definition)"
    )
    parts.append(
        section(
            "domain-context",
            f"You are the `{domain}` expert. Focus on these in-scope files "
            f"matched against your domain's module_paths:\n"
            f"{files_block}\n"
            f"{other_line}",
        )
    )
    user_prompt = "\n\n".join(parts)
    if feedback:
        prefix = ""
        if previous_attempt_summary:
            prefix = (
                section(
                    "previous-attempt",
                    "Your previous edit pass produced this summary "
                    "(already on disk):\n"
                    f"{previous_attempt_summary}",
                )
                + "\n\n"
            )
        if feedback.startswith("[REVIEW"):
            block = section(
                "review-feedback",
                "The code review flagged issues. Address these review "
                "comments before proceeding:\n"
                f"{feedback}",
            )
            user_prompt = prefix + block + "\n\n" + user_prompt
        elif feedback.startswith("[SCOPE"):
            user_prompt = (
                prefix
                + user_prompt
                + "\n\n"
                + section(
                    "scope-violation",
                    "Your previous edit pass is already on disk, but it "
                    "modified files outside the ticket's stated scope. "
                    f"{feedback}",
                )
                + "\n\nRevert the out-of-scope changes and stop."
            )
        else:
            user_prompt = (
                prefix
                + user_prompt
                + "\n\n"
                + section(
                    "test-failure",
                    "Your previous edit pass is already on disk, but the test "
                    "suite then failed. Diagnosis:\n"
                    f"{feedback}",
                )
                + "\n\nFix exactly this failure and stop."
            )
    return user_prompt


def _aggregate_expert_results(
    results: list[tuple[str, ImplementResult]],
    *,
    settings: Settings,
    repo_dir: Path,
) -> ImplementResult:
    """Merge per-expert `(domain, ImplementResult)` tuples into one.

    - summary: ``[{domain}] {expert.summary}`` joined by newlines.
    - reference_files: deduplicated union, preserving first-seen order,
      then trimmed to ``reference_files_max_count`` and
      ``reference_files_max_total_lines`` so large expert result sets
      don't bloat the coordinator's preload context.
    - updated_memory: empty string (per-expert memory is persisted by
      the runner; the implement-stage memory ledger is the coordinator's
      responsibility, handled at the stage level).
    """
    lines: list[str] = []
    seen_refs: set[str] = set()
    merged_refs: list[str] = []
    for domain, r in results:
        if r.summary:
            lines.append(f"[{domain}] {r.summary}")
        for f in r.reference_files:
            if f not in seen_refs:
                seen_refs.add(f)
                merged_refs.append(f)

    # Enforce the reference-file caps (config: core.memory.*). Trim by
    # count first, then by cumulative line count across the referenced
    # files' on-disk contents.
    if len(merged_refs) > settings.reference_files_max_count:
        merged_refs = merged_refs[: settings.reference_files_max_count]

    total_lines = 0
    trimmed: list[str] = []
    for ref_file in merged_refs:
        try:
            line_count = len(
                (repo_dir / ref_file)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except OSError:
            line_count = 0
        if trimmed and total_lines + line_count > (
            settings.reference_files_max_total_lines
        ):
            break
        trimmed.append(ref_file)
        total_lines += line_count
    merged_refs = trimmed

    return ImplementResult(
        summary="\n".join(lines) if lines else "(no expert produced a summary)",
        updated_memory="",
        reference_files=merged_refs,
    )


def run_coordinator_with_experts(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    memory: str = "",
    model_name: str | None = None,
    feedback: str | None = None,
    epic_context: str = "",
    reference_files: list[dict] | None = None,
    message_history: list | None = None,
    previous_attempt_summary: str | None = None,
    file_map: set[str] | None = None,
    board_id: str = "",
) -> ImplementResult:
    """Route the implement pass through one-or-more domain experts.

    Behaviour and fallback rules — see the module-level comment above.

    Returns an aggregated :class:`ImplementResult`. Falls back to
    :func:`run_coordinator` (with the same kwargs minus ``file_map``)
    when no expert routes the work.
    """
    from .expert_manager import ExpertManager
    from ..runners.pass_runner import load_memory, persist_memory
    from .retry import run_agent

    def _fallback(reason: str) -> ImplementResult:
        log.info("run_coordinator_with_experts: falling back (%s)", reason)
        return run_coordinator(
            settings=settings,
            repo_dir=repo_dir,
            spec=spec,
            memory=memory,
            model_name=model_name,
            feedback=feedback,
            epic_context=epic_context,
            reference_files=reference_files,
            message_history=message_history,
            previous_attempt_summary=previous_attempt_summary,
            board_id=board_id,
        )

    # Step 1: Load definitions. Failure → fallback.
    mgr = ExpertManager(settings, repo_dir)
    try:
        definitions = mgr.load_definitions()
    except FileNotFoundError as e:
        return _fallback(f"no expert definitions: {e}")
    except Exception as e:  # noqa: BLE001 — pessimistic on YAML parse errors
        return _fallback(f"failed to load definitions: {e}")
    if not definitions:
        return _fallback("definitions dir loaded but empty")

    # Step 2: Route. Today we only support the file_map path; the
    # LLM-routing fallback is documented and left as a future hook.
    if not file_map:
        return _fallback("file_map missing or empty (LLM routing not yet implemented)")

    files_by_domain: dict[str, list[str]] = {}
    for domain, definition in definitions.items():
        matched = [
            f
            for f in sorted(file_map)
            if ExpertManager.match_module_paths(definition.module_paths, f)
        ]
        if matched:
            files_by_domain[domain] = matched

    if not files_by_domain:
        return _fallback("no expert's module_paths matched any in-scope file")

    log.info(
        "run_coordinator_with_experts: routing to %d expert(s): %s",
        len(files_by_domain),
        sorted(files_by_domain.keys()),
    )

    # Build the same synthetic read_file message_history that
    # ``run_coordinator`` uses for reference_files, so each expert
    # starts with the refine-curated files already "read". Without
    # this every expert pass had to re-explore the codebase from
    # scratch and burned tokens on read_file calls that the refine
    # stage already paid for.
    preseed_history: list | None = message_history
    if reference_files and message_history is None:
        from .fs_tools import build_preseed_history

        preseed_history = build_preseed_history(
            repo_dir,
            [rf["path"] for rf in reference_files],
        )

    # Step 3: Delegate sequentially.
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits
    from pydantic_ai.exceptions import (
        UnexpectedModelBehavior,
        UsageLimitExceeded,
    )

    results: list[tuple[str, ImplementResult]] = []
    active_domains = sorted(files_by_domain.keys())
    try:
        for domain in active_domains:
            definition = definitions[domain]
            memory_path = _resolve_expert_memory_path(settings, definition, board_id)
            try:
                expert_memory = load_memory(
                    memory_path,
                    max_chars=definition.memory.max_memory_chars,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "could not load memory for expert %s at %s",
                    domain,
                    memory_path,
                    exc_info=True,
                )
                expert_memory = ""

            agent = mgr.create_expert(
                definition,
                output_type=PromptedOutput(ImplementResult),
                memory_text=expert_memory,
            )

            other_domains = [d for d in active_domains if d != domain]
            user_prompt = _build_expert_prompt(
                spec=spec,
                domain=domain,
                matched_files=files_by_domain[domain],
                other_domains=other_domains,
                feedback=feedback,
                previous_attempt_summary=previous_attempt_summary,
                epic_context=epic_context,
            )
            limits = UsageLimits(
                request_limit=settings.coordinator_request_limit,
            )
            try:
                run_result = run_agent(
                    agent,
                    lambda h: h.run_sync(
                        user_prompt,
                        usage_limits=limits,
                        message_history=preseed_history or None,
                    ),
                    settings=settings,
                    what=f"expert:{domain}",
                )
            except (UsageLimitExceeded, UnexpectedModelBehavior) as e:
                log.warning(
                    "expert %s failed (%s); skipping and continuing",
                    domain,
                    type(e).__name__,
                )
                continue
            except Exception:  # noqa: BLE001
                log.exception(
                    "expert %s raised; skipping and continuing",
                    domain,
                )
                continue

            # NOTE: prose-only guard intentionally not yet wired into the expert path — see ticket ce37
            expert_output: ImplementResult = run_result.output
            results.append((domain, expert_output))

            # Persist this expert's memory eagerly so a later failure
            # in another expert can't lose the learning.
            if expert_output.updated_memory:
                try:
                    persist_memory(memory_path, expert_output.updated_memory)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "failed to persist memory for expert %s at %s",
                        domain,
                        memory_path,
                        exc_info=True,
                    )
    finally:
        mgr.close_all()

    if not results:
        return _fallback("every expert failed")

    return _aggregate_expert_results(results, settings=settings, repo_dir=repo_dir)
