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

import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .._resources import agent_definitions_dir
from ..core.duration import parse_duration

_MAX_INCLUDE_DEPTH = 10

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
    # via llmio's tier defaults (see llmio tier config for current mapping).
    # Replaces the old provider-specific ``model`` field.
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
    workflows: bool = False
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
    def _interval_xor(self) -> AgentDefinition:
        if self.interval is not None and self.interval_seconds is not None:
            raise ValueError(
                "set at most one of 'interval' (human-readable, e.g. '1d') "
                "or 'interval_seconds' (legacy integer seconds), not both"
            )
        if self.interval is not None:
            self.interval_seconds = parse_duration(self.interval)
        return self


def _resolve_includes(
    raw_text: str,
    base_dir: Path,
    _depth: int = 0,
    _containment_root: Path | None = None,
) -> str:
    """Replace ``!include <path>`` directives with file content.

    Each ``!include`` line's leading whitespace is preserved and applied
    to every line of the included file.  Paths are resolved relative to
    *base_dir*.  Leading ``#``-comment lines in included files are
    stripped so that shared-partial YAML files can carry discoverability
    comments without polluting the prompt.

    Nested includes are resolved recursively up to ``_MAX_INCLUDE_DEPTH``.

    The *_containment_root* parameter (defaults to
    ``agent_definitions_dir()``) is the outermost directory that
    ``!include`` is allowed to reach — paths that resolve outside it
    are rejected.  Tests may override this to a temp directory.
    """
    if _depth >= _MAX_INCLUDE_DEPTH:
        raise RecursionError(f"!include nesting depth exceeded ({_MAX_INCLUDE_DEPTH})")

    if _containment_root is None:
        _containment_root = agent_definitions_dir().resolve()

    _INCLUDE_RE = re.compile(r"^(\s*)!include\s+(.+)$")

    lines = raw_text.split("\n")
    result: list[str] = []

    for line in lines:
        m = _INCLUDE_RE.match(line)
        if m is None:
            result.append(line)
            continue

        indent = m.group(1)
        include_path = m.group(2).strip()
        resolved = (base_dir / include_path).resolve()

        # Safety: only allow includes within the containment root.
        try:
            resolved.relative_to(_containment_root)
        except ValueError as err:
            raise ValueError(
                f"!include path {include_path!r} escapes containment root "
                f"{_containment_root}"
            ) from err

        if not resolved.is_file():
            raise FileNotFoundError(f"!include target not found: {resolved}")

        included_raw = resolved.read_text(encoding="utf-8")

        # Strip leading YAML comment lines (discoverability metadata
        # that should not land in the composed prompt).
        included_lines = included_raw.split("\n")
        while included_lines and included_lines[0].lstrip().startswith("#"):
            included_lines.pop(0)
        # Also strip a single blank line that immediately follows
        # the comment block (separator between header and content).
        if included_lines and included_lines[0].strip() == "":
            included_lines.pop(0)

        included_text = "\n".join(included_lines)

        # Recurse in case the included file itself has !include lines.
        included_text = _resolve_includes(
            included_text, base_dir, _depth + 1, _containment_root
        )

        # Split, preserving empty lines within the content but dropping
        # a single trailing empty string that results from a final \n.
        ilines = included_text.split("\n")
        if ilines and ilines[-1] == "":
            ilines.pop()

        for iline in ilines:
            result.append(indent + iline)

    return "\n".join(result)


