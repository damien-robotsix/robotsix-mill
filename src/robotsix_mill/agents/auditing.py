"""The audit agent: meta-audit to identify gaps in quality/security
tooling coverage.

Seam: tests monkeypatch ``run_audit_agent``. Structured output so
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
SYSTEM_PROMPT: str = load_periodic_system_prompt("audit")

MAX_GAPS = 5

AuditResult = PeriodicAgentResult


def _audit_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {"usage_limits": UsageLimits(request_limit=settings.audit_request_limit)}


run_audit_agent = make_agent_runner(
    definition_name="audit",
    prompt_tail="Perform the audit and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    include_jscpd=True,
    include_workflow_caller_audit=True,
    include_run_command=True,
    include_write_file=True,
    dynamic_kwargs_fn=_audit_dynamic_kwargs,
)
