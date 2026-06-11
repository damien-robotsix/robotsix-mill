"""pydantic-ai agent factory over OpenRouter.

``pydantic_ai`` is imported lazily inside :func:`build_agent` so the
core imports without the heavy LLM stack and runs offline. The main
agent's model is ALWAYS the plain (non-``:online``) model — web search
would otherwise bill a per-request surcharge on this expensive model.
``web=True`` instead exposes a single ``web_research`` tool that
delegates to a cheap, bounded sub-agent (see :mod:`.web_research`) and
returns only its conclusion. Skills are always injected into the
prompt. ``_model_name`` and skill assembly are factored out so they're
unit-testable without a key or pydantic_ai.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from ..config import Settings, get_secrets
from .prompt_tool_consistency import unregistered_call_directives
from .report_issue import make_report_issue_tool
from .tool_registry import ToolRegistry

# Defensive char cap on the inlined ``## Module Map`` block so the static
# prompt can't grow unbounded as ``docs/modules.yaml`` grows. A hardcoded
# constant (not a Settings field) per the repo's pattern for internal
# tuning constants; the default sits well above today's rendered size.
MODULE_MAP_MAX_CHARS = 12000


def _close_async_client(client: "httpx.AsyncClient") -> None:
    """Close an httpx.AsyncClient from outside its original event loop.

    Creates a temporary event loop to run aclose(), catching any errors
    so cleanup never raises in a finally/del context."""
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(client.aclose())
        loop.close()
    except Exception:
        pass


async def _aclose_async_client(client: "httpx.AsyncClient") -> None:
    """Close an httpx.AsyncClient from inside the loop it lives on.

    Sub-agent tools run on the parent coordinator's event loop (under the
    Claude SDK backend that loop is already running), so ``aclose`` must be
    awaited directly here — :func:`_close_async_client`'s spin-up of a fresh
    loop via ``run_until_complete`` is illegal while another loop runs on the
    thread (it would be swallowed, leaking the client's connections). Errors
    are swallowed so cleanup never breaks the caller."""
    try:
        await client.aclose()
    except Exception:
        pass


def _safe_close(agent: Any) -> None:
    """Close an agent's HTTP client if it has a close method.

    Safe to call on any object — silently no-ops if the object lacks
    a ``close`` method or if closing raises."""
    close_fn = getattr(agent, "close", None)
    if close_fn is not None:
        try:
            close_fn()
        except Exception:
            pass


def timeout_http_client(settings: Settings):
    """A fresh httpx.AsyncClient with a hard per-request timeout, so a
    hung/glacial provider connection raises instead of blocking the
    worker forever. Pass to OpenRouterProvider(http_client=...)."""
    import httpx

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.model_request_timeout, connect=15.0)
    )
    weakref.finalize(client, _close_async_client, client)
    return client


def build_openrouter_model(settings: Settings, model_name: str):
    """Construct a cost-instrumented OpenRouter model + its http client.

    Wraps the ``timeout_http_client → CostInstrumentedOpenRouterModel(
    OpenRouterProvider(api_key=..., http_client=...))`` chain shared by
    the agents that build a model directly (web_research, web_knowledge,
    trace_inspector, consult_expert). Caller owns closing the client
    (pair with :func:`_aclose_async_client`)."""
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from .openrouter_cost import CostInstrumentedOpenRouterModel

    client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        model_name,
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=client,
        ),
    )
    return model, client


class AgentHandle:
    """Wraps a pydantic-ai Agent with its httpx client so callers can
    deterministically close the client after use.

    Delegates attribute access to the underlying agent so existing
    code (including test mocks) works unchanged."""

    def __init__(self, agent: Any, http_client: Any) -> None:
        self._agent = agent
        self._http_client = http_client

    def close(self) -> None:
        """Close the HTTP client. Idempotent; safe to call multiple times."""
        if self._http_client is not None:
            _close_async_client(self._http_client)
            self._http_client = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


def build_agent_from_definition(
    settings: Settings,
    definition: "AgentDefinition",
    *,
    tools: list | None = None,
    repo_dir: "Path | None" = None,
    current_ticket_id: str = "",
    **overrides,
) -> AgentHandle:
    """Build an agent from an :class:`AgentDefinition`, bridging the YAML
    loader and the agent runtime.

    Any keyword in ``**overrides`` that matches a :func:`build_agent`
    parameter name (``system_prompt``, ``model_name``, ``output_type``,
    ``web``, ``report_issue``, ``retries``, ``name``) replaces the value
    extracted from *definition*.

    If *repo_dir* is provided and *definition.inject_agent_md* is
    ``True``, the contents of ``AGENT.md`` (if it exists at the repo
    root) are injected into the system prompt as a
    ``<repo_conventions>`` block, placed after the role preamble but
    before the procedure steps.
    """
    import importlib

    from pydantic_ai import PromptedOutput

    # Resolve output_type
    if definition.output_type and definition.output_type.strip():
        if not definition.module or not definition.module.strip():
            raise ValueError(
                f"Agent definition '{definition.name}' specifies "
                f"output_type='{definition.output_type}' but module is None"
            )
        # A dotted ``module`` value is a full path under the package
        # root (e.g. ``meta.agent`` → ``robotsix_mill.meta.agent``); a
        # dotless value stays under ``agents`` for backward compat.
        if "." in definition.module:
            module = importlib.import_module(f"robotsix_mill.{definition.module}")
        else:
            module = importlib.import_module(
                f"robotsix_mill.agents.{definition.module}"
            )
        output_cls = getattr(module, definition.output_type)
        resolved_output_type: Any = PromptedOutput(output_cls)
    else:
        resolved_output_type = str

    kwargs: dict[str, Any] = dict(
        name=definition.name,
        system_prompt=definition.system_prompt,
        model_name=definition.model,
        web_knowledge=definition.web_knowledge,
        report_issue=definition.report_issue,
        read_ticket=definition.read_ticket,
        list_epic_children=definition.list_epic_children,
        current_ticket_id=current_ticket_id,
        reply_to_thread=definition.reply_to_thread,
        close_thread=definition.close_thread,
        list_threads=definition.list_threads,
        ask_user=definition.ask_user,
        retries=definition.retries,
        max_tokens=definition.max_tokens,
        output_type=resolved_output_type,
        skills=definition.skills,
        modules=definition.modules,
    )
    kwargs.update(overrides)
    kwargs["tools"] = tools
    # Forward the workspace so build_agent can confine the Claude SDK's
    # built-in edit tools to the ticket clone (see build_agent docstring).
    kwargs.setdefault("repo_dir", repo_dir)

    # Inject AGENT.md conventions when available
    if definition.inject_agent_md and repo_dir is not None:
        agent_md_path = repo_dir / "AGENT.md"
        try:
            conventions = agent_md_path.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            conventions_block = (
                "\n\n## Repository Conventions (from AGENT.md)\n\n"
                "<repo_conventions>\n" + conventions.rstrip() + "\n</repo_conventions>"
            )
            kwargs["system_prompt"] += conventions_block

    return build_agent(settings, **kwargs)


# llmio-convention tier aliases for the YAML `model` field. Letting a
# definition say `model: cheap` / `model: default` keeps it provider-agnostic:
# the alias maps to a concrete model on the DeepSeek backend, and the Claude
# backend reads the tier from the resolved "flash"/"pro" substring (see
# build_agent). The concrete strings mirror mill's Settings defaults — kept
# here (not imported from llmio internals) to avoid the mill↔llmio skew class.
_MODEL_TIER_ALIASES: dict[str, str] = {
    "cheap": "deepseek/deepseek-v4-flash",  # llmio CHEAP / fast tier
    "default": "deepseek/deepseek-v4-pro",  # llmio DEFAULT / capable tier
    "normal": "deepseek/deepseek-v4-pro",  # alias of default
}


def _model_name(settings: Settings) -> str:
    # No "openrouter:" prefix — the provider is set explicitly so we can
    # use the cost-instrumented model subclass. The main agent NEVER
    # gets ":online": web search lives only in the cheap web_research
    # sub-agent, so the pricey model isn't surcharged on every request.
    return settings.model


def _use_claude_sdk(settings: Settings, name: str | None) -> bool:
    """Whether *name* should be built on the Claude SDK transport.

    REVERSIBLE toggle: True when this agent is explicitly opted in via
    ``settings.claude_sdk_agents`` OR the global ``settings.llm_backend``
    is ``"claude_sdk"``. Default config → always False (DeepSeek path).
    """
    if name and name in set(settings.claude_sdk_agents or []):
        return True
    return settings.llm_backend == "claude_sdk"


def claude_sdk_supports_inline_image(settings: Settings) -> bool:
    """Single source of truth: may an agent attach an inline image
    (``BinaryContent``) to a Claude SDK run?

    Default False — the installed robotsix-llmio claude_sdk bridge cannot
    consume image parts: its ``_content_to_text`` stringifies any non-``str``
    content into a useless repr that hangs the ``claude`` CLI until the 1200s
    per-call cap fires. Until that bridge gains real image-input support (and
    its pin is bumped), the refine/review screenshot paths MUST degrade to a
    text note instead of emitting inline ``BinaryContent``. Gated by
    ``settings.claude_sdk_vision_enabled`` so vision can be re-enabled with a
    one-line config flip once the bridge supports it.
    """
    return bool(settings.claude_sdk_vision_enabled)


def _render_module_map(module_list: list[dict]) -> str:
    """Render a scannable ``## Module Map`` section from the taxonomy.

    If *module_list* has more than 20 entries, only modules without
    ``dependencies`` (top-level / foundational) are rendered with a
    pointer to ``docs/modules.yaml`` for the rest.  Otherwise every
    module gets a ``### <id>`` sub-heading with its description, paths,
    and dependency hints.
    """
    lines: list[str] = ["## Module Map"]

    if len(module_list) > 20:
        top_level = [m for m in module_list if not m.get("dependencies")]
        for m in top_level:
            lines.append(f"### {m['id']}")
            lines.append(m.get("description", ""))
            for p in m.get("paths", []):
                lines.append(f"- `{p}`")
        lines.append(
            "\nSee `docs/modules.yaml` for additional sub-divisions and "
            "the complete module taxonomy."
        )
    else:
        for m in module_list:
            lines.append(f"### {m['id']}")
            lines.append(m.get("description", ""))
            for p in m.get("paths", []):
                lines.append(f"- `{p}`")
            deps = m.get("dependencies", [])
            if deps:
                lines.append(f"Depends on: {', '.join(deps)}")

    rendered = "\n".join(lines)
    if len(rendered) > MODULE_MAP_MAX_CHARS:
        # Truncate on a line boundary and append a pointer to the full
        # taxonomy. Reserve room for the pointer so the whole block stays
        # within MODULE_MAP_MAX_CHARS.
        pointer = (
            "\n…(module map truncated — see docs/modules.yaml for the full taxonomy)"
        )
        budget = MODULE_MAP_MAX_CHARS - len(pointer)
        truncated = rendered[:budget].rsplit("\n", 1)[0]
        rendered = truncated + pointer
    return rendered


def compose_prompt(
    settings: Settings,
    system_prompt: str,
    skills: list[str] | None = None,
    modules: bool = False,
) -> str:
    """Compose the final system prompt: the YAML ``system_prompt`` plus
    any ``skills`` sections.

    Tool descriptions are NOT appended here. pydantic-ai already
    forwards each tool's signature + docstring to the model as a
    structured ``tools`` array on every API call, so a prose copy in
    the system prompt is pure duplication — same surface, twice the
    tokens, on every coordinator iteration.
    """
    prompt = system_prompt

    if skills:
        import logging
        import re

        logger = logging.getLogger(__name__)
        skill_sections: list[str] = []

        for name in skills:
            skill_path = settings.skills_dir / name / "SKILL.md"
            try:
                raw = skill_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.warning("Skill file not found: %s", skill_path)
                continue

            # Strip YAML frontmatter (--- ... ---)
            body = re.sub(
                r"^---\n.*?\n---\n", "", raw, count=1, flags=re.DOTALL
            ).strip()

            if body:
                skill_sections.append(body)

        if skill_sections:
            prompt += "\n\n## Skills\n\n" + "\n\n".join(skill_sections)

    if modules:
        import logging
        from pathlib import Path

        import yaml

        logger = logging.getLogger(__name__)
        modules_path = Path("docs/modules.yaml")
        try:
            with modules_path.open(encoding="utf-8") as fh:
                taxonomy = yaml.safe_load(fh)
        except (FileNotFoundError, yaml.YAMLError) as exc:
            logger.warning("Cannot load module taxonomy: %s", exc)
        else:
            module_list: list[dict] = (
                taxonomy.get("modules", []) if isinstance(taxonomy, dict) else []
            )
            if module_list:
                block = _render_module_map(module_list)
                prompt += "\n\n" + block

    return prompt


def _build_deepseek_handle(
    settings: Settings,
    *,
    effective_model: str,
    composed_system: str,
    all_tools: list,
    output_type: Any,
    name: str | None,
    retries: int,
    max_tokens: int | None = None,
) -> AgentHandle:
    """Build the DeepSeek/OpenRouter ``AgentHandle`` for an agent.

    Factored out of :func:`build_agent` so it backs both the default path AND
    the Claude→DeepSeek fallback (which needs to build the same agent on the
    OpenRouter backend on demand). ``CostInstrumentedOpenRouterModel`` is a thin
    shim over robotsix-llmio's ``OpenRouterDeepseekModel``; construction stays
    here so the model-patch test seam keeps working."""
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.settings import ModelSettings

    from .openrouter_cost import CostInstrumentedOpenRouterModel

    http_client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        effective_model,
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=http_client,
        ),
    )
    agent_kwargs: dict[str, Any] = dict(
        model=model,
        system_prompt=composed_system,
        output_type=output_type,
        tools=all_tools,
        retries=retries,
    )
    if max_tokens is not None:
        agent_kwargs["model_settings"] = ModelSettings(max_tokens=max_tokens)
    if name is not None:
        agent_kwargs["name"] = name
    agent = Agent(**agent_kwargs)
    return AgentHandle(agent, http_client)


def build_agent(  # noqa: C901
    settings: Settings,
    *,
    system_prompt: str,
    output_type: Any = str,
    tools: list | None = None,
    web_knowledge: bool = False,
    report_issue: bool = True,
    read_ticket: bool = False,
    list_epic_children: bool = False,
    current_ticket_id: str = "",
    reply_to_thread: bool = True,
    close_thread: bool = True,
    list_threads: bool = True,
    ask_user: bool = True,
    model_name: str | None = None,
    name: str | None = None,
    retries: int = 2,
    max_tokens: int | None = None,
    skills: list[str] | None = None,
    modules: bool = False,
    board_id: str = "",
    repo_dir: "Path | None" = None,
):
    """Construct a pydantic-ai Agent on an OpenRouter model. Each agent
    role passes its own ``model_name`` (see Settings per-agent models);
    falls back to the coordinator ``model``. Raises if no key.

    Set ``report_issue=False`` for agents that already emit draft
    tickets through their structured output (audit, retrospect).

    Note: for a structured ``output_type`` on a model whose provider
    rejects forced ``tool_choice``, wrap it in ``PromptedOutput`` at
    the call site (the default ``ToolOutput`` mode 404s there)."""
    all_tools = list(tools or [])
    if report_issue:
        # Every agent can self-report a blocking/degrading issue (missing
        # tool, error, workflow gap, missing input) as a draft ticket.
        # Dedup-guarded so a looping agent can't spam identical tickets.
        all_tools.append(
            make_report_issue_tool(
                settings,
                agent_name=name,
                board_id=board_id,
            )
        )
    if read_ticket:
        # Read-only tool so periodic agents can fetch full context of a
        # past proposal when the one-line summary in <recent_proposals>
        # isn't enough. Only injected when explicitly requested.
        from .read_ticket import make_read_ticket_tool

        all_tools.append(make_read_ticket_tool(settings))
    if list_epic_children and current_ticket_id:
        # Read-only tool so an agent can enumerate its sibling epic
        # children (children of its parent epic) when it needs the
        # substantive content of an intended sibling ticket. The current
        # ticket id is bound at build time so the agent calls it with no
        # argument. Only injected when both flag and id are present.
        from .list_epic_children import make_list_epic_children_tool

        all_tools.append(make_list_epic_children_tool(settings, current_ticket_id))
    if reply_to_thread:
        # Tool so agents can reply to a comment thread on the current
        # ticket, enabling real conversation with humans.
        from .reply_thread import make_reply_to_thread_tool

        all_tools.append(make_reply_to_thread_tool(settings, agent_name=name))
    if close_thread:
        # Tool so agents can close a comment thread on the current
        # ticket after addressing review feedback.
        from .close_thread import make_close_thread_tool

        all_tools.append(make_close_thread_tool(settings, agent_name=name))
    if list_threads:
        # Tool so agents can discover valid thread IDs on the current
        # ticket before calling reply_to_thread / close_thread.
        from .list_threads import make_list_threads_tool

        all_tools.append(make_list_threads_tool(settings, agent_name=name))
    if ask_user:
        from .ask_user import make_ask_user_tool

        all_tools.append(make_ask_user_tool(settings, agent_name=name))
    if web_knowledge:
        # The SINGLE gateway to the internet. A multi-turn flash
        # agent that owns a mill-global Markdown knowledge base
        # (``<data_dir>/web_knowledge/*.md`` + ``_general.md``)
        # AND a web-search tool, and decides autonomously which to
        # use. The previous ``web`` flag (direct ``web_research``)
        # and ``library_knowledge`` flag (deterministic cache) are
        # gone — every web hit now flows through one agent name
        # so cost attribution is tractable, and the cache is shared
        # across boards because library facts don't change between
        # repos.
        from .web_knowledge import make_ask_web_knowledge_tool

        all_tools.append(make_ask_web_knowledge_tool(settings))

    composed_system = compose_prompt(
        settings,
        system_prompt,
        skills=skills,
        modules=modules,
    )
    # Deterministic build-time guard (PR #755, PR #780): the prompt must
    # not instruct the agent to *call* a tool absent from its resolved
    # set. ``known_tools`` is the real mill-tool catalog, so an unrelated
    # parenthesised backtick span in the prompt can't trip a false
    # positive — only a call directive naming an actual mill tool the
    # agent lacks is flagged.
    resolved_tool_names = {
        getattr(t, "name", None) or getattr(t, "__name__", "") for t in all_tools
    }
    unreg = unregistered_call_directives(
        composed_system,
        resolved_tools=resolved_tool_names,
        known_tools={t.name for t in ToolRegistry.list_tools()},
    )
    if unreg:
        raise ValueError(
            f"Prompt contains call directives to unavailable tools: "
            f"{', '.join(sorted(unreg))}"
        )
    effective_model = model_name or _model_name(settings)
    # llmio Tier convention: agent definitions may set `model: cheap` /
    # `model: default` (or `normal`) instead of a provider-specific model id,
    # so they stay backend-agnostic. Resolve the alias once here (before the
    # backend branch): the DeepSeek path then uses the concrete model string,
    # and the Claude path infers the tier from the resolved "flash"/"pro"
    # substring. Non-alias values (real model ids, ${VAR}-resolved) pass
    # through unchanged.
    effective_model = _MODEL_TIER_ALIASES.get(
        effective_model.strip().lower(), effective_model
    )

    # --- Backend selection (REVERSIBLE; default DeepSeek) ----------------
    # When this agent is routed to the Claude SDK (global llm_backend or a
    # per-agent claude_sdk_agents entry), build via robotsix-llmio's
    # ClaudeSDKProvider (subscription auth, tier→opus/haiku). Mill's tools
    # bridge through the SDK's @tool/MCP mechanism. Everything below the
    # branch is the unchanged DeepSeek/OpenRouter path.
    if _use_claude_sdk(settings, name):
        # Lazy: claude_agent_sdk is only installed/needed when the toggle
        # is on, so the import must not run on the default DeepSeek path.
        from robotsix_llmio.claude_sdk.provider import ClaudeSDKProvider
        from robotsix_llmio.core.provider import Tier

        from .claude_concurrency import bound_claude_handle

        tier = Tier.CHEAP if "flash" in effective_model else Tier.DEFAULT
        handle = ClaudeSDKProvider().build_agent(
            tier=tier,
            system_prompt=composed_system,
            tools=all_tools,
            output_type=output_type,
            name=name,
            retries=retries,
            # Confine the SDK's built-in Write/Edit tools to the ticket's
            # workspace clone. Without this, an agent on a mill-board ticket
            # edited the container's own /app source (which shadows the
            # workspace) instead of its checkout, producing no valid diff.
            # repo_dir is None for board-less agents (e.g. meta refine before
            # a workspace exists) → no confinement, unchanged behavior.
            workspace_root=repo_dir,
        )
        # Bound concurrent CLI-subprocess spawns process-wide so a worker
        # fanning out many runs at startup can't stall on spawn contention.
        primary = bound_claude_handle(handle, settings.claude_max_concurrency)

        # Resilience: if the Claude run terminally fails (after its local
        # retries), fall back to the equivalent DeepSeek build of THIS agent —
        # same prompt/tools/output, same tier-resolved model (effective_model).
        # Lazily built (only on an actual fallback) and only when an OpenRouter
        # key exists, so the no-key Claude-only setup is unaffected. run_agent()
        # drives the retry-then-fallback; see agents/fallback.py.
        if settings.claude_fallback_to_deepseek and get_secrets().openrouter_api_key:
            from .fallback import FallbackAgentHandle

            return FallbackAgentHandle(
                primary,
                lambda: _build_deepseek_handle(
                    settings,
                    effective_model=effective_model,
                    composed_system=composed_system,
                    all_tools=all_tools,
                    output_type=output_type,
                    name=name,
                    retries=retries,
                    max_tokens=max_tokens,
                ),
            )
        return primary

    # --- DeepSeek / OpenRouter (default) ---------------------------------
    return _build_deepseek_handle(
        settings,
        effective_model=effective_model,
        composed_system=composed_system,
        all_tools=all_tools,
        output_type=output_type,
        name=name,
        retries=retries,
        max_tokens=max_tokens,
    )
