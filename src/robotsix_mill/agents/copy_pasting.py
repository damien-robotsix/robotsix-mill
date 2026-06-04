"""The copy-paste agent: deterministic clone detection and triage.

Runs jscpd (via ``detect_duplication``) to find copy-paste duplication,
triages clone pairs by severity, cross-references against the memory
ledger and ``recent-proposals``, and files one draft ticket per
high-severity clone pair.

Seam: tests monkeypatch ``run_copy_paste_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from ..pass_runner import ProposedActionItem

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "periodic"
    / "copy_paste.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 8


class CopyPasteResult(BaseModel):
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
    proposed_actions: list[ProposedActionItem] = Field(default_factory=list)


def run_copy_paste_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> CopyPasteResult:
    """Run the copy-paste detection pass.

    Runs deterministic clone detection (jscpd via
    ``detect_duplication``), triages clone pairs by severity
    (``files × lines`` product, ≥3 files OR ≥30 duplicated lines),
    cross-references against the memory ledger and ``recent-proposals``
    for resolved/declined clones, reads clone files with ``read_file``
    to confirm genuine copy-paste, and files one draft ticket per
    high-severity clone pair.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``), the ``explore`` scout tool, and
    the ``detect_duplication`` tool (injected at runtime via
    ``periodic_base``, following the audit pattern).

    Args:
        settings: Application configuration — model names
            (``copy_paste_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        recent_proposals: Prior proposals string from pass runner.
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``CopyPasteResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (8) entries, plus the updated memory
        ledger.
    """
    from .periodic_base import run_periodic_agent

    return run_periodic_agent(
        settings=settings,
        definition_name="copy_paste",
        definition_override=definition_override,
        model_setting=settings.copy_paste_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Run detect_duplication, triage the clone pairs, and return your findings.",
        include_jscpd=True,
    )
