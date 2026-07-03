"""Shared pipeline for periodic agents.

Every periodic agent — audit, health, survey, bc_check,
completeness_check, test_gap, copy_paste, agent_check — follows the
same 7-step pipeline.  This module provides the single
:func:`run_periodic_agent` entry point that the eight thin wrappers
delegate to.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field

from robotsix_mill._resources import agent_definitions_dir
from ..config import Settings

# Uniform verification gate appended to every periodic detector's prompt.
# Modelled on the trace-review optimization gate (PR #1135) in
# ``trace_inspector.py`` but generalized to every proposal type, so a
# detector cannot file a draft on inference from logs/traces/git output
# alone — it must ground each concrete claim in the live tree first.
_VERIFICATION_GATE: str = (
    "\n\nBEFORE filing ANY draft, verify every concrete claim against the live "
    "tree using the tools already available to you (`read_file`, `list_dir`, "
    "`explore`/`parallel_explore`, and `run_command` when present):\n"
    "- Stale-path / cleanup / 'file no longer exists' claims: confirm the path "
    "or glob truly resolves to ZERO existing files before claiming it is "
    "missing or stale; conversely confirm a flagged file is genuinely "
    "unclaimed before proposing reclassification.\n"
    "- Optimization / refactoring / code-citation claims: open the cited source "
    "and confirm the code and control flow you reason about actually exist; "
    "cite the specific `path/to/file.py:LINE` you read.\n"
    "A claim you cannot ground in a verified path or location is NOT ready to "
    "file — drop it rather than filing on inference from logs, traces, or git "
    "output alone."
)


def _count_active_proposals(recent_proposals: str) -> int:
    """Parse the ``<recent_proposals>`` block and return the number of
    proposals whose state is *not* a terminal/resolved state.

    Terminal states are ``done`` and ``closed`` — every other state
    value (``draft``, ``ready``, ``blocked``, ``human_issue_approval``,
    etc.) counts as active.
    """
    if not recent_proposals or "(no recent proposals)" in recent_proposals:
        return 0

    active = 0
    for line in recent_proposals.splitlines():
        # Lines look like:  [state_value] ticket_id | title
        if line.startswith("[") and "] " in line:
            state = line[1:].split("]", 1)[0].strip()
            if state not in ("done", "closed"):
                active += 1
    return active


def load_periodic_system_prompt(name: str) -> str:
    """Load a periodic agent's static system prompt from its YAML
    definition without env-var resolution.

    Resolves ``agent_definitions/periodic/<name>.yaml`` (relative to the
    package root) and returns its ``system_prompt`` value.  Periodic
    agent modules re-export the result as a module-level
    ``SYSTEM_PROMPT`` for their test seams.
    """
    import yaml

    path = agent_definitions_dir() / "periodic" / f"{name}.yaml"
    return yaml.safe_load(path.read_text())["system_prompt"]


class PeriodicAgentResult(BaseModel):
    """Shared structured-output model for the periodic gap-finding agents.

    Agents whose result shape matches these six fields alias this class
    directly (e.g. ``AuditResult = PeriodicAgentResult``); agents that
    add fields subclass it (e.g. ``AgentCheckResult`` adds ``findings``).
    """

    updated_memory: str = ""
    summary: str = Field(
        default="",
        description=(
            "One sentence: what you examined and the basis for the number "
            "of drafts filed (e.g. 'scanned 142 files; jscpd found 3 clone "
            "pairs, 0 above the severity threshold'). ALWAYS fill this so "
            "an operator can verify a 0-draft run is legitimate."
        ),
    )
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)
    verified_gap_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Subset of gap_ids that the runner confirmed were actually "
            "filed as draft tickets. Populated by the runner after ticket "
            "creation — the agent MUST NOT set this field. Only gap IDs "
            "that appear in both gap_ids AND draft_titles/draft_bodies AND "
            "were successfully created belong here."
        ),
    )


def _build_periodic_tools(
    *,
    settings: Settings,
    repo_dir: Path,
    include_jscpd: bool,
    include_workflow_caller_audit: bool,
    include_run_command: bool,
    include_write_file: bool = False,
    extra_roots: list[Path] | None,
) -> list:
    """Build the conditional tool list for a periodic agent run.

    Extracted from :func:`run_periodic_agent` so the entry point stays
    under the cyclomatic-complexity gate.
    """
    from .explore import make_explore_tool, make_parallel_explore_tool
    from .fs_tools import build_fs_tools
    from .validate_artifact_tool import make_validate_artifact_tool

    fs_filter: set[str] = {"read_file", "list_dir"}
    if include_run_command:
        fs_filter.add("run_command")
    if include_write_file:
        fs_filter.add("write_file")

    ro = [
        t
        for t in build_fs_tools(
            repo_dir,
            settings,
            extra_roots=extra_roots or None,
        )
        if t.__name__ in fs_filter
    ]
    tools = [
        make_explore_tool(settings, repo_dir),
        make_parallel_explore_tool(settings, repo_dir),
        make_validate_artifact_tool(repo_dir),
    ]

    if include_jscpd:
        from .jscpd_tool import make_jscpd_tool

        tools.append(make_jscpd_tool(repo_dir))

    if include_workflow_caller_audit:
        from .workflow_caller_audit import make_workflow_caller_audit_tool

        tools.append(make_workflow_caller_audit_tool(repo_dir))

    tools.extend(ro)
    return tools


def run_periodic_agent(
    *,
    settings: Settings,
    definition_name: str,
    max_gaps: int,
    repo_dir: Path | None,
    memory: str,
    recent_proposals: str,
    verified_proposals: str = "",
    prompt_tail: str,
    include_forge_url: bool = False,
    include_jscpd: bool = False,
    include_workflow_caller_audit: bool = False,
    include_run_command: bool = False,
    include_write_file: bool = False,
    extra_roots: list[Path] | None = None,
    usage_limits: Any = None,
    definition_override: Any = None,
    max_errors: int = 0,
) -> Any:
    """Run a periodic agent through the standard 7-step pipeline.

    Parameters
    ----------
    settings:
        Application configuration.
    definition_name:
        Stem for the YAML path and overlay key.  Must match a file in
        ``agent_definitions/periodic/<name>.yaml``.
    max_gaps:
        Clipping constant for ``draft_titles``, ``draft_bodies``, and
        ``gap_ids`` on the final result.
    repo_dir:
        Optional path to the local repository clone.  When not
        ``None``, fs tools + explore (and optionally jscpd) are
        injected.
    memory:
        The agent's memory ledger as a Markdown string.
    recent_proposals:
        Prior proposals string from the pass runner.
    verified_proposals:
        Ephemeral verified-state table (rendered Markdown) recomputed
        from the ticket DB every pass and injected into the prompt
        between *recent_proposals* and *memory*. Passed separately —
        not concatenated onto *memory* — so it does not round-trip
        through the agent's ``updated_memory`` field into the persisted
        ledger.  Empty string means no prior proposals to verify.
    prompt_tail:
        Agent-specific final sentence of the prompt.
    include_forge_url:
        When ``True``, prepends a ``section("forge-remote-url", …)``
        block after *recent_proposals* and before *memory*.
    include_jscpd:
        When ``True``, appends ``make_jscpd_tool(repo_dir)`` to the
        tool list.
    include_workflow_caller_audit:
        When ``True``, appends ``make_workflow_caller_audit_tool(repo_dir)``
        — a deterministic detector for broken reusable-workflow callers
        (wrong org / missing permissions) — to the tool list.
    include_run_command:
        When ``True``, adds ``"run_command"`` to the fs-tool name
        filter (default filter is ``{"read_file", "list_dir"}``).
    include_write_file:
        When ``True``, adds ``"write_file"`` to the fs-tool name
        filter (default filter is ``{"read_file", "list_dir"}``).
    extra_roots:
        Forwarded to ``build_fs_tools(…, extra_roots=extra_roots)``.
    usage_limits:
        When not ``None``, passed as ``usage_limits=…`` to
        ``agent.run_sync(prompt, …)``.
    max_errors:
        When > 0, tools are wrapped with a shared error counter that
        raises ``UsageLimitExceeded`` after *max_errors* tool-call
        errors — terminating runaway agent loops.  Defaults to 0
        (no error limit).

    Returns
    -------
    Any
        The ``output`` attribute of the run result — typically a
        Pydantic model with ``draft_titles``, ``draft_bodies``,
        ``gap_ids``, and ``updated_memory`` fields, each clipped to
        *max_gaps*.
    """
    # ------------------------------------------------------------------
    # Step 1 — resolve the agent definition
    # ------------------------------------------------------------------
    # When the per-repo periodic supervisor resolved a merged definition
    # (partial-override + in-file prompt overlay from
    # .robotsix-mill/periodic/<name>.yaml), use it verbatim — its prompt
    # and model already reflect the repo's overrides. Otherwise fall back
    # to the shipped built-in yaml (legacy / direct-call path).
    if definition_override is not None:
        definition = definition_override
    else:
        from .yaml_loader import load_agent_definition

        yaml_path = agent_definitions_dir() / "periodic" / f"{definition_name}.yaml"
        definition = load_agent_definition(yaml_path)

    # ------------------------------------------------------------------
    # Step 2 — conditionally build the tool list
    # ------------------------------------------------------------------
    tools: list[Any] = []
    if repo_dir is not None:
        tools = _build_periodic_tools(
            settings=settings,
            repo_dir=repo_dir,
            include_jscpd=include_jscpd,
            include_workflow_caller_audit=include_workflow_caller_audit,
            include_run_command=include_run_command,
            include_write_file=include_write_file,
            extra_roots=extra_roots,
        )
    # Wrap tools with an error budget when the caller requests one
    # (mirrors the trace_inspector guardrail for runaway tool loops).
    if max_errors > 0:
        from .trace_inspector import _wrap_tools_with_error_limit

        tools = _wrap_tools_with_error_limit(tools, max_errors=max_errors)

    # ------------------------------------------------------------------
    # Step 3 — resolve the system prompt
    # ------------------------------------------------------------------
    # The merged override already applied any in-file prompt_overlay /
    # system_prompt, so use its prompt directly. Only the legacy built-in
    # path consults the deprecated .robotsix-mill/agent_overlays/<name>.md.
    if definition_override is not None:
        system_prompt = definition.system_prompt
    else:
        from .overlays import apply_overlay, load_overlay

        system_prompt = apply_overlay(
            definition.system_prompt,
            load_overlay(repo_dir, definition_name),
        )

    # ------------------------------------------------------------------
    # Step 3½ — warn when the detector has active proposals on the board
    # ------------------------------------------------------------------
    active_count = _count_active_proposals(recent_proposals)
    if active_count > 0:
        system_prompt += (
            "\n\n## ⚠️ Active Proposals\n\n"
            f"There are currently **{active_count}** active proposal(s) from this "
            "detector on the board (states other than `done` / `closed`). "
            "Review the `<recent_proposals>` block in your prompt before "
            "filing any new draft. Filing many concurrent proposals dilutes "
            "focus — ensure your proposal either supersedes or meaningfully "
            "supplements those already in flight."
        )

    # ------------------------------------------------------------------
    # Step 4 — build the agent
    # ------------------------------------------------------------------
    from .base import build_agent_from_definition, _safe_close

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        system_prompt=system_prompt,
    )

    # ------------------------------------------------------------------
    # Step 5 — construct the prompt
    # ------------------------------------------------------------------
    from .prompt_blocks import section

    prompt = recent_proposals

    if verified_proposals:
        prompt += "\n\n" + verified_proposals

    if include_forge_url:
        forge_url = settings.forge_remote_url or "(not configured)"
        prompt += section("forge-remote-url", forge_url) + "\n\n"

    prompt += section("memory", memory or "(empty — start a new ledger)")
    prompt += "\n\n" + prompt_tail
    # Always require a one-line run summary so an operator can tell a
    # legitimate 0-draft run (scanned, nothing met the bar) from a no-op.
    prompt += (
        "\n\nALWAYS populate the `summary` field of your structured output with "
        "ONE sentence stating what you examined and the basis for the number of "
        'drafts you filed (e.g. "reviewed 142 files; 3 clone pairs, 0 above the '
        'severity threshold"). This is how an operator verifies a 0-draft run '
        "was legitimate rather than a no-op — never leave it empty."
    )
    # Require every concrete claim to be grounded in the live tree before a
    # draft is filed — applied uniformly to all periodic detectors.
    prompt += _VERIFICATION_GATE

    # ------------------------------------------------------------------
    # Step 6 — run with retry
    # ------------------------------------------------------------------
    from .retry import run_agent

    _run_kwargs: dict[str, Any] = {}
    if usage_limits is not None:
        _run_kwargs["usage_limits"] = usage_limits

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt, **_run_kwargs),
            what=definition_name,
        )
    finally:
        _safe_close(agent)

    # ------------------------------------------------------------------
    # Step 7 — clip and return
    # ------------------------------------------------------------------
    result.output.draft_titles = result.output.draft_titles[:max_gaps]
    result.output.draft_bodies = result.output.draft_bodies[:max_gaps]
    result.output.gap_ids = result.output.gap_ids[:max_gaps]
    return result.output


def make_agent_runner(
    *,
    definition_name: str,
    prompt_tail: str,
    max_gaps: int = 5,
    include_forge_url: bool = False,
    include_jscpd: bool = False,
    include_workflow_caller_audit: bool = False,
    include_run_command: bool = False,
    include_write_file: bool = False,
    dynamic_kwargs_fn: Callable[[Settings], dict[str, Any]] | None = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> Callable[..., Any]:
    """Create a ``run_*_agent`` function with the standard periodic-agent
    signature.

    The returned callable accepts ``(*, settings, memory, recent_proposals,
    verified_proposals, repo_dir=None, definition_override=None)`` and
    delegates to :func:`run_periodic_agent` with the parameters captured
    at factory time.  The model is resolved from the definition's ``level``.

    *dynamic_kwargs_fn* is called with ``settings`` at invocation time to
    produce extra keyword arguments for :func:`run_periodic_agent` — use
    it for settings-dependent parameters such as ``usage_limits`` and
    ``max_errors``.

    The returned function looks up ``run_periodic_agent`` at call time
    (module-level name lookup) so that monkeypatching
    ``periodic_base.run_periodic_agent`` in tests is honoured.
    """

    def run_agent(
        *,
        settings: Settings,
        memory: str = "",
        recent_proposals: str = "",
        verified_proposals: str = "",
        repo_dir: Path | None = None,
        definition_override: Any = None,
    ) -> PeriodicAgentResult:
        kwargs: dict[str, Any] = dict(extra_kwargs) if extra_kwargs else {}
        if dynamic_kwargs_fn is not None:
            kwargs.update(dynamic_kwargs_fn(settings))

        # run_periodic_agent returns the agent's structured output (typed Any
        # at the pydantic-ai seam); the factory contractually narrows it to
        # PeriodicAgentResult.
        return cast(
            PeriodicAgentResult,
            run_periodic_agent(
                settings=settings,
                definition_name=definition_name,
                definition_override=definition_override,
                max_gaps=max_gaps,
                repo_dir=repo_dir,
                memory=memory,
                recent_proposals=recent_proposals,
                verified_proposals=verified_proposals,
                prompt_tail=prompt_tail,
                include_forge_url=include_forge_url,
                include_jscpd=include_jscpd,
                include_workflow_caller_audit=include_workflow_caller_audit,
                include_run_command=include_run_command,
                include_write_file=include_write_file,
                **kwargs,
            ),
        )

    return run_agent
