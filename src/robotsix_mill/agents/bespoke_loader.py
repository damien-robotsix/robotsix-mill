"""Loader for bespoke per-repo periodic-agent definitions.

A managed repo can carry repo-specific periodic agents in its own
source tree at:

    <repo_root>/.robotsix-mill/agents/<name>.yaml

Each YAML defines a single agent that mill schedules independently.
The schema is intentionally smaller than ``AgentDefinition`` — bespoke
agents don't need to wire output types, modules, or arbitrary tools;
they share a single generic runner with a read-only tool palette
plus draft emission via structured output.

Malformed YAMLs are skipped with a log warning (not raised). A
managed repo MUST NOT be able to crash mill by committing a broken
agent definition.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("robotsix_mill.bespoke_loader")

# Operator-chosen names land in both the ticket ``source`` string
# ("bespoke:<name>") and the memory filename. Restrict to a slug
# that is safe for both — no path separators, no colons.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class BespokeAgentDefinition(BaseModel):
    """One bespoke per-repo periodic agent loaded from
    ``.robotsix-mill/agents/<name>.yaml``."""

    name: str = Field(
        description="kebab-case slug used as the agent's identity",
    )
    description: str = ""
    interval_seconds: int = Field(
        ge=60,
        description="seconds between passes; clamped to >= 60 to keep "
        "LLM costs bounded even if an operator drops a tiny value",
    )
    model: str = Field(
        default="",
        description="provider/model id; empty → settings.bespoke_default_model",
    )
    web_knowledge: bool = Field(
        default=True,
        description="enable the ask_web_knowledge gateway tool",
    )
    system_prompt: str = Field(
        description="operator-authored prompt; the entire prompt body",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"name {v!r} must match {_NAME_RE.pattern} "
                "(kebab-case, ASCII letters/digits/hyphens, "
                "starting with a letter, up to 64 chars)"
            )
        return v


def load_bespoke_definitions(
    repo_dir: Path | None,
) -> list[BespokeAgentDefinition]:
    """Return every well-formed bespoke definition found under
    ``<repo_dir>/.robotsix-mill/agents/*.yaml``.

    Returns an empty list when *repo_dir* is None, the agents
    directory does not exist, or every YAML is malformed. Each
    malformed file is skipped with a ``log.warning`` — the bespoke
    discovery surface MUST be no-op-safe for new managed repos and
    MUST NOT be able to take mill down with a typo.
    """
    if repo_dir is None:
        return []
    agents_dir = Path(repo_dir) / ".robotsix-mill" / "agents"
    if not agents_dir.is_dir():
        return []
    out: list[BespokeAgentDefinition] = []
    seen_names: set[str] = set()
    for path in sorted(agents_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            log.warning(
                "bespoke definition %s: YAML parse error — skipping (%s)",
                path,
                exc,
            )
            continue
        if not isinstance(raw, dict):
            log.warning(
                "bespoke definition %s: top-level must be a mapping — skipping",
                path,
            )
            continue
        try:
            definition = BespokeAgentDefinition.model_validate(raw)
        except ValidationError as exc:
            log.warning(
                "bespoke definition %s: schema error — skipping (%s)",
                path,
                exc,
            )
            continue
        if definition.name in seen_names:
            # Two files with the same name would collide in
            # ``source: bespoke:<name>`` (and in their memory file).
            # First write wins; warn about the duplicate.
            log.warning(
                "bespoke definition %s: duplicate name %r — skipping",
                path,
                definition.name,
            )
            continue
        seen_names.add(definition.name)
        out.append(definition)
    return out
