"""The triage-boilerplate agent: identifies recurring triage patterns
and proposes boilerplate response templates.

Seam: tests monkeypatch ``run_triage_boilerplate_agent``. Structured
output so the runner has a clear result to work with.
"""

from __future__ import annotations

from .periodic_base import (
    PeriodicAgentResult,  # noqa: F401 — accessed via getattr by build_agent_from_definition
    load_periodic_system_prompt,
    make_agent_runner,
)

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("triage_boilerplate")

MAX_GAPS = 8

run_triage_boilerplate_agent = make_agent_runner(
    definition_name="triage_boilerplate",
    prompt_tail="Scan recent triage tickets for recurring patterns and return your findings.",
    max_gaps=MAX_GAPS,
)
