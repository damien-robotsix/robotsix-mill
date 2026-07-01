"""The exploration sub-agent.

The cheap driver model has a LIMITED context window, so it must never
read the repository directly. Instead it asks this sub-agent specific
questions. The sub-agent gets its OWN fresh, bounded context plus
read-only repo tools, does the navigating/reading, and returns only a
concise answer (or the specific file contents the driver requested) —
keeping the driver's context small.

``run_explore`` is the mockable seam — tests monkeypatch it (no key /
network), like the other agent seams.
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Any

from ..config import Settings, get_secrets
from ..runtime.tracing import trace_stage

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Budget-exhausted sentinel — set when the explore sub-agent exceeds its
# UsageLimits.request_limit even after a bounded retry.  ``coding.py``
# checks this after the coordinator run to escalate to BLOCKED.
# --------------------------------------------------------------------------

_explore_budget_exhausted: bool = False


def mark_explore_budget_exhausted() -> None:
    """Set the explore-budget-exhausted sentinel.

    Called by the explore sub-agent retry path when it exceeds
    ``UsageLimits.request_limit`` even after a bounded retry.
    ``coding.py`` checks this after the coordinator run.
    """
    global _explore_budget_exhausted
    _explore_budget_exhausted = True


def is_explore_budget_exhausted() -> bool:
    """Return whether the explore-budget-exhausted sentinel is set."""
    return _explore_budget_exhausted


def reset_explore_budget_exhausted() -> None:
    """Reset the explore-budget-exhausted sentinel for the next
    coordinator run."""
    global _explore_budget_exhausted
    _explore_budget_exhausted = False


# Number of outer retry attempts when the explore sub-agent fails with a
# non-transient (or exhausts its transient retries) error.  Each retry
# simplifies the question so the sub-agent has a better chance of
# completing before another connection hiccup.
_EXPLORE_MAX_ATTEMPTS = 3

# Maximum backoff delay (seconds) between outer explore retries.
_EXPLORE_BACKOFF_CAP = 30.0


_SYSTEM_PROMPT = """\
You are a code-orientation scout for ONE git repository. You have
read-only tools (run_command, read_file, list_dir). The caller will read the files
it needs itself — your job is only to point it there fast.

Reply with a TIGHT answer:
- the relevant file path(s) and the specific symbol / line range,
- a one-line note on what's there and how it relates to the question,
- at most a SHORT snippet (<=15 lines) ONLY if essential to answer.

NEVER paste whole files or large blocks — that is explicitly not your
job and wastes the caller's context. No speculation, no preamble.
Return the minimum that orients the caller.

SANDBOX GUARD — before making ANY tool call, classify the question:
- If the question is about a REPO-FILE (path exists inside this repo
  checkout), proceed normally with grep/read_file/list_dir.
- If the question is about an EXTERNAL LIBRARY (something installed,
  like a site-packages module, a stdlib internals path, or any file
  outside the repo checkout), do NOT make any tool call — reply
  immediately: "This requires external-library knowledge." The parent
  agent will resolve it via ``ask_web_knowledge``.

SCOPE DISCIPLINE — always follow these limits:
- CHECK KNOWN CONTEXT FIRST: if the user message contains a "Known
  context" block, read it FIRST. If it already answers the question,
  reply directly WITHOUT calling any tool. Never re-discover facts the
  caller has already supplied.
- TOOL NAMES ARE NOT SHELL COMMANDS: ``read_file``, ``run_command``, and
  ``list_dir`` are tool-call names — NOT shell commands. Invoke them
  only through the tool_call mechanism. Never pass any tool name (e.g.
  ``"list_dir src/foo/"``) as a command argument to ``run_command``.
- GREP BEFORE READ: for any symbol-lookup, string-search, or
  pattern-matching task, use run_command("grep -rn 'pattern'")
  BEFORE reading files.  Only reach for read_file when you need
  surrounding context beyond what the grep match line provides.
  Examples:
    run_command("grep -rn 'UnexpectedModelBehavior'")
    run_command("grep -rn 'def build_fs_tools' src/")
    run_command("grep -n '^## ' README.md")
