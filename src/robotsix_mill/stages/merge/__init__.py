"""Merge stage: IMPLEMENT_COMPLETE -> HUMAN_MR_APPROVAL (gates passed)
                     -> DONE (merged) | BLOCKED (closed unmerged)
                     -> FIXING_CI (failing CI, deferred)
                     -> REBASING (conflicting, deferred)

HUMAN_MR_APPROVAL -> DONE (merged) | BLOCKED (closed unmerged)
              -> IMPLEMENT_COMPLETE (gate degradation — silent fallback)
              -> WAITING_AUTO_MERGE (eligible, CI pending)

REBASING -> IMPLEMENT_COMPLETE (rebase succeeded, re-verify gates)

FIXING_CI -> IMPLEMENT_COMPLETE (fix succeeded, re-verify gates)

The PR is the review. This stage is re-run by the worker's lightweight
poll while the ticket sits in IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL,
REBASING, FIXING_CI, or WAITING_AUTO_MERGE; it checks the forge:

IMPLEMENT_COMPLETE (gate-check):
- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> check CI status:
    - failing CI    -> FIXING_CI (auto-fix agent)
    - green CI      -> HUMAN_MR_APPROVAL (gates passed! notify human)
    - pending CI    -> IMPLEMENT_COMPLETE (no-op; re-poll)
- open, conflicting -> REBASING (defer rebase agent)

HUMAN_MR_APPROVAL:
- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> check CI status:
    - failing CI    -> IMPLEMENT_COMPLETE (silent fallback)
    - green CI      -> HUMAN_MR_APPROVAL (no-op; re-poll)
    - pending CI    -> HUMAN_MR_APPROVAL (no-op; re-poll)
- open, conflicting -> IMPLEMENT_COMPLETE (silent fallback)

Returning the *same* state is the worker's "leave it, re-poll" signal —
no history spam, no busy loop.

This module is a thin façade (Pattern A) over the ``merge`` package:

- ``_shared`` — module-level helpers, constants, and the package ``log``
- ``core`` — :class:`MergeStage` (run dispatch + shared class-level helpers)
- ``ci_fix_mixin`` — :class:`MultiRepoCiFixMixin` (inline CI-fix recovery)
- ``multi_repo`` — :class:`MultiRepoMixin`
- ``ci_poll`` — :class:`CIPollMixin`
- ``rebase`` — :class:`RebaseMixin`
- ``review_revision`` — :class:`ReviewRevisionMixin`

The patchable seams (``git_ops``, ``tracing``, ``run_rebase_agent``,
``run_ci_fix_agent``, ``run_review_revision_agent``) are bound as
package attributes so the existing test monkeypatches
(``robotsix_mill.stages.merge.git_ops``, ``merge_mod.run_rebase_agent``,
etc.) intercept the real call sites.
"""

from __future__ import annotations

# Sub-module references re-exported for backward-compatible monkeypatching
# (tests use ``merge_mod.git_ops``, ``merge_mod.tracing``, etc.).
from ...agents.ci_fixing import run_ci_fix_agent
from ...agents.rebasing import run_rebase_agent
from ...agents.review_revision import run_review_revision_agent
from ...forge.auth import _resolve_remote_url, github_token
from ...runners.pass_runner import load_memory, persist_memory
from ...runtime import tracing
from ...vcs import git_ops

from .core import MergeStage
from ._shared import (
    _MERGE_REASON,
    _REBASE_COUNTER,
    _REV_REV_COUNTER,
    _build_failing_summary,
    _duplicate_changelog_fragments,
    _is_pr_check_run,
    _latest_failing_workflows,
    _load_pr_urls,
    _read_counter,
    _read_reason,
    _repo_config_for_entry,
    _verify_merge_ancestor,
    _workspace_repo_dir,
    _write_counter,
    _write_reason,
    log,
)

__all__ = [
    "MergeStage",
    # agent seams (re-exported so ``merge_mod.<name>`` monkeypatches resolve)
    "run_ci_fix_agent",
    "run_rebase_agent",
    "run_review_revision_agent",
    # sub-module references (re-exported for ``merge_mod.git_ops``, etc.)
    "git_ops",
    "tracing",
    # patchable seams
    "_resolve_remote_url",
    "github_token",
    "load_memory",
    "persist_memory",
    # constants
    "_REBASE_COUNTER",
    "_MERGE_REASON",
    "_REV_REV_COUNTER",
    "log",
    # helpers
    "_load_pr_urls",
    "_repo_config_for_entry",
    "_read_counter",
    "_write_counter",
    "_build_failing_summary",
    "_read_reason",
    "_write_reason",
    "_workspace_repo_dir",
    "_verify_merge_ancestor",
    "_duplicate_changelog_fragments",
    "_latest_failing_workflows",
    "_is_pr_check_run",
]
