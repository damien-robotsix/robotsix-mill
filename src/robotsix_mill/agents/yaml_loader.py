"""YAML loader for agent definitions.

Parses ``agent_definitions/<name>.yaml``, resolves ``${ENV_VAR}``
references in the ``model`` field, validates the result against the
``AgentDefinition`` Pydantic model, and returns a structured object.

This module is independent of the agent runtime (``build_agent``,
``Settings``, ``pydantic_ai``) — it only depends on ``pydantic``
(already in the tree via ``pydantic-settings``), ``PyYAML``, and stdlib.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict


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
    web: bool = False
    library_knowledge: bool = False
    report_issue: bool = True
    read_ticket: bool = False
    reply_to_thread: bool = True
    close_thread: bool = True
    ask_user: bool = True
    output_type: str | None = None
    retries: int = 2
    module: str | None = None
    skills: list[str] = []
    inject_agent_md: bool = True


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
