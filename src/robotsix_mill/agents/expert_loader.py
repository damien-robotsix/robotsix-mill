"""YAML loader for expert domain definitions.

Parses ``expert_definitions/<domain>.yaml``, resolves ``${ENV_VAR}``
references in the ``model`` field, validates the result against the
``ExpertDefinition`` Pydantic model, and returns a structured object.

This module is independent of the agent runtime (``build_agent``,
``Settings``, ``pydantic_ai``) — it only depends on ``pydantic``
(already in the tree via ``pydantic-settings``), ``PyYAML``, and stdlib.

This is purely declarative: zero runtime integration.  The
``ExpertManager`` that instantiates experts from these definitions is a
separate future ticket.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class ExpertMemoryConfig(BaseModel):
    """Memory/retrieval tuning for an expert.

    These parameters control how much context the expert loads and how
    the repo_map retriever splits/returns results.  All fields are
    optional with sensible defaults — omit the ``memory`` key in YAML
    to get the defaults.
    """

    model_config = ConfigDict(extra="forbid")

    max_memory_chars: int = 8000
    chunk_size: int = 2000
    max_chunks: int = 20
    memory_path: str | None = None
    extras: dict[str, Any] = {}


class ExpertDefinition(BaseModel):
    """A validated expert domain definition loaded from a YAML file.

    Describes an expert's domain identity, module scope (via
    ``module_paths`` globs), custom instructions, model override,
    memory/retrieval tuning, tool allow-list, skills, and an
    ``extras`` dict for future extension.

    All fields are validated at parse time.  Unknown top-level keys
    are rejected (``extra="forbid"``) — use ``extras`` for deliberate
    passthrough.
    """

    model_config = ConfigDict(extra="forbid")

    domain: str
    description: str | None = None
    module_paths: list[str]
    system_prompt: str
    model: str = ""
    memory: ExpertMemoryConfig = ExpertMemoryConfig()
    skills: list[str] = []
    tools: list[str] = ["explore", "read_file", "list_dir"]
    extras: dict[str, Any] = {}

    @field_validator("domain", mode="after")
    @classmethod
    def _validate_domain_slug(cls, v: str) -> str:
        """Domain must be a slug-like identifier: lowercase letters,
        digits, and single hyphens between segments."""
        if not re.fullmatch(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", v):
            raise ValueError(
                f"domain must be a slug-like identifier (e.g. 'python-backend'), got {v!r}"
            )
        return v


# Only bare ``${VAR}`` — the existing YAML does not use defaults or nesting.
_ENV_VAR_RE = re.compile(r"\$\{([^{}]+)\}")


def _resolve_env_vars(raw: str) -> str:
    """Replace ``${VAR}`` placeholders in *raw* with their values from
    ``os.environ``.  Returns ``""`` for unset variables — the caller
    (``ExpertManager``) then falls back to the global expert model when
    ``model`` is falsy.
    """

    def _replacer(m: re.Match[str]) -> str:
        var = m.group(1)
        return os.environ.get(var, "")

    return _ENV_VAR_RE.sub(_replacer, raw)


def load_expert_definition(path: Path) -> ExpertDefinition:
    """Parse, validate, and env-resolve an expert YAML definition.

    ``path`` must point to a YAML file whose top-level keys map to
    ``ExpertDefinition`` fields.

    Returns a validated ``ExpertDefinition`` instance.

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

    # Env-var resolution: only the ``model`` field.
    if "model" in data and isinstance(data["model"], str):
        data["model"] = _resolve_env_vars(data["model"])

    return ExpertDefinition.model_validate(data)
