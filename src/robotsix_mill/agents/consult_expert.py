"""Domain expert consultation sub-agent.

The implement coordinator can call ``consult_expert(domain, question)``
to ask a domain expert a focused question. The expert is a read-only
sub-agent with its own memory ledger — it answers the question and
returns a concise string; it does NOT drive the ticket or touch the
filesystem. The coordinator remains the sole author of every change.

The expert keeps its own per-domain, per-board memory ledger at
``<data_dir>/<board>/expert_<domain>_memory.md``. The ledger is
loaded into the expert's system prompt before each consultation and
the expert may return an ``updated_memory`` field which the runner
persists verbatim, so observations carry across consultations.

``run_consult_expert`` is the single mockable seam: tests monkeypatch it
(no real LLM), exactly like the other agent seams.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field, ConfigDict

from ..config import Settings, get_secrets

log = logging.getLogger(__name__)


class ExpertConsultResult(BaseModel):
    """Structured output from a domain-expert consultation.

    ``answer`` is the concise response handed back to the coordinator.
    ``updated_memory`` is the expert's revised memory ledger; the
    runner persists it verbatim when non-empty so future consultations
    of the same domain see the expert's accumulated knowledge."""

    model_config = ConfigDict(strict=True, extra="forbid")

    answer: str = Field(
        description="Concise, focused answer to the coordinator's "
        "question. The coordinator uses this to decide what code "
        "changes to make.",
    )
    updated_memory: str = Field(
        default="",
        description="Revised memory ledger after this consultation. "
        "Append observations, conventions, or gotchas you discovered "
        "while answering. Return the FULL revised ledger (not a diff). "
        "Leave empty to keep the existing memory unchanged.",
    )


async def run_consult_expert(
    *,
    settings: Settings,
    repo_dir: Path,
    domain: str,
    question: str,
    board_id: str = "",
) -> str:
    """Run a read-only domain expert sub-agent for *domain* with
    *question* and return its answer string.

    Bounded by ``consult_request_limit``. Never raises out — failure
    degrades to a short message so the coordinator can carry on.
    Expert memory is loaded before the run and the expert's
    ``updated_memory`` (when non-empty) is persisted verbatim
    afterwards, so insights accumulate across consultations.
    """
    if not get_secrets().openrouter_api_key:
        return "consult unavailable: OPENROUTER_API_KEY is not set"
    if not repo_dir.exists():
        return "consult unavailable: workspace repo directory does not exist"

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .base import _aclose_async_client, build_openrouter_model
    from .fs_tools import build_fs_tools

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
    from ..runners.pass_runner import load_memory

    memory_path = settings.memory_file_for(f"expert_{domain}", board_id)
    try:
        expert_memory = load_memory(
            memory_path,
            max_chars=definition.memory.max_memory_chars,
        )
    except Exception:
        log.warning(
            "could not load memory for expert %s at %s",
            domain,
            memory_path,
            exc_info=True,
        )
        expert_memory = ""

    # 3. Build a fresh agent (bypass ExpertManager cache for stale-memory safety).
    system_prompt = definition.system_prompt
    if expert_memory:
        system_prompt = f"{system_prompt}\n\n<memory>\n{expert_memory}\n</memory>"
    system_prompt += (
        "\n\n## Memory\n"
        "Return a structured ``ExpertConsultResult`` with two fields:\n"
        "- ``answer``: your concise answer to the coordinator's question.\n"
        "- ``updated_memory``: the FULL revised memory ledger after this\n"
        "  consultation. Add observations, conventions, or gotchas you\n"
        "  discovered while answering. Return the existing memory\n"
        "  unchanged when nothing new was learned. Leave EMPTY only when\n"
        "  the current memory is already complete for the question.\n"
        "  Keep entries concise — one short paragraph per insight,\n"
        "  ticket-scoped where useful."
    )

    # Read-only tools: explore, read_file, list_dir — no mutation.
    all_fs = build_fs_tools(repo_dir, settings)
    ro_tools = [t for t in all_fs if t.__name__ in ("explore", "read_file", "list_dir")]

    # "explore" in the fs_tools list is not the explore sub-agent — we
    # also need the actual `make_explore_tool` if the definition requests it.
    if "explore" in definition.tools:
        from .explore import make_explore_tool

        ro_tools.append(make_explore_tool(settings, repo_dir))

    model, client = build_openrouter_model(definition.level)
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        output_type=PromptedOutput(ExpertConsultResult),
        tools=ro_tools,
        name=f"consult:{domain}",
    )
    limits = UsageLimits(request_limit=settings.consult_request_limit)
    try:
        from .retry import acall_with_retry

        result = await acall_with_retry(
            lambda: agent.run(question, usage_limits=limits),
            what=f"consult:{domain}",
        )
    except Exception as e:  # noqa: BLE001 — degrade, never break the coordinator
        return f"consult {domain} failed: {e}"
    finally:
        await _aclose_async_client(client)

    output = result.output
    if not isinstance(output, ExpertConsultResult):
        # Belt-and-braces: a misconfigured model could return a raw
        # string. Use what's there and skip the memory write.
        return str(output)

    if output.updated_memory and output.updated_memory.strip():
        try:
            from ..runners.pass_runner import persist_memory

            persist_memory(
                memory_path,
                output.updated_memory,
                max_chars=definition.memory.max_memory_chars,
            )
        except Exception:  # noqa: BLE001 — memory persistence is best-effort
            log.warning(
                "could not persist memory for expert %s at %s",
                domain,
                memory_path,
                exc_info=True,
            )

    return output.answer


def make_consult_expert_tool(settings: Settings, repo_dir: Path, board_id: str = ""):
    """Build the ``consult_expert`` tool exposed to the coordinator. It
    only ever returns the expert sub-agent's answer string."""

    async def consult_expert(domain: str, question: str) -> str:
        """Consult a domain expert sub-agent about a specific codebase
        question. Use when the ticket touches a domain you're less
        familiar with (e.g. Python backend internals). The expert has
        deep knowledge of that domain's conventions, file layout, and
        gotchas — ask focused questions and use the answer to guide
        your edits. Available domains match expert_definitions/*.yaml
        (e.g. 'python-backend')."""
        return await run_consult_expert(
            settings=settings,
            repo_dir=repo_dir,
            domain=domain,
            question=question,
            board_id=board_id,
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
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
        )
    )

    return consult_expert
