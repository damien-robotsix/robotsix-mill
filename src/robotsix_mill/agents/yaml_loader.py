"""YAML loader for agent definitions.

Parses ``agent_definitions/<name>.yaml``, validates the result against the
``AgentDefinition`` Pydantic model, and returns a structured object. Each
definition declares a capability ``level`` (1/2/3) that ``build_agent``
resolves to a ``(transport, model)`` via llmio's tier defaults.

This module is independent of the agent runtime (``build_agent``,
``Settings``, ``pydantic_ai``) — it only depends on ``pydantic``
(already in the tree via ``pydantic-settings``), ``PyYAML``, stdlib,
and the stdlib-only ``core.duration`` helper for the human-readable
``interval`` form.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.duration import parse_duration
from .._resources import agent_definitions_dir

if TYPE_CHECKING:
    from ..config import Settings


class AgentDefinition(BaseModel):
    """A validated agent definition loaded from a YAML file.

    All fields map 1:1 to the keys demonstrated in
    ``agent_definitions/refine.yaml``.  No fields beyond that set are
    introduced.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    category: str | None = None
    # Capability level (1/2/3) → resolved to (transport, model) by build_agent
    # via llmio's tier defaults: L1 DeepSeek flash, L2 DeepSeek pro, L3 Claude
    # opus. Replaces the old provider-specific ``model`` field.
    level: int = Field(ge=1, le=3)
    system_prompt: str
    tools: list[str] = []
    # Single web/library knowledge gateway. When True the agent gets
    # ``ask_web_knowledge`` (a multi-turn flash agent that owns a
    # per-repo Markdown knowledge base AND a web-search tool, and
    # decides autonomously which to use). The previous ``web`` flag
    # (direct ``web_research`` injection) and ``library_knowledge``
    # flag (deterministic per-library cache) are gone — every route
    # to the internet now goes through the web_knowledge agent so
    # cost attribution stays tractable and the knowledge base
    # accumulates instead of fragmenting.
    web_knowledge: bool = False
    report_issue: bool = True
    read_ticket: bool = False
    list_epic_children: bool = False
    reply_to_thread: bool = True
    close_thread: bool = True
    list_threads: bool = True
    ask_user: bool = True
    output_type: str | None = None
    retries: int = 2
    module: str | None = None
    skills: list[str] = []
    modules: bool = False
    inject_agent_md: bool = True
    # Opt-in: inject the repo's ``## Language conventions`` block (resolved
    # via ``resolve_language_instructions``) into the system prompt when a
    # ``repo_dir`` is available. The refine/implement stages inject these
    # themselves; this flag wires the SAME conventions into review-type
    # agents (retrospect/review/audit) so they don't misjudge valid
    # version-specific syntax (e.g. PEP-758 ``except A, B:`` on Python 3.14).
    inject_language_conventions: bool = False
    max_tokens: int | None = None
    # Periodic-only scheduling fields. None means "fall back to the
    # corresponding Settings field" — keeps existing YAMLs and the
    # global config.example.json schedule section working unchanged.
    #
    # ``interval`` is the preferred human-readable form (``1w2d3h40m10s``);
    # ``interval_seconds`` is the legacy integer-seconds form. They are
    # mutually exclusive; when ``interval`` is set, the after-validator
    # parses it and backfills ``interval_seconds`` so every downstream
    # reader continues to see an int with no change.
    interval: str | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _interval_xor(self) -> "AgentDefinition":
        if self.interval is not None and self.interval_seconds is not None:
            raise ValueError(
                "set at most one of 'interval' (human-readable, e.g. '1d') "
                "or 'interval_seconds' (legacy integer seconds), not both"
            )
        if self.interval is not None:
            self.interval_seconds = parse_duration(self.interval)
        return self


