"""CIPollMixin: CI polling and auto-merge eligibility for the merge stage.

Handles the IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL, and WAITING_AUTO_MERGE
poll paths: checks PR mergeability, CI status, auto-merge eligibility, and
routes to FIXING_CI / REBASING / WAITING_AUTO_MERGE as appropriate.
"""

from __future__ import annotations

import contextlib
import datetime
import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ...config import target_branch_for
from ...core.block_reason import TARGET_BRANCH_RED, encode
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...forge import Forge, get_forge
from ...stages.ci_transient import is_transient_ci_failure
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _APPROVED_DIFF_HASH,
    _AUTO_FIX_CYCLES,
    _CI_POLL_REFRESH_SHA,
    _EMPTY_ROLLUP_COUNT,
    _EMPTY_ROLLUP_SELF_HEAL_DONE,
    _GREEN_UNPROMOTABLE_COUNT,
    _LAST_AUTO_FIX_STAGE,
    _PING_PONG_COUNT,
    _PR_MISSING_COUNT,
    _REBASE_COUNTER,
    _REBASE_FROM_STATE,
    _REBASE_LAST_TS,
    _ci_truly_green,
    _is_pr_check_run,
    _latest_failing_workflows,
    _merge_rejection_outcome,
    _next_consecutive,
    _read_counter,
    _refresh_branch_for_ci,
    _reset_consecutive,
    _verify_merge_ancestor,
    _workspace_repo_dir,
    _write_counter,
    log,
)