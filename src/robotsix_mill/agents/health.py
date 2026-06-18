"""The health agent: codebase-health inspection for module size,
function length, documentation coverage, test gaps, complexity
hotspots, and dead code.

Seam: tests monkeypatch ``run_health_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("health")

MAX_GAPS = 8

HealthResult = PeriodicAgentResult

run_health_agent = make_agent_runner(
    definition_name="health",
    prompt_tail="Perform the health inspection and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
)