- BATCH GREP PATTERNS: when searching for multiple unrelated symbols or
  strings, use a single ``grep -E`` with alternation instead of separate
  ``grep`` calls — this saves a model turn and reduces total output.
  Example:
    run_command("grep -rn -E 'symbol_a|symbol_b|symbol_c'")
- PINPOINT WITH grep -n BEFORE read_file: before calling read_file on a
  large file, use ``grep -n`` to find the exact line numbers of
  interest, then pass them as ``offset``/``limit``.  Do not read wide
  ranges guessing where the code lives — that wastes the caller's token
  budget.
- NO WHOLE-FILE SHELL DUMPS: never use run_command to ``cat``/``head``/
  ``tail``/``less`` an entire file to inspect its contents — that
  streams the whole body into the caller's context. When you need file
  *content*, use read_file (with ``offset``/``limit`` once grep has
  given you a line number). run_command is for grep/find/path checks,
  NOT for streaming file bodies.
- CONSOLIDATE DISCOVERY: prefer ONE combined grep/find/list_dir
  invocation over several overlapping discovery commands for the same
  facet, and never re-run a search whose answer a prior command already
  returned.
- EXTERNAL DEPENDENCIES ARE OUTSIDE THE SANDBOX: source for installed/
  external dependencies (anything under ``site-packages`` or an absolute
  system path like ``/usr/local/lib/...``) lives OUTSIDE the repo
  checkout and is NOT reachable by read_file OR run_command. Do not
  attempt or retry such paths. Instead, report back that the question
  requires external-library knowledge — the parent agent can resolve it
  via ``ask_web_knowledge``.
  BAD: read_file("/usr/local/lib/python3.14/site-packages/pydantic_ai/usage.py")
  — this WILL fail with a sandbox error. Always decline gracefully.
- CONFIRM PATHS: before calling read_file OR a targeted grep (e.g.
  ``grep -A5 'pattern' some/path.py``) on a path you have not already
  seen in a previous list_dir or grep result, verify it exists with
  list_dir on the parent directory or run_command with find. Never guess
  a file path.
- NO GREP CHASING: when a grep produces NO output (exit 1 or empty
  result), do NOT immediately issue a follow-up grep targeting the same
  path with a slight pattern variation. Instead, list-dir the parent
  directory first to confirm the path and any adjacent files actually
  exist.
- USE LIMIT + OFFSET ON read_file: never read a whole large file
  when you already know the line range. ``read_file`` accepts
  ``offset:`` and ``limit:`` arguments — pass them whenever grep
  has given you a line number. A 30-line window is almost always
  enough to see the function and its callers; reading a 2000-line
  file end-to-end burns the caller's token budget on context you
  won't reference.
  Examples:
    read_file("src/foo.py", offset=140, limit=40)   # function at L150
    read_file("config/repos.yaml", offset=1, limit=30)
- NEVER RE-READ A RANGE YOU ALREADY HOLD: ``read_file`` refuses any
  partial slice whose line range (including a subset of a wider prior
  read) you have already read this answer, returning a ``"refused: ...
  already loaded earlier ..."`` string and **no new content** — a
  wasted turn.  Track the ``(path, offset, limit)`` ranges you have
  already read; never re-issue a ``read_file`` for a range you already
  hold.  If you need those lines again, scroll back to the earlier
  result instead of re-calling.  A subset re-read (e.g. you read
  ``offset=29, limit=12`` then ask for ``offset=29, limit=1``) is also
  refused — the narrower range is already contained in the wider one.
