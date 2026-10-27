"""Tests for the deterministic uv-audit / pip-audit remediation plan."""

from __future__ import annotations

from robotsix_mill.stages.ci_fix_audit import (
    AuditPlan,
    audit_is_only_failure,
    audit_note,
    parse_audit_failure,
    upgrade_commands,
)

_AUDIT_JOB_LOG = """\
### Job: Tests (id=11) — failing step: Audit dependencies
Job URL: https://github.com/o/r/actions/runs/1/job/11
2026-10-27T10:00:05.1000000Z Name    Version  ID                  Fix Versions
2026-10-27T10:00:05.1000000Z certifi 2024.2.2 GHSA-248v-346w-9cwc 2024.7.4
2026-10-27T10:00:05.1000000Z mcp     1.9.0    PYSEC-2025-12       1.10.0
2026-10-27T10:00:05.2000000Z Found 2 known vulnerabilities in 2 packages
2026-10-27T10:00:06.0000000Z ##[error]Process completed with exit code 1.
"""

_TEST_JOB_LOG = """\
### Job: Tests (id=12) — failing step: Run tests
2026-10-27T10:00:05.1000000Z FAILED tests/test_x.py::test_y - AssertionError
2026-10-27T10:00:06.0000000Z ##[error]Process completed with exit code 1.
"""


def test_ghsa_line_yields_upgrade_plan():
    plan = parse_audit_failure(_AUDIT_JOB_LOG)
    assert plan == AuditPlan(
        packages=("certifi", "mcp"),
        advisories=("GHSA-248v-346w-9cwc", "PYSEC-2025-12"),
    )
    assert upgrade_commands(plan) == [
        ["uv", "lock", "--upgrade-package", "certifi", "--upgrade-package", "mcp"],
        ["uv", "lock", "--check"],
        ["uv", "sync", "--frozen", "--group", "dev"],
    ]
    assert audit_note(plan) == (
        "CI Security Audit failed on GHSA-248v-346w-9cwc, PYSEC-2025-12 in "
        "transitive dep(s) certifi, mcp; remediated via uv lock "
        "--upgrade-package; no source changes."
    )


def test_non_audit_failure_yields_no_plan():
    assert parse_audit_failure(_TEST_JOB_LOG) is None
    # The summary line alone names no package — nothing to upgrade.
    assert parse_audit_failure("Found 2 known vulnerabilities") is None


def test_audit_only_requires_every_failing_step_to_be_the_audit():
    assert audit_is_only_failure(_AUDIT_JOB_LOG, []) is True
    assert audit_is_only_failure(_AUDIT_JOB_LOG + _TEST_JOB_LOG, []) is False


def test_audit_only_falls_back_to_check_names_without_logs():
    assert audit_is_only_failure("", [{"name": "Security Audit"}]) is True
    assert (
        audit_is_only_failure("", [{"name": "Security Audit"}, {"name": "ci / tests"}])
        is False
    )
