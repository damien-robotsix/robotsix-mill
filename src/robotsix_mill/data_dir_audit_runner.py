"""Data-dir audit runner — periodic survey of ``.data/`` monotonic growth.

This is the scaffold (ticket 1 of the epic) for a daily periodic agent
that surveys ``.data/`` for monotonic growth and files draft tickets
when it finds problems. The actual inspection logic (top-N largest
items, growth deltas, unbounded-collection candidates, orphan
workspaces, ticket filing & dedup, rich summary) is added by child
tickets 2–7.

Until those land, ``run_data_dir_audit_pass`` is a no-op that simply
returns a ``DataDirAuditPassResult`` with empty findings.

Seam: tests monkeypatch ``robotsix_mill.data_dir_audit_runner.Settings``
to inject fake settings instances. The :class:`Settings` import below
is kept (``# noqa: F401`` is not needed since :class:`Settings` is
also instantiated at runtime) precisely to expose that attribute on
the module namespace for monkeypatching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import RepoConfig, Settings

log = logging.getLogger("robotsix_mill.data_dir_audit")


@dataclass
class DataDirAuditPassResult:
    """Result of running a data-dir audit pass."""

    drafts_created: list[dict]  # [{"id": ..., "title": ...}]
    summary: str
    updated_memory: str = ""
    session_id: str = ""


def run_data_dir_audit_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> DataDirAuditPassResult:
    """Execute one data-dir audit pass.

    This scaffold returns an empty result. Inspection logic is added
    by child tickets 2–7 of the epic.

    Args:
        session_id: Langfuse session id from the poll loop (optional).
        repo_config: Per-repo config (optional).

    Returns:
        ``DataDirAuditPassResult`` with empty findings.
    """
    # Settings is intentionally instantiated even though the scaffold
    # has no inspection logic yet — this preserves the monkeypatch
    # seam (tests stub ``robotsix_mill.data_dir_audit_runner.Settings``)
    # and surfaces any environment-variable parsing errors early.
    _settings = Settings()
    del _settings  # silence unused-local lints until child tickets wire it.

    return DataDirAuditPassResult(
        drafts_created=[],
        summary="no findings",
        updated_memory="",
        session_id=session_id,
    )
