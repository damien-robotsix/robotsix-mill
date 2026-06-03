"""The bc-check agent: scans the repository for backward-compatibility
shims, no-op compat entry points, legacy property accessors, alias
assignments, default-arg compat branches, and legacy shape fallbacks —
then files draft tickets proposing cleanup for those that are ripe for
removal.

Seam: tests monkeypatch ``run_bc_check_agent``. Structured output so the
runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "periodic"
    / "bc_check.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 12


class BcCheckResult(BaseModel):
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


def run_bc_check_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> BcCheckResult:
    """Run the backward-compatibility inspection pass.

    Scans the repository for backward-compatibility shims, determines
    which are ripe for removal, and returns a structured
    ``BcCheckResult`` with draft tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent` with the
    ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(BcCheckResult)``, ``web=False``, and
    ``report_issue=False``.

    Args:
        settings: Application configuration — model names
            (``bc_check_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``BcCheckResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (12) entries, plus the updated memory
        ledger.
    """
    from .periodic_base import run_periodic_agent

    return run_periodic_agent(
        settings=settings,
        definition_name="bc_check",
        definition_override=definition_override,
        model_setting=settings.bc_check_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Scan the repository for backward-compatibility code and return your findings.",
    )
