"""Module-level constants, regexes, and pure helper functions for the
refine stage.

These are the responsibility-free pieces of the refine stage: prefix
constants shared between the dedup guard's producer and validator, the
deployed-log-summary builder, spec-degeneracy detection, auto-approve /
triage classification, next-state resolution, and dedup candidate-block
rendering.  They are kept co-located because several call each other by
bare name (e.g. ``_resolve_next_state`` → ``_spec_is_degenerate``).
External-fix claim detection and live branch-merged verification live in
``external_fix.py``.
"""

from __future__ import annotations

import fnmatch
import itertools
import logging
import re
import subprocess
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ...agents import refining
from ...config.settings import Settings
from ...core import constants as _constants
from ...core.constants import BINARY_EXTENSIONS
from ...core.models import Ticket, TicketEvent
from ...core.states import State
from ...scm.vcs import git_ops
from ..base import StageContext

# External-fix claim detection / branch-merged verification moved to
# external_fix.py; helpers.py keeps the pieces ``verify_claim`` still calls.
from .external_fix import (
    _COMMIT_SHA_RE,
    _PR_MR_REF_RE,
    _rationale_claims_external_fix,
)

# Spec-quality validation helpers live in spec_validation.py.
# ``_resolve_next_state`` calls ``_spec_is_degenerate`` by bare name.
from .spec_validation import _spec_is_degenerate

# Re-export prefix constants for backward compatibility
# (consumers import them from .helpers).
DEDUP_DUPLICATE_PREFIX: str = _constants.DEDUP_DUPLICATE_PREFIX
DEDUP_ALREADY_DONE_PREFIX: str = _constants.DEDUP_ALREADY_DONE_PREFIX
FRESHNESS_STALE_PREFIX: str = _constants.FRESHNESS_STALE_PREFIX
OBSOLESCENCE_GAP_PREFIX: str = _constants.OBSOLESCENCE_GAP_PREFIX
WORKFLOW_PORTABILITY_GATE_PREFIX: str = _constants.WORKFLOW_PORTABILITY_GATE_PREFIX
STANDARDS_GATE_PREFIX: str = _constants.STANDARDS_GATE_PREFIX
SCOPE_TRIAGE_REPO_AWARENESS_GATE_PREFIX: str = (
    _constants.SCOPE_TRIAGE_REPO_AWARENESS_GATE_PREFIX
)
DEPENDENCY_BLOCKED_PREFIX: str = _constants.DEPENDENCY_BLOCKED_PREFIX
MARK_DONE_NOTE_PREFIX: str = _constants.MARK_DONE_NOTE_PREFIX
NON_IMPLEMENTATION_CLOSE_PREFIXES: tuple[str, ...] = (
    _constants.NON_IMPLEMENTATION_CLOSE_PREFIXES
)

log = logging.getLogger("robotsix_mill.stages.refine")


UNMERGED_BRANCH_PREFIX = "Implementation exists on branch"

# -- doc-only change detection ------------------------------------------

# Regex that matches file paths in backtick-wrapped text.  More
# permissive than _triage.py's _PATH_RE (which requires a /) — we
# also accept root-level paths like ``README.md`` and ``CHANGELOG.md``
# because documentation tickets often mention root-level .md files.
_DOC_ONLY_PATH_RE = re.compile(r"`([^`]*\.[a-zA-Z]{1,10})`")

# Extensions that signal a code/config change (not doc-only).
_CODE_EXTENSIONS: frozenset[str] = frozenset({".py", ".ts", ".js", ".yaml", ".yml"})

# Terms that, when present in a draft title or body, indicate the
# change is NOT documentation-only — even when every backtick path
# looks like a .md file.  Used as a pre-check in _is_doc_only_change
# (and _is_doc_only_draft in _triage.py) to prevent false doc-only
# classification that would short-circuit refine for code tickets.
_NOT_DOC_ONLY_TERMS_RE = re.compile(
    r"\b(implement|refactor|migrate|deploy|pipeline|workflow|"
    r"mypy|ruff|lint|bandit|type:\s*ignore|"
    r"hotfix|rollback|revert)\b",
    re.IGNORECASE,
)


def _is_doc_only_change(draft: str, title: str = "") -> bool:
    """Return True if *draft* describes a documentation-only change.

    Considers a change doc-only when every file path extracted from the
    draft is a Markdown or docs path (``docs/**``, ``*.md``,
    ``CHANGELOG.md``) and no code/config file (``.py``, ``.ts``,
    ``.js``, ``.yaml``, ``.yml``) is mentioned.
    """
    text = f"{title}\n\n{draft}" if title else draft
    if _NOT_DOC_ONLY_TERMS_RE.search(text):
        return False
    paths = _DOC_ONLY_PATH_RE.findall(text)
    if not paths:
        return False

    for p in paths:
        # Fast rejection: any code extension → not doc-only.
        for ext in _CODE_EXTENSIONS:
            if p.endswith(ext):
                return False
        # Acceptable doc paths: docs/ tree or .md extension anywhere.
        if p.startswith("docs/") or p.endswith(".md"):
            continue
        # Anything else (no recognised doc pattern) → ambiguous, not doc-only.
        return False

    return True


# -- ops-shaped retrospect follow-up detection --------------------------

