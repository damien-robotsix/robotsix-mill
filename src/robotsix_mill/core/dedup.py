"""Shared ticket-dedup primitives.

Source-agnostic helpers for spotting that a would-be new ticket
duplicates one that was recently filed (or already shipped). Extracted
from ``trace_review_runner`` so multiple producers (trace-review,
epic-decomposition pre-filing checks, …) share one matching seam
instead of each growing its own copy.

The matcher is best-effort: any query failure logs and returns
``None`` rather than raising into the caller.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone
from typing import Literal

from ..config import Settings
from .models import SourceKind, Ticket
from .service import TicketService
from .states import State
from .workspace import Workspace

log = logging.getLogger("robotsix_mill.core.dedup")


def normalize(s: str) -> str:
    """Lower-case *s* and collapse every run of non-alphanumeric
    characters into a single space, stripping the ends."""
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


# ---------------------------------------------------------------------------
# _ci_draft_fingerprint
# ---------------------------------------------------------------------------

# Metadata line prefixes from CI monitor draft bodies.
_CI_DRAFT_META_PREFIXES = (
    "**Workflow:**",
    "**Path:**",
    "**Branch:**",
    "**Run:**",
    "**Commit:**",
    "**Created:**",
)

# ANSI escape sequences (e.g. ``\x1b[31m``, ``\x1b[0m``).
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# ISO 8601 timestamp: ``2024-01-15T10:30:45Z``, ``2024-01-15T10:30:45.123456+00:00``.
_ISO8601_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)

# GitHub Actions run URL: ``https://github.com/owner/repo/actions/runs/1234567890/…``.
_GH_RUN_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/actions/runs/\d+\S*")


def _ci_draft_fingerprint(body: str, *, path: str = "") -> str:
    """Compute a stable hex fingerprint from a CI monitor draft *body*.

    Strips metadata lines, run-specific data (URLs, commit SHAs,
    timestamps), and ANSI escapes so the fingerprint reflects the
    ERROR CONTENT only.  When *path* is non-empty (the workflow file
    path, e.g. ``.github/workflows/ci.yml``), it is prepended so
    that the same error in different workflows produces distinct
    fingerprints.  Returns the first 16 hex digits of SHA-256.
    """
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        # Skip metadata lines.
        if any(stripped.startswith(p) for p in _CI_DRAFT_META_PREFIXES):
            continue
        # Strip ANSI escapes.
        cleaned = _ANSI_ESCAPE_RE.sub("", stripped)
        # Strip timestamps.
        cleaned = _ISO8601_RE.sub("", cleaned)
        # Strip GitHub run URLs.
        cleaned = _GH_RUN_URL_RE.sub("", cleaned)
        if cleaned:
            lines.append(cleaned)

    # Collapse remaining whitespace.
    collapsed = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if path:
        collapsed = path + "\n" + collapsed
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Private helpers for find_prior_matching_ticket
# ---------------------------------------------------------------------------


def _is_eligible_candidate(
    ticket: Ticket,
    exclude_ids: Collection[str],
    cutoff: datetime,
    service: TicketService,
) -> bool:
    """Return True if *ticket* passes all pre-match guard clauses."""
    if ticket.id in exclude_ids:
        return False
    created_at = ticket.created_at
    if created_at is None:
        return False
    # Normalize to UTC-aware before comparing.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if created_at < cutoff:
        return False

    state = ticket.state
    if state == State.ERRORED:
        return False
    if state == State.CLOSED:
        history = service.history(ticket.id)
        if not any(ev.state == State.DONE for ev in history):
            return False
        # else: CLOSED after DONE → eligible
    return True


def _label_gate(
    ticket: Ticket,
    dedup_labels: list[str] | None,
) -> Literal["match", "skip", "continue"]:
    """Evaluate label-based dedup for *ticket*.

    Returns:
        ``"match"`` — *ticket* carries a matching label; return it immediately.
        ``"skip"`` — ``ci_fp:*`` mismatch; skip this candidate entirely.
        ``"continue"`` — no label signal; fall through to subsequent checks.
    """
    if not dedup_labels:
        return "continue"

    cand_labels: list[str] = []
    if ticket.labels:
        try:
            parsed = json.loads(ticket.labels)
            if isinstance(parsed, list):
                cand_labels = parsed
        except json.JSONDecodeError, TypeError:
            pass

    if any(label in cand_labels for label in dedup_labels):
        return "match"

    # Label mismatch: a ``ci_fp:*`` fingerprint differs — skip this
    # candidate entirely rather than falling through to the weak
    # title-only fallback.
    if any(label.startswith("ci_fp:") for label in dedup_labels):
        return "skip"

    return "continue"


def _is_path_strong(
    matched: list[str],
    body: str,
    require_scope_for_single_path: bool,
) -> bool:
    """Decide whether matched paths strongly corroborate a duplicate."""
    if len(matched) >= 2:
        return True  # ≥2 distinct shared paths
    if not require_scope_for_single_path:
        return True  # permissive prose-mention rule
    return matched[0] in _scope_paths(body)  # declared in scope section


def _concern_gate(
    ticket_title: str,
    body: str,
    target_concern_tokens: set[str],
    concern_min_overlap: int,
) -> bool:
    """Return True if concern-token overlap supports the path match."""
    cand_concern = _extract_concern_tokens(ticket_title + "\n" + body)

    if target_concern_tokens and cand_concern:
        overlap = target_concern_tokens & cand_concern
        return len(overlap) >= concern_min_overlap
    # One or both sides have no concern tokens.
    if concern_min_overlap > 1:
        return False  # absence of tokens not evidence of sameness
    return True  # conservative: cannot determine difference


def _path_match(
    ticket: Ticket,
    board_id: str,
    settings: Settings,
    target_files: list[str],
    require_scope_for_single_path: bool,
    target_concern_tokens: set[str] | None,
    concern_min_overlap: int,
) -> bool:
    """Return True if *ticket* matches via shared file paths."""
    if not target_files:
        return False

    body = Workspace(
        settings.workspaces_dir_for(ticket.board_id or board_id),
        ticket.id,
    ).read_description()
    matched = [p for p in target_files if p and p in body]
    if not matched:
        return False
    if not _is_path_strong(matched, body, require_scope_for_single_path):
        return False

    if target_concern_tokens is not None:
        return _concern_gate(
            ticket.title, body, target_concern_tokens, concern_min_overlap
        )
    return True


def _fingerprint_match(
    ticket: Ticket,
    fingerprint: str,
    suppress_title_only_match: bool,
) -> bool:
    """Return True if *ticket*'s normalized title contains *fingerprint*."""
    if suppress_title_only_match:
        return False
    if not fingerprint:
        return False
    return fingerprint in normalize(ticket.title)


