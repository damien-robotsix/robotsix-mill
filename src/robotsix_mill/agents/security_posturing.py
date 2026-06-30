"""The security-posture agent: continuous security-scanning coverage oversight.

Inspects CI workflows and pre-commit config, compares against evolving
OWASP/OpenSSF/SLSA best practices, and proposes draft tickets for missing
security scanning layers and outdated tool versions.

Seam: tests monkeypatch ``run_security_posture_agent``. Structured output
so the runner has a clear result to work with.
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
SYSTEM_PROMPT: str = load_periodic_system_prompt("security_posture")

MAX_GAPS = 5

SecurityPostureResult = PeriodicAgentResult


def _security_posture_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {
        "usage_limits": UsageLimits(
            request_limit=settings.security_posture_request_limit,
        ),
    }


run_security_posture_agent = make_agent_runner(
    definition_name="security_posture",
    prompt_tail="Perform the security-posture inspection and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    include_run_command=True,
    dynamic_kwargs_fn=_security_posture_dynamic_kwargs,
)
