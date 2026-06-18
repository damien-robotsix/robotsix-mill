"""The board-cleanup agent: kanban-board hygiene oversight.

Reviews the live board (a snapshot of recent tickets, injected by the
bespoke runner) and proposes hygiene actions — close stale/obsolete
tickets, transition mis-stated ones, comment for clarification, or
relabel — emitted as ``proposed_actions`` (``ProposedActionItem``) for
human approval via the Proposals panel. System prompt and schema are
loaded from ``agent_definitions/periodic/board_cleanup.yaml``.

Seam: tests monkeypatch ``run_board_cleanup_agent``. Structured output
(``BoardCleanupResult``) so the runner has a clear result to work with;
draft lists are clipped to ``MAX_DRAFTS`` (20).
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("board_cleanup")


MAX_DRAFTS = 20


BoardCleanupResult = PeriodicAgentResult


def run_board_cleanup_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    board_snapshot: str = "",
    repo_dir=None,
    definition_override=None,
) -> BoardCleanupResult:
    """Run the board-cleanup pass.

    Reviews the injected board snapshot (recent tickets across all
    sources), identifies hygiene issues — stale/obsolete/duplicate
    tickets, mis-stated states, missing labels — and returns a
    structured ``BoardCleanupResult`` whose ``proposed_actions`` carry
    close/transition/comment/relabel proposals for human approval.

    Args:
        settings: Application configuration — model names
            (``board_cleanup_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        board_snapshot: A rendered snapshot of recent board tickets,
            injected by the bespoke runner.
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``BoardCleanupResult`` with draft titles, bodies, gap IDs
        (clipped to ``MAX_DRAFTS``), proposed actions, and the updated
        memory ledger.
    """
    from .base import build_agent_from_definition, _safe_close

    if definition_override is not None:
        definition = definition_override
    else:
        from .yaml_loader import load_agent_definition

        definition = load_agent_definition(
            Path(__file__).parent.parent.parent.parent
            / "agent_definitions"
            / "periodic"
            / "board_cleanup.yaml"
        )

    from ._repo_tools import _build_repo_tools

    tools = _build_repo_tools(repo_dir, settings)

    if definition_override is not None:
        system_prompt = definition.system_prompt
    else:
        from .overlays import apply_overlay, load_overlay

        system_prompt = apply_overlay(
            definition.system_prompt,
            load_overlay(repo_dir, "board_cleanup"),
        )
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        system_prompt=system_prompt,
    )
    verified_block = ("\n\n" + verified_proposals) if verified_proposals else ""
    prompt = (
        f"{recent_proposals}"
        + verified_block
        + section("board_snapshot", board_snapshot or "(no tickets on the board)")
        + section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + "Review the board snapshot above and propose hygiene actions "
        "(close, transition, comment, relabel) for stale, obsolete, "
        "duplicate, or mis-stated tickets. Use read_ticket to inspect any "
        "ticket before proposing an action on it."
    )
    from .retry import run_agent

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            what="board_cleanup",
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_DRAFTS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_DRAFTS]
    result.output.gap_ids = result.output.gap_ids[:MAX_DRAFTS]
    return result.output
