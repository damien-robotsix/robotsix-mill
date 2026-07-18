"""Per-repo periodic-workflow discovery + override resolution.

A managed repo declares which periodic *workflows* run against it by
committing files into its own source tree at:

    <repo_root>/.robotsix-mill/periodic/<name>.yaml

The PRESENCE of a file enables that workflow for the repo — there is no
separate enable flag. A "periodic workflow" is the umbrella; an LLM agent
is one *kind* of workflow (see ``WorkflowKind``). "Agent" is reserved for
the ``llm_agent`` kind.

Resolution by name:
  * The name matches a built-in workflow → the file PARTIAL-MERGES over the
    built-in: only the fields present override; absent fields inherit. A
    name-only file just enables the built-in with its shipped parameters.
    The prompt overlay is handled in-file: ``prompt_overlay:`` appends to
    the built-in system prompt, ``system_prompt:`` fully replaces it (the
    two are mutually exclusive).
  * The name matches NO built-in → it defines a brand-new repo-specific
    agent (``bespoke`` kind); it must carry a ``system_prompt``.

Malformed files are skipped with a ``log.warning`` (never raised): a
managed repo MUST NOT be able to crash mill by committing a broken file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from robotsix_mill._resources import agent_definitions_dir
from ..core.duration import parse_duration
from .overlays import apply_overlay
from .yaml_loader import AgentDefinition, load_agent_definition

log = logging.getLogger("robotsix_mill.periodic_loader")

# Per-repo periodic-workflow directory, relative to the repo clone root.
PERIODIC_DIR = (".robotsix-mill", "periodic")

# Name slug shared by the ticket ``source`` string and memory filenames for
# bespoke workflows — no path separators or colons. Allows snake_case (built-in
# names like cost_warmer, agent_check) and kebab-case (bespoke slugs).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


# ---------------------------------------------------------------------------
# Workflow kinds + the canonical built-in kind map
# ---------------------------------------------------------------------------

# kind values (kept as plain strings for trivial serialization/compare):
#   "llm_agent"     — built-in LLM periodic agent with a prompt yaml in
#                     agent_definitions/periodic/<name>.yaml; the per-repo
#                     file partial-merges over it.
#   "schedule_only" — built-in pass with NO prompt yaml (or a deterministic
#                     runner): the per-repo file only carries presence +
#                     interval_seconds/enabled; prompt fields are ignored.
#   "bespoke"       — brand-new repo-specific agent (name matches no
#                     built-in); requires system_prompt.
#   "global_only"   — recognized built-in that is NOT per-repo presence
#                     managed (cross-repo or always-on infra); a per-repo
#                     file for it is ignored with a warning.
#   "mill_only"     — recognized built-in that is ONLY valid for the
#                     robotsix-mill repo itself; its system prompt is
#                     hardcoded to mill's own source paths. A per-repo
#                     presence file for it on any OTHER repo is rejected.
_BUILTIN_KINDS: dict[str, str] = {
    # LLM periodic agents (prompt yaml + partial-merge override).
    "audit": "llm_agent",
    "health": "llm_agent",
    "agent_check": "llm_agent",
    "bc_check": "llm_agent",
    "completeness_check": "llm_agent",
    "copy_paste": "llm_agent",
    "survey": "llm_agent",
    "test_gap": "llm_agent",
    "module_curator": "llm_agent",
    "forge_parity": "llm_agent",
    "state_sync": "llm_agent",
    "frontend_sync": "mill_only",
    "repo_description_sync": "schedule_only",
    "security_posture": "llm_agent",
    "triage_boilerplate": "llm_agent",
    # Schedule-only passes (no prompt yaml / deterministic runner).
    "diagnostic": "schedule_only",
    "trace_review": "schedule_only",
    "config_sync": "schedule_only",
    "member_sync": "schedule_only",
    "data_dir_gc": "schedule_only",
    "changelog_autofill": "schedule_only",
    "pin_bump": "schedule_only",
    # Recognized but NOT per-repo-presence managed (cross-repo / always-on).
    "langfuse_cleanup": "global_only",
    "meta": "global_only",
    "run_health": "global_only",
    "timeout_escalation": "global_only",
    "trace_health": "global_only",
}


def kind_for(name: str) -> str:
    """Return the workflow kind for *name* (``"bespoke"`` when unknown)."""
    return _BUILTIN_KINDS.get(name, "bespoke")


def validate_periodic_file_content(
    name: str,
    system_prompt: str | None,
) -> list[str]:
    """Return a list of human-readable error strings for a proposed presence file.

    Empty list → valid.  Callers MUST NOT write the file when errors are returned.
    """
    kind = kind_for(name)
    if kind == "global_only":
        return [
            f"'{name}' is a global/cross-repo periodic and cannot be "
            "per-repo presence-managed. Remove this file."
        ]
    if kind == "mill_only":
        return [
            f"'{name}' is a mill-internal periodic agent (its prompt is "
            "hardcoded to robotsix-mill's own source paths) and cannot "
            "be enabled on managed repos via a presence file. Remove this "
            "file — it will not be loaded."
        ]
    if kind == "bespoke" and (system_prompt is None or not system_prompt.strip()):
        valid_builtins = sorted(
            k for k, v in _BUILTIN_KINDS.items() if v != "global_only"
        )
        return [
            f"'{name}' is not a recognised built-in periodic name. "
            "Either use one of the valid built-in names "
            f"({', '.join(valid_builtins)}) or include a non-empty "
            "`system_prompt` field to define a new bespoke agent."
        ]
    return []


# ---------------------------------------------------------------------------
# Per-repo override file schema
# ---------------------------------------------------------------------------


class PeriodicWorkflowFile(BaseModel):
    """A per-repo ``.robotsix-mill/periodic/<name>.yaml`` file.

    Only ``name`` is required; every other field is optional and, when
    present, overrides the built-in of the same name (partial merge). For
    a brand-new (bespoke) workflow whose name matches no built-in,
    ``system_prompt`` is required (validated in :func:`resolve_periodic_workflow`,
    not here, so discovery can report a clear per-file reason).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    # Prompt knobs — mutually exclusive. ``prompt_overlay`` appends to the
    # built-in prompt; ``system_prompt`` replaces it.
    prompt_overlay: str | None = None
    system_prompt: str | None = None
    # Everything below mirrors AgentDefinition (all optional here).
    description: str | None = None
    category: str | None = None
    level: int | None = None
    tools: list[str] | None = None
    web_knowledge: bool | None = None
    report_issue: bool | None = None
    read_ticket: bool | None = None
    reply_to_thread: bool | None = None
    close_thread: bool | None = None
    ask_user: bool | None = None
    output_type: str | None = None
    retries: int | None = None
    module: str | None = None
    skills: list[str] | None = None
    modules: bool | None = None
    inject_agent_md: bool | None = None
    # ``interval`` is the human-readable form (``1w2d3h40m10s``);
    # ``interval_seconds`` is the legacy integer-seconds form. Mutually
    # exclusive — see ``_interval_xor``.
    interval: str | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _interval_xor(self) -> "PeriodicWorkflowFile":
        if self.interval is not None and self.interval_seconds is not None:
            raise ValueError(
                "set at most one of 'interval' (human-readable, e.g. '2d') "
                "or 'interval_seconds' (legacy integer seconds), not both"
            )
        if self.interval is not None:
            self.interval_seconds = parse_duration(self.interval)
            # A field assigned only inside an after-validator is NOT in
            # __pydantic_fields_set__, so ``model_dump(exclude_unset=True)``
            # (used by _merge_over_builtin) would drop the backfilled value
            # and the per-repo override would silently fail to apply. Mark
            # it set so the merge actually carries it through.
            self.__pydantic_fields_set__.add("interval_seconds")
        return self

    @model_validator(mode="after")
    def _prompt_xor(self) -> "PeriodicWorkflowFile":
        if self.prompt_overlay is not None and self.system_prompt is not None:
            raise ValueError(
                "set at most one of 'prompt_overlay' (append) or "
                "'system_prompt' (replace), not both"
            )
        return self

    @model_validator(mode="after")
    def _name_slug(self) -> "PeriodicWorkflowFile":
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"name {self.name!r} must match {_NAME_RE.pattern} "
                "(kebab-case ASCII, starts with a letter, <= 64 chars)"
            )
        return self


