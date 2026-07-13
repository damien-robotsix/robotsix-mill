"""The forge-parity agent: detects drift between forge adapter implementations.

Reads ``forge/base.py`` to enumerate the ``Forge`` ABC's public methods,
compares coverage across ``forge/github.py`` and ``forge/gitlab/core.py``,
flags single-adapter overrides and divergent implementations, and files
at most 3 draft tickets per pass.

Seam: tests monkeypatch ``run_forge_parity_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("forge_parity")

MAX_GAPS = 3

ForgeParityResult = PeriodicAgentResult

run_forge_parity_agent = make_agent_runner(
    definition_name="forge_parity",
    prompt_tail=(
        "Read forge/base.py to enumerate the Forge ABC methods, then "
        "compare forge/github.py and forge/gitlab/core.py for coverage and "
        "divergence. Use detect_duplication to measure structural "
        "similarity for methods overridden by both adapters. File at "
        "most 3 draft tickets for confirmed drift."
    ),
    max_gaps=MAX_GAPS,
    include_jscpd=True,
)