# Patterns that mark a draft as "ops-shaped": it asks to RUN something
# against a deployed service or a high-memory host, rather than describing
# a code change.  A retrospect follow-up matching one of these cannot be
# satisfied by a mill implement run (no network, ~1 GiB sandbox) and must
# be routed to a human/ops gate instead of auto-approved into implement.
# Marker list is intentionally small and extensible — no NLP generalisation.
_OPS_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(POST|GET|PUT|PATCH|DELETE)\s+/api/", re.IGNORECASE),
    re.compile(r"\bdeployed service\b", re.IGNORECASE),
    re.compile(r"environment with adequate memory", re.IGNORECASE),
    re.compile(r"\bhigh-memory host\b", re.IGNORECASE),
)


def ops_shape_marker(text: str) -> str | None:
    """Return the matched substring of the first ops-shape pattern hit.

    Returns ``None`` when *text* contains no ops-shape marker.  Pure
    function — used by the refine triage gate to route ops-shaped
    retrospect follow-ups to a human gate instead of auto-implement.
    """
    for pattern in _OPS_SHAPE_PATTERNS:
        m = pattern.search(text)
        if m is not None:
            return m.group(0)
    return None


# -- operator-prerequisite detection ------------------------------------

# Markers that a draft needs an operator ACTION (a repo-settings toggle, a
# hosting control-panel change) or an operator-supplied FACT no agent can
# produce.  The mechanical fast-path must not auto-approve such a draft
# into implement — implement can only ASK_USER and block (live bb70: PVR
# toggle; a4e7: "Operator confirms the PHP version ... Do not guess it").
# Small, extensible list — no NLP generalisation.
_OPERATOR_PREREQ_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\boperator confirms\b", re.IGNORECASE),
    re.compile(r"\brepo(?:sitory)? owner\b", re.IGNORECASE),
    re.compile(r"\bdo not guess\b", re.IGNORECASE),
    re.compile(r"\bsettings\s*(?:→|->)", re.IGNORECASE),
    re.compile(r"\bin the [^\n]*\bcontrol panel\b", re.IGNORECASE),
    re.compile(r"\ba settings change, not a file change\b", re.IGNORECASE),
    re.compile(r"\bchecked in repo settings\b", re.IGNORECASE),
)