@dataclass
class ResolvedPeriodicWorkflow:
    """One discovered + resolved per-repo periodic workflow."""

    name: str
    kind: str  # one of the _BUILTIN_KINDS values, or "bespoke"
    # The fully-resolved agent definition for ``llm_agent``/``bespoke``;
    # ``None`` for ``schedule_only`` (no prompt).
    definition: AgentDefinition | None
    interval_seconds: int | None  # file override; None → caller's default
    enabled: bool  # presence implies True unless the file sets enabled: false


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

# Fields that exist on PeriodicWorkflowFile but are handled specially / are
# not AgentDefinition fields when merging.
# ``interval`` is the human-readable input only; the after-validator
# backfills ``interval_seconds`` (which IS merged), so the raw ``interval``
# string must not be forwarded into AgentDefinition (it would collide with
# the backfilled ``interval_seconds`` and trip the mutual-exclusion check).
_NON_MERGE_FIELDS = frozenset({"name", "prompt_overlay", "system_prompt", "interval"})


def _builtin_definition(name: str) -> AgentDefinition:
    """Load the shipped ``agent_definitions/periodic/<name>.yaml``."""
    builtin = agent_definitions_dir() / "periodic" / f"{name}.yaml"
    return load_agent_definition(builtin)


def _merge_over_builtin(
    builtin: AgentDefinition, pwf: PeriodicWorkflowFile
) -> AgentDefinition:
    """Partial-merge the set fields of *pwf* over *builtin* + resolve prompt."""
    data: dict[str, Any] = builtin.model_dump()
    # The builtin may have been authored with ``interval`` (human-readable);
    # its validator already backfilled ``interval_seconds``. Drop the raw
    # ``interval`` so the re-validated AgentDefinition sees only the canonical
    # seconds and doesn't trip the interval/interval_seconds exclusion check.
    data.pop("interval", None)
    provided = pwf.model_dump(exclude_unset=True)
    for key, value in provided.items():
        if key in _NON_MERGE_FIELDS:
            continue
        data[key] = value
    # Resolve the prompt: replace > overlay > inherit.
    if pwf.system_prompt is not None:
        data["system_prompt"] = pwf.system_prompt
    elif pwf.prompt_overlay is not None:
        data["system_prompt"] = apply_overlay(builtin.system_prompt, pwf.prompt_overlay)
    return AgentDefinition.model_validate(data)


