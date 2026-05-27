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
        for k in ("summary_text", "summary_str", "summaryText",
                  "result_summary", "text", "result", "output"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                data["summary"] = v
                return data
        # Tier 2: any non-updated_memory string value. Pick the
        # longest — heuristically the most likely candidate for a
        # multi-sentence summary.
        candidates = [
            (k, v) for k, v in data.items()
            if k not in ("summary", "updated_memory")
            and isinstance(v, str) and v.strip()
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

        passed, feedback = run_test_agent(
            settings=settings, repo_dir=repo_dir
        )
        return f"{'PASS' if passed else 'FAIL'}: {feedback}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(ToolInfo(
        name="run_tests",
        description="Run the project's test suite (isolated sandbox) via the test sub-agent.",
        category="testing",
        parameters={},
    ))

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
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "implement.yaml"
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
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                log.warning(
                    "reference_files: %s not found on disk, skipping",
                    rf["path"],
                )

    extra_roots: list[Path] | None = None

    fs = build_fs_tools(repo_dir, settings, pre_seeded=pre_seeded,
                        extra_roots=extra_roots)
    # the main agent reads + writes itself and includes run_command for
    # focused diagnosis (re-run a single failing test, run a linter,
    # inspect git diff, etc.). The full suite is run by the stage.
    fs_tools = [
        t for t in fs if t.__name__ in
        ("read_file", "write_file", "list_dir", "edit_file", "delete_file", "run_command")
    ]

    # Build synthetic message_history when reference files are provided
    # and the caller hasn't supplied an explicit message_history.
    # NOTE: do NOT inject a TextPart-wrapped system prompt as the first
    # message — TextPart is only valid in ModelResponse.parts. Placing
    # it in a ModelRequest triggers pydantic-ai's "Expected code to be
    # unreachable" assertion and aborts the entire implement run. The
    # system prompt is already added by build_agent below; the synthetic
    # history starts directly with the preloaded read_file ToolCall /
    # ToolReturn pairs, which pydantic-ai accepts (helper shared with
    # the review agent — see fs_tools.build_preseed_history).
    if reference_files and message_history is None:
        from .fs_tools import build_preseed_history

        final_message_history = build_preseed_history(
            repo_dir,
            [rf["path"] for rf in reference_files],
        )

    overrides = {}
    if model_name is not None:
        overrides["model_name"] = model_name
    elif not definition.model:
        overrides["model_name"] = settings.model

    from .consult_expert import make_consult_expert_tool

    agent = build_agent_from_definition(
        settings, definition,
        tools=[
            make_explore_tool(settings, repo_dir, extra_roots=extra_roots),
            make_consult_expert_tool(settings, repo_dir),
            *fs_tools,
        ],
        **overrides,
    )
    try:
        limits = UsageLimits(request_limit=settings.coordinator_request_limit)
        user_prompt = ""
        if epic_context:
            user_prompt += f"{epic_context}\n\n"
        user_prompt += (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
        )
        if feedback:
            if previous_attempt_summary:
                # Inject prior summary before the feedback block so the
                # model doesn't undo its prior correct work.
                user_prompt = (
                    "<previous_attempt>\n"
                    "Your previous edit pass produced this summary "
                    "(already on disk):\n"
                    f"{previous_attempt_summary}\n"
                    "</previous_attempt>\n\n"
                ) + user_prompt
            if feedback.startswith("[REVIEW"):
                # Review feedback — prepend to the spec so the coordinator
                # addresses the flagged issues first.
                user_prompt = (
                    "<review_feedback>\n"
                    "The code review flagged issues. Address these review "
                    "comments before proceeding.\n"
                    "For each comment you fully address, call "
                    "`close_thread(comment_id)` to mark it resolved. If you "
                    "need to explain your approach or ask a clarifying "
                    "question, call `reply_to_thread(thread_id, body)` first.\n"
                    f"{feedback}\n"
                    "</review_feedback>\n\n"
                ) + user_prompt
            elif feedback.startswith("[SCOPE"):
                user_prompt += (
                    "\n\n<scope_violation>\n"
                    "Your previous edit pass is already on disk, but it "
                    "modified files outside the ticket's stated scope. "
                    "The ticket spec is the source of truth for what is "
                    "in scope.\n"
                    f"{feedback}\n"
                    "</scope_violation>\n\n"
                    "Revert the out-of-scope changes and stop."
                )
            else:
                user_prompt += (
                    "\n\n<test_failure>\n"
                    "Your previous edit pass is already on disk, but the test "
                    "suite then failed. Diagnosis:\n"
                    f"{feedback}\n"
                    "</test_failure>\n\n"
                    "Fix exactly this failure and stop."
                )
        result = call_with_retry(
            lambda: agent.run_sync(
                user_prompt,
                message_history=final_message_history,
                usage_limits=limits,
            ),
            settings=settings, what="implement",
        )
    finally:
        _safe_close(agent)
    return result.output


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