def find_prior_matching_ticket(
    service: TicketService,
    board_id: str,
    target_files: list[str],
    fingerprint_text: str,
    settings: Settings,
    now: datetime,
    *,
    sources: Sequence[SourceKind] | None = None,
    lookback_days: int = 7,
    exclude_ids: Collection[str] = (),
    require_scope_for_single_path: bool = False,
    target_concern_tokens: set[str] | None = None,
    concern_min_overlap: int = 1,
    dedup_labels: list[str] | None = None,
    suppress_title_only_match: bool = False,
) -> Ticket | None:
    """Look up recent tickets on *board_id* and return the first one
    that matches the given fix signal.

    A candidate matches when, within the recency window
    (``now - timedelta(days=lookback_days)``):
    - any path in *target_files* appears verbatim in the candidate's
      description body, OR
    - the normalized fingerprint (first ~60 normalized chars of
      *fingerprint_text*) appears in the candidate's normalized title.

    *sources* restricts the candidate pool: ``None`` matches across
    every source, a sequence unions the listed kinds. *exclude_ids*
    skips candidates by ``id`` (e.g. the epic itself and its existing
    children).

    Candidates in ERRORED state, and CLOSED candidates that were never
    DONE (declined drafts), are EXCLUDED — neither is a fix, so a new
    occurrence deserves a fresh draft.

    When *require_scope_for_single_path* is ``True``, a lone shared path
    only flags when the candidate *declares* it as modified (it appears
    in the candidate's ``## Scope`` / ``## Acceptance`` / ``file_map``
    block, per :func:`_scope_paths`); a path mentioned only in prose (or
    under an ``## Out of scope`` heading) no longer drives the match.
    Two or more distinct shared paths always corroborate regardless of
    the flag. Defaults to ``False`` (the permissive prose-mention rule)
    so the trace-review and AGENT.md-proposal callers are unaffected.

    When *target_concern_tokens* is a non-empty set, a path match is
    suppressed when the candidate's backtick-enclosed concern tokens
    (extracted from its title and declared-scope body sections) are also
    non-empty and share fewer than *concern_min_overlap* tokens with
    *target_concern_tokens* — i.e. the two tickets touch the same file
    but name completely different symbols/concerns.  When either side has
    no concern tokens and *concern_min_overlap* > 1, the path match is
    also suppressed (absence of symbols is not evidence of sameness when
    the caller requires multiple substantive tokens).  When
    *concern_min_overlap* is 1 (the default), the legacy conservative
    rule applies: a path match is suppressed only when both sides have
    concern tokens AND none overlap.

    When *suppress_title_only_match* is ``True``, the final title-fingerprint
    fallback is skipped: a candidate is never returned solely because its
    normalized title contains the fingerprint.  Matches driven by
    *dedup_labels*, shared file paths (with concern-token gating), or a
    corroborated scope section still proceed.  Defaults to ``False`` so
    every existing caller's behaviour is preserved byte-for-byte.

    Returns ``None`` when no match is found.
    """
    try:
        cutoff = now - timedelta(days=lookback_days)
        candidates = service.recent_tickets(limit=200, sources=sources)
        fingerprint = normalize(fingerprint_text)[:60]
        # fmt: off
        for ticket in candidates:
            if not _is_eligible_candidate(ticket, exclude_ids, cutoff, service):
                continue

            gate = _label_gate(ticket, dedup_labels)
            if gate == "match":
                return ticket
            if gate == "skip":
                continue

            if _path_match(
                ticket, board_id, settings, target_files,
                require_scope_for_single_path, target_concern_tokens, concern_min_overlap,
            ):
                return ticket

            if _fingerprint_match(ticket, fingerprint, suppress_title_only_match):
                return ticket
        # fmt: on
        return None
    except Exception:  # noqa: BLE001 — best-effort dedup
        log.exception("dedup: find_prior_matching_ticket failed")
        return None


