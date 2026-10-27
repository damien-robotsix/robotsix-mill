"""Deterministic remediation of a ``uv audit`` / ``pip-audit`` CVE gate.

When the failing CI step is the dependency-audit gate flagging a
(usually transitive) package, the fix is mechanical:
``uv lock --upgrade-package <pkg>`` for each flagged package, verified by
``uv lock --check`` and ``uv sync --frozen --group dev``. ``CIFixStage``
applies it before any agent turn (see ``_try_deterministic_audit_pass``).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("robotsix_mill.ci_fix")

_ADVISORY = r"(?:GHSA(?:-[0-9a-z]{4}){3}|PYSEC-\d{4}-\d+|CVE-\d{4}-\d+)"
_ADVISORY_RE = re.compile(_ADVISORY, re.IGNORECASE)
# ``<pkg> <version> … <advisory>`` on one line (pip-audit table rows,
# ``uv audit`` findings). The version must start with a digit so prose like
# "Found 2 known vulnerabilities" never parses as a package.
_PKG_ADVISORY_LINE_RE = re.compile(
    r"^[\s\-*|]*(?P<pkg>[A-Za-z0-9][A-Za-z0-9._-]*)[\s|:=@]+v?(?P<ver>\d[\w.+!-]*)"
    rf"\b.*?(?P<adv>{_ADVISORY})",
    re.IGNORECASE,
)
_FOUND_RE = re.compile(r"Found \d+ known vulnerabilit", re.IGNORECASE)
# GitHub Actions log-line timestamp prefix.
_TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")
# Job-log section header from ``GitHubForgeCIMixin._fetch_workflow_job_logs``.
_JOB_HEADER_RE = re.compile(r"^### Job: .*$", re.MULTILINE)
# Check-run names that ARE the audit gate (when no logs are available).
_AUDIT_CHECK_TOKENS = ("audit",)


@dataclass(frozen=True)
class AuditPlan:
    """Packages to ``uv lock --upgrade-package`` and the advisories behind them."""

    packages: tuple[str, ...]
    advisories: tuple[str, ...]


def parse_audit_failure(text: str) -> AuditPlan | None:
    """Return the upgrade plan when *text* carries the audit-gate signature.

    The signature is at least one ``<pkg> <version> … <advisory-id>`` line;
    ``Found N known vulnerabilities`` alone (no package named) is not
    actionable and yields ``None``.
    """
    packages: list[str] = []
    advisories: list[str] = []
    for raw in text.splitlines():
        line = _TS_PREFIX_RE.sub("", raw)
        m = _PKG_ADVISORY_LINE_RE.match(line)
        if m is None:
            continue
        pkg = m.group("pkg").lower()
        if pkg not in packages:
            packages.append(pkg)
        for adv in _ADVISORY_RE.findall(line):
            # Canonical forms: GHSA-xxxx-xxxx-xxxx, PYSEC-…, CVE-….
            adv = "GHSA" + adv[4:].lower() if adv[:4].upper() == "GHSA" else adv.upper()
            if adv not in advisories:
                advisories.append(adv)
    if not packages:
        return None
    return AuditPlan(tuple(packages), tuple(advisories))


def has_audit_signature(text: str) -> bool:
    """True when *text* shows the audit gate failing."""
    return parse_audit_failure(text) is not None or bool(_FOUND_RE.search(text))


def audit_is_only_failure(logs: str, failing: list[dict[str, Any]]) -> bool:
    """True when the audit gate is the ONLY failing step.

    With job logs, every failed-job section must carry the audit signature
    (a job whose failing step is something else — tests, lint — keeps the
    ticket open). Without sections, fall back to the failing check names.
    """
    headers = list(_JOB_HEADER_RE.finditer(logs))
    if headers:
        for i, h in enumerate(headers):
            end = headers[i + 1].start() if i + 1 < len(headers) else len(logs)
            if not has_audit_signature(logs[h.start() : end]):
                return False
        return True
    names = [(c.get("name") or "").lower() for c in failing]
    return bool(names) and all(
        any(tok in n for tok in _AUDIT_CHECK_TOKENS) for n in names
    )


def audit_note(plan: AuditPlan) -> str:
    """The standard commit/history note for a lock-bump remediation."""
    ids = ", ".join(plan.advisories) or "unknown advisory"
    pkgs = ", ".join(plan.packages)
    return (
        f"CI Security Audit failed on {ids} in transitive dep(s) {pkgs}; "
        "remediated via uv lock --upgrade-package; no source changes."
    )


def upgrade_commands(plan: AuditPlan) -> list[list[str]]:
    """The deterministic command sequence for *plan*."""
    lock = ["uv", "lock"]
    for pkg in plan.packages:
        lock += ["--upgrade-package", pkg]
    return [
        lock,
        ["uv", "lock", "--check"],
        ["uv", "sync", "--frozen", "--group", "dev"],
    ]


def apply_upgrade(repo_dir: str, plan: AuditPlan) -> bool:
    """Run :func:`upgrade_commands` in *repo_dir*; ``False`` on any failure."""
    for cmd in upgrade_commands(plan):
        try:
            # Fixed argv resolved from PATH, no shell, package names parsed
            # from a strict identifier regex.
            proc = subprocess.run(  # noqa: S603
                cmd,
                cwd=repo_dir,
                check=False,
                capture_output=True,
                timeout=600,
            )
        except OSError, subprocess.SubprocessError:
            log.warning("audit remediation: %s could not run", cmd, exc_info=True)
            return False
        if proc.returncode != 0:
            log.warning(
                "audit remediation: %s exited %d: %s",
                cmd,
                proc.returncode,
                (proc.stderr or b"")[-2000:].decode(errors="replace"),
            )
            return False
    return True
