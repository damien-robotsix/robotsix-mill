"""The forge-parity agent: detects drift between forge adapter implementations.

Reads ``forge/base.py`` to enumerate the ``Forge`` ABC's public methods,
compares coverage across ``forge/github.py`` and ``forge/gitlab.py``,
flags single-adapter overrides and divergent implementations, and files
at most 3 draft tickets per pass.

Seam: tests monkeypatch ``run_forge_parity_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("forge_parity")


MAX_GAPS = 3


ForgeParityResult = PeriodicAgentResult


def run_forge_parity_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir: Path | None = None,
    definition_override: Any = None,
) -> ForgeParityResult:
    """Run the forge-parity detection pass.

    Reads the ``Forge`` ABC in ``forge/base.py``, then compares
    ``forge/github.py`` and ``forge/gitlab.py`` for method coverage
    and implementation divergence. Files at most ``MAX_GAPS`` (3)
    draft tickets per pass.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``), the ``explore`` scout tool, and
    the ``detect_duplication`` tool (injected at runtime via
    ``periodic_base``, following the audit pattern).

    Args:
        settings: Application configuration — model names
            (``forge_parity_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        recent_proposals: Prior proposals string from pass runner.
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``ForgeParityResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (3) entries, plus the updated memory
        ledger.
    """
    from .periodic_base import run_periodic_agent

    return run_periodic_agent(  # type: ignore[no-any-return]
        settings=settings,
        definition_name="forge_parity",
        definition_override=definition_override,
        model_setting=settings.forge_parity_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail=(
            "Read forge/base.py to enumerate the Forge ABC methods, then "
            "compare forge/github.py and forge/gitlab.py for coverage and "
            "divergence. Use detect_duplication to measure structural "
            "similarity for methods overridden by both adapters. File at "
            "most 3 draft tickets for confirmed drift."
        ),
        include_jscpd=True,
    )