# Path-like tokens carry a file extension, optionally prefixed by one or
# more directory segments — e.g. ``ci.yml``, ``CONTRIBUTING.md``,
# ``tests/foo/test_bar.py``. Used to extract a child's ``target_files``
# from its free-text body for the overlap checks below.
#
# Multi-segment tokens (those containing ``/``) stay permissive — any
# alpha-leading extension counts, so real paths like ``tests/foo/test_bar.py``
# still match. Single-segment tokens must end in a recognised source/file
# extension; this keeps dotted prose fragments like ``e.g`` / ``i.e`` (from
# "e.g." / "i.e.") from being mistaken for file paths.
_SOURCE_EXT = "py|md|yml|yaml|json|toml|js|mjs|cfg|ini|txt|sh|html|css"
_PATH_TOKEN_RE = re.compile(
    r"[\w.+-]+/[\w.+-]+(?:/[\w.+-]+)*\.[A-Za-z][A-Za-z0-9]{0,6}\b"
    rf"|[\w.+-]+\.(?:{_SOURCE_EXT})\b"
)

# Backtick-enclosed code tokens — e.g. `` `new_model()` ``, `` `.secrets.baseline` ``,
# `` `OpenRouterProvider` ``.  These are high-signal concern indicators: when two
# tickets share a file path but their backtick-enclosed tokens are disjoint, the
# tickets are about unrelated concerns within that file and the path match is a
# false positive.
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# Punctuation-only token — e.g. `` ``, ``, `` `-` ``, `` `_` ``, `` `...` `` —
# carries no semantic signal and must not drive concern-token overlap decisions.
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")