def operator_prerequisite_lines(text: str) -> list[str]:
    """Return the stripped lines of *text* carrying an operator-only marker.

    Empty when no line matches.  Pure function — used by the refine
    fast-path gate to route operator-prerequisite drafts to a human gate
    instead of auto-approving them into implement.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if any(p.search(line) for p in _OPERATOR_PREREQ_PATTERNS)
    ]


_PATH_TOKEN_RE = re.compile(r"[\w.*/\-]+")


def cited_sensitive_paths(text: str, globs: list[str]) -> list[tuple[str, str]]:
    """Return ``(path, glob)`` pairs for path tokens in *text* matching *globs*.

    Uses the same ``fnmatch`` semantics as the merge gate's
    sensitive-path check over ``auto_merge_sensitive_globs``.
    """
    hits: list[tuple[str, str]] = []
    for raw in _PATH_TOKEN_RE.findall(text or ""):
        token = raw.rstrip(".,;:")
        if not token or any(token == p for p, _ in hits):
            continue
        for pattern in globs:
            if fnmatch.fnmatch(token, pattern):
                hits.append((token, pattern))
                break
    return hits


# ---------------------------------------------------------------------------
# content-aware workflow-change gate (sensitive-path blanket exception)
# ---------------------------------------------------------------------------

# The blanket ".github/workflows/** ⇒ human approval" rule (cited over
# ``auto_merge_sensitive_globs``) stalls routine additive CI changes —
# matrix/version bumps, dependabot blocks, test relocations, reusable-SHA
# pins — that are reversible and non-pipeline-destructive.  Workflow
# citations are therefore classified by the CHANGE's content instead of
# the glob alone: destructive constructs still fail closed to human
# approval; additive/config-only changes are auto-approved.  Non-workflow
# sensitive globs (secrets, .env) always fail closed.  Small, extensible
# regex lists — no NLP generalisation (same philosophy as
# _OPERATOR_PREREQ_PATTERNS).
_WORKFLOW_SENSITIVE_GLOB = ".github/workflows/**"

# Destructive constructs — any hit ⇒ human approval.  Deliberately does
# NOT include a bare "job" marker: replacing a hand-rolled job with a
# shared reusable keeps the same check context and is additive (live
# robotsix-ui #456f), and the word alone is too noisy on prose specs.
_WF_DESTRUCTIVE_RE: tuple[re.Pattern[str], ...] = (
    # permissions: / GITHUB_TOKEN / OIDC / workflow permissions scope
    re.compile(r"permissions\s*:", re.IGNORECASE),
    re.compile(r"\bGITHUB_TOKEN\b", re.IGNORECASE),
    re.compile(r"\bid-token\s*:\s*write", re.IGNORECASE),
    re.compile(r"\bworkflow\s*permissions\b", re.IGNORECASE),
    # secrets referenced (added/removed) by the change
    re.compile(r"\bsecrets?(\s*[:.]|\b)", re.IGNORECASE),
    # required status checks / branch protection / merge gates
    re.compile(
        r"\brequired[_ -]?status[_ -]?checks?\b|\bbranch protection\b|"
        r"\bmerge (queue|gate)\b|\brequired[_ -]?checks?\b",
        re.IGNORECASE,
    ),
    # deploy / release / environment targets
    re.compile(
        r"\benvironment\s*:\s*\S|docker push|helm\b|kubectl\b|terraform\b|"
        r"npm publish|\bssh\b|aws\s|gcloud\s|\baz\s",
        re.IGNORECASE,
    ),
    re.compile(r"\bdeploy\b|\brelease\b", re.IGNORECASE),
    # reusable-workflow call REMOVED (diff '-' line or prose removal)
    re.compile(r"^\s*-\s*uses:\s*\S*\.github/workflows", re.IGNORECASE | re.MULTILINE),
    re.compile(
        r"\bremov\w*[^\n]*\breusable\b|\bdelet\w*[^\n]*\breusable\b|"
        r"\bdrop\b[^\n]*\breusable\b|\bstop\b[^\n]*\breusable\b",
        re.IGNORECASE,
    ),
)

# Additive/config-only constructs — confirmation that a workflow change is
# APPROVE-safe (matrix/version bumps, dependabot config, test relocations,
# lint/ignore tables, reusable-SHA pins / reusable adoption).
_WF_ADDITIVE_RE: tuple[re.Pattern[str], ...] = (
    # dependabot ecosystem/group/ignore blocks
    re.compile(r"\bdependabot\b|\becosystem\s*:|\bversion-update\b", re.IGNORECASE),
    # version pins / matrix entry bumps
    re.compile(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-version\s*:", re.IGNORECASE),
    re.compile(r"\bmatrix\b", re.IGNORECASE),
    re.compile(r"\b\d+\.\d+\s*(?:→|->)\s*\d+\.\d+\b", re.IGNORECASE),
    re.compile(r"\bbump\w*[^\n]*\b(version|python|node|matrix)\b", re.IGNORECASE),
    # test relocation
    re.compile(
        r"\brelocat\w*\b|\bmov\w*\b[^\n]*\btest\b|\btest\b[^\n]*\bmov\w*\b",
        re.IGNORECASE,
    ),
    # lint / ignore config tables
    re.compile(
        r"\.prettierignore|extend-select|\[tool\.ruff\]|lint[_ -]?table",
        re.IGNORECASE,
    ),
    # reusable-workflow SHA pin or adoption (adding a shared call)
    re.compile(r"\buses:\s*\S*@[0-9a-f]{40}\b", re.IGNORECASE),
    re.compile(
        r"\bpin(?:ned|ning)?\b[^\n]*\bsha\b|\bsha\b[^\n]*\bpin(?:ned|ning)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breplac\w*\b[^\n]*\bwith\b[^\n]*\breusable\b|\bmigrat\w*\b[^\n]*\breusable\b|"
        r"\badopt\w*\b[^\n]*\breusable\b|\buse\b[^\n]*\bshared\b[^\n]*\breusable\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*\+\s*uses:\s*\S*\.github/workflows", re.IGNORECASE | re.MULTILINE),
)


def workflow_change_review_kind(
    text: str,
) -> Literal["destructive", "additive", "ambiguous"]:
    """Classify a workflow *change* text (diff hunks or prose description).

    Returns ``"destructive"`` when any destructive construct is present
    (token/permission scope, secrets, required checks, deploy/release
    targets, reusable-workflow removal) — human approval required.
    Returns ``"additive"`` when no destructive construct is present but at
    least one additive/config-only construct is (dependabot block,
    version/matrix bump, test relocation, lint/ignore table, reusable-SHA
    pin or reusable adoption) — safe to auto-approve.  Returns
    ``"ambiguous"`` otherwise — the caller fails closed.
    """
    for pattern in _WF_DESTRUCTIVE_RE:
        if pattern.search(text or ""):
            return "destructive"
    for pattern in _WF_ADDITIVE_RE:
        if pattern.search(text or ""):
            return "additive"
    return "ambiguous"


WorkflowDisposition = Literal["approve", "fail_closed", "none"]


def workflow_sensitive_disposition(
    sensitive: list[tuple[str, str]], change_text: str
) -> tuple[WorkflowDisposition, str]:
    """Content-aware disposition for cited sensitive paths.

    *sensitive* is the ``(path, glob)`` list from ``cited_sensitive_paths``
    and *change_text* the spec/draft/diff describing the change.

    Returns ``("approve", reason)`` when EVERY cited path matches the
    workflow glob AND the change is classified additive/config-only.
    Returns ``("fail_closed", reason)`` when a cited path is non-workflow
    sensitive (secrets, .env) or the workflow change is destructive or
    ambiguous.  Returns ``("none", "")`` when *sensitive* is empty.
    """
    if not sensitive:
        return "none", ""
    non_workflow = [(p, g) for p, g in sensitive if g != _WORKFLOW_SENSITIVE_GLOB]
    if non_workflow:
        p, g = non_workflow[0]
        return "fail_closed", f"non-workflow sensitive path {p!r} (glob {g!r})"
    kind = workflow_change_review_kind(change_text)
    if kind == "destructive":
        return "fail_closed", "destructive workflow change"
    if kind == "ambiguous":
        return "fail_closed", "workflow change not proven additive/config-only"
    return "approve", "additive/config-only workflow change"


def _load_refine_memory(s: Settings, memory_board_id: str) -> str:
    """Load the refine memory ledger from the DB-backed store.

    Falls back to the legacy Markdown file when the DB row doesn't exist
    yet (first run after migration).  The file path is still resolved via
    ``s.memory_file_for("refine", memory_board_id)`` but only used as a
    fallback; the DB is the primary store.
    """
    from robotsix_mill.agents.runners.pass_runner import load_memory as _file_load
    from robotsix_mill.core.db import load_memory_db

    content = load_memory_db(s, memory_board_id, "refine", max_chars=s.max_memory_chars)
    if content:
        return content
    # Fallback: legacy file (first run after migration, or DB not yet populated).
    legacy_path = s.memory_file_for("refine", memory_board_id)
    return _file_load(legacy_path, max_chars=s.max_memory_chars)


def _persist_refine_memory(s: Settings, memory_board_id: str, text: str) -> None:
    """Persist the refine memory ledger to the DB-backed store.

    On first write, migrates data from the legacy Markdown file (if it
    exists) and renames it to ``refine_memory.md.migrated``.
    """
    from robotsix_mill.core.db import persist_memory_db

    persist_memory_db(s, memory_board_id, "refine", text, max_chars=s.max_memory_chars)


# States that prove a ticket has completed (or moved past) refine.
# Used by _is_valid_dedup_target to reject an un-refined DRAFT
# candidate, so a further-along ticket is never buried in it.
REFINE_PROGRESS_STATES = frozenset(
    {
        State.HUMAN_ISSUE_APPROVAL,
        State.READY,
        State.DOCUMENTING,
        State.CODE_REVIEW,
        State.DELIVERABLE,
        State.HUMAN_MR_APPROVAL,
        State.IMPLEMENT_COMPLETE,
        State.WAITING_AUTO_MERGE,
        State.REBASING,
        State.FIXING_CI,
        State.ADDRESSING_REVIEW,
        State.DONE,
    }
)

# History-note prefix written by ``TicketService.request_changes`` when an
# operator sends a refined ticket back to DRAFT with feedback. Its presence
# means a human is actively shaping THIS ticket — the dedup guard must not
# then auto-close it as a "duplicate"/"already done" (that silently discards
# the operator's intent; see the auto-mail board-columns ticket that was
# dedup-closed after two rounds of operator "changes requested").
OPERATOR_SENDBACK_PREFIX = "changes requested:"

# File extensions that are safe to preview as text (last 100 lines).
_TEXT_SAFE_EXTENSIONS = frozenset(
    {
        ".log",
        ".txt",
        ".json",
        ".csv",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".md",
        ".env",
        ".cfg",
        ".conf",
        ".ini",
    }
)

# ``BINARY_EXTENSIONS`` is imported from ``...core.constants`` above.

_MAX_SUMMARY_ENTRIES = 20
_PREVIEW_MAX_FILE_SIZE = 512 * 1024  # 512 KB
_TAIL_LINES_FULL = 100
_TAIL_LINES_HINT = 5


def _build_deployed_log_summary(path: Path, config_path: str) -> str:
    """Build a Markdown summary of the deployed log folder at *path*.

    Best-effort: any OSError during listing or reading is caught and the
    summary is still emitted with whatever was successfully collected
    (never blocks refine).

    *config_path* is the original string from the repo config, shown in
    the header so the agent knows where the logs came from.
    """
    header = (
        f"The repo's `.robotsix-mill/config.yaml` points to a deployed "
        f"log folder at `{config_path}`. These logs are from a live "
        f"deployment of this application — use them to help diagnose "
        f"issues. Use `list_dir` on the path to enumerate files and "
        f"`read_file` to read them.\n"
    )
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError:
        return header + "\n*(folder could not be listed)*"
    if not entries:
        return header + "\n*(folder is empty)*"

    lines: list[str] = [header, ""]
    count = 0
    for entry in entries:
        if count >= _MAX_SUMMARY_ENTRIES:
            lines.append(
                f"… and {len(entries) - _MAX_SUMMARY_ENTRIES} more entries "
                f"(use `list_dir` to see them all)"
            )
            break
        count += 1
        try:
            stat = entry.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            size = _human_size(stat.st_size)
        except OSError:
            mtime = "unknown"
            size = "unknown"

        if entry.is_dir():
            lines.append(f"- `{entry.name}/` (directory, {size}, {mtime})")
            continue

        # Regular file
        suffix = entry.suffix.lower()
        if suffix in BINARY_EXTENSIONS:
            lines.append(
                f"- `{entry.name}` ({size}, {mtime}) — "
                f"binary file, use `read_file` for raw content"
            )
            continue

        if suffix not in _TEXT_SAFE_EXTENSIONS and suffix != "":
            lines.append(
                f"- `{entry.name}` ({size}, {mtime}) — "
                f"non-standard extension, use `read_file` to inspect"
            )
            continue

        # Text-safe (including no-extension files)
        if stat.st_size > _PREVIEW_MAX_FILE_SIZE:
            lines.append(
                f"- `{entry.name}` ({size}, {mtime}) — "
                f"file too large for preview, use `read_file` to inspect"
            )
            tail = _tail_file(entry, _TAIL_LINES_HINT)
            if tail:
                lines.append(f"\n  ```\n{tail}\n  ```")
            continue

        # Full preview (last 100 lines)
        tail = _tail_file(entry, _TAIL_LINES_FULL)
        lines.append(f"- `{entry.name}` ({size}, {mtime})")
        if tail:
            lines.append(f"\n  ```\n{tail}\n  ```")
        else:
            lines.append("  *(empty file)*")

    return "\n".join(lines)


def _human_size(num_bytes: int) -> str:
    """Return a human-readable file size string (e.g. '45 KB')."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num_bytes /= 1024.0
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}"
    return f"{num_bytes:.1f} TB"  # unreachable but satisfies type checkers


