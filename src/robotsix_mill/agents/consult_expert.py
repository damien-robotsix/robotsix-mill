"""Domain expert consultation sub-agent.

The implement coordinator can call ``consult_expert(domain, question)``
to ask a domain expert a focused question. The expert is a read-only
sub-agent with its own memory ledger — it answers the question and
returns a concise string; it does NOT drive the ticket or touch the
filesystem. The coordinator remains the sole author of every change.

``run_consult_expert`` is the single mockable seam: tests monkeypatch it
(no real LLM), exactly like the other agent seams.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings, get_secrets

log = logging.getLogger(__name__)


def run_consult_expert(
    *,
    settings: Settings,
    repo_dir: Path,
    domain: str,
    question: str,
) -> str:
    """Run a read-only domain expert sub-agent for *domain* with
    *question* and return its answer string.

    Bounded by ``consult_request_limit``. Never raises out — failure
    degrades to a short message so the coordinator can carry on.
    Expert memory is loaded but NOT persisted (read-only consultation
    for v1).
    """
    if not get_secrets().openrouter_api_key:
        return "consult unavailable: OPENROUTER_API_KEY is not set"
    if not repo_dir.exists():
        return "consult unavailable: workspace repo directory does not exist"

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .base import _close_async_client, timeout_http_client
    from .fs_tools import build_fs_tools
    from .openrouter_cost import CostInstrumentedOpenRouterModel

    # 1. Load expert definition.
    from .expert_manager import ExpertManager

    mgr = ExpertManager(settings, repo_dir)
    try:
        definitions = mgr.load_definitions()
    except Exception as e:
        return f"consult {domain} failed: {e}"

    definition = definitions.get(domain)
    if definition is None:
        return (
            f"consult {domain} failed: no expert definition found "
            f"for '{domain}' (available: {sorted(definitions.keys())})"
        )

    # 2. Load expert memory ledger.
    from ..pass_runner import load_memory

    memory_path = Path(settings.data_dir) / f"expert_{domain}_memory.md"
    try:
        expert_memory = load_memory(
            memory_path,
            max_chars=definition.memory.max_memory_chars,
        )
    except Exception:
        log.warning(
            "could not load memory for expert %s at %s",
            domain, memory_path, exc_info=True,
        )
        expert_memory = ""

    # 3. Build a fresh agent (bypass ExpertManager cache for stale-memory safety).
    system_prompt = definition.system_prompt
    if expert_memory:
        system_prompt = (
            f"{system_prompt}\n\n<memory>\n{expert_memory}\n</memory>"
        )

    # Read-only tools: explore, read_file, list_dir — no mutation.
    all_fs = build_fs_tools(repo_dir, settings)
    ro_tools = [t for t in all_fs if t.__name__ in ("explore", "read_file", "list_dir")]

    # "explore" in the fs_tools list is not the explore sub-agent — we
    # also need the actual `make_explore_tool` if the definition requests it.
    if "explore" in definition.tools:
        from .explore import make_explore_tool

        ro_tools.append(make_explore_tool(settings, repo_dir))

    model_name = definition.model or settings.model
    client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        model_name,
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=client,
        ),
    )
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        output_type=str,
        tools=ro_tools,
        name=f"consult:{domain}",
    )
    limits = UsageLimits(request_limit=settings.consult_request_limit)
    try:
        from .retry import call_with_retry

        result = call_with_retry(
            lambda: agent.run_sync(question, usage_limits=limits),
            settings=settings, what=f"consult:{domain}",
        )
    except Exception as e:  # noqa: BLE001 — degrade, never break the coordinator
        return f"consult {domain} failed: {e}"
    finally:
        _close_async_client(client)
    return str(result.output)


def make_consult_expert_tool(settings: Settings, repo_dir: Path):
    """Build the ``consult_expert`` tool exposed to the coordinator. It
    only ever returns the expert sub-agent's answer string."""

    def consult_expert(domain: str, question: str) -> str:
        """Consult a domain expert sub-agent about a specific codebase
        question. Use when the ticket touches a domain you're less
        familiar with (e.g. Python backend internals). The expert has
        deep knowledge of that domain's conventions, file layout, and
        gotchas — ask focused questions and use the answer to guide
        your edits. Available domains match expert_definitions/*.yaml
        (e.g. 'python-backend')."""
        return run_consult_expert(
            settings=settings, repo_dir=repo_dir,
            domain=domain, question=question,
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(ToolInfo(
        name="consult_expert",
        description=(
            "Consult a domain expert sub-agent about a specific codebase "
            "question. Use when the ticket touches a domain you're less "
            "familiar with (e.g. Python backend internals). The expert has "
            "deep knowledge of that domain's conventions, file layout, and "
            "gotchas — ask focused questions and use the answer to guide "
            "your edits. Available domains match expert_definitions/*.yaml "
            "(e.g. 'python-backend')."
        ),
        category="exploration",
        parameters={
            "domain": "str (the expert domain to consult, e.g. 'python-backend')",
            "question": "str (a focused, specific question for the expert)",
        },
    ))

    return consult_expert
