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

import logging
import re
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone

from .config import Settings
from .core.models import SourceKind, Ticket
from .core.service import TicketService
from .core.states import State
from .core.workspace import Workspace

log = logging.getLogger("robotsix_mill.dedup")


def normalize(s: str) -> str:
    """Lower-case *s* and collapse every run of non-alphanumeric
    characters into a single space, stripping the ends."""
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


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

    Returns ``None`` when no match is found.
    """
    try:
        cutoff = now - timedelta(days=lookback_days)
        candidates = service.recent_tickets(limit=200, sources=sources)
        fingerprint = normalize(fingerprint_text)[:60]
        for ticket in candidates:
            if ticket.id in exclude_ids:
                continue
            created_at = ticket.created_at
            if created_at is None:
                continue
            # Normalize to UTC-aware before comparing.
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < cutoff:
                continue

            # Classify candidate by state.
            state = ticket.state
            if state == State.ERRORED:
                # Fix attempt failed — let a fresh draft retry.
                continue
            if state == State.CLOSED:
                # Was it ever DONE? If yes, treat as merged-then-closed
                # (a match). If no, it was declined-as-noise; skip.
                history = service.history(ticket.id)
                if not any(ev.state == State.DONE for ev in history):
                    continue
                # else: fall through, this is a match-eligible candidate.
            # DONE or any non-terminal (DRAFT/READY/IMPLEMENTING/etc.)
            # falls through here as a match-eligible candidate.

            # File-path substring check (body).
            if target_files:
                body = Workspace(
                    settings.workspaces_dir_for(ticket.board_id or board_id),
                    ticket.id,
                ).read_description()
                matched = [p for p in target_files if p and p in body]
                if len(matched) >= 2:
                    # ≥2 distinct shared paths is strong corroboration —
                    # flag regardless of the strict-scope flag.
                    return ticket
                if len(matched) == 1:
                    if not require_scope_for_single_path:
                        return ticket
                    if matched[0] in _scope_paths(body):
                        # The candidate declares this path as modified.
                        return ticket
                    # else: lone prose-only path — fall through to the
                    # fingerprint check rather than flagging on prose.

            # Fingerprint check (normalized title).
            if fingerprint and fingerprint in normalize(ticket.title):
                return ticket
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


def _extract_paths(text: str) -> list[str]:
    """Extract de-duplicated path-like tokens from *text*, preserving
    first-seen order."""
    out: list[str] = []
    for tok in _PATH_TOKEN_RE.findall(text or ""):
        if tok not in out:
            out.append(tok)
    return out


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
) -> str:
    """Best-effort description of which signal matched *ticket*: a shared
    file path (preferred, matching ``find_prior_matching_ticket``'s order)
    or the title overlap.

    Stays consistent with the strict single-path rule: a lone shared path
    is only reported as a ``file path`` when the candidate *declares* it
    (it is in :func:`_scope_paths`); ≥2 shared paths always count as a
    ``file path`` match. Otherwise the advisory reports ``title overlap``
    so it never claims a path signal that did not drive the match."""
    try:
        if paths:
            body = Workspace(
                settings.workspaces_dir_for(ticket.board_id or fallback_board_id),
                ticket.id,
            ).read_description()
            matched = [p for p in paths if p and p in body]
            if len(matched) >= 2:
                return f"file path `{matched[0]}`"
            if len(matched) == 1 and matched[0] in _scope_paths(body):
                return f"file path `{matched[0]}`"
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
    :func:`find_child_overlaps` does for epic children. Returns an
    advisory note naming the matched ticket on a strong match, or
    ``None`` when nothing overlaps.

    Best-effort: any failure logs and returns ``None`` so refine still
    proceeds.
    """
    try:
        board_id = service.board_id
        paths = _extract_paths(body)
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
        )
        if prior is None:
            return None
        signal = _describe_recent_signal(prior, paths, settings, board_id)
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
        for i, (title, body) in enumerate(zip(child_titles, child_bodies)):
            paths = _extract_paths(body)
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
            )
            if prior is not None:
                signal = _describe_recent_signal(prior, paths, settings, board_id)
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