def _extract_concern_tokens(text: str) -> set[str]:
    """Return the set of backtick-enclosed code tokens in *text*.

    Each token is stripped of surrounding whitespace.  Tokens that are
    punctuation-only (matching :data:`_PUNCT_ONLY_RE`) or that look like
    file paths (matching :data:`_PATH_TOKEN_RE`) are excluded — they
    carry no signal about what *concern* within the file is being
    addressed.

    An empty set is returned when *text* contains no backtick-enclosed
    spans — this is a meaningful signal: the author did not name a
    specific symbol, so we cannot determine whether concerns differ.
    """
    return {
        m.group(1).strip()
        for m in _BACKTICK_RE.finditer(text or "")
        if not _PUNCT_ONLY_RE.match(m.group(1).strip())
        and not _PATH_TOKEN_RE.fullmatch(m.group(1).strip())
    }


def _extract_paths(text: str) -> list[str]:
    """Extract de-duplicated path-like tokens from *text*, preserving
    first-seen order."""
    out: list[str] = []
    for tok in _PATH_TOKEN_RE.findall(text or ""):
        if tok not in out:
            out.append(tok)
    return out


def paths_excluding_out_of_scope(text: str) -> list[str]:
    """Like :func:`_extract_paths`, but skip tokens inside out-of-scope
    and cross-reference regions.

    Recognises two exclusion-marker styles:

    - **Markdown heading** (``## Out of scope``, ``### Explicitly out of
      scope``, ``## Reference``, ``## See also``, ``## Related work``,
      and any heading whose title starts with ``reference`` or
      ``see also``): exclusion runs from the heading through to the
      next markdown heading (inclusive).  A non-excluded heading
      resumes capture.
    - **Inline marker** (``**Explicitly out of scope — …**``, ``- Out of
      scope: …``): exclusion runs from that line through the next blank
      line OR the next markdown heading (paragraph boundary).

    A path token that appears in BOTH an in-scope section AND an
    out-of-scope section is still returned (because of the in-scope
    occurrence).  De-duplicated, first-seen order.

    Total/defensive: any parsing failure logs and returns an empty list
    rather than raising into the caller."""
    try:
        captured: list[str] = []
        excluding = False
        exclusion_mode: str | None = None  # 'heading' or 'inline'

        for line in (text or "").splitlines():
            stripped = line.strip()
            is_heading = stripped.startswith("#")

            if is_heading:
                title = stripped.lstrip("#*- ").strip().casefold()
                if (
                    title.startswith("out of scope")
                    or title.startswith("explicitly out of scope")
                    or title.startswith("reference")
                    or title.startswith("see also")
                    or title.startswith("related work")
                ):
                    excluding = True
                    exclusion_mode = "heading"
                else:
                    excluding = False
                    exclusion_mode = None
                continue

            # Non-heading line: blank line ends inline-mode exclusion.
            if excluding and exclusion_mode == "inline" and stripped == "":
                excluding = False
                exclusion_mode = None
                captured.append(line)
                continue

            # Still inside an exclusion region (any mode).
            if excluding:
                continue

            # Not excluding — check for an inline exclusion marker.
            cleaned = stripped.lstrip("#*- ").strip().casefold()
            if cleaned.startswith("out of scope") or cleaned.startswith(
                "explicitly out of scope"
            ):
                excluding = True
                exclusion_mode = "inline"
                continue

            captured.append(line)

        return _extract_paths("\n".join(captured))
    except Exception:  # noqa: BLE001 — best-effort
        log.exception("dedup: paths_excluding_out_of_scope failed")
        return []


