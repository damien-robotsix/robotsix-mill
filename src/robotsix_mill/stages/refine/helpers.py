"""Module-level constants, regexes, and pure helper functions for the
refine stage.

These are the responsibility-free pieces of the refine stage: prefix
constants shared between the dedup guard's producer and validator, the
deployed-log-summary builder, spec-degeneracy detection, external-fix
claim detection, branch-merged verification, next-state resolution, and
dedup candidate-block rendering.  They are kept co-located because several
call each other by bare name (e.g. ``_resolve_next_state`` →
``_spec_is_degenerate``; ``_is_valid_dedup_target`` → ``_verify_branch_merged``).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ...agents import refining
from ...config.settings import Settings
from ...core import constants as _constants
from ...core.constants import BINARY_EXTENSIONS
from ...core.models import Ticket
from ...core.states import State
from ..base import StageContext

# Re-export prefix constants for backward compatibility
# (consumers import them from .helpers).
DEDUP_DUPLICATE_PREFIX: str = _constants.DEDUP_DUPLICATE_PREFIX
DEDUP_ALREADY_DONE_PREFIX: str = _constants.DEDUP_ALREADY_DONE_PREFIX
FRESHNESS_STALE_PREFIX: str = _constants.FRESHNESS_STALE_PREFIX
OBSOLESCENCE_GAP_PREFIX: str = _constants.OBSOLESCENCE_GAP_PREFIX
REFINE_MILL_MISROUTE_PREFIX: str = _constants.REFINE_MILL_MISROUTE_PREFIX
REFINE_MILL_CONSUMER_FOLLOWUP_PREFIX: str = (
    _constants.REFINE_MILL_CONSUMER_FOLLOWUP_PREFIX
)
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


def _is_doc_only_change(draft: str, title: str = "") -> bool:
    """Return True if *draft* describes a documentation-only change.

    Considers a change doc-only when every file path extracted from the
    draft is a Markdown or docs path (``docs/**``, ``*.md``,
    ``CHANGELOG.md``) and no code/config file (``.py``, ``.ts``,
    ``.js``, ``.yaml``, ``.yml``) is mentioned.
    """
    text = f"{title}\n\n{draft}" if title else draft
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


def _load_refine_memory(s: Settings, memory_board_id: str) -> str:
    """Load the refine memory ledger from the DB-backed store.

    Falls back to the legacy Markdown file when the DB row doesn't exist
    yet (first run after migration).  The file path is still resolved via
    ``s.memory_file_for("refine", memory_board_id)`` but only used as a
    fallback; the DB is the primary store.
    """
    from robotsix_mill.core.db import load_memory_db
    from robotsix_mill.runners.pass_runner import load_memory as _file_load

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
        State.MAINTENANCE,
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


# Short pointer phrases a refine/conciseness agent sometimes emits in the
# structured spec field *instead of* the actual spec — the real content was
# only in its prose ("…as written above"). Matched against a normalized,
# length-capped string so a genuine (always far longer) spec never trips it.
_PLACEHOLDER_SPEC_PHRASES = (
    "see spec above",
    "see the spec above",
    "see above",
    "see spec",
    "see the spec",
    "see description",
    "see the description",
    "spec above",
    "as above",
    "as written above",
    "see previous",
    "see below",
    "refer to spec",
    "tbd",
    "todo",
)


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
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
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
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max_lines)
        return "".join(tail).rstrip("\n")
    except OSError:
        return ""


def _spec_is_degenerate(spec: str | None) -> bool:
    """True when *spec* is empty or a placeholder pointer, not a real spec.

    The refine agent's structured ``spec_markdown`` occasionally collapses
    to a short reference like ``"(see spec above)"`` — non-empty, so the
    bare ``not spec.strip()`` guard misses it, and refine writes the
    pointer straight into the canonical ``description.md`` (blanking the
    ticket body on the board). Treat such degenerate output as "no spec"
    so refine falls back to the original draft instead of clobbering it.

    Only short (≤120-char) single-idea strings can match; a genuine spec
    is much longer, so real content is never dropped.
    """
    text = (spec or "").strip()
    if not text:
        return True
    if len(text) > 120:
        return False
    # Drop markdown/punctuation, collapse whitespace, lowercase.
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())
    if not norm:
        return True
    return any(p in norm for p in _PLACEHOLDER_SPEC_PHRASES)


# --- external-fix claim detection (live re-verification gate) ---------------
# A refine ``no_change_needed`` verdict that asserts the work was *already
# shipped elsewhere* must NOT be trusted on its word: the 2026-06-09 incident
# closed a live CI-failure ticket as a duplicate while the fix had been
# reverted at HEAD. ``git merge-base --is-ancestor`` cannot detect that
# (the reverted fix commit is still an ancestor). So when this detector
# fires, the stage routes the ticket to implement for a live re-check
# instead of closing to DONE.

# Unambiguous "already shipped elsewhere" phrases — any single one fires.
_EXTERNAL_FIX_PHRASES: tuple[str, ...] = (
    "already implemented",
    "already fixed",
    "already shipped",
    "already merged",
    "already applied",
    "already resolved",
    "already done",
    "duplicate of",
    "shipped the fix",
    "parallel ticket",
)

# Repo ticket-id shape and commit-SHA-like token.
_TICKET_ID_RE = re.compile(r"\b\d{8}T\d{6}Z\b")
_COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
# PR / MR number references: e.g. "#1386", "!123", "PR #42".
_PR_MR_REF_RE = re.compile(r"(?<!\w)(?:#|!)\d+\b")

# Resolved-claim verbs that, co-occurring with a cited ticket id / commit /
# PR/MR reference, imply an external-fix claim even without one of the
# canned phrases.
_RESOLVED_VERB_RE = re.compile(
    r"\b(implemented|fixed|shipped|merged|applied|resolved|addressed|landed)\b"
)

# Markers of the two legitimate no-change subclasses that must KEEP closing
# to DONE: detector false-positives and information-only deliverables. They
# suppress only the fuzzy ref+verb co-occurrence rule (never an unambiguous
# external-fix phrase).
_FALSE_POSITIVE_MARKERS: tuple[str, ...] = (
    "does not exist",
    "doesn't exist",
    "false positive",
    "disproves",
    "not actually",
    "cannot reproduce",
    "can't reproduce",
)
_INFO_ONLY_MARKERS: tuple[str, ...] = (
    "post a comment",
    "documenting",
    "information-only",
    "informational",
    "explaining why",
)


def _rationale_claims_external_fix(rationale: str) -> bool:
    """Return ``True`` when *rationale* asserts the work is already done
    elsewhere (so the fix's live presence must be re-verified, not trusted).

    Fires on an unambiguous "already shipped elsewhere" phrase, or on a
    cited ticket id / commit SHA / PR-MR number co-occurring with a
    resolved-claim verb.
    Returns ``False`` for the two legitimate no-change subclasses — detector
    false-positives and information-only deliverables — so they keep closing
    to DONE.  Empty/whitespace rationale → ``False``.  Bias is toward NOT
    firing except when an external-fix verb is unambiguously present.
    """
    text = (rationale or "").strip().lower()
    if not text:
        return False

    # Unambiguous external-fix phrase → fire regardless of other markers.
    for phrase in _EXTERNAL_FIX_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", text):
            return True

    # The two legitimate subclasses suppress the fuzzy co-occurrence rule.
    if any(m in text for m in _FALSE_POSITIVE_MARKERS) or any(
        m in text for m in _INFO_ONLY_MARKERS
    ):
        return False

    # Cited ticket id / commit SHA / PR-MR number co-occurring with a
    # resolved-claim verb.
    has_ref = bool(
        _TICKET_ID_RE.search(text)
        or _COMMIT_SHA_RE.search(text)
        or _PR_MR_REF_RE.search(text)
    )
    if has_ref and _RESOLVED_VERB_RE.search(text):
        return True

    return False


def _verify_cited_fix_at_head(repo_dir: Path | None, rationale: str) -> bool:
    """Best-effort: whether a SHA cited in *rationale* is a valid commit that
    is an ancestor of ``origin/main``.

    For logging/enrichment only — it MUST NOT short-circuit back to DONE,
    because ancestry passing does not prove the fix is live (a revert leaves
    the original fix commit as an ancestor while the bug is back). Mirrors the
    defensive subprocess style of ``_verify_branch_merged``: any git error is
    swallowed and the function returns ``False`` (nothing proven).
    """
    if repo_dir is None:
        return False
    shas = _COMMIT_SHA_RE.findall((rationale or "").lower())
    if not shas:
        return False
    try:
        for sha in shas:
            type_check = subprocess.run(
                ["git", "-C", str(repo_dir), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
            )
            if type_check.returncode != 0 or type_check.stdout.strip() != "commit":
                continue
            anc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "merge-base",
                    "--is-ancestor",
                    sha,
                    "origin/main",
                ],
                capture_output=True,
                text=True,
            )
            if anc.returncode == 0:
                return True
    except Exception:  # noqa: BLE001 — best-effort; never raise out of the stage
        log.debug(
            "cited-fix ancestry check failed for rationale — ignoring",
            exc_info=True,
        )
    return False


def _verify_branch_merged(repo_dir: Path | None, ticket: Ticket) -> bool:
    """Check whether *ticket*'s branch is an ancestor of the base branch.

    When a redrafted ticket has a branch from a prior implement run,
    the refine agent may conclude ``no_change_needed`` because the
    implementation already exists — but if the branch was never merged
    to the base branch, closing as DONE strands the work on an orphaned
    branch.

    Returns ``True`` when the branch is confirmed merged to the base
    branch, or when the check cannot be performed (best-effort: we
    never block a ticket on a transient git error).

    Returns ``False`` only when the branch is confirmed **unmerged**
    — i.e. ``git merge-base --is-ancestor <branch> origin/main`` exits 1.

    When the branch cannot be fetched from origin (e.g. it was committed
    locally by a prior implement run but never pushed), fall back to the
    **local** branch ref ``refs/heads/<branch>`` and check its ancestry
    against ``origin/main``.  Only when neither an origin branch nor a
    local ref can be resolved do we best-effort ``return True`` — there
    is then genuinely nothing to verify.
    """
    if repo_dir is None or not ticket.branch:
        # Nothing to verify — let the no-change-needed pass through.
        return True

    branch = ticket.branch
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin", branch],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        # The branch is absent from origin (fetch failed). It may still
        # exist as a local-only ref committed by a prior implement run
        # that never pushed. Fall back to the local ref before allowing
        # the no-change-needed pass-through — otherwise a complete,
        # working feature strands on an orphaned local WIP commit.
        log.debug(
            "%s: cannot fetch branch '%s' from origin — "
            "falling back to local ref for merge check",
            ticket.id,
            branch,
        )
        local_ref = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
        )
        if local_ref.returncode != 0:
            # Neither an origin branch nor a local ref — nothing to
            # verify, best-effort allow.
            log.debug(
                "%s: branch '%s' resolves on neither origin nor locally "
                "— allowing no-change-needed (best-effort)",
                ticket.id,
                branch,
            )
            return True
        local_check = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "merge-base",
                "--is-ancestor",
                f"refs/heads/{branch}",
                "origin/main",
            ],
            capture_output=True,
            text=True,
        )
        if local_check.returncode == 0:
            return True  # local branch is merged
        if local_check.returncode == 1:
            # Squash-merge detection: search main for a commit
            # referencing this ticket.
            grep = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "log",
                    "origin/main",
                    "--oneline",
                    "--fixed-strings",
                    f"--grep={ticket.id}",
                ],
                capture_output=True,
                text=True,
            )
            if grep.returncode == 0 and grep.stdout.strip():
                log.info(
                    "%s: local branch '%s' is not an ancestor of "
                    "origin/main, but a commit referencing this ticket "
                    "was found on origin/main — treating as squash-merged",
                    ticket.id,
                    branch,
                )
                return True
            log.info(
                "%s: local branch '%s' is NOT an ancestor of origin/main "
                "— implementation unmerged",
                ticket.id,
                branch,
            )
            return False  # local branch is unmerged
        # Any other exit code (git error) — best-effort, don't block.
        log.debug(
            "%s: local merge-base check failed for branch '%s' — "
            "allowing no-change-needed (best-effort)",
            ticket.id,
            branch,
        )
        return True

    # git merge-base --is-ancestor: exit 0 = is ancestor, 1 = not ancestor
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "merge-base",
            "--is-ancestor",
            f"origin/{branch}",
            "origin/main",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True  # branch is merged
    if result.returncode == 1:
        # Squash-merge detection: search main for a commit
        # referencing this ticket.
        grep = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "log",
                "origin/main",
                "--oneline",
                "--fixed-strings",
                f"--grep={ticket.id}",
            ],
            capture_output=True,
            text=True,
        )
        if grep.returncode == 0 and grep.stdout.strip():
            log.info(
                "%s: branch '%s' is not an ancestor of origin/main, "
                "but a commit referencing this ticket was found on "
                "origin/main — treating as squash-merged",
                ticket.id,
                branch,
            )
            return True
        log.info(
            "%s: branch '%s' is NOT an ancestor of main — implementation unmerged",
            ticket.id,
            branch,
        )
        return False  # branch is unmerged

    # Any other exit code (git error) — best-effort, don't block.
    log.debug(
        "%s: merge-base check failed for branch '%s' — "
        "allowing no-change-needed (best-effort)",
        ticket.id,
        branch,
    )
    return True


def _draft_has_complete_spec(text: str) -> bool:
    """True when *text* is already a self-contained spec.

    Heuristic: the draft contains a markdown ``## Problem`` heading AND
    at least one of ``## Scope`` / ``## Acceptance criteria`` (case-
    insensitive, matched only on markdown heading lines). This guards the
    CI fast-path: a CI ticket whose draft is a raw error dump with no
    scope section still routes to the full refine agent.
    """
    if not text or not text.strip():
        return False

    # Match headings only at the start of a line (allow leading whitespace)
    # with 1-6 `#` characters.
    def _has_heading(title: str) -> bool:
        return bool(
            re.search(
                r"^\s*#{1,6}\s+" + re.escape(title) + r"\b",
                text,
                re.IGNORECASE | re.MULTILINE,
            )
        )

    if not _has_heading("Problem"):
        return False
    for h in ("Scope", "Acceptance criteria", "Acceptance"):
        if _has_heading(h):
            return True
    return False


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
}

# Substrings (lowercased) in a triage note that signal a draft should NOT be
# auto-approved — even for _AUTO_APPROVE_SOURCES.  When any pattern matches,
# _resolve_next_state returns HUMAN_ISSUE_APPROVAL instead of READY so a human
# can inspect the ticket.  The list is deliberately conservative: a legitimate
# SKIP reason should never contain one of these phrases.
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


def _summarize_spec_for_auto_approve(spec: str, max_chars: int = 2000) -> str:
    """Return a bounded summary of *spec* for the auto-approve classifier.

    The auto-approve classifier only needs to detect design decisions —
    it doesn't need the full verbose spec.  Keep the first *max_chars*
    characters (which covers ## Problem + ## Scope + start of
    ## Acceptance criteria for typical specs), newline-aligned so
    sections aren't truncated mid-word.  When the spec is already within
    the limit, return it unchanged.
    """
    if len(spec) <= max_chars:
        return spec
    note = "\n… (truncated for auto-approve — full spec is available)"
    # Truncate to max_chars minus note length, then back up to the last newline.
    effective = max_chars - len(note)
    if effective <= 0:
        return spec[:max_chars]
    truncated = spec[:effective]
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]
    return truncated + note


def _resolve_next_state(
    ctx: StageContext,
    spec: str,
    ticket_id: str,
    source: str | None = None,
    *,
    triage_note: str | None = None,
) -> tuple[State, str | None]:
    """Return (next_state, auto_approve_note_or_None).

    Encapsulates the decision: if approval is not required → READY;
    if auto-approve is disabled → HUMAN_ISSUE_APPROVAL; otherwise run
    the auto-approve triage on the spec → READY on APPROVE (no design
    decision found), HUMAN_ISSUE_APPROVAL otherwise (or on error).
    Empty/whitespace specs skip the triage entirely and go to
    HUMAN_ISSUE_APPROVAL when gated, mirroring the original behaviour.

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
        return State.HUMAN_ISSUE_APPROVAL, None
    if not ctx.settings.auto_approve_enabled:
        return State.HUMAN_ISSUE_APPROVAL, None
    # Deterministic auto-approve for sources whose drafts are
    # internal-only by construction: they're proposed by mill's own
    # periodic agents (audit, agent_check, bc_check, …) whose scope
    # is dead-code removal, prompt updates, memory ledger structure,
    # config cleanup, docstring additions — no behavioural risk a
    # human reviewer can meaningfully veto. test_gap (the original
    # rule in 28a6b02) joins the same family. Three rounds of
    # rubber-stamping all 21+ tickets from these sources without
    # rejection (see 09cc) made the LLM hop pure toil.
    if triage_note:
        triage_lower = triage_note.lower()
        for pattern in _TRIAGE_REJECTION_PATTERNS:
            if pattern in triage_lower:
                return State.HUMAN_ISSUE_APPROVAL, (
                    f"auto-approve: REJECTED — triage note contains "
                    f"rejection signal matching '{pattern}'"
                )
    if source in _AUTO_APPROVE_SOURCES:
        return State.READY, (
            f"auto-approve: APPROVE — {source} (deterministic rule: "
            "mill-internal periodic-agent proposal, no design risk)"
        )
    try:
        result = refining.triage_auto_approve(
            settings=ctx.settings,
            spec=_summarize_spec_for_auto_approve(spec),
        )
        if result.decision == "APPROVE":
            return State.READY, f"auto-approve: APPROVE — {result.reason}"
        # NEEDS_APPROVAL — return the reason as a structured history
        # note (no side-effect comment; this is the sole surface).
        return (
            State.HUMAN_ISSUE_APPROVAL,
            f"auto-approve: NEEDS_APPROVAL — {result.reason}",
        )
    except Exception:
        log.warning(
            "auto-approve triage failed, falling back to human approval",
            exc_info=True,
        )
    return (
        State.HUMAN_ISSUE_APPROVAL,
        "auto-approve: triage failed — falling back to human approval",
    )


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