def resolve_agent_level(settings: Settings | None, definition: AgentDefinition) -> int:
    """Return the capability level a stage should run at.

    ``settings.agent_levels[definition.name]`` wins when set (the operator's
    per-stage L1..L4 choice); otherwise the level declared in the YAML
    definition is the default.  Call sites that pick a cheaper level for a
    specific ticket (config-only implement/review, the refine trivial route)
    apply their choice on top of this resolution.
    """
    overrides = getattr(settings, "agent_levels", None) or {}
    # ``getattr`` rather than attribute access: some callers/tests hand in a
    # minimal stub carrying only ``level``.
    level = overrides.get(getattr(definition, "name", None))
    if level is None:
        return int(definition.level)
    return int(level)


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
    # Resolve !include directives before YAML parsing — they are
    # text-level includes that must be expanded so the resulting
    # YAML string is self-contained.  Paths are relative to the
    # YAML file's own directory.
    raw_text = _resolve_includes(raw_text, path.parent.resolve())
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
    settings: Settings,
    definition_name: str,
    tools: list[Any] | None = None,
    level: int | None = None,
    prompt: str,
    what: str,
    repo_dir: Path | None = None,
    run_kwargs: dict[str, Any] | None = None,
    system_prompt_format_kwargs: dict[str, Any] | None = None,
    validate: Callable[[Any], None] | None = None,
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

    Steps 2-4 run inside llmio's provider-failover loop: the capability
    level NEVER changes, but a provider-shaped failure (outage, rate limit,
    exhausted subscription credits) on the active provider slot retries the
    same level on the other slot — rebuilding the agent, since the slot
    selects the provider. Local retry of transient errors still happens
    inside ``run_agent``; only what survives that escalates. Failover is
    OFF unless ``settings.provider_failover_enabled`` is on — the fallback
    slot is paid OpenRouter, and with it off the worker parks the ticket
    until the quota resets.

    Args:
        settings: Application configuration.
        definition_name: YAML file name under ``agent_definitions/``,
            e.g. ``"scope_triage"`` or ``"periodic/module_curator"``.
        tools: Tool list for the agent (default ``[]``).
        level: Override capability level (default: ``settings.agent_levels``
            for this definition's name, else ``definition.level``).
        prompt: The user prompt passed to ``h.run_sync(prompt, **run_kwargs)``.
        what: Human-readable label for retry log messages.
        repo_dir: Optional repo clone directory (passed through to
            ``build_agent_from_definition``).
        run_kwargs: Extra keyword arguments forwarded to
            ``h.run_sync(prompt, **run_kwargs)`` (e.g. ``usage_limits``,
            ``message_history``).
        validate: Optional check run on the agent's result inside the
            failover loop. It should raise when the result is unusable,
            so a hollow success surfaces as a real error instead of being
            returned (a task-shaped failure is never retried on the other
            provider — re-running a doomed task would just spend twice).
        system_prompt_format_kwargs: When set, ``definition.system_prompt``
            is formatted with these kwargs (via ``str.format(**kwargs)``)
            and passed as ``system_prompt`` to ``build_agent_from_definition``.
            Ignored when ``system_prompt`` is already in ``**build_overrides``.
        **build_overrides: Extra keyword arguments forwarded to
            ``build_agent_from_definition``
            (e.g. ``system_prompt``, ``board_id``).
    """
    from robotsix_llmio.config.tier import TierLevelConfig
    from robotsix_llmio.core.factory import default_tier_config
    from robotsix_llmio.core.failover import call_with_failover

    from .base import _safe_close, build_agent_from_definition
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
    start_level = (
        level if level is not None else resolve_agent_level(settings, definition)
    )

    # A provider can be unavailable for reasons that have nothing to do with
    # this agent — an outage, or a Claude subscription whose usage credits
    # are exhausted until they reset. llmio's failover loop rebuilds the
    # agent on the OTHER provider slot at the SAME level and retries, so the
    # run lands somewhere instead of dying. The agent is rebuilt inside the
    # factory, not reused: the slot selects the provider, so a new slot
    # needs a new agent and a new HTTP client — ``tier_binding`` forces the
    # attempted slot's binding (active-slot resolution would rebuild the
    # provider that just failed).
    def _slot_factory(tlc: TierLevelConfig) -> Callable[[], Any]:
        def _build_and_run() -> Any:
            # Only force the binding when the attempted slot differs from
            # what active-slot resolution would give (the loop's cross-slot
            # attempt before the sticky window arms); the normal attempt
            # keeps the plain level-resolution path.
            active = default_tier_config().for_level(start_level)
            binding = None if tlc.model == active.model else tlc
            agent = build_agent_from_definition(
                settings,
                definition,
                tools=tools or [],
                level=start_level,
                tier_binding=binding,
                repo_dir=repo_dir,
                **build_overrides,
            )
            try:
                result = run_agent(
                    agent,
                    lambda h: h.run_sync(prompt, **(run_kwargs or {})),
                    what=what,
                )
                if validate is not None:
                    # Raising here is deliberate: a result that parsed but is
                    # unusable is surfaced as a real error instead of being
                    # returned as a hollow success.
                    validate(result)
                return result
            finally:
                _safe_close(agent)

        return _build_and_run

    # The fallback slot is keyed OpenRouter — real money per token for a
    # subscription quota that comes back by itself. Failover is therefore
    # opt-in (``provider_failover_enabled``); with it off the failure
    # propagates and the worker parks the ticket until the stated reset
    # (see ``runtime.transient_errors``).
    return call_with_failover(
        _slot_factory,
        tier_config=default_tier_config(),
        level=start_level,
        failover_enabled=bool(settings.provider_failover_enabled),
        what=what,
    )
