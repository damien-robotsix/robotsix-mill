"""Refine stage: raw DRAFT -> actionable READY ticket.

Rewrites the file-canonical ``description.md`` into a precise spec the
implement agent can act on unattended; the original draft is kept as an
artifact for traceability. Empty draft or missing OpenRouter key ->
BLOCKED with a clear note (not a crash).

When ``require_approval`` is true (the default), the refined ticket
enters ``human_issue_approval`` instead of ``ready`` — a human must approve
before the implement stage picks it up.

Before the expensive refine agent runs, a cheap **dedup / already-done
check** inspects the draft against existing tickets. If the draft is a
clear duplicate or the change is already covered by a recently-closed
ticket, the ticket is short-circuited to ``CLOSED`` — no refiner, no
human approval gate, no wasted cost.

This module is a thin façade (Pattern A) over the ``refine`` package:

- ``helpers`` — module-level constants/regexes and pure functions
- ``gates`` — :class:`RefineGatesMixin` (pre-refine guard phases)
- ``orchestration`` — :class:`RefineAgentMixin` (refine-agent pipeline)
- ``core`` — :class:`RefineStage` (the assembled ``Stage`` subclass)

The agent submodules are bound as package attributes (``dedup``,
``freshness``, ``obsolescence``, ``refining``) so the dotted-string
monkeypatch targets used by the tests (e.g.
``robotsix_mill.stages.refine.dedup.run_dedup_check``) resolve to the same
module objects the gate/orchestration code calls through.
"""

from __future__ import annotations

# Bind the agent submodules as package attributes so the existing
# dotted-string monkeypatch targets (``robotsix_mill.stages.refine.dedup``,
# ``...refine.refining``, etc.) resolve to the same module objects the
# gate/orchestration code calls through.
from ...agents import dedup, freshness, obsolescence, refining
from ...agents.runners.pass_runner import load_memory, persist_memory
from ...forge.auth import _resolve_remote_url
from .core import RefineStage
from .helpers import (
    _AUTO_APPROVE_SOURCES,
    _TRIAGE_REJECTION_PATTERNS,
    DEDUP_ALREADY_DONE_PREFIX,
    DEDUP_DUPLICATE_PREFIX,
    FRESHNESS_STALE_PREFIX,
    NON_IMPLEMENTATION_CLOSE_PREFIXES,
    OBSOLESCENCE_GAP_PREFIX,
    OPERATOR_SENDBACK_PREFIX,
    REFINE_PROGRESS_STATES,
    UNMERGED_BRANCH_PREFIX,
    _build_candidates_block,
    _build_deployed_log_summary,
    _draft_has_complete_spec,
    _human_size,
    _is_doc_only_change,
    _rationale_claims_external_fix,
    _resolve_next_state,
    _spec_is_degenerate,
    _tail_file,
    _verify_branch_merged,
    _verify_cited_fix_at_head,
    log,
    verify_claim,
)

__all__ = [
    "DEDUP_ALREADY_DONE_PREFIX",
    # constants
    "DEDUP_DUPLICATE_PREFIX",
    "FRESHNESS_STALE_PREFIX",
    "NON_IMPLEMENTATION_CLOSE_PREFIXES",
    "OBSOLESCENCE_GAP_PREFIX",
    "OPERATOR_SENDBACK_PREFIX",
    "REFINE_PROGRESS_STATES",
    "UNMERGED_BRANCH_PREFIX",
    "_AUTO_APPROVE_SOURCES",
    "_TRIAGE_REJECTION_PATTERNS",
    "RefineStage",
    "_build_candidates_block",
    # pure helpers
    "_build_deployed_log_summary",
    "_draft_has_complete_spec",
    "_human_size",
    "_is_doc_only_change",
    "_rationale_claims_external_fix",
    "_resolve_next_state",
    "_resolve_remote_url",
    "_spec_is_degenerate",
    "_tail_file",
    "_verify_branch_merged",
    "_verify_cited_fix_at_head",
    # agent submodules (bound for monkeypatch targets)
    "dedup",
    "freshness",
    # patchable seams (re-exported so ``refine_module.<name>`` patches resolve)
    "load_memory",
    "log",
    "obsolescence",
    "persist_memory",
    "refining",
    "verify_claim",
]
