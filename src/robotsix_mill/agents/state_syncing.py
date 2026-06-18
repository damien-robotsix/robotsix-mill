"""The state-sync agent: cross-surface State enum consistency validation.

Cross-references every State member against all string-literal reference
sites in the codebase — prompts, YAML configs, transition tables, docs,
tests — and files draft tickets for staleness, typos, and missing references.
"""

from __future__ import annotations

from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

SYSTEM_PROMPT: str = load_periodic_system_prompt("state_sync")

MAX_GAPS = 5

StateSyncResult = PeriodicAgentResult

run_state_sync_agent = make_agent_runner(
    definition_name="state_sync",
    prompt_tail="Perform the state-sync consistency inspection and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
)
