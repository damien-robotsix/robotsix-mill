"""The bc-check agent: scans the repository for backward-compatibility
shims, no-op compat entry points, legacy property accessors, alias
assignments, default-arg compat branches, and legacy shape fallbacks —
then files draft tickets proposing cleanup for those that are ripe for
removal.

Seam: tests monkeypatch ``run_bc_check_agent``. Structured output so the
runner has a clear result to work with.
"""

from __future__ import annotations

from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("bc_check")

MAX_GAPS = 12

BcCheckResult = PeriodicAgentResult

run_bc_check_agent = make_agent_runner(
    definition_name="bc_check",
    prompt_tail="Scan the repository for backward-compatibility code and return your findings.",
    max_gaps=MAX_GAPS,
)
