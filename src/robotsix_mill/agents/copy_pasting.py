"""The copy-paste agent: deterministic clone detection and triage.

Runs jscpd (via ``detect_duplication``) to find copy-paste duplication,
triages clone pairs by severity, cross-references against the memory
ledger and ``recent-proposals``, and files one draft ticket per
high-severity clone pair.

Seam: tests monkeypatch ``run_copy_paste_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("copy_paste")

MAX_GAPS = 8

CopyPasteResult = PeriodicAgentResult


def _copy_paste_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {
        "usage_limits": UsageLimits(request_limit=settings.copy_paste_request_limit)
    }


run_copy_paste_agent = make_agent_runner(
    definition_name="copy_paste",
    prompt_tail="Run detect_duplication, triage the clone pairs, and return your findings.",
    max_gaps=MAX_GAPS,
    include_jscpd=True,
    dynamic_kwargs_fn=_copy_paste_dynamic_kwargs,
)