def _scope_paths(text: str) -> set[str]:
    """Return the path-like tokens (per :data:`_PATH_TOKEN_RE`, via
    :func:`_extract_paths`) that appear within a ticket body's
    *declared-modification* sections only.

    Captured blocks:
    - Markdown heading sections (``#`` / ``##`` / ``###``, case-insensitive)
      whose title — after stripping leading ``#`` and surrounding
      whitespace — *starts with* ``scope`` or ``acceptance`` (covers
      ``## Scope``, ``## Acceptance``, ``## Acceptance criteria``), up to
      the next heading or end-of-text.
    - A ``file_map`` block: a heading whose title starts with ``file map``
      / ``file_map``, or a fenced ```` ```file_map ```` block.

    Sections whose heading starts with ``out of scope`` are explicitly
    EXCLUDED, so a path declared *not* modified never counts. Paths in
    free prose / problem-statement paragraphs are likewise excluded.

    Total/defensive: any parsing failure logs and returns an empty set
    rather than raising into the caller."""
    try:
        captured: list[str] = []
        capturing = False
        in_fenced_file_map = False
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                fence_lang = stripped[3:].strip().casefold()
                if in_fenced_file_map:
                    in_fenced_file_map = False
                elif fence_lang == "file_map":
                    in_fenced_file_map = True
                continue
            if in_fenced_file_map:
                captured.append(line)
                continue
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip().casefold()
                if title.startswith("out of scope"):
                    capturing = False
                elif (
                    title.startswith("scope")
                    or title.startswith("acceptance")
                    or title.startswith("file map")
                    or title.startswith("file_map")
                ):
                    capturing = True
                else:
                    capturing = False
                continue
            if capturing:
                captured.append(line)
        return set(_extract_paths("\n".join(captured)))
    except Exception:  # noqa: BLE001 — best-effort
        log.exception("dedup: _scope_paths failed")
        return set()


def _describe_recent_signal(
    ticket: Ticket,
    paths: list[str],
    settings: Settings,
    fallback_board_id: str,
    *,
    target_concern_tokens: set[str] | None = None,
) -> str:
    """Best-effort description of which signal matched *ticket*: a shared
    file path (preferred, matching ``find_prior_matching_ticket``'s order)
    or the title overlap.

    Stays consistent with the strict single-path rule: a lone shared path
    is only reported as a ``file path`` when the candidate *declares* it
    (it is in :func:`_scope_paths`); ≥2 shared paths always count as a
    ``file path`` match. Otherwise the advisory reports ``title overlap``
    so it never claims a path signal that did not drive the match.

    When *target_concern_tokens* is supplied and at least one token also
    appears in the candidate's backtick-enclosed symbols, the description
    includes the overlapping token(s) — e.g.
    ``file path `src/foo.py` (symbol `new_model`)`` — so the advisory
    notes *which* specific symbol/function/class overlaps rather than just
    the file path.
    """
    try:
        if paths:
            body = Workspace(
                settings.workspaces_dir_for(ticket.board_id or fallback_board_id),
                ticket.id,
            ).read_description()
            matched = [p for p in paths if p and p in body]
            if len(matched) >= 2:
                desc = f"file path `{matched[0]}`"
            elif len(matched) == 1 and matched[0] in _scope_paths(body):
                desc = f"file path `{matched[0]}`"
            else:
                return "title overlap"

            # Enrich with overlapping concern tokens when available.
            if target_concern_tokens:
                cand_concern = _extract_concern_tokens(ticket.title + "\n" + body)
                overlap = target_concern_tokens & cand_concern
                if overlap:
                    symbols = sorted(overlap)
                    quoted = "`, `".join(symbols[:3])
                    if len(symbols) > 3:
                        quoted += "`, …"
                    desc += f" (symbol `{quoted}`)"
            return desc
    except Exception:  # noqa: BLE001 — best-effort
        log.exception("dedup: _describe_recent_signal failed for %s", ticket.id)
    return "title overlap"


def annotate_child_body(
    body: str,
    note: str,
    *,
    source_desc: str = "epic-decomposition pre-filing dedup",
) -> str:
    """Prepend an advisory ``[!warning]`` blockquote naming the suspected
    overlap to *body* so a later refine cycle sees the flag and can
    close-as-duplicate cheaply. Surfaces the overlap without dropping the
    work. *source_desc* names the producer of the flag (e.g.
    ``"draft-intake pre-refine dedup"`` for independent drafts)."""
    block = (
        f"> [!warning] {note}\n"
        ">\n"
        f"> _Advisory flag from {source_desc}; "
        "verify and close as duplicate during refine if confirmed._\n\n"
    )
    return block + (body or "")