def _tail_file(filepath: Path, max_lines: int) -> str:
    """Return the last *max_lines* of *filepath* as a string, or '' on error.

    Uses ``collections.deque`` for memory-efficient tail reading.
    Never raises — best-effort only.
    """
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max_lines)
        return "".join(tail).rstrip("\n")
    except OSError:
        return ""


# Sources whose tickets are deterministically auto-approved because they
# are proposed by mill's own periodic agents (audit, agent_check, bc_check,
# …) whose scope is dead-code removal, prompt updates, memory ledger
# structure, config cleanup, docstring additions — no behavioural risk a
# human reviewer can meaningfully veto.  ``test_gap`` joins the same family.
# Used by both ``_resolve_next_state`` and the pre-refine mechanical
# fast-path in ``orchestration.py``.
_AUTO_APPROVE_SOURCES: set[str] = {
    "test_gap",
    "audit",
    "agent_check",
    "bc_check",
    "completeness_check",
    "module_curator",
    "copy_paste",
    "config_standard",
}

# Substrings (lowercased) in a triage note that make a draft SUSPICIOUS —
# it may be describing something that doesn't exist.  A match suppresses the
# deterministic _AUTO_APPROVE_SOURCES shortcut so the LLM classifier decides
# instead; it is NOT a rejection on its own.
#
# These are substrings of free-form LLM prose, and the identical phrase means
# opposite things depending on its referent ("the test file does not exist"
# is the work to do; "'mail-ingester' does not exist" is an ungrounded
# premise).  Treating a match as a verdict rejected healthy tickets into
# HUMAN_ISSUE_APPROVAL, which has no automated stage and nothing that closes
# it, so they accumulated for days.  Keep the list broad — over-matching only
# costs one classifier call now, instead of stranding a ticket.
_TRIAGE_REJECTION_PATTERNS: list[str] = [
    "no change is needed",
    "no change needed",
    "no changes needed",
    "no code change is needed",
    "no code changes needed",
    "assertion is factually wrong",
    "factually wrong",
    "factually incorrect",
    "false positive",
    # Confirmed mis-route / non-existent symbols (triage trace a051eceb)
    "contains no",
    "does not exist",
    "doesn't exist",
    "no related",
    "entirely ungrounded",
    "different repositor",
    "different repo",
    "wrong repositor",
    "wrong repo",
    "belongs to a different",
    "lives in a different",
    "not present in this",
    "no such",
]