def load_agent_definition(path: Path) -> AgentDefinition:
    """Parse and validate an agent YAML definition.

    ``path`` must point to a YAML file whose top-level keys map to
    ``AgentDefinition`` fields.

    Returns a validated ``AgentDefinition`` instance.

    Raises:
        ``FileNotFoundError`` — *path* does not exist (from
            ``Path.read_text()``).
        ``yaml.YAMLError`` — the file is not valid YAML.
        ``pydantic.ValidationError`` — a required field is missing,
            a value has the wrong type, or an unknown key is present.
    """
    import yaml

    raw_text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)

    if not isinstance(data, dict):
        raise yaml.YAMLError(
            f"Expected a top-level mapping in {path}, got {type(data).__name__}"
        )

    return AgentDefinition.model_validate(data)


def load_periodic_agent_definition(
    name: str,
    repo_dir: Path | None = None,
) -> AgentDefinition:
    """Load a periodic agent's definition with per-repo override support.

    Lookup order:
      1. ``<repo_dir>/.robotsix-mill/agents/<name>.yaml`` — if present,
         it fully replaces the built-in definition (same schema). This
         is the per-repo override path; a repo can ship a different
         prompt, model, interval, or enabled flag without touching the
         mill image.
      2. ``agent_definitions/periodic/<name>.yaml`` — the built-in.

    Raises ``FileNotFoundError`` when neither file exists.
    """
    if repo_dir is not None:
        override = Path(repo_dir) / ".robotsix-mill" / "agents" / f"{name}.yaml"
        if override.is_file():
            return load_agent_definition(override)
    builtin = agent_definitions_dir() / "periodic" / f"{name}.yaml"
    return load_agent_definition(builtin)


def load_and_run_agent(
    *,
    settings: "Settings",
    definition_name: str,
    tools: list | None = None,
    level: int | None = None,
    prompt: str,
    what: str,
    repo_dir: Path | None = None,
    run_kwargs: dict | None = None,
    system_prompt_format_kwargs: dict | None = None,
    **build_overrides,
):
    """Load a YAML agent definition, build the agent, run it, and return output.

    This is the single shared helper for the canonical pattern repeated
    across ~11+ non-periodic agent files:

    1. ``load_agent_definition`` from ``agent_definitions/<definition_name>.yaml``
    2. ``build_agent_from_definition`` with *tools*, *model_name*, *repo_dir*,
       and any ``**build_overrides``
    3. ``run_agent`` with *prompt* and any ``**run_kwargs``
    4. ``_safe_close`` in a ``finally`` block

    Args:
        settings: Application configuration.
        definition_name: YAML file name under ``agent_definitions/``,
            e.g. ``"scope_triage"`` or ``"periodic/module_curator"``.
        tools: Tool list for the agent (default ``[]``).
        level: Override capability level (default ``definition.level``).
        prompt: The user prompt passed to ``h.run_sync(prompt, **run_kwargs)``.
        what: Human-readable label for retry log messages.
        repo_dir: Optional repo clone directory (passed through to
            ``build_agent_from_definition``).
        run_kwargs: Extra keyword arguments forwarded to
            ``h.run_sync(prompt, **run_kwargs)`` (e.g. ``usage_limits``,
            ``message_history``).
        system_prompt_format_kwargs: When set, ``definition.system_prompt``
            is formatted with these kwargs (via ``str.format(**kwargs)``)
            and passed as ``system_prompt`` to ``build_agent_from_definition``.
            Ignored when ``system_prompt`` is already in ``**build_overrides``.
        **build_overrides: Extra keyword arguments forwarded to
            ``build_agent_from_definition``
            (e.g. ``system_prompt``, ``board_id``).
    """
    from .base import build_agent_from_definition, _safe_close
    from .retry import run_agent

    definition = load_agent_definition(
        agent_definitions_dir() / f"{definition_name}.yaml"
    )
    # Allow callers to format the definition's system_prompt template with
    # runtime values (e.g. repo_dir, branch, target) without loading the
    # definition themselves.  ``system_prompt`` in build_overrides wins
    # over this auto-formatting when both are provided.
    if system_prompt_format_kwargs and "system_prompt" not in build_overrides:
        build_overrides["system_prompt"] = definition.system_prompt.format(
            **system_prompt_format_kwargs
        )
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools or [],
        level=level if level is not None else definition.level,
        repo_dir=repo_dir,
        **build_overrides,
    )
    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt, **(run_kwargs or {})),
            what=what,
        )
    finally:
        _safe_close(agent)
    return result