def find_inflight_overlap(
    service: TicketService,
    ticket_id: str,
    title: str,
    body: str,
    settings: Settings,
    now: datetime,
    *,
    dedup_labels: list[str] | None = None,
) -> str | None:
    """Advisory pre-refine dedup for an INDEPENDENT (non-epic) draft.

    Reuses :func:`find_prior_matching_ticket` to spot a recent ticket
    whose scope overlaps *title* / *body* within
    ``settings.epic_dedup_lookback_days`` — crucially including
    CONCURRENT in-flight ones (DRAFT/READY/REFINING/IMPLEMENT, not just
    DONE), the structural gap the refine dedup guard cannot close
    (it only short-circuits against a genuinely-DONE candidate). The
    draft itself (*ticket_id*) is excluded so it does not self-match.

    Path-like tokens are extracted from *body* as ``target_files`` and
    *title* is the ``fingerprint_text``, exactly as
    :func:`find_child_overlaps` does for epic children.  Concern tokens
    (backtick-enclosed code symbols) are extracted from *title* and
    *body* and passed as ``target_concern_tokens`` so that a lone shared
    file path is NOT flagged when the two tickets name completely
    different symbols (e.g. `` `new_model()` `` vs `` `.secrets.baseline` ``
    within the same test file).

    Returns an advisory note naming the matched ticket on a strong
    match, or ``None`` when nothing overlaps.

    Best-effort: any failure logs and returns ``None`` so refine still
    proceeds.
    """
    try:
        board_id = service.board_id
        paths = _extract_paths(body)
        concern_tokens = _extract_concern_tokens(title + "\n" + body)
        prior = find_prior_matching_ticket(
            service,
            board_id,
            paths,
            title,
            settings,
            now,
            sources=None,
            lookback_days=settings.epic_dedup_lookback_days,
            exclude_ids={ticket_id},
            require_scope_for_single_path=True,
            target_concern_tokens=concern_tokens,
            concern_min_overlap=3,
            dedup_labels=dedup_labels,
            suppress_title_only_match=True,
        )
        if prior is None:
            return None
        signal = _describe_recent_signal(
            prior, paths, settings, board_id, target_concern_tokens=concern_tokens
        )
        return (
            f"Possible duplicate of {prior.id} ({prior.title!r}) — matched on {signal}"
        )
    except Exception:  # noqa: BLE001 — best-effort dedup
        log.exception("dedup: find_inflight_overlap failed")
        return None


def find_agent_md_proposal_overlap(
    service: TicketService,
    section: str,
    rule: str,
    settings: Settings,
    now: datetime,
    *,
    exclude_ids: Collection[str] = (),
) -> Ticket | None:
    """Code-level dedup for a retrospect AGENT.md proposal.

    Reuses :func:`find_prior_matching_ticket` to spot a recent ticket
    whose scope is equivalent to the proposed *section* / *rule* within
    ``settings.epic_dedup_lookback_days`` — crucially including
    CONCURRENT in-flight ones (DRAFT/READY/...) AND merged-then-DONE
    candidates, the two classes the filing path's exact-open-title check
    misses (a rephrased title, or an already-shipped proposal).

    The candidate fingerprint mirrors the SAME title shape the filing
    path constructs (``AGENT.md: <section label> — <rule snippet>``) so
    it matches prior proposal tickets. AGENT.md proposals all share one
    file, so path matching carries no signal — ``target_files`` is empty
    and the title fingerprint does the work.

    Returns the matched :class:`Ticket` on a strong match, or ``None``
    when nothing overlaps.

    Best-effort: any failure logs and returns ``None`` so the proposal
    is still filed.
    """
    try:
        section_label = section.lstrip("#").strip() or "AGENT.md"
        rule_snippet = " ".join(rule.split())  # collapse whitespace
        if len(rule_snippet) > 80:
            rule_snippet = rule_snippet[:79].rstrip() + "…"
        if rule_snippet:
            fingerprint_text = f"AGENT.md: {section_label} — {rule_snippet}"
        else:
            fingerprint_text = f"AGENT.md: {section_label} — proposed rule"
        return find_prior_matching_ticket(
            service,
            service.board_id,
            [],
            fingerprint_text,
            settings,
            now,
            sources=None,
            lookback_days=settings.epic_dedup_lookback_days,
            exclude_ids=exclude_ids,
        )
    except Exception:  # noqa: BLE001 — best-effort dedup
        log.exception("dedup: find_agent_md_proposal_overlap failed")
        return None


