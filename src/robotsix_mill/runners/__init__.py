"""Deprecated: use ``robotsix_mill.agents.runners`` instead.

This shim exists for backward compatibility with code that still imports
from ``robotsix_mill.runners`` (e.g. runtime/worker.py string paths and
monkeypatched tests).  It will be removed in a future release.
"""

from __future__ import annotations

import importlib
import sys

# Every submodule and subpackage that used to live under runners/.
_SUBMODULES: list[str] = [
    "bespoke_runner",
    "changelog_autofill_runner",
    "credit_balance_runner",
    "data_dir_gc",
    "diagnostic_check_errors",
    "diagnostic_check_recurring_ci",
    "diagnostic_checks",
    "diagnostic_data",
    "diagnostic_events",
    "diagnostic_runner",
    "langfuse_cleanup_runner",
    "member_sync_runner",
    "orphaned_pr_check",
    "pass_runner",
    "periodic_runner",
    "pin_bump_runner",
    "repo_description_sync_runner",
    "roadmap_sync_runner",
    "run_health_runner",
    "timeout_escalation_runner",
    "trace_health_runner",
    "trace_review_runner",
    "verify_runner",
]


def _register() -> None:
    """Lazily register every legacy submodule in ``sys.modules`` so that
    ``from robotsix_mill.runners.xxx import yyy`` continues to work."""
    if getattr(_register, "_done", False):
        return
    for name in _SUBMODULES:
        new_full = f"robotsix_mill.agents.runners.{name}"
        old_full = f"robotsix_mill.runners.{name}"
        if old_full not in sys.modules:
            try:
                mod = importlib.import_module(new_full)
            except ImportError:
                continue
            sys.modules[old_full] = mod
    _register._done = True


# Re-export top-level names (run_*_pass, PeriodicPassResult, Settings, etc.).
from robotsix_mill.agents.runners import *  # noqa: E402,F403

# Register submodules on first access.  This is called at the bottom so
# that any import of ``robotsix_mill.runners`` triggers registration
# exactly once.  Eager registration is safe here because the worker
# already imports every runner at startup.
_register()