# Wrong-repository signal patterns — a subset of _TRIAGE_REJECTION_PATTERNS
# that unambiguously indicate the ticket was filed against the wrong repo.
# When the triage note matches any of these, auto-approve MUST NOT override
# the signal: the ticket routes to HUMAN_ISSUE_APPROVAL so a human can
# re-file it on the correct board.
_WRONG_REPO_PATTERNS: list[str] = [
    "different repositor",
    "different repo",
    "wrong repositor",
    "wrong repo",
    "belongs to a different",
    "lives in a different",
    "migrated from board",
]


def _triage_note_signals_wrong_repo(triage_note: str | None) -> bool:
    """Return ``True`` when *triage_note* contains a wrong-repository signal.

    These patterns are unambiguous: they mean the ticket's scope belongs
    to a different repository.  Auto-approve must never override this
    signal — the ticket needs a human to re-file on the correct board.
    """
    if not triage_note:
        return False
    lower = triage_note.lower()
    return any(p in lower for p in _WRONG_REPO_PATTERNS)


# Headings whose sections carry the decision-relevant content (what to
# change, where, and how success is judged).  When a spec must be cut,
# these are kept ahead of narrative / log sections.
_AUTO_APPROVE_PRIORITY_HEADING = re.compile(
    r"scope|acceptance|criteria|change|fix|solution|remediation|plan"
    r"|implementation|required|proposed|files",
    re.IGNORECASE,
)
_HEADING_LINE = re.compile(r"^#{1,6}\s")
# Fenced blocks longer than this are collapsed to their head + tail.
_FENCE_KEEP_HEAD = 2
_FENCE_KEEP_TAIL = 8


def _collapse_long_fences(spec: str) -> str:
    """Collapse long fenced blocks (CI log tails, tracebacks) to their
    first and last lines — the error lines sit at the tail of a log.
    """
    out: list[str] = []
    block: list[str] | None = None
    for line in spec.split("\n"):
        if line.lstrip().startswith("```"):
            if block is None:
                out.append(line)
                block = []
                continue
            if len(block) > _FENCE_KEEP_HEAD + _FENCE_KEEP_TAIL + 1:
                elided = len(block) - _FENCE_KEEP_HEAD - _FENCE_KEEP_TAIL
                block = [
                    *block[:_FENCE_KEEP_HEAD],
                    f"… ({elided} lines elided)",
                    *block[-_FENCE_KEEP_TAIL:],
                ]
            out.extend(block)
            out.append(line)
            block = None
            continue
        if block is not None:
            block.append(line)
        else:
            out.append(line)
    if block is not None:  # unterminated fence — keep as-is
        out.extend(block)
    return "\n".join(out)


