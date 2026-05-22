"""Pre-merge guard: fail when core schema files change but db.py is
not touched, ensuring _run_migrations stays in sync with the schema.

This is a CI-enforced gate, not a pre-commit hook.  It inspects the
git diff between the current branch and the merge base and fails when
``states.py`` or ``models.py`` changed without a corresponding change
to ``db.py``.
"""

from __future__ import annotations

import os
import subprocess

import pytest

SCHEMA_FILES = [
    "src/robotsix_mill/core/states.py",
    "src/robotsix_mill/core/models.py",
]
DB_FILE = "src/robotsix_mill/core/db.py"


def _run_git(*args: str) -> tuple[int, str]:
    """Run a git command; return (returncode, stdout-stripped)."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""


def _in_git_repo() -> bool:
    rc, _ = _run_git("rev-parse", "--git-dir")
    return rc == 0


def test_migration_guard() -> None:
    """Fail when schema files (states.py / models.py) change without a
    corresponding change to db.py."""

    # -- override mechanisms ------------------------------------------------
    if os.environ.get("SKIP_MIGRATION_GUARD") == "1":
        pytest.skip("SKIP_MIGRATION_GUARD=1 is set")

    if not _in_git_repo():
        pytest.skip("Not in a git repository; migration guard is a CI check")

    base_ref = os.environ.get("GIT_BASE_REF", "origin/main")

    # Check for the override token in any commit in the range.
    rc, log_output = _run_git(
        "log", "--oneline", f"{base_ref}..HEAD", "--grep=no-migration-needed",
    )
    if rc == 0 and log_output:
        pytest.skip(
            "Found 'no-migration-needed' in commit message(s); "
            "skipping migration guard."
        )

    # -- find the merge base ------------------------------------------------
    rc, merge_base = _run_git("merge-base", base_ref, "HEAD")
    if rc != 0:
        pytest.skip(
            f"Cannot find merge base for '{base_ref}'; "
            "migration guard requires a base ref to compare against."
        )

    # -- collect changed files ----------------------------------------------
    rc, diff_output = _run_git("diff", "--name-only", f"{merge_base}..HEAD")
    if rc != 0:
        pytest.skip("git diff failed; cannot determine changed files")

    changed: set[str] = set(diff_output.split("\n")) if diff_output else set()

    schema_changed = [f for f in SCHEMA_FILES if f in changed]
    db_changed = DB_FILE in changed

    if schema_changed and not db_changed:
        schema_names = ", ".join(f.rsplit("/", 1)[-1] for f in schema_changed)
        pytest.fail(
            f"Schema file(s) changed ({schema_names}) but db.py was NOT "
            "touched. If _run_migrations truly needs no update, include "
            "'no-migration-needed' in a commit message or set "
            "SKIP_MIGRATION_GUARD=1. Otherwise add the required migration "
            "to _run_migrations in core/db.py."
        )