- MERGE ADJACENT READ RANGES: when you need several nearby
  regions of the same file, request the maximum contiguous range
  you expect to need in a single read.  Do NOT issue several short
  sequential reads — that wastes tool turns on windows that could
  have been one call.
  Example: instead of
    read_file("..._http.py", offset=20, limit=80)   # lines 20-99
    read_file("..._http.py", offset=100, limit=40)  # lines 100-139
  merge into a single read:
    read_file("..._http.py", offset=20, limit=120)  # lines 20-139
- FILE BUDGET: read at most 5 files per answer. If you need more, stop
  and return the most relevant files found so far, with a note that
  more exist.
- NO CALL-CHAIN TRACING: do NOT trace full call chains through base
  classes, abstractions, or transitive dependencies unless the
  question explicitly asks for a complete trace (e.g. "what is the
  full call chain for X?"). When the question is about where to make a
  change, identify the most likely files directly.
- PREFER SPECIFICITY: when choosing which files to read, prefer config
  files, the most specific implementation files, and test files — over
  general abstractions, base classes, and framework-level plumbing.
"""


def _compose_explore_prompt(
    question: str,
    known_context: str | None,
    pre_seeded_paths: list[str] | None,
) -> str:
    """Compose the effective scout prompt.

    When the caller supplied ``known_context`` (compact facts already
    gathered) or ``pre_seeded_paths`` (files the calling agent already has
    loaded in full), prepend a clearly-delimited block so the scout can
    short-circuit redundant exploration. ``pre_seeded_paths`` contributes a
    paths-only ``<preloaded_files>`` block telling the scout NOT to re-read
    them; it is merged with — not overwriting — any ``known_context``. When
    neither is supplied the question is returned verbatim (no behavior
    change).
    """
    preamble = None
    if pre_seeded_paths:
        preloaded = "\n".join(pre_seeded_paths)
        preamble = (
            "The calling agent already has these files loaded in full and "
            "analyzed; treat their contents as KNOWN. Do NOT spend tokens "
            "re-reading them. If the question is answerable from them, say "
            "so concisely and point to the relevant symbols/lines instead "
            "of re-dumping content:\n"
            "<preloaded_files>\n"
            f"{preloaded}\n"
            "</preloaded_files>"
        )
    effective_known = "\n\n".join(p for p in (preamble, known_context) if p)
    if not effective_known:
        return question
    return (
        "Known context already gathered by the caller (CHECK THIS "
        "BEFORE calling any tool — if it already answers the question, "
        "reply directly without exploring):\n"
        "<known_context>\n"
        f"{effective_known}\n"
        "</known_context>\n\n"
        "Question:\n"
        f"{question}"
    )


async def _run_single_explore_attempt(
    *,
    agent: Any,
    prompt: str,
    limits: object,
    settings: Settings,
) -> str:
    """Run one explore agent attempt, handling truncation and budget exhaustion.

    Returns the explore output on success.  Raises on failure — the caller
    (``run_explore``) owns the outer retry loop.
    """
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    from .retry import acall_with_retry

    with trace_stage("explore"):
        try:
            # Capture loop variables as defaults so the lambda binds
            # them at definition time, not at call time (B023).
            _agent = agent
            _prompt = prompt
            _limits = limits
            result = await acall_with_retry(
                lambda a=_agent, p=_prompt, lim=_limits: a.run(p, usage_limits=lim),  # type: ignore[misc]
                what="explore",
            )
        except UsageLimitExceeded:
            # Budget exhausted — retry ONCE with a stricter prompt and
            # no tools.  The outer retry loop in run_explore handles
            # connection errors, not budget caps, so this fires on
            # every attempt.
            retry_agent = Agent(
                model=getattr(agent, "model", None),
                system_prompt=(
                    "You already exceeded your exploration budget on a "
                    "previous attempt. Return ONLY your single best answer "
                    "now — at most 3 file paths with one-line notes. Do "
                    "NOT call any tools. No speculation, no preamble. If "
                    "you cannot answer, say 'unable to answer'."
                ),
                output_type=str,
                tools=[],
                name="explore-retry",
                model_settings=getattr(agent, "model_settings", None),
            )
            retry_limits = UsageLimits(request_limit=2)
            try:
                retry_result = await retry_agent.run(
                    prompt,
                    usage_limits=retry_limits,
                )
            except UsageLimitExceeded:
                mark_explore_budget_exhausted()
                raise
            return str(retry_result.output).strip()

    # Detect truncation (finish_reason == 'length') and auto-continue
    # with a single follow-up call so the caller gets a complete answer.
    output = str(result.output).strip()
    finish_reason = getattr(getattr(result, "response", None), "finish_reason", None)
    if finish_reason == "length":
        all_msgs = getattr(result, "all_messages", None)
        try:
            history = all_msgs() if all_msgs is not None else None
        except TypeError:
            history = None
        if history is not None:
            continuation_result = await agent.run(
                "Continue exactly from where you were cut off. "
                "Do not repeat anything already said. "
                "Start from the last incomplete sentence.",
                message_history=history,
                usage_limits=limits,
            )
        else:
            continuation_result = await agent.run(
                "Continue exactly from where you were cut off. "
                "Do not repeat anything already said. "
                "Start from the last incomplete sentence.",
                usage_limits=limits,
            )
        output += "\n" + str(continuation_result.output).strip()

    return output


async def run_explore(
    *,
    settings: Settings,
    repo_dir: Path,
    question: str,
    known_context: str | None = None,
    pre_seeded_paths: list[str] | None = None,
    extra_roots: list[Path] | None = None,
) -> str:
    """Run the read-only exploration sub-agent against ``repo_dir`` and
    return its concise findings. Degrades to a short message instead of
    raising so the driver can react.

    Async so it composes with whichever event loop is driving the parent
    coordinator: under the Claude SDK backend the ``explore`` tool callback
    fires inside the SDK's already-running loop, so the sub-agent is awaited
    via pydantic-ai's async ``agent.run`` rather than ``run_sync`` (which
    would call ``asyncio.run`` and raise "event loop is already running")."""
    if not get_secrets().openrouter_api_key:
        return "explore unavailable: OPENROUTER_API_KEY is not set"
    if not repo_dir.exists():
        return "explore unavailable: workspace repo directory does not exist — the repository has not been cloned yet"

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import UsageLimits

    from .fs_tools import build_fs_tools

    # read-only subset of the fs tools (no write_file / edit_file / delete_file)
    all_fs = build_fs_tools(repo_dir, settings, extra_roots=extra_roots)
    ro_tools = [
        t for t in all_fs if t.__name__ in ("read_file", "list_dir", "run_command")
    ]

    from .base import _aclose_async_client, build_openrouter_model

    limits = UsageLimits(request_limit=settings.explore_request_limit)

    from pydantic_ai.exceptions import UsageLimitExceeded

    last_error: Exception | None = None

    for attempt in range(1, _EXPLORE_MAX_ATTEMPTS + 1):
        # Simplify question on retries so the sub-agent has a better
        # chance of completing before another transient failure.
        if attempt == 1:
            current_question = question
            current_known_context = known_context
        elif attempt == 2:
            current_question = question[:500] + "…" if len(question) > 500 else question
            current_known_context = None
        else:
            current_question = question[:200] + "…" if len(question) > 200 else question
            current_known_context = None

        current_prompt = _compose_explore_prompt(
            current_question, current_known_context, pre_seeded_paths
        )

        model, main_client = build_openrouter_model(1)
        agent = Agent(
            model=model,
            system_prompt=_SYSTEM_PROMPT,
            output_type=str,
            tools=ro_tools,
            name="explore",
            model_settings=ModelSettings(max_tokens=settings.explore_max_tokens),
        )

        try:
            result = await _run_single_explore_attempt(
                agent=agent,
                prompt=current_prompt,
                limits=limits,
                settings=settings,
            )
            return result
        except UsageLimitExceeded:
            # Budget cap exhausted even after no-tools retry —
            # don't loop, return the failure immediately.
            return f"explore failed: budget exhausted after {attempt} attempt(s)"
        except Exception as e:  # noqa: BLE001 — degrade, don't break the driver
            last_error = e
            if attempt < _EXPLORE_MAX_ATTEMPTS:
                delay = min(_EXPLORE_BACKOFF_CAP, 2.0**attempt)
                delay += random.uniform(0, delay / 2)  # noqa: S311 — jitter, not crypto
                log.warning(
                    "explore attempt %d/%d failed (%s: %s) — "
                    "retrying in %.1fs with simplified question",
                    attempt,
                    _EXPLORE_MAX_ATTEMPTS,
                    type(e).__name__,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
                # continue to next attempt
            # else: final attempt failed — fall through to return below
        finally:
            await _aclose_async_client(main_client)

    return f"explore failed after {_EXPLORE_MAX_ATTEMPTS} attempts: {last_error}"


def make_explore_tool(
    settings: Settings,
    repo_dir: Path,
    extra_roots: list[Path] | None = None,
    pre_seeded_paths: list[str] | None = None,
):
    """Return the ``explore(question)`` closure.

    Factory that creates a per-coordinator explore function wired to
    the given settings, repo directory, and extra roots, and registers
    itself in ``ToolRegistry`` so agents can discover it.

    ``pre_seeded_paths`` lists reference-file paths the calling agent has
    already loaded into its own context; the factory forwards them to
    :func:`run_explore` so the scout is told NOT to re-read them. It is
    injected by the factory, not a parameter the LLM supplies.
    """

    async def explore(question: str, known_context: str | None = None) -> str:
        """Ask a fresh, context-isolated sub-agent a complex, multi-step
        question about the repository — questions that would require
        navigating several files to answer. For simple, single-step
        lookups (one file path, one symbol name), use read_file or
        list_dir directly instead. Returns concise paths/symbols/
        line-ranges, never whole files. Batch related questions into a
        single call where possible.

        Optionally pass ``known_context``: COMPACT facts you have ALREADY
        gathered (file paths you have read, symbol names, line ranges you
        already know) so the scout can short-circuit redundant
        exploration instead of re-discovering them. Keep it terse — paths
        and symbols, not whole file dumps. Leave it unset when you have
        nothing relevant to share."""
        # Only forward known_context / pre_seeded_paths when populated, so
        # the default call shape (and existing seam fakes) stays unchanged.
        extra: dict[str, object] = (
            {} if known_context is None else {"known_context": known_context}
        )
        if pre_seeded_paths is not None:
            extra["pre_seeded_paths"] = pre_seeded_paths
        return await run_explore(
            settings=settings,
            repo_dir=repo_dir,
            question=question,
            extra_roots=extra_roots,
            **extra,
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="explore",
            description="Ask a fresh, context-isolated sub-agent a complex, multi-step question about the repository.",
            category="exploration",
            parameters={"question": "str", "known_context": "str | None"},
        )
    )

    return explore


def make_repo_scoped_explore_tool(settings: Settings, repo_clones: dict[str, Path]):
    """Return an ``explore(repo, question)`` closure for multi-repo agents.

    The plain :func:`make_explore_tool` binds a single ``repo_dir`` (in a
    multi-repo workspace, the *first* clone), so every relative path the
    scout uses resolves against that one repo — it then reports the other
    repos as "empty". This variant takes the target repo as an explicit
    argument and scopes the scout to THAT repo's clone, so a cross-repo
    agent (e.g. meta) can reliably survey any selected repository.
    """
    repo_list = ", ".join(sorted(repo_clones))

    async def explore(
        repo: str, question: str, known_context: str | None = None
    ) -> str:
        """Ask a fresh, context-isolated sub-agent a complex, multi-step
        question about ONE selected repository clone.

        ``repo`` MUST be exactly one of the registered repo ids:
        {repo_list}. The scout is scoped to that repo only, so ask about
        one repo per call (issue several calls to compare repos). Returns
        concise paths/symbols/line-ranges, never whole files.

        Optionally pass ``known_context``: COMPACT facts you ALREADY have
        (paths, symbols, line ranges) so the scout skips redundant work.
        """
        clone = repo_clones.get(repo)
        if clone is None:
            return f"explore: unknown repo {repo!r}. Choose exactly one of: {repo_list}"
        extra = {} if known_context is None else {"known_context": known_context}
        return await run_explore(
            settings=settings,
            repo_dir=clone,
            question=question,
            extra_roots=[clone],
            **extra,
        )

    # pydantic-ai derives the tool schema from __doc__; inject the repo list.
    if explore.__doc__:
        explore.__doc__ = explore.__doc__.replace("{repo_list}", repo_list)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="explore",
            description=(
                "Ask a context-isolated sub-agent a complex question about "
                "ONE selected repository clone (pass repo=<id>)."
            ),
            category="exploration",
            parameters={
                "repo": "str",
                "question": "str",
                "known_context": "str | None",
            },
        )
    )

    return explore


def make_parallel_explore_tool(
    settings: Settings, repo_dir: Path, extra_roots: list[Path] | None = None
):
    """Return a ``parallel_explore(questions)`` closure that batches
    questions into a single scout call.

    Instead of spawning one independent :func:`run_explore` scout per
    question (each re-sending the ~68k-char system prompt), all questions
    are batched into a single explore call so the system prompt is sent
    only once.  The scout answers every question in one response, labeled
    by number.  This cuts the input-token cost by a factor of N for N
    questions.
    """

    async def parallel_explore(questions: list[str]) -> str:
        """Batch SEVERAL independent questions into a single scout call.

        Use this (instead of many serial ``explore`` calls) for long,
        splittable work: e.g. partition a big task into independent slices
        ("enumerate warnings in tests/core", "...in tests/agents", …) and
        pass them all at once.  All questions are sent in one prompt so the
        system context is loaded only once.  Returns every answer labeled by
        its question.  Keep each question self-contained; the scout answers
        them sequentially within the same call."""
        if not questions:
            return "parallel_explore: no questions provided"

        if len(questions) == 1:
            # Single question — no batching benefit; delegate directly.
            with trace_stage("parallel_explore"):
                ans = await run_explore(
                    settings=settings,
                    repo_dir=repo_dir,
                    question=questions[0],
                    extra_roots=extra_roots,
                )
            return f"### [1] {questions[0]}\n{ans}"

        # Compose a batched prompt so the system prompt is sent only once.
        numbered = "\n\n".join(
            f"### Question {i + 1}: {q}" for i, q in enumerate(questions)
        )
        batch_prompt = (
            "Answer ALL of the following independent questions. "
            "For each question, start your answer with exactly "
            '"### [N]" on its own line where N is the question number, '
            "then provide your tight, concise answer following your "
            "normal discipline. Do NOT combine answers — keep each "
            "question's response separate.\n\n"
            f"{numbered}"
        )

        with trace_stage("parallel_explore"):
            try:
                result = await run_explore(
                    settings=settings,
                    repo_dir=repo_dir,
                    question=batch_prompt,
                    extra_roots=extra_roots,
                )
            except Exception as e:  # noqa: BLE001 — degrade, don't break
                result = f"(explore failed: {e})"

        # Prepend question labels so the caller always sees which
        # question each answer block belongs to — even when the scout
        # omits or mis-formats the "### [N]" markers.
        prefix = "\n\n".join(f"### [{i + 1}] {q}" for i, q in enumerate(questions))
        return f"{prefix}\n\n{result}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="parallel_explore",
            description=(
                "Run several independent explore investigations batched "
                "into a single call — the system context is loaded only once."
            ),
            category="exploration",
            parameters={"questions": "list[str]"},
        )
    )

    return parallel_explore
