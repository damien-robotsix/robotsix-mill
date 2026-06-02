"""Shared pipeline for periodic agents.

Every periodic agent — audit, health, survey, bc_check,
completeness_check, test_gap, copy_paste, agent_check — follows the
same 7-step pipeline.  This module provides the single
:func:`run_periodic_agent` entry point that the eight thin wrappers
delegate to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings


def run_periodic_agent(
    *,
    settings: Settings,
    definition_name: str,
    model_setting: str,
    max_gaps: int,
    repo_dir: Path | None,
    memory: str,
    recent_proposals: str,
    verified_proposals: str = "",
    prompt_tail: str,
    include_forge_url: bool = False,
    include_jscpd: bool = False,
    include_run_command: bool = False,
    extra_roots: list[Path] | None = None,
    usage_limits: Any = None,
    definition_override: Any = None,
) -> Any:
    """Run a periodic agent through the standard 7-step pipeline.

    Parameters
    ----------
    settings:
        Application configuration.
    definition_name:
        Stem for the YAML path and overlay key.  Must match a file in
        ``agent_definitions/periodic/<name>.yaml``.
    model_setting:
        Fallback model when ``definition.model`` is ``None``.  Callers
        pass the per-agent ``settings.<agent>_model`` field.
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
    include_run_command:
        When ``True``, adds ``"run_command"`` to the fs-tool name
        filter (default filter is ``{"read_file", "list_dir"}``).
    extra_roots:
        Forwarded to ``build_fs_tools(…, extra_roots=extra_roots)``.
    usage_limits:
        When not ``None``, passed as ``usage_limits=…`` to
        ``agent.run_sync(prompt, …)``.

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

        yaml_path = (
            Path(__file__).parent.parent.parent.parent
            / "agent_definitions"
            / "periodic"
            / f"{definition_name}.yaml"
        )
        definition = load_agent_definition(yaml_path)

    # ------------------------------------------------------------------
    # Step 2 — conditionally build the tool list
    # ------------------------------------------------------------------
    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        fs_filter: set[str] = {"read_file", "list_dir"}
        if include_run_command:
            fs_filter.add("run_command")

        ro = [
            t
            for t in build_fs_tools(
                repo_dir,
                settings,
                extra_roots=extra_roots or None,
            )
            if t.__name__ in fs_filter
        ]
        tools = [make_explore_tool(settings, repo_dir)]

        if include_jscpd:
            from .jscpd_tool import make_jscpd_tool

            tools.append(make_jscpd_tool(repo_dir))

        tools.extend(ro)

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
    # Step 4 — build the agent
    # ------------------------------------------------------------------
    from .base import build_agent_from_definition, _safe_close

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        model_name=definition.model or model_setting,
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
            settings=settings,
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