def _resolve_expert_memory_path(settings: "Settings", definition) -> Path:
    """Resolve the on-disk memory ledger path for an expert.

    Prefers ``definition.memory.memory_path`` when explicitly set;
    otherwise derives ``{data_dir}/expert_{domain}_memory.md``.
    """
    explicit = definition.memory.memory_path if definition.memory else None
    if explicit:
        return Path(explicit)
    return Path(settings.data_dir) / f"expert_{definition.domain}_memory.md"


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
    parts: list[str] = []
    if epic_context:
        parts.append(epic_context)
    parts.append(f"<ticket_spec>\n{spec}\n</ticket_spec>")
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
        "<domain_context>\n"
        f"You are the `{domain}` expert. Focus on these in-scope files "
        f"matched against your domain's module_paths:\n"
        f"{files_block}\n"
        f"{other_line}\n"
        "</domain_context>"
    )
    user_prompt = "\n\n".join(parts)
    if feedback:
        prefix = ""
        if previous_attempt_summary:
            prefix = (
                "<previous_attempt>\n"
                "Your previous edit pass produced this summary "
                "(already on disk):\n"
                f"{previous_attempt_summary}\n"
                "</previous_attempt>\n\n"
            )
        if feedback.startswith("[REVIEW"):
            block = (
                "<review_feedback>\n"
                "The code review flagged issues. Address these review "
                "comments before proceeding:\n"
                f"{feedback}\n"
                "</review_feedback>"
            )
            user_prompt = prefix + block + "\n\n" + user_prompt
        elif feedback.startswith("[SCOPE"):
            user_prompt = (
                prefix + user_prompt
                + "\n\n<scope_violation>\n"
                "Your previous edit pass is already on disk, but it "
                "modified files outside the ticket's stated scope. "
                f"{feedback}\n"
                "</scope_violation>\n\n"
                "Revert the out-of-scope changes and stop."
            )
        else:
            user_prompt = (
                prefix + user_prompt
                + "\n\n<test_failure>\n"
                "Your previous edit pass is already on disk, but the test "
                "suite then failed. Diagnosis:\n"
                f"{feedback}\n"
                "</test_failure>\n\n"
                "Fix exactly this failure and stop."
            )
    return user_prompt


def _aggregate_expert_results(
    results: list[tuple[str, ImplementResult]],
) -> ImplementResult:
    """Merge per-expert `(domain, ImplementResult)` tuples into one.

    - summary: ``[{domain}] {expert.summary}`` joined by newlines.
    - reference_files: deduplicated union, preserving first-seen order.
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
) -> ImplementResult:
    """Route the implement pass through one-or-more domain experts.

    Behaviour and fallback rules — see the module-level comment above.

    Returns an aggregated :class:`ImplementResult`. Falls back to
    :func:`run_coordinator` (with the same kwargs minus ``file_map``)
    when no expert routes the work.
    """
    from .expert_manager import ExpertManager
    from ..pass_runner import load_memory, persist_memory
    from .retry import call_with_retry
    from .base import _safe_close

    def _fallback(reason: str) -> ImplementResult:
        log.info("run_coordinator_with_experts: falling back (%s)", reason)
        return run_coordinator(
            settings=settings, repo_dir=repo_dir, spec=spec, memory=memory,
            model_name=model_name, feedback=feedback,
            epic_context=epic_context, reference_files=reference_files,
            message_history=message_history,
            previous_attempt_summary=previous_attempt_summary,
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
        return _fallback(
            "file_map missing or empty (LLM routing not yet implemented)"
        )

    files_by_domain: dict[str, list[str]] = {}
    for domain, definition in definitions.items():
        matched = [
            f for f in sorted(file_map)
            if ExpertManager.match_module_paths(definition.module_paths, f)
        ]
        if matched:
            files_by_domain[domain] = matched

    if not files_by_domain:
        return _fallback("no expert's module_paths matched any in-scope file")

    log.info(
        "run_coordinator_with_experts: routing to %d expert(s): %s",
        len(files_by_domain), sorted(files_by_domain.keys()),
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
        UnexpectedModelBehavior, UsageLimitExceeded,
    )

    results: list[tuple[str, ImplementResult]] = []
    active_domains = sorted(files_by_domain.keys())
    try:
        for domain in active_domains:
            definition = definitions[domain]
            memory_path = _resolve_expert_memory_path(settings, definition)
            try:
                expert_memory = load_memory(
                    memory_path,
                    max_chars=definition.memory.max_memory_chars,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "could not load memory for expert %s at %s",
                    domain, memory_path, exc_info=True,
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
                run_result = call_with_retry(
                    lambda: agent.run_sync(
                        user_prompt,
                        usage_limits=limits,
                        message_history=preseed_history,
                    ),
                    settings=settings, what=f"expert:{domain}",
                )
            except (UsageLimitExceeded, UnexpectedModelBehavior) as e:
                log.warning(
                    "expert %s failed (%s); skipping and continuing",
                    domain, type(e).__name__,
                )
                continue
            except Exception:  # noqa: BLE001
                log.exception(
                    "expert %s raised; skipping and continuing", domain,
                )
                continue

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
                        domain, memory_path, exc_info=True,
                    )
    finally:
        mgr.close_all()

    if not results:
        return _fallback("every expert failed")

    return _aggregate_expert_results(results)