def _cut_at_newline(text: str, limit: int) -> str:
    """Return the longest newline-aligned prefix of *text* within *limit*."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_nl = cut.rfind("\n")
    return cut[: last_nl + 1] if last_nl > 0 else cut


def _summarize_spec_for_auto_approve(spec: str, max_chars: int = 2000) -> str:
    """Return a bounded summary of *spec* for the auto-approve classifier.

    The auto-approve classifier only needs to detect design decisions —
    it doesn't need the full verbose spec.  When the spec is already
    within *max_chars* it is returned unchanged.  Otherwise long fenced
    blocks (log tails) are collapsed first; if that is still too long,
    the decision-relevant sections (Scope, Acceptance criteria, Proposed
    solution, …) get the budget before the remaining sections, so a
    bulky problem statement or log can't crowd out what the change is.
    Every section keeps its heading and opening lines; sections keep
    their original order and are cut newline-aligned.
    """
    if len(spec) <= max_chars:
        return spec
    note = "\n… (truncated for auto-approve — full spec is available)"
    budget = max_chars - len(note)
    if budget <= 0:
        return spec[:max_chars]

    collapsed = _collapse_long_fences(spec)
    if len(collapsed) <= budget:
        return collapsed + note

    # Split into sections at markdown headings (preamble = first section).
    sections: list[str] = []
    for line in collapsed.splitlines(keepends=True):
        if not sections or _HEADING_LINE.match(line):
            sections.append(line)
        else:
            sections[-1] += line

    def _is_priority(section: str) -> bool:
        first = section.split("\n", 1)[0]
        return bool(
            _HEADING_LINE.match(first) and _AUTO_APPROVE_PRIORITY_HEADING.search(first)
        )

    order = [i for i, sec in enumerate(sections) if _is_priority(sec)]
    order += [i for i in range(len(sections)) if i not in order]
    # First give every section a small floor (its heading + opening
    # lines) so none vanishes, then hand the rest out priority-first.
    floor = min(300, budget // len(sections))
    kept = [_cut_at_newline(sec, floor) for sec in sections]
    remaining = budget - sum(len(k) for k in kept)
    for i in order:
        if remaining <= 0:
            break
        grown = _cut_at_newline(sections[i], len(kept[i]) + remaining)
        remaining -= len(grown) - len(kept[i])
        kept[i] = grown
    return "".join(kept).rstrip("\n") + note


def _operator_already_approved(ctx: StageContext, ticket_id: str) -> bool:
    """True when *ticket_id*'s history holds a ``human_issue_approval →
    ready`` transition (the operator's approval). Best-effort: a history
    lookup failure returns False so the normal gate still applies.
    """
    try:
        events: list[TicketEvent] = list(ctx.service.history(ticket_id))
        return any(
            prev.state == State.HUMAN_ISSUE_APPROVAL and cur.state == State.READY
            for prev, cur in itertools.pairwise(events)
        )
    except Exception:
        log.warning("%s: approval-history lookup failed", ticket_id, exc_info=True)
        return False


def _resolve_next_state(
    ctx: StageContext,
    spec: str,
    ticket_id: str,
    source: str | None = None,
    *,
    triage_note: str | None = None,
    _ws: object | None = None,
) -> tuple[State, str | None]:
    """Return (next_state, auto_approve_note_or_None).

    Encapsulates the decision: if approval is not required → READY;
    auto-approve runs the triage on the spec → READY on APPROVE (no design
    decision found), HUMAN_ISSUE_APPROVAL otherwise (or on error).
    Empty/whitespace specs skip the triage entirely and go to
    HUMAN_ISSUE_APPROVAL when gated, mirroring the original behaviour.

    When the post-refine check has written a
    ``post_refine_auto_approve.json`` artifact (via
    ``_result_paths._run_post_refine_check``), reads the decision from
    there instead of making a separate LLM call.

    Test-gap tickets (``source == "test_gap"``) auto-approve
    deterministically — they only add test coverage with no
    production-code change, so there's no design decision a human
    could meaningfully veto. Three triage runs on test-gap tickets
    on 2026-05-28 all fell back to human approval and were
    rubber-stamped, so the LLM hop was pure toil + cost. Short-
    circuit before the LLM call.

    Every triage outcome carries a structured note so the auto-approve
    decision is visible in ticket history.  Triage failures or
    unexpected errors in note assembly fall through to
    HUMAN_ISSUE_APPROVAL with a fallback note — the transition is
    never blocked.
    """
    if not ctx.settings.require_approval:
        return State.READY, None
    if _spec_is_degenerate(spec):
        return State.BLOCKED, "refine produced no spec"
    # An operator already approved this ticket (a human_issue_approval →
    # ready event exists): never re-gate it — a re-refine after e.g. a
    # dependency unblock must not make the operator approve it twice.
    if _operator_already_approved(ctx, ticket_id):
        return State.READY, (
            "auto-approve: APPROVE — operator already approved this ticket "
            "(human_issue_approval → ready); not re-gating"
        )
    # Deterministic auto-approve for sources whose drafts are
    # internal-only by construction.
    suspicious = False
    if triage_note:
        triage_lower = triage_note.lower()
        suspicious = any(p in triage_lower for p in _TRIAGE_REJECTION_PATTERNS)
    if source in _AUTO_APPROVE_SOURCES and not suspicious:
        return State.READY, (
            f"auto-approve: APPROVE — {source} (deterministic rule: "
            "mill-internal periodic-agent proposal, no design risk)"
        )
    # Wrong-repo hard gate: when triage detected a repository mismatch,
    # auto-approve MUST NOT override — route to human approval so the
    # ticket can be re-filed on the correct board.
    if _triage_note_signals_wrong_repo(triage_note):
        return (
            State.HUMAN_ISSUE_APPROVAL,
            (
                "auto-approve: NEEDS_APPROVAL — wrong-repository signal "
                f"from triage: {triage_note}"
            ),
        )

    # Check for post-refine auto-approve artifact (written by
    # _result_paths._run_post_refine_check).
    import json as _json
    from pathlib import Path as _Path

    if _ws is not None:
        artifact_path = getattr(_ws, "artifacts_dir", None)
        if artifact_path is not None:
            auto_approve_path = _Path(artifact_path) / "post_refine_auto_approve.json"
            if auto_approve_path.exists():
                try:
                    data = _json.loads(auto_approve_path.read_text(encoding="utf-8"))
                    decision = data.get("decision", "NEEDS_APPROVAL")
                    reason = data.get("reason", "")
                    if decision == "APPROVE":
                        return State.READY, f"auto-approve: APPROVE — {reason}"
                    return (
                        State.HUMAN_ISSUE_APPROVAL,
                        f"auto-approve: NEEDS_APPROVAL — {reason}",
                    )
                except Exception:
                    log.warning(
                        "post_refine_auto_approve.json unreadable, falling back to LLM",
                        exc_info=True,
                    )

    # Fallback: legacy LLM call (for paths that don't go through
    # the post-refine check, e.g. multi-scope split children).
    try:
        result = _auto_approve_triage_with_retry(ctx.settings, spec)
    except Exception as exc:
        from ...runtime.transient_errors import is_model_unavailable_error

        if is_model_unavailable_error(exc):
            raise
        log.exception(
            "auto-approve triage failed after retry, falling back to "
            "human approval: %s",
            type(exc).__name__,
        )
        return (
            State.HUMAN_ISSUE_APPROVAL,
            "auto-approve: triage failed — falling back to human approval",
        )
    if result.decision == "APPROVE":
        return State.READY, f"auto-approve: APPROVE — {result.reason}"
    return (
        State.HUMAN_ISSUE_APPROVAL,
        f"auto-approve: NEEDS_APPROVAL — {result.reason}",
    )


def _auto_approve_triage_with_retry(
    settings: Settings,
    spec: str,
) -> refining.AutoApproveResult:
    """Run the auto-approve triage classifier, retrying once on failure.

    A genuine triage-step failure is retried exactly once before the caller
    falls back to the human gate.  Model-unavailable errors are NOT retried —
    they propagate so the worker parks the ticket (re-running a doomed model
    call would just spend twice).
    """
    from ...runtime.transient_errors import is_model_unavailable_error

    for attempt in range(2):
        try:
            return refining.triage_auto_approve(
                settings=settings,
                spec=_summarize_spec_for_auto_approve(spec),
            )
        except Exception as exc:
            if is_model_unavailable_error(exc):
                raise
            if attempt == 0:
                log.exception(
                    "auto-approve triage failed (attempt 1/2), retrying: %s",
                    type(exc).__name__,
                )
                continue
            raise

    # Unreachable: the loop above always returns on success or raises on the
    # final attempt. Present so the function has no implicit ``None`` return.
    raise AssertionError("auto-approve retry loop exited without returning")


# ---------------------------------------------------------------------------
# Advisory dedup helpers
# ---------------------------------------------------------------------------

_ADVISORY_RE = re.compile(r"Possible duplicate of (\S+)")


def _advisory_candidate_id(text: str) -> str | None:
    """Extract the candidate ticket id from a dedup-advisory blockquote.

    Matches the exact format emitted by :func:`annotate_child_body` /
    :func:`find_inflight_overlap`::

        > [!warning] Possible duplicate of 20260622T...-92d0 (…)

    Returns the captured id (``group(1)``) or ``None`` when no advisory
    block is present.
    """
    m = _ADVISORY_RE.search(text)
    return m.group(1) if m else None


def _strip_advisory_block(text: str) -> str:
    """Remove the leading ``> [!warning] Possible duplicate of …`` blockquote
    produced by :func:`annotate_child_body`.

    Strips the **contiguous leading block** of lines starting with ``>``
    (the advisory) plus the single blank line that separates it from the
    original body.  Only fires when the block contains the ``Possible
    duplicate of`` anchor — unrelated ``> [!warning]`` blocks are left
    intact.  Returns *text* unchanged when no such block is present.

    The operation is idempotent: stripping a body that has already been
    stripped is a no-op.
    """
    if not text:
        return text

    # Must contain the anchor text — otherwise it's not an advisory.
    if "Possible duplicate of" not in text:
        return text

    # Find the span of the leading blockquote by tracking character
    # positions so we preserve exact trailing newlines.
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip(" \t").startswith(">"):
            pos += len(line)
        else:
            break
    else:
        # All lines are blockquote lines — the entire text is an advisory?
        # This shouldn't happen in practice, but return empty.
        return ""

    if pos == 0:
        return text  # no leading blockquote

    # Verify the anchor text is within the leading block.
    leading_block = text[:pos]
    if "Possible duplicate of" not in leading_block:
        return text

    # Skip the blank separator line that follows the blockquote.
    remaining = text[pos:]
    if remaining.startswith("\n"):
        pos += 1
    elif remaining.startswith("\r\n"):
        pos += 2

    return text[pos:]


def verify_claim(
    claim_text: str,
    target_files: list[str],
    repo_dir: Path | None,
) -> bool:
    """Verify that a claim citing a PR or commit SHA actually touches
    *target_files*.

    Best-effort: returns ``True`` when verification passes *or* when there
    is nothing to verify (no PR/SHA reference, no target files, or no
    *repo_dir*).  Returns ``False`` only when a cited artifact is
    **confirmed** NOT to touch any target file — i.e. when the stage can
    prove the claim is wrong.

    Verification rules:

    1. Extract PR numbers (``#NNN``) and commit SHAs from *claim_text*.
    2. For each PR reference, check ``git log origin/main --grep '#NNN'
       -- <file>`` for each target file — any match confirms the claim.
    3. For each commit SHA, check ``git diff --stat <sha>~1..<sha>
       -- <file>`` for each target file — any match confirms the claim.
    4. When no concrete reference (PR/SHA) is found but *claim_text*
       contains an external-fix phrase (e.g. "already fixed"), check
       ``git log -n 1 -- <file>`` for each target file — any recent
       commit touching the file counts as plausible confirmation.
    """
    if repo_dir is None:
        return True  # nothing to verify against
    if not target_files:
        return True  # nothing to verify

    # Bail out early when the repo is not a valid git repository —
    # every git subcommand will fail and we cannot prove anything.
    if not (repo_dir / ".git").exists():
        return True

    # Refresh origin/main before any PR/SHA lookups so a stale clone cannot
    # prove a cited artifact "absent" when the remote has actually advanced.
    git_ops.refresh_origin_target(repo_dir, "main")

    pr_numbers: list[str] = _PR_MR_REF_RE.findall(claim_text or "")
    commit_shas: list[str] = _COMMIT_SHA_RE.findall(claim_text or "")

    # ── verify PR references ──────────────────────────────────────────
    for pr_ref in pr_numbers:
        pr_num = pr_ref.lstrip("#!")
        try:
            for f in target_files:
                grep_result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_dir),
                        "log",
                        "origin/main",
                        "--oneline",
                        "--grep",
                        f"#{pr_num}",
                        "--",
                        f,
                    ],
                    capture_output=True,
                    text=True,
                )
                if grep_result.returncode == 0 and grep_result.stdout.strip():
                    log.debug(
                        "verify_claim: PR %s confirmed — touches %s",
                        pr_ref,
                        f,
                    )
                    return True
        except Exception:
            log.debug(
                "verify_claim: git log for PR %s failed — allowing (best-effort)",
                pr_ref,
                exc_info=True,
            )
            return True  # can't verify → allow

    # ── verify commit SHAs ────────────────────────────────────────────
    for sha in commit_shas:
        try:
            type_check = subprocess.run(
                ["git", "-C", str(repo_dir), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
            )
            if type_check.returncode != 0 or type_check.stdout.strip() != "commit":
                continue
        except Exception:
            log.debug(
                "verify_claim: git cat-file for %s failed — allowing (best-effort)",
                sha[:10],
                exc_info=True,
            )
            return True  # can't verify → allow

        try:
            for f in target_files:
                # Use diff-tree --root which works for both initial and
                # normal commits (unlike diff sha~1..sha which fails on
                # root commits with no parent).
                diff_result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_dir),
                        "diff-tree",
                        "--root",
                        "--no-commit-id",
                        "-r",
                        sha,
                        "--",
                        f,
                    ],
                    capture_output=True,
                    text=True,
                )
                if diff_result.returncode == 0 and diff_result.stdout.strip():
                    log.debug(
                        "verify_claim: commit %s confirmed — touches %s",
                        sha[:10],
                        f,
                    )
                    return True
        except Exception:
            log.debug(
                "verify_claim: git diff for %s failed — allowing (best-effort)",
                sha[:10],
                exc_info=True,
            )
            return True  # can't verify → allow

    # ── no concrete reference → check for external-fix phrase ─────────
    if not pr_numbers and not commit_shas:
        if _rationale_claims_external_fix(claim_text):
            try:
                for f in target_files:
                    log_result = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo_dir),
                            "log",
                            "origin/main",
                            "--oneline",
                            "-n",
                            "1",
                            "--",
                            f,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    if log_result.returncode == 0 and log_result.stdout.strip():
                        log.debug(
                            "verify_claim: HEAD claim plausible — "
                            "recent commit touches %s",
                            f,
                        )
                        return True
            except Exception:
                log.debug(
                    "verify_claim: git log for HEAD claim failed — "
                    "allowing (best-effort)",
                    exc_info=True,
                )
                return True
            # External-fix phrase but no commit touches any target file.
            log.info(
                "verify_claim: external-fix claim unverified — "
                "no recent commit touches any target file (%s)",
                ", ".join(target_files[:5]),
            )
            return False

        # No concrete reference and no external-fix phrase → nothing to verify.
        return True

    # Concrete references were found but NONE touched any target file.
    cited = pr_numbers + [s[:10] for s in commit_shas]
    log.info(
        "verify_claim: cited refs (%s) do not touch any target file (%s) — "
        "claim unverified",
        ", ".join(cited[:5]),
        ", ".join(target_files[:5]),
    )
    return False


def _build_candidates_block(candidates: list[Ticket], ctx: StageContext) -> str:
    """Render candidates for the dedup check as one Markdown section
    per ticket.

    The previous implementation emitted a single JSON blob which is
    fine for the model but renders as an unreadable wall of escaped
    JSON in Langfuse's prompt viewer when an operator audits the run.
    The new format is one ``## <id>`` heading per candidate followed
    by a short metadata list and a fenced ``body`` block so each
    ticket is a readable section both inline and in the trace UI.

    Each rendered candidate carries the same fields the dedup yaml
    asks for (``id``, ``title``, ``state``, ``source``, ``body``);
    only the encoding changed.
    """
    if not candidates:
        return "(no candidates)"
    from ...core.text_utils import truncate_at_boundary

    max_chars = ctx.settings.dedup_candidate_body_max_chars
    sections: list[str] = []
    for t in candidates:
        try:
            body = ctx.service.workspace(t).read_description()
        except Exception:
            body = ""
        from ...agents.prompt_blocks import section as _section

        body = body.strip()
        # Bound pathologically long candidate bodies so they can't blow
        # up the dedup prompt. ``max_chars <= 0`` disables truncation
        # (and guards the ``truncate_at_boundary(text, 0) == ""`` edge).
        if max_chars > 0:
            body = truncate_at_boundary(body, max_chars)
        meta = (
            f"## {t.id}\n"
            f"- title: {t.title}\n"
            f"- state: {t.state.value}\n"
            f"- source: {t.source}\n"
        )
        sections.append(meta + "\n" + _section("body", body))
    return "\n\n".join(sections)
