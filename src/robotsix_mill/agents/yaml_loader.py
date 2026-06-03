"""YAML loader for agent definitions.

Parses ``agent_definitions/<name>.yaml``, resolves ``${ENV_VAR}``
references in the ``model`` field, validates the result against the
``AgentDefinition`` Pydantic model, and returns a structured object.

This module is independent of the agent runtime (``build_agent``,
``Settings``, ``pydantic_ai``) — it only depends on ``pydantic``
(already in the tree via ``pydantic-settings``), ``PyYAML``, stdlib,
and the stdlib-only ``core.duration`` helper for the human-readable
``interval`` form.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from ..core.duration import parse_duration


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
    model: str
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
    reply_to_thread: bool = True
    close_thread: bool = True
    ask_user: bool = True
    output_type: str | None = None
    retries: int = 2
    module: str | None = None
    skills: list[str] = []
    modules: bool = False
    inject_agent_md: bool = True
    # Periodic-only scheduling fields. None means "fall back to the
    # corresponding Settings field" — keeps existing YAMLs and the
    # global mill.defaults.yaml schedule section working unchanged.
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


# Only bare ``${VAR}`` — the existing YAML does not use defaults or nesting.
_ENV_VAR_RE = re.compile(r"\$\{([^{}]+)\}")


def _resolve_env_vars(raw: str) -> str:
    """Replace ``${VAR}`` placeholders in *raw* with their values from
    ``os.environ``.  Returns ``""`` for unset variables — the caller
    (``build_agent``) then falls back to ``settings.model`` when
    ``model_name`` is falsy.
    """

    def _replacer(m: re.Match[str]) -> str:
        var = m.group(1)
        return os.environ.get(var, "")

    return _ENV_VAR_RE.sub(_replacer, raw)


def load_agent_definition(path: Path) -> AgentDefinition:
    """Parse, validate, and env-resolve an agent YAML definition.

    ``path`` must point to a YAML file whose top-level keys map to
    ``AgentDefinition`` fields.

    Returns a validated ``AgentDefinition`` instance.

    Raises:
        ``FileNotFoundError`` — *path* does not exist (from
            ``Path.read_text()``).
        ``yaml.YAMLError`` — the file is not valid YAML.
        ``pydantic.ValidationError`` — a required field is missing,
            a value has the wrong type, or an unknown key is present.
        ``KeyError`` — the ``model`` field references an
            environment variable that is not set.
    """
    import yaml

    raw_text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)

    if not isinstance(data, dict):
        raise yaml.YAMLError(
            f"Expected a top-level mapping in {path}, got {type(data).__name__}"
        )

    # Env-var resolution: only the ``model`` field.
    if "model" in data and isinstance(data["model"], str):
        data["model"] = _resolve_env_vars(data["model"])

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
    builtin = (
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "periodic"
        / f"{name}.yaml"
    )
    return load_agent_definition(builtin)
