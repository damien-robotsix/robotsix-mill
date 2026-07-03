"""The repo-description-sync agent: keep forge description in sync with README.

Reads the repository's README, compares it against the current forge
description, and returns a judgment (``should_update`` / ``new_description``).
The runner handles the actual ``Forge.update_repo()`` call based on the
agent's output.

This agent does NOT file draft tickets — it returns a binary judgment.
The runner acts on that judgment directly.
"""

from __future__ import annotations

from pydantic import Field

from .periodic_base import PeriodicAgentResult


class RepoDescriptionSyncResult(PeriodicAgentResult):
    """Structured output for the repo-description-sync agent.

    Inherits ``draft_titles``, ``draft_bodies``, ``gap_ids``,
    ``updated_memory``, and ``summary`` from ``PeriodicAgentResult``
    (required by the clipping logic in ``run_periodic_agent``).

    Adds two fields that the runner reads after the agent completes:
    """

    should_update: bool = Field(
        default=False,
        description=(
            "True when the forge description is empty/a placeholder or "
            "materially inaccurate and a replacement is proposed."
        ),
    )
    new_description: str = Field(
        default="",
        description=(
            "The proposed replacement description (under 350 characters) "
            "when should_update is True; empty string otherwise."
        ),
    )