def _bespoke_definition(pwf: PeriodicWorkflowFile) -> AgentDefinition:
    """Build a full AgentDefinition for a brand-new (unmatched-name) agent."""
    if pwf.system_prompt is None:
        raise ValueError(
            f"periodic workflow {pwf.name!r} matches no built-in, so it "
            "defines a new agent and MUST set 'system_prompt'"
        )
    data = pwf.model_dump(exclude_unset=True)
    data.pop("prompt_overlay", None)
    # ``interval`` is human-readable input only; the validator already
    # backfilled ``interval_seconds``. Drop it so it doesn't collide with
    # interval_seconds and trip AgentDefinition's mutual-exclusion check.
    data.pop("interval", None)
    # A new bespoke periodic agent defaults to level 1 (cheap) unless it
    # declares a level explicitly.
    data.setdefault("level", 1)
    return AgentDefinition.model_validate(data)


def resolve_periodic_workflow(
    path: Path,
) -> ResolvedPeriodicWorkflow | None:
    """Parse + resolve a single ``.robotsix-mill/periodic/<name>.yaml`` file.

    Returns a :class:`ResolvedPeriodicWorkflow`, or ``None`` when the file is
    malformed / unsupported (a ``global_only`` name). Never raises — a bad
    file is logged and skipped so a managed repo can't take mill down.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("periodic workflow %s: read/parse error — skipping (%s)", path, exc)
        return None
    if not isinstance(raw, dict):
        log.warning(
            "periodic workflow %s: top-level must be a mapping — skipping", path
        )
        return None
    # Default name from the filename stem when the file omits it.
    raw.setdefault("name", path.stem)
    try:
        pwf = PeriodicWorkflowFile.model_validate(raw)
    except ValidationError as exc:
        log.warning("periodic workflow %s: schema error — skipping (%s)", path, exc)
        return None

    kind = kind_for(pwf.name)
    if kind == "global_only":
        log.warning(
            "periodic workflow %s: %r is a global/cross-repo workflow and is "
            "not per-repo presence-managed — ignoring this file",
            path,
            pwf.name,
        )
        return None

    enabled = True if pwf.enabled is None else bool(pwf.enabled)

    definition: AgentDefinition | None = None
    try:
        if kind == "llm_agent":
            definition = _merge_over_builtin(_builtin_definition(pwf.name), pwf)
        elif kind == "bespoke":
            definition = _bespoke_definition(pwf)
        # schedule_only carries no prompt; definition stays None.
    except (FileNotFoundError, ValidationError, ValueError) as exc:
        log.warning(
            "periodic workflow %s: resolution failed — skipping (%s)", path, exc
        )
        return None

    return ResolvedPeriodicWorkflow(
        name=pwf.name,
        kind=kind,
        definition=definition,
        interval_seconds=pwf.interval_seconds,
        enabled=enabled,
    )


def discover_periodic_workflows(
    repo_dir: Path | None,
) -> list[ResolvedPeriodicWorkflow]:
    """Return every well-formed workflow under ``<repo_dir>/.robotsix-mill/periodic/``.

    Empty list when *repo_dir* is None or the directory is absent. Malformed
    / unsupported files are skipped with a warning. Duplicate names: first
    file (alphabetical) wins.
    """
    if repo_dir is None:
        return []
    pdir = Path(repo_dir).joinpath(*PERIODIC_DIR)
    if not pdir.is_dir():
        return []
    out: list[ResolvedPeriodicWorkflow] = []
    seen: set[str] = set()
    for path in sorted(pdir.glob("*.yaml")):
        resolved = resolve_periodic_workflow(path)
        if resolved is None:
            continue
        if resolved.name in seen:
            log.warning(
                "periodic workflow %s: duplicate name %r — skipping",
                path,
                resolved.name,
            )
            continue
        seen.add(resolved.name)
        out.append(resolved)
    return out
