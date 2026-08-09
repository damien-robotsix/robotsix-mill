"""Regression tests for scripts/check_cve_ignore_sync.py.

Covers:
    * Happy path against the real on-disk workflow files — zero drift.
    * Synthetic violations for each parsing and comparison function.
"""

from __future__ import annotations

from pathlib import Path

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_cve_ignore_sync.py"

_checker = load_script(_SCRIPT_PATH)

_parse_audit_ignore_from_ci = _checker._parse_audit_ignore_from_ci
_parse_audit_ignore_from_security_audit = (
    _checker._parse_audit_ignore_from_security_audit
)
_check_sync = _checker._check_sync
collect_drift = _checker.collect_drift


# ---------------------------------------------------------------------------
#  Happy path — real repo state
# ---------------------------------------------------------------------------


def test_real_repo_has_no_cve_ignore_drift() -> None:
    drift = collect_drift()
    assert drift == [], f"CVE-ignore drift detected: {drift}"


# ---------------------------------------------------------------------------
#  ci.yml parsing
# ---------------------------------------------------------------------------


def test_parse_audit_ignore_from_ci_normal() -> None:
    workflow = {
        "jobs": {
            "ci": {
                "with": {
                    "audit-ignore": "PYSEC-2025-183, MAL-2026-4750, GHSA-9xwg-3r6f-jcx2"
                }
            }
        }
    }
    result = _parse_audit_ignore_from_ci(workflow)
    assert result == {"PYSEC-2025-183", "MAL-2026-4750", "GHSA-9xwg-3r6f-jcx2"}


def test_parse_audit_ignore_from_ci_handles_whitespace() -> None:
    workflow = {
        "jobs": {"ci": {"with": {"audit-ignore": " PYSEC-2025-183 ,  MAL-2026-4750 "}}}
    }
    result = _parse_audit_ignore_from_ci(workflow)
    assert result == {"PYSEC-2025-183", "MAL-2026-4750"}


def test_parse_audit_ignore_from_ci_missing_key() -> None:
    workflow = {"jobs": {"ci": {"with": {}}}}
    assert _parse_audit_ignore_from_ci(workflow) == set()


def test_parse_audit_ignore_from_ci_no_ci_job() -> None:
    assert _parse_audit_ignore_from_ci({}) == set()


# ---------------------------------------------------------------------------
#  security-audit.yml parsing
# ---------------------------------------------------------------------------


def test_parse_audit_ignore_from_security_audit_normal() -> None:
    workflow = {
        "jobs": {
            "audit": {
                "steps": [
                    {
                        "name": "Audit dependencies",
                        "run": (
                            "uv audit --frozen \\\n"
                            "  --ignore PYSEC-2025-183 \\\n"
                            "  --ignore MAL-2026-4750 \\\n"
                            "  --ignore GHSA-9xwg-3r6f-jcx2\n"
                        ),
                    }
                ]
            }
        }
    }
    result = _parse_audit_ignore_from_security_audit(workflow)
    assert result == {"PYSEC-2025-183", "MAL-2026-4750", "GHSA-9xwg-3r6f-jcx2"}


def test_parse_audit_ignore_from_security_audit_no_audit_job() -> None:
    assert _parse_audit_ignore_from_security_audit({}) == set()


def test_parse_audit_ignore_from_security_audit_empty_steps() -> None:
    workflow = {"jobs": {"audit": {"steps": []}}}
    assert _parse_audit_ignore_from_security_audit(workflow) == set()


def test_parse_audit_ignore_from_security_audit_ignores_non_run_steps() -> None:
    workflow = {
        "jobs": {
            "audit": {
                "steps": [
                    {"name": "Checkout", "uses": "actions/checkout@v4"},
                    {
                        "name": "Audit dependencies",
                        "run": "uv audit --frozen --ignore PYSEC-2025-183",
                    },
                ]
            }
        }
    }
    result = _parse_audit_ignore_from_security_audit(workflow)
    assert result == {"PYSEC-2025-183"}


# ---------------------------------------------------------------------------
#  Sync check
# ---------------------------------------------------------------------------


def test_check_sync_equal_sets() -> None:
    ci_set = {"PYSEC-2025-183", "MAL-2026-4750"}
    sec_set = {"PYSEC-2025-183", "MAL-2026-4750"}
    assert _check_sync(ci_set, sec_set) == []


def test_check_sync_only_in_ci() -> None:
    ci_set = {"PYSEC-2025-183", "MAL-2026-4750"}
    sec_set = {"PYSEC-2025-183"}
    drift = _check_sync(ci_set, sec_set)
    assert len(drift) == 1
    assert "MAL-2026-4750" in drift[0]
    assert "ci.yml" in drift[0]
    assert "security-audit.yml" in drift[0]


def test_check_sync_only_in_sec() -> None:
    ci_set = {"PYSEC-2025-183"}
    sec_set = {"PYSEC-2025-183", "MAL-2026-4750"}
    drift = _check_sync(ci_set, sec_set)
    assert len(drift) == 1
    assert "MAL-2026-4750" in drift[0]
    assert "security-audit.yml" in drift[0]
    assert "ci.yml" in drift[0]


def test_check_sync_both_directions() -> None:
    ci_set = {"A-1", "B-2"}
    sec_set = {"B-2", "C-3"}
    drift = _check_sync(ci_set, sec_set)
    assert len(drift) == 2
    assert any("A-1" in d and "ci.yml" in d for d in drift)
    assert any("C-3" in d and "security-audit.yml" in d for d in drift)
