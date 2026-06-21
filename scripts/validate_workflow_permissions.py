#!/usr/bin/env python3
"""Validate that workflow jobs calling a SARIF-uploading reusable workflow
grant ``security-events: write``.

Usage (from the repo root):
    python scripts/validate_workflow_permissions.py

Why this exists: a job that ``uses:`` a reusable workflow whose nested jobs
upload SARIF (``python-ci.yml`` / ``python-security.yml``) must itself grant
``permissions: security-events: write``. If it doesn't, GitHub rejects the run
with a 0-second ``startup_failure`` *before any job runs* — so there are no job
logs to diagnose, and the auto-fixer tends to half-fix it (GitHub's validation
names only the FIRST offending job, so granting the scope to that one job just
surfaces the same error on the next caller). This check catches every offending
caller job at once, in pre-commit and CI, before it can dark a repo's CI.

A caller job is OK when the scope is granted either on the job itself or
inherited from the workflow's top-level ``permissions`` (a job's own
``permissions`` block fully replaces the top-level one, so an overriding block
that omits the scope is NOT ok). ``write-all`` counts as granting it.

Exit codes:
    0 — every reusable-workflow caller job grants ``security-events: write``.
    1 — at least one caller job is missing the grant; details go to stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Reusable workflows whose nested jobs upload SARIF, so any CALLER job must
# itself grant ``security-events: write``. Cross-repo ``uses:`` references can't
# be introspected, so the well-known ones are listed by basename; local
# reusable workflows are additionally auto-discovered (see _required_workflows).
_KNOWN_SARIF_REUSABLE = {"python-ci.yml", "python-security.yml"}


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        print(f"::error::could not parse {path}: {exc}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def _grants_sarif(perms: Any) -> bool:
    """Whether a ``permissions`` value grants ``security-events: write``."""
    if perms == "write-all":
        return True
    return isinstance(perms, dict) and perms.get("security-events") == "write"


def _reusable_basename(uses: str) -> str | None:
    """The reusable-workflow filename in a job-level ``uses:``, or None.

    Handles ``./.github/workflows/x.yml`` and
    ``owner/repo/.github/workflows/x.yml@<ref>``.
    """
    if ".github/workflows/" not in uses:
        return None
    return uses.split("@", 1)[0].rsplit("/", 1)[-1]


def _required_workflows() -> set[str]:
    """Reusable-workflow basenames whose callers must grant the SARIF scope:
    the well-known set plus any LOCAL reusable workflow that declares
    ``security-events: write`` on one of its own jobs."""
    required = set(_KNOWN_SARIF_REUSABLE)
    for wf in _WORKFLOWS_DIR.glob("*.y*ml"):
        data = _load(wf)
        for job in (data or {}).get("jobs", {}).values():
            if isinstance(job, dict) and _grants_sarif(job.get("permissions")):
                required.add(wf.name)
                break
    return required


def main() -> int:
    if not _WORKFLOWS_DIR.is_dir():
        return 0
    required = _required_workflows()
    problems: list[str] = []
    for wf in sorted(_WORKFLOWS_DIR.glob("*.y*ml")):
        data = _load(wf)
        if data is None:
            continue
        top_perms = data.get("permissions")
        for job_name, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict) or not isinstance(job.get("uses"), str):
                continue
            if _reusable_basename(job["uses"]) not in required:
                continue
            # A job's own permissions block fully replaces the top-level one;
            # otherwise the job inherits the workflow-level permissions.
            effective = job["permissions"] if "permissions" in job else top_perms
            if not _grants_sarif(effective):
                problems.append(
                    f"{wf.relative_to(_REPO_ROOT)}: job '{job_name}' calls "
                    f"{_reusable_basename(job['uses'])} but does not grant "
                    f"`security-events: write`"
                )

    if problems:
        print(
            "Each workflow job that calls a SARIF-uploading reusable workflow "
            "must grant `permissions: security-events: write` (on the job or "
            "the workflow's top level), or GitHub rejects the whole run with a "
            "0-second startup_failure. Offending jobs:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
