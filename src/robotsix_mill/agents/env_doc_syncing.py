"""The env-doc-sync agent: env-var documentation consistency validation.

Cross-references every env-var name declared across the Pydantic Settings
mixins and secrets.py against docs/config/configuration.md, and files draft tickets
for documentation discrepancies (missing-from-docs, stale-in-docs,
alias-mismatch, default-mismatch, secrets-gap).
"""

from __future__ import annotations

from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

SYSTEM_PROMPT: str = load_periodic_system_prompt("env_doc_sync")

MAX_GAPS = 5

EnvDocSyncResult = PeriodicAgentResult

run_env_doc_sync_agent = make_agent_runner(
    definition_name="env_doc_sync",
    prompt_tail="Perform the env-doc-sync consistency inspection and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    include_write_file=True,
)
