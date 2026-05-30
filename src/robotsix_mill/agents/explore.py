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

from pathlib import Path

from ..config import Settings, get_secrets


# --------------------------------------------------------------------------
# Budget-exhausted sentinel — set when the explore sub-agent exceeds its
# UsageLimits.request_limit even after a bounded retry.  ``coding.py``
# checks this after the coordinator run to escalate to BLOCKED.
# --------------------------------------------------------------------------

_explore_budget_exhausted: bool = False


def mark_explore_budget_exhausted() -> None:
    global _explore_budget_exhausted
    _explore_budget_exhausted = True


def is_explore_budget_exhausted() -> bool:
    return _explore_budget_exhausted


def reset_explore_budget_exhausted() -> None:
    global _explore_budget_exhausted
    _explore_budget_exhausted = False


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

SCOPE DISCIPLINE — always follow these limits:
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


def run_explore(
    *,
    settings: Settings,
    repo_dir: Path,
    question: str,
    extra_roots: list[Path] | None = None,
) -> str:
    """Run the read-only exploration sub-agent against ``repo_dir`` and
    return its concise findings. Degrades to a short message instead of
    raising so the driver can react."""
    if not get_secrets().openrouter_api_key:
        return "explore unavailable: OPENROUTER_API_KEY is not set"
    if not repo_dir.exists():
        return "explore unavailable: workspace repo directory does not exist — the repository has not been cloned yet"

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import UsageLimits

    from .fs_tools import build_fs_tools
    from .openrouter_cost import CostInstrumentedOpenRouterModel

    # read-only subset of the fs tools (no write_file / edit_file / delete_file)
    all_fs = build_fs_tools(repo_dir, settings, extra_roots=extra_roots)
    ro_tools = [
        t for t in all_fs if t.__name__ in ("read_file", "list_dir", "run_command")
    ]

    from .base import _close_async_client, timeout_http_client

    main_client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(  # dedicated cheap explore model
        settings.explore_model,
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=main_client,
        ),
    )
    agent = Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        output_type=str,
        tools=ro_tools,
        name="explore",
        model_settings=ModelSettings(max_tokens=settings.explore_max_tokens),
    )
    limits = UsageLimits(request_limit=settings.explore_request_limit)

    fallback_client = None
    try:
        from pydantic_ai.exceptions import UsageLimitExceeded

        from .retry import call_with_retry

        # Build fallback agent if a fallback model is configured
        fallback_fn = None
        if settings.rate_limit_fallback_model:
            fallback_client = timeout_http_client(settings)
            fallback_model = CostInstrumentedOpenRouterModel(
                settings.rate_limit_fallback_model,
                provider=OpenRouterProvider(
                    api_key=get_secrets().openrouter_api_key,
                    http_client=fallback_client,
                ),
            )
            fallback_agent = Agent(
                model=fallback_model,
                system_prompt=_SYSTEM_PROMPT,
                output_type=str,
                tools=ro_tools,
                name="explore-fallback",
                model_settings=ModelSettings(max_tokens=settings.explore_max_tokens),
            )
            fallback_fn = lambda: fallback_agent.run_sync(  # noqa: E731
                question, usage_limits=limits
            )

        try:
            result = call_with_retry(
                lambda: agent.run_sync(question, usage_limits=limits),
                settings=settings,
                what="explore",
                fallback_fn=fallback_fn,
            )
        except UsageLimitExceeded:
            # Budget exhausted — retry ONCE with a stricter prompt and no tools
            retry_agent = Agent(
                model=model,
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
                model_settings=ModelSettings(max_tokens=settings.explore_max_tokens),
            )
            retry_limits = UsageLimits(request_limit=2)
            try:
                retry_result = retry_agent.run_sync(
                    question,
                    usage_limits=retry_limits,
                )
            except UsageLimitExceeded:
                mark_explore_budget_exhausted()
                raise
            return str(retry_result.output).strip()

        # Detect truncation (finish_reason == 'length') and auto-continue
        # with a single follow-up call so the caller gets a complete answer.
        output = str(result.output).strip()
        finish_reason = getattr(
            getattr(result, "response", None), "finish_reason", None
        )
        if finish_reason == "length":
            continuation_result = agent.run_sync(
                "Continue exactly from where you were cut off. "
                "Do not repeat anything already said. "
                "Start from the last incomplete sentence.",
                usage_limits=limits,
            )
            output += "\n" + str(continuation_result.output).strip()

        return output
    except Exception as e:  # noqa: BLE001 — degrade, don't break the driver
        return f"explore failed: {e}"
    finally:
        _close_async_client(main_client)
        if fallback_client is not None:
            _close_async_client(fallback_client)


def make_explore_tool(
    settings: Settings, repo_dir: Path, extra_roots: list[Path] | None = None
):
    def explore(question: str) -> str:
        """Ask a fresh, context-isolated sub-agent a complex, multi-step
        question about the repository — questions that would require
        navigating several files to answer. For simple, single-step
        lookups (one file path, one symbol name), use read_file or
        list_dir directly instead. Returns concise paths/symbols/
        line-ranges, never whole files. Batch related questions into a
        single call where possible."""
        return run_explore(
            settings=settings,
            repo_dir=repo_dir,
            question=question,
            extra_roots=extra_roots,
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="explore",
            description="Ask a fresh, context-isolated sub-agent a complex, multi-step question about the repository.",
            category="exploration",
            parameters={"question": "str"},
        )
    )

    return explore
