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

from ..config import Settings

_SYSTEM_PROMPT = """\
You are a code exploration assistant for ONE git repository. You have
read-only tools (read_file, list_dir). Answer the caller's question as
concisely as possible:

- Start with list_dir; never assume paths.
- If asked "what/where", reply with a tight summary (paths + the
  specific relevant lines), NOT whole files.
- If explicitly asked for a file's full content (so it can be handed to
  an authoring model), return that file VERBATIM under a clear
  `FILE: <path>` header.

No speculation, no preamble. Be the caller's eyes — return the minimum
that fully answers the question.
"""


def run_explore(*, settings: Settings, repo_dir: Path, question: str) -> str:
    """Run the read-only exploration sub-agent against ``repo_dir`` and
    return its concise findings. Degrades to a short message instead of
    raising so the driver can react."""
    if not settings.openrouter_api_key:
        return "explore unavailable: OPENROUTER_API_KEY is not set"

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .fs_tools import build_fs_tools
    from .openrouter_cost import CostInstrumentedOpenRouterModel

    # read-only subset of the fs tools (no write_file / run_command)
    all_fs = build_fs_tools(repo_dir, settings)
    ro_tools = [t for t in all_fs if t.__name__ in ("read_file", "list_dir")]

    model = CostInstrumentedOpenRouterModel(  # cheap driver model, no :online
        settings.model,
        provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
    )
    agent = Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        output_type=str,
        tools=ro_tools,
    )
    limits = UsageLimits(request_limit=settings.explore_request_limit)
    try:
        from .retry import call_with_retry

        result = call_with_retry(
            lambda: agent.run_sync(question, usage_limits=limits),
            settings=settings, what="explore",
        )
    except Exception as e:  # noqa: BLE001 — degrade, don't break the driver
        return f"explore failed: {e}"
    return str(result.output).strip()


def make_explore_tool(settings: Settings, repo_dir: Path):
    def explore(question: str) -> str:
        """Ask a fresh, context-isolated sub-agent a specific question
        about the repository (structure, where something lives, or the
        full content of named files). It reads the repo so you don't
        have to — keep YOUR context lean by delegating all reading
        here. Ask for exactly what you need next."""
        return run_explore(
            settings=settings, repo_dir=repo_dir, question=question
        )

    return explore