def find_child_overlaps(
    service: TicketService,
    parent_epic_id: str,
    child_titles: Sequence[str],
    child_bodies: Sequence[str],
    settings: Settings,
    now: datetime,
) -> list[str | None]:
    """Advisory pre-filing dedup for epic-decomposition children.

    Returns one entry per proposed child (parallel to *child_titles* /
    *child_bodies*): an advisory note describing the suspected overlap,
    or ``None`` when nothing overlaps. For each child, in order:

    1. **Recent-ticket check** (the concurrent independent-ticket class):
       extract path-like tokens from the child body as ``target_files``,
       use the child title as ``fingerprint_text``, and call
       :func:`find_prior_matching_ticket` across every source within
       ``settings.epic_dedup_lookback_days``. The epic and its existing
       children are excluded so they don't self-match.
    2. **In-batch sibling check** (the same-batch overlap class): compare
       the child against earlier siblings accepted in THIS batch by
       shared extracted file path or normalized-title overlap.

    Best-effort: any failure logs and yields all-``None`` so children are
    still filed.
    """
    notes: list[str | None] = [None] * len(child_titles)
    try:
        board_id = service.board_id
        exclude_ids: set[str] = {parent_epic_id}
        try:
            exclude_ids |= {c.id for c in service.list_children(parent_epic_id)}
        except Exception:  # noqa: BLE001 — best-effort
            log.exception("dedup: list_children failed for %s", parent_epic_id)

        # (normalized title, extracted path set) for each accepted sibling.
        accepted: list[tuple[str, set[str]]] = []
        for i, (title, body) in enumerate(zip(child_titles, child_bodies, strict=True)):
            paths = _extract_paths(body)
            concern_tokens = _extract_concern_tokens(title + "\n" + body)
            note: str | None = None

            # 1. Recent shipped/in-flight ticket.
            prior = find_prior_matching_ticket(
                service,
                board_id,
                paths,
                title,
                settings,
                now,
                sources=None,
                lookback_days=settings.epic_dedup_lookback_days,
                exclude_ids=exclude_ids,
                require_scope_for_single_path=True,
                target_concern_tokens=concern_tokens,
            )
            if prior is not None:
                signal = _describe_recent_signal(
                    prior,
                    paths,
                    settings,
                    board_id,
                    target_concern_tokens=concern_tokens,
                )
                note = (
                    f"Possible duplicate of {prior.id} ({prior.title!r}) — "
                    f"matched on {signal}"
                )

            # 2. Earlier sibling in this batch.
            if note is None:
                norm_title = normalize(title)
                path_set = set(paths)
                for j, (sib_title, sib_paths) in enumerate(accepted):
                    shared = path_set & sib_paths
                    if shared:
                        note = (
                            f"Possible duplicate of sibling #{j} "
                            f"in this batch — shared file path "
                            f"`{sorted(shared)[0]}`"
                        )
                        break
                    if (
                        norm_title
                        and sib_title
                        and (norm_title in sib_title or sib_title in norm_title)
                    ):
                        note = (
                            f"Possible duplicate of sibling #{j} "
                            f"in this batch — overlapping title"
                        )
                        break

            notes[i] = note
            accepted.append((normalize(title), set(paths)))
        return notes
    except Exception:  # noqa: BLE001 — best-effort dedup
        log.exception("dedup: find_child_overlaps failed")
        return [None] * len(child_titles)
