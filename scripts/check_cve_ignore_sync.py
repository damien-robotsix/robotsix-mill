#!/usr/bin/env python3
"""Check that CVE ignore sets in ci.yml and security-audit.yml are in sync.

Usage (from the repo root):
    python scripts/check_cve_ignore_sync.py

Parses two YAML workflow files and asserts that the CVE IDs in
``audit-ignore:`` (ci.yml, forwarded to the reusable python-ci.yml) match
the ``--ignore <id>`` flags in the ``audit`` job of security-audit.yml.

Why this exists: the CVE-ignore policy is enforced in two independent
CI invocations carrying identical rationale comments — the
``audit-ignore:`` param in ci.yml:43 and three ``uv audit --ignore <id>``
flags in security-audit.yml.  They can silently drift: dropping a CVE's
ignore in one file leaves the other stale.  Because the ci.yml
``audit-ignore`` step is a gating pre-test in the consolidated
``ci / Tests`` job, a stale set there turns the entire main pipeline red.

This check gates in CI (``mill-specific`` job) so drift is caught before
merge rather than at runtime.

Exit codes:
    0 — the two CVE-ignore sets are equal.
    1 — at least one CVE ID is only in one file; details are printed to stderr.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SECURITY_AUDIT_YML = _REPO_ROOT / ".github" / "workflows" / "security-audit.yml"

# Regex extracting CVE IDs from a ``--ignore <id>`` flag.
_IGNORE_FLAG_RE = re.compile(r"--ignore\s+(\S+)")


# ---------------------------------------------------------------------------
#  Pure parse helpers (parameterised so synthetic cases need no monkeypatching)
# ---------------------------------------------------------------------------


def _parse_audit_ignore_from_ci(workflow: dict[str, Any]) -> set[str]:
    """Extract the CVE IDs from the ``audit-ignore:`` string in the ``ci`` job.

    Looks for ``jobs.ci.with.audit-ignore`` in the parsed workflow dict.
    Returns a set of CVE IDs (whitespace-trimmed).
    """
    ci_job = workflow.get("jobs", {}).get("ci", {})
    if not isinstance(ci_job, dict):
        return set()
    audit_ignore: str = ci_job.get("with", {}).get("audit-ignore", "")
    if not isinstance(audit_ignore, str) or not audit_ignore.strip():
        return set()
    return {s.strip() for s in audit_ignore.split(",") if s.strip()}


def _parse_audit_ignore_from_security_audit(workflow: dict[str, Any]) -> set[str]:
    """Extract the CVE IDs from ``--ignore <id>`` flags in the ``audit`` job.

    Looks for ``jobs.audit.steps[*].run`` blocks and scans each for
    ``--ignore <id>`` tokens.
    """
    audit_job = workflow.get("jobs", {}).get("audit", {})
    if not isinstance(audit_job, dict):
        return set()
    cve_ids: set[str] = set()
    for step in audit_job.get("steps", []):
        if not isinstance(step, dict):
            continue
        run_block = step.get("run", "")
        if not isinstance(run_block, str):
            continue
        cve_ids.update(_IGNORE_FLAG_RE.findall(run_block))
    return cve_ids


def _check_sync(ci_set: set[str], sec_set: set[str]) -> list[str]:
    """Compare two CVE-ignore sets and return drift messages."""
    drift: list[str] = []
    only_in_ci = ci_set - sec_set
    only_in_sec = sec_set - ci_set
    for cve in sorted(only_in_ci):
        drift.append(
            f"CVE {cve!r} is in ci.yml audit-ignore but "
            f"missing from security-audit.yml --ignore flags"
        )
    for cve in sorted(only_in_sec):
        drift.append(
            f"CVE {cve!r} is in security-audit.yml --ignore flags but "
            f"missing from ci.yml audit-ignore"
        )
    return drift


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def collect_drift() -> list[str]:
    """Parse both workflow files and return any CVE-ignore drift."""
    drift: list[str] = []

    for path in (_CI_YML, _SECURITY_AUDIT_YML):
        if not path.is_file():
            drift.append(f"workflow file not found: {path}")
            return drift

    try:
        ci_data = yaml.safe_load(_CI_YML.read_text())
    except yaml.YAMLError as exc:
        drift.append(f"could not parse {_CI_YML}: {exc}")
        return drift

    try:
        sec_data = yaml.safe_load(_SECURITY_AUDIT_YML.read_text())
    except yaml.YAMLError as exc:
        drift.append(f"could not parse {_SECURITY_AUDIT_YML}: {exc}")
        return drift

    if not isinstance(ci_data, dict) or not isinstance(sec_data, dict):
        drift.append("one or both workflow files did not parse as a YAML mapping")
        return drift

    ci_set = _parse_audit_ignore_from_ci(ci_data)
    sec_set = _parse_audit_ignore_from_security_audit(sec_data)

    if not ci_set:
        drift.append(
            f"no CVE IDs found in {_CI_YML.relative_to(_REPO_ROOT)} "
            f"audit-ignore (ci job 'with:' block)"
        )
    if not sec_set:
        drift.append(
            f"no CVE IDs found in {_SECURITY_AUDIT_YML.relative_to(_REPO_ROOT)} "
            f"--ignore flags (audit job steps)"
        )

    drift += _check_sync(ci_set, sec_set)
    return drift


def main() -> int:
    drift = collect_drift()
    if drift:
        for entry in drift:
            print(f"STALE: {entry}", file=sys.stderr)
        print(
            f"FAIL: {len(drift)} CVE-ignore drift item(s) detected",
            file=sys.stderr,
        )
        return 1

    print("CVE-ignore sync OK (ci.yml audit-ignore matches security-audit.yml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
