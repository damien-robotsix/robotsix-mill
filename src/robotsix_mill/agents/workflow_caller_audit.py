"""``audit_workflow_callers`` — deterministic detection of broken
reusable-workflow callers in a member repo's ``.github/workflows/``.

Member repos repeatedly break their CI when wiring a caller for mill's
shared reusable workflows
(``damien-robotsix/robotsix-mill/.github/workflows/python-ci.yml`` /
``python-docs.yml``).  Two mistakes each produce a ``startup_failure``
that turns ``main`` red and masks every real gate:

1. **Wrong org** — ``uses: robotsix/robotsix-mill/...`` (the org is
   ``damien-robotsix``, not ``robotsix``).
2. **Missing permissions grant** — the reusable ``python-ci.yml`` job
   declares ``permissions: {contents: read, security-events: write}``;
   a calling job that grants no (or an insufficient) ``permissions:``
   block makes the reusable workflow request a permission the caller
   cannot provide → ``startup_failure``.

This module provides a pure, unit-testable detector — no LLM, no
network — used as a deterministic tool the audit agent invokes on a
cloned member repo.  Mill's own ``ci.yml`` uses the LOCAL path
(``./.github/workflows/python-ci.yml``) because it hosts the workflow;
local refs are mill-internal and intentionally ignored.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Offense kinds.
WRONG_ORG = "WRONG_ORG"
MISSING_PERMISSION = "MISSING_PERMISSION"

# The one true org that owns ``robotsix-mill``.
CANONICAL_ORG = "damien-robotsix"

# Permissions each reusable workflow requires its CALLER to grant on the
# job that invokes it.  Keyed by reusable-workflow filename.  Values are
# the minimum access level for each scope.
_REQUIRED_PERMISSIONS: dict[str, dict[str, str]] = {
    "python-ci.yml": {"contents": "read", "security-events": "write"},
    "python-docs.yml": {"contents": "write"},
}

# Ordering of GitHub permission access levels (write implies read).
_LEVELS: dict[str, int] = {"none": 0, "read": 1, "write": 2}

# Cross-repo ``uses:`` reference to a ``robotsix-mill`` reusable workflow.
# Local refs (``./.github/workflows/...``) and Docker refs (``docker://``)
# do not match — they carry no ``<org>/robotsix-mill/`` prefix.
_USES_LINE_RE = re.compile(
    r"uses:\s*['\"]?"
    r"(?P<org>[\w.-]+)/robotsix-mill/\.github/workflows/"
    r"(?P<wf>[\w.-]+\.ya?ml)@(?P<ref>[^\s'\"]+)",
    re.IGNORECASE,
)

# Same shape but matched against a bare ``uses:`` VALUE (no ``uses:``
# prefix) — used to parse a YAML job's ``uses`` string.
_REF_VALUE_RE = re.compile(
    r"(?P<org>[\w.-]+)/robotsix-mill/\.github/workflows/"
    r"(?P<wf>[\w.-]+\.ya?ml)@(?P<ref>[^\s'\"]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkflowCallerFinding:
    """A single broken-caller finding.

    Attributes
    ----------
    file:
        Repo-relative POSIX path of the offending workflow file.
    line:
        1-based line number of the offending ``uses:`` line.
    kind:
        ``WRONG_ORG`` or ``MISSING_PERMISSION``.
    correct_form:
        The EXACT fix — the corrected ``uses:`` line (wrong-org) or the
        required ``permissions:`` block snippet (missing-permission).
    message:
        A precise, actionable one-paragraph description naming the file,
        line, and correct form.
    """

    file: str
    line: int
    kind: str
    correct_form: str
    message: str


def _required_permissions_block(wf: str) -> str:
    """Render the required ``permissions:`` block snippet for *wf*."""
    required = _REQUIRED_PERMISSIONS.get(wf, {})
    lines = ["permissions:"]
    lines.extend(f"  {scope}: {level}" for scope, level in required.items())
    return "\n".join(lines)


def _satisfies(granted: str | None, required: str) -> bool:
    """True when *granted* access level meets or exceeds *required*."""
    if granted is None:
        return False
    return _LEVELS.get(granted.lower(), -1) >= _LEVELS.get(required, 0)


def _permissions_satisfied(perms: object, wf: str) -> bool:
    """True when a job's ``permissions:`` block *perms* grants every scope
    the reusable workflow *wf* requires.

    *perms* is whatever the YAML loader produced for ``jobs.<id>.permissions``
    — ``None`` (no block), a mapping of ``scope -> level``, or a bare string
    shorthand (``read-all`` / ``write-all``).
    """
    required = _REQUIRED_PERMISSIONS.get(wf)
    if not required:
        # Unknown workflow — nothing required, never a finding.
        return True
    if perms is None:
        return False
    if isinstance(perms, str):
        # GitHub shorthand: ``read-all`` / ``write-all`` grant that level
        # to every scope; anything else (e.g. ``{}``) grants nothing.
        if perms == "write-all":
            return True
        if perms == "read-all":
            return all(req == "read" for req in required.values())
        return False
    if not isinstance(perms, dict):
        return False
    return all(_satisfies(perms.get(scope), level) for scope, level in required.items())


def _scan_wrong_org(
    rel: str,
    lines: list[str],
    line_index: dict[tuple[str, str, str], int],
) -> list[WorkflowCallerFinding]:
    """Regex pass: record each reference's line and flag wrong-org ones.

    Mutates *line_index* (``(org, wf, ref) -> first 1-based line``) so the
    YAML pass can attach a precise line to each job's reference.
    """
    findings: list[WorkflowCallerFinding] = []
    for i, line in enumerate(lines, start=1):
        m = _USES_LINE_RE.search(line)
        if not m:
            continue
        org, wf, ref = m.group("org"), m.group("wf"), m.group("ref")
        line_index.setdefault((org.lower(), wf, ref), i)

        if org != CANONICAL_ORG:
            correct = (
                f"uses: {CANONICAL_ORG}/robotsix-mill/.github/workflows/{wf}@{ref}"
            )
            findings.append(
                WorkflowCallerFinding(
                    file=rel,
                    line=i,
                    kind=WRONG_ORG,
                    correct_form=correct,
                    message=(
                        f"{rel}:{i}: reusable-workflow caller uses org "
                        f"'{org}', but robotsix-mill is owned by "
                        f"'{CANONICAL_ORG}'. The 'robotsix/robotsix-mill' "
                        "slug does not resolve and causes a "
                        f"startup_failure. Use: {correct}"
                    ),
                )
            )
    return findings


def _scan_missing_permission(
    rel: str,
    text: str,
    line_index: dict[tuple[str, str, str], int],
) -> list[WorkflowCallerFinding]:
    """YAML pass: flag every job whose ``permissions:`` block does not grant
    the scopes its called reusable workflow requires."""
    import yaml

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    findings: list[WorkflowCallerFinding] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if not isinstance(uses, str):
            continue
        m = _REF_VALUE_RE.search(uses)
        if not m:
            continue
        org, wf, ref = m.group("org"), m.group("wf"), m.group("ref")
        if wf not in _REQUIRED_PERMISSIONS:
            continue
        if _permissions_satisfied(job.get("permissions"), wf):
            continue

        line = line_index.get((org.lower(), wf, ref), 0)
        block = _required_permissions_block(wf)
        findings.append(
            WorkflowCallerFinding(
                file=rel,
                line=line,
                kind=MISSING_PERMISSION,
                correct_form=block,
                message=(
                    f"{rel}:{line}: job calling '{wf}' does not grant the "
                    "permissions the reusable workflow requires "
                    "(insufficient or missing 'permissions:' block), which "
                    "causes a startup_failure. Add this per-job block:\n"
                    f"{block}"
                ),
            )
        )
    return findings


def audit_workflow_callers(repo_dir: Path) -> list[WorkflowCallerFinding]:
    """Scan *repo_dir*'s ``.github/workflows/`` for broken reusable-workflow
    callers and return a list of findings.

    Detects two offenses on every cross-repo ``uses:`` reference to a
    ``robotsix-mill`` reusable workflow:

    - ``WRONG_ORG`` — the org is not ``damien-robotsix`` (regex-driven,
      so it works even when the YAML is otherwise malformed).
    - ``MISSING_PERMISSION`` — the calling job's ``permissions:`` block
      does not grant every scope the called workflow requires
      (structurally resolved from the parsed YAML).

    Local refs (``./...``) and Docker refs (``docker://``) are ignored.
    """
    findings: list[WorkflowCallerFinding] = []

    wf_dir = repo_dir / ".github" / "workflows"
    if not wf_dir.is_dir():
        return findings

    paths = sorted(
        p for ext in ("*.yml", "*.yaml") for p in wf_dir.glob(ext) if p.is_file()
    )

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = path.relative_to(repo_dir).as_posix()
        line_index: dict[tuple[str, str, str], int] = {}
        findings.extend(_scan_wrong_org(rel, text.splitlines(), line_index))
        findings.extend(_scan_missing_permission(rel, text, line_index))

    return findings


def _render_findings(findings: list[WorkflowCallerFinding]) -> str:
    """Render findings as precise, actionable text for the audit agent."""
    if not findings:
        return (
            "workflow-caller audit complete — **no broken reusable-workflow "
            "callers detected**. Every `uses:` reference to a robotsix-mill "
            "workflow uses the 'damien-robotsix' org and grants the required "
            "per-job permissions."
        )
    out: list[str] = [
        f"workflow-caller audit complete — **{len(findings)} finding(s)**:",
        "",
    ]
    for f in findings:
        out.append(f"### {f.kind} — `{f.file}:{f.line}`")
        out.append(f.message)
        out.append("")
    return "\n".join(out)


def make_workflow_caller_audit_tool(repo_dir: Path) -> Callable[[], str]:
    """Create the ``audit_workflow_callers`` agent tool closure.

    Follows the same factory pattern as ``make_jscpd_tool``: wraps the
    deterministic detector in a closure scoped to *repo_dir* and
    self-registers into ``ToolRegistry``.
    """

    def audit_workflow_callers_tool() -> str:
        """Scan the repository's .github/workflows/ for reusable-workflow
        callers that use the wrong org or omit the required per-job
        permissions, returning each finding with file, line, and the exact
        correct form."""
        return _render_findings(audit_workflow_callers(repo_dir))

    from .tool_registry import ToolInfo, ToolRegistry

    if not any(
        t.name == "audit_workflow_callers" for t in ToolRegistry.list_tools()
    ):
        ToolRegistry.register(
            ToolInfo(
                name="audit_workflow_callers",
                description=(
                    "Scan .github/workflows/ for reusable-workflow callers "
                    "that reference the wrong org or omit the required per-job "
                    "permissions, returning each finding with file, line, and "
                    "the exact correct form."
                ),
                category="exploration",
                parameters={},
            )
        )

    return audit_workflow_callers_tool
