"""Workflow portability classification for periodic agents.

Extracted from ``periodic_loader`` to avoid a cyclic import:
``base.py`` → ``periodic_loader`` → ``yaml_loader`` → ``base.py``.

This module is dependency-free (stdlib only) so any module can import it
without creating a cycle.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Built-in workflow kinds + portability classification
# ---------------------------------------------------------------------------

# kind values:
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
    "docstring_coverage": "llm_agent",
    "module_curator": "llm_agent",
    "module_size": "llm_agent",
    "forge_parity": "llm_agent",
    "state_sync": "mill_only",
    "frontend_sync": "mill_only",
    "repo_description_sync": "schedule_only",
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

# Kinds that are enable-able on managed repos via a presence file.
_PORTABLE_KINDS: frozenset[str] = frozenset({"llm_agent", "schedule_only"})


def kind_for(name: str) -> str:
    """Return the workflow kind for *name* (``"bespoke"`` when unknown)."""
    return _BUILTIN_KINDS.get(name, "bespoke")


def is_portable(name: str) -> bool:
    """Return ``True`` iff *name* can be enabled on a managed repo via a
    ``.robotsix-mill/periodic/<name>.yaml`` presence file.

    ``bespoke`` workflows (names absent from ``_BUILTIN_KINDS``) are
    treated as **not portable** because they require a ``system_prompt``
    and are repo-specific — agents should not propose them blindly.
    """
    return kind_for(name) in _PORTABLE_KINDS


def render_workflow_portability() -> str:
    """Render a compact workflow-portability reference block for agent prompts.

    Returns a Markdown table listing every built-in workflow with its
    portability classification and a one-line note.  Agents use this to
    gate workflow-enablement proposals: **internal** workflows MUST NOT be
    proposed for managed repos; **portable** workflows are safe to propose.
    """
    lines: list[str] = [
        "## Workflow Portability",
        "",
        "Only **portable** workflows can be enabled on managed repos via a",
        "`.robotsix-mill/periodic/<name>.yaml` presence file.  **Internal**",
        "workflows cannot — they run inside the mill itself or are cross-repo",
        "infra, not per-repo presence-managed.  Tickets proposing to enable an",
        "**internal** workflow on a managed repo MUST be rejected (auto-closed",
        "at refine time or never filed).",
        "",
        "| Workflow | Portability | Notes |",
        "|----------|-------------|-------|",
    ]

    # Internal-first sort so the "can't enable these" signal is prominent.
    for name in sorted(_BUILTIN_KINDS, key=lambda n: (is_portable(n), n)):
        kind = _BUILTIN_KINDS[name]
        portable = is_portable(name)
        label = "**portable**" if portable else "**internal**"

        if kind == "mill_only":
            note = "Mill-only: hardcoded to robotsix-mill source paths"
        elif kind == "global_only":
            note = "Cross-repo infra, not per-repo presence-managed"
        elif kind == "schedule_only":
            note = "Deterministic schedule task"
        elif kind == "llm_agent":
            note = "LLM periodic agent, enable-able on any repo"
        else:
            note = ""

        lines.append(f"| `{name}` | {label} | {note} |")

    return "\n".join(lines)
