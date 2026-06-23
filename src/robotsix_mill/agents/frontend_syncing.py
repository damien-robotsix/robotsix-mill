"""The frontend-sync agent: detects drift between Python enum values and
their mirrored frontend representations in CSS selectors and JS maps.

Cross-references State and SourceKind enum members against .s-* and .src-*
CSS class selectors in board-mill.css, and against SOURCE_CLASS, STATE_TRACE,
and AGENT_COLORS maps in board-mill.js. Files draft tickets for missing,
stale, or mismatched frontend representations.
"""

from __future__ import annotations

from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

SYSTEM_PROMPT: str = load_periodic_system_prompt("frontend_sync")

MAX_GAPS = 5

FrontendSyncResult = PeriodicAgentResult

run_frontend_sync_agent = make_agent_runner(
    definition_name="frontend_sync",
    prompt_tail=(
        "Read the canonical Python enums (State in states.py, SourceKind in "
        "models.py), then cross-reference against CSS selectors in "
        "board-mill.css and JS maps in board-mill.js. File at most 5 draft "
        "tickets for confirmed drift."
    ),
    max_gaps=MAX_GAPS,
)
