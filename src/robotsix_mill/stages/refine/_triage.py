"""Triage skip logic for the refine stage.

Pre-agent classification: detects no-change / skip /
migrate decisions, split-child fast-path, and sendback re-entry
detection.  Runs before the expensive Opus refine agent so tickets
that don't need refinement can short-circuit early.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...agents import refining
from ...config.settings import Settings
from ...core.models import SourceKind, Ticket, TicketKind
from ...core.service import TicketService
from ...core.states import State
from ...core.text_noop import is_degenerate_body, is_placeholder_ticket
from ...core.workspace import Workspace
from ..base import Outcome, StageContext
from . import _reconcile, _result_paths
from .ci_tooling_policy import (
    CIToolingRoute,
    classify_ci_tooling_proposal,
    resolve_ci_tooling_route,
)
from .helpers import (
    _AUTO_APPROVE_SOURCES,
    _NOT_DOC_ONLY_TERMS_RE,
    OPERATOR_SENDBACK_PREFIX,
    _summarize_spec_for_auto_approve,
    _triage_note_signals_wrong_repo,
    _verify_branch_merged,
    log,
    ops_shape_marker,
)
from .scanner_disposition import ScannerDisposition, resolve_scanner_disposition
from .spec_validation import _draft_has_complete_spec, _fast_path_scope_checks

# ---------------------------------------------------------------------------
# module-level triage helpers
# ---------------------------------------------------------------------------

_MIGRATE_NOTE_PREFIX = "migrated from board "

# Regex that matches a fenced code block line — three or more backticks
# optionally followed by a language hint.  Used by
# :func:`_count_code_block_lines` to count lines inside code fences.
_CODE_FENCE_RE = re.compile(r"^\s*```")

# File extensions that indicate a source-code change (not documentation).
# Used by :func:`_is_doc_only_draft` to disqualify a draft from the
# doc-only fast-path when any touched file has one of these extensions.
_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".js",
        ".jsx",
        ".tsx",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".ini",
        ".json",
        ".css",
        ".scss",
        ".less",
        ".html",
        ".htm",
        ".sh",
        ".bash",
        ".zsh",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".r",
        ".m",
        ".sql",
    }
)

# Broader regex than _PATH_RE (which requires a '/'): matches any
# backtick-delimited token that looks like a filename with extension.
# Used by :func:`_is_doc_only_draft` to find file paths in draft text
# for documentation-only classification.
_DOC_PATH_RE = re.compile(r"`([^`]+\.[a-zA-Z]{1,10})`")

# Lower-case substrings in a triage SKIP reason that assert there is
# nothing to implement.  In mill semantics SKIP means "the draft is
# ALREADY an implementation-ready spec — route to implement"; the discard
# verdict for nothing-to-do / no-op / placeholder drafts is NO_CHANGE.
# A SKIP reason that instead says there is nothing to do is a category
# error that would route a nothing-to-do draft straight to implement with
# auto-approve (live noop-8835), so we guard against it deterministically.
_SKIP_NOOP_MARKERS: tuple[str, ...] = (
    "nothing to implement",
    "nothing to do",
    "nothing to change",
    "nothing to do here",
    "no actionable work",
    "no action needed",
    "no changes needed",
    "deliberate no-op",
    "nothing to flag",
    "not actionable",
    "no work to do",
    "no-op",
)


def _count_code_block_lines(text: str) -> int:
    """Return the number of lines inside fenced code blocks in *text*.

    Tracks open/closed state across triple-backtick fences.  Only
    counts lines between a `` ``` `` opener and its matching closer.
    Consecutive openers without an intervening closer are treated as
    nested (inner fences are part of the outer block's content).
    """
    if not text:
        return 0
    count = 0
    depth = 0
    for line in text.splitlines():
        if _CODE_FENCE_RE.match(line):
            if depth == 0:
                depth = 1  # entering a code block
            else:
                depth = 0  # leaving a code block
        elif depth > 0:
            count += 1
    return count


def _is_doc_only_draft(draft: str) -> bool:
    """Return ``True`` when every file path in *draft* is documentation-only.

    Extracts backtick-delimited file paths from the draft text and
    classifies each as doc-only (under ``docs/``, ``.md`` extension,
    or ``CHANGELOG.md``) or a code file (any known code extension).
    Returns ``False`` when no paths are found or when any path is a
    code file — the caller should fall through to normal triage.
    """
    if _NOT_DOC_ONLY_TERMS_RE.search(draft):
        return False
    paths = _DOC_PATH_RE.findall(draft)
    if not paths:
        return False
    for path in paths:
        # docs/ directory — always doc-only
        if path.startswith("docs/"):
            continue
        # .md files anywhere in the tree are doc-only
        if path.endswith(".md"):
            continue
        # Known code extension — not doc-only
        for ext in _CODE_EXTENSIONS:
            if path.endswith(ext):
                return False
        # Unknown extension or no recognised pattern — can't classify
        # as doc-only; err on the side of full triage.
        return False
    return True


def _skip_reason_asserts_noop(reason: str | None) -> bool:
    """Return ``True`` when a triage SKIP *reason* asserts there is nothing to do.

    SKIP legitimately means "the draft is already an implementation-ready
    spec — route to implement."  A SKIP reason that instead says there is
    nothing to implement is a category error: the discard verdict is
    NO_CHANGE.  Conservative substring match on the lower-cased reason.
    """
    t = (reason or "").strip().lower()
    if not t:
        return False
    return any(m in t for m in _SKIP_NOOP_MARKERS)


def _draft_is_placeholder(draft: str | None) -> bool:
    """Return ``True`` when *draft* is a placeholder with no actionable content.

    Reuses :func:`core.text_noop.is_degenerate_body` — the shared, vetted
    placeholder detector (empty / punctuation-only / short pointer phrases
    like "tbd", "see above", "see spec").  Keyed on placeholder content, not
    raw length, so genuinely terse but real mechanical specs ("Rename
    ``foo.py`` to ``bar.py``") are never mistaken for a no-op.
    """
    return is_degenerate_body(draft)


def _triage_skip_is_noop(reason: str | None, draft: str | None) -> bool:
    """Return ``True`` when a SKIP verdict actually means "nothing to do".

    Coerces a SKIP to NO_CHANGE when the classifier's reason text asserts
    there is nothing to implement, or the draft body is a placeholder with
    no actionable content.  Either signal means routing straight to
    implement with auto-approve would run a real implement pass on a
    nothing-to-do draft (live noop-8835).
    """
    return _skip_reason_asserts_noop(reason) or _draft_is_placeholder(draft)


def _triage_outcome(
    ctx: StageContext,
    ws: Workspace,
    draft: str,
    ticket_id: str,
    reason: str,
    *,
    source: str | None = None,
    triage_note: str | None = None,
    write_file_map_args: list[dict[str, str]] | None = None,
    extract_paths_from_draft: bool = False,
    state: State | None = None,
) -> Outcome:
    """Write draft-original.md and file_map.json, then return an Outcome.

    Encapsulates the repeated 3-statement pattern found across triage
    decision handlers: write draft-original.md, write file_map.json,
    and return an ``Outcome`` (either resolved via
    ``resolved_outcome``, or a direct ``Outcome(state, reason)`` when
    *state* is provided).
    """
    (ws.artifacts_dir / "draft-original.md").write_text(
        draft if draft else "(title-only ticket, no body provided)",
        encoding="utf-8",
    )

    if extract_paths_from_draft:
        _PATH_RE = re.compile(r"`([^`]*/[^`]*\.[a-zA-Z]{1,10})`")
        extracted = _PATH_RE.findall(draft)
        if extracted:
            _reconcile.write_file_map(
                ws,
                [{"file": p, "note": "from draft"} for p in extracted],
                only_if_absent=True,
            )
        else:
            _reconcile.write_file_map(ws, [], only_if_absent=True)
    elif write_file_map_args is not None:
        _reconcile.write_file_map(ws, write_file_map_args, only_if_absent=True)
    else:
        _reconcile.write_file_map(ws, [], only_if_absent=True)

    if state is not None:
        return Outcome(state, reason)

    return _result_paths.resolved_outcome(
        ctx,
        draft,
        ticket_id,
        reason,
        source=source,
        triage_note=triage_note,
    )


def _parse_prior_boards(service: TicketService, ticket_id: str) -> tuple[set[str], int]:
    """Parse migration-history events to find boards this ticket has been on.

    Returns ``(prior_boards, migration_count)``.  ``prior_boards`` is the
    set of destination board ids extracted from ``"migrated from board …"``
    notes.  ``migration_count`` is the total number of migration events.
    """
    prior_boards: set[str] = set()
    migration_count = 0
    for ev in service.history(ticket_id):  # type: ignore[attr-defined]
        note = ev.note or ""
        if note.startswith(_MIGRATE_NOTE_PREFIX):
            migration_count += 1
            to_pos = note.find(" to ")
            if to_pos != -1:
                rest = note[to_pos + 4 :]
                suffix_pos = rest.find(" (was ")
                if suffix_pos == -1:
                    suffix_pos = rest.find(": ")
                dst_repr = rest[:suffix_pos] if suffix_pos != -1 else rest
                dst_board = dst_repr.strip().strip("'\"")
                if dst_board:
                    prior_boards.add(dst_board)
    return prior_boards, migration_count


def _anti_bounce_escalate(
    ctx: StageContext,
    ws: Workspace,
    draft: str,
    ticket: Ticket,
    triage: Any,
    resolved_board: str,
) -> Outcome | None:
    """Check migration anti-bounce guard; escalate to human if triggered.

    Derives prior boards from migration history via
    :func:`_parse_prior_boards`.  If history cannot be read, or if the
    ticket has already been migrated at least once (or the target board
    is a prior destination), writes the standard draft-artifact + empty
    file_map and returns a human-escalation :class:`Outcome`.  Returns
    ``None`` when migration is safe to proceed.
    """
    try:
        prior_boards, migration_count = _parse_prior_boards(ctx.service, ticket.id)
    except Exception:
        log.warning(
            "%s: could not read ticket history for anti-bounce check, "
            "escalating to human",
            ticket.id,
            exc_info=True,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"triage MIGRATE anti-bounce error: {triage.reason}",
            source=ticket.source,
            triage_note=triage.reason,
        )

    if migration_count >= 1 or resolved_board in prior_boards:
        log.info(
            "%s: anti-bounce blocked MIGRATE to %r "
            "(prior boards=%r, migration_count=%d) — escalating to human",
            ticket.id,
            resolved_board,
            prior_boards,
            migration_count,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"triage MIGRATE anti-bounce blocked: {triage.reason}",
            source=ticket.source,
            triage_note=triage.reason,
        )

    return None


def is_sendback_reentry(service: TicketService, ticket_id: str) -> bool:
    """Return ``True`` when this refine run follows an operator "changes requested"
    sendback — i.e. a prior ``DRAFT`` event whose note starts with
    ``OPERATOR_SENDBACK_PREFIX``.
    """
    for ev in service.history(ticket_id):  # type: ignore[attr-defined]
        if (
            ev.state == State.DRAFT
            and ev.note
            and ev.note.startswith(OPERATOR_SENDBACK_PREFIX)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# phase: split-child fast-path
# ---------------------------------------------------------------------------


def split_child_fast_path(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    reviewer_comments: str | None,
) -> Outcome | None:
    """Skip re-refinement for split children.

    A child ticket created from a split already has a refined
    spec in its description.md.  Detect this by checking whether
    the parent is CLOSED with a "split into" note — the canonical
    signal that this ticket's description is already the refined
    output.  When children are reparented to an umbrella epic
    the direct parent is no longer CLOSED, so also check the
    ticket's own history for a "split from" transition note.
    We must NOT short-circuit for retrospect-spawned drafts
    (whose parent is also CLOSED but for a different reason and
    whose description is a raw draft, not a spec).
    IMPORTANT: even split children must fall through to the full
    refine agent when there are open reviewer comments — the
    human requested changes that the spec must address.

    Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
    through to the full pipeline.
    """
    is_split_child = False
    if ticket.parent_id is not None:
        parent = ctx.service.get(ticket.parent_id)
        if parent is not None and parent.state == State.CLOSED:
            parent_history = ctx.service.history(parent.id)
            is_split_child = any(
                ev.state == State.CLOSED
                and ev.note
                and ev.note.startswith("split into")
                for ev in parent_history  # type: ignore[attr-defined]
            )
    if not is_split_child:
        own_history = ctx.service.history(ticket.id)
        is_split_child = any(
            ev.note and ev.note.startswith("split from")
            for ev in own_history  # type: ignore[attr-defined]
        )
    if not (is_split_child and not reviewer_comments):
        return None

    _reconcile.write_triage_complexity(ws, "simple")

    spec = draft
    if not spec.strip():
        return Outcome(State.BLOCKED, "split child has empty description")
    return _triage_outcome(
        ctx,
        ws,
        spec,
        ticket.id,
        "split child — spec already refined",
        source=ticket.source,
    )


# ---------------------------------------------------------------------------
# phase: triage skip
# ---------------------------------------------------------------------------

#: Sources whose drafts are checked against the CI/dev-tooling proposal
#: routing policy.  Survey/audit are the periodic agents observed filing
#: recurring standing-gate proposals; other scanner sources fall through
#: to the per-source scanner disposition instead.
_CI_TOOLING_PROPOSAL_SOURCES: frozenset[str] = frozenset(
    {
        SourceKind.SURVEY,
        SourceKind.AUDIT,
    }
)


def _ci_tooling_auto_disposition(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
) -> Outcome | None:
    """Route an auto-proposed CI/dev-tooling addition per the class policy.

    Survey/audit file recurring "add a standing CI gate / dev-tooling
    standard" proposals (coverage gate, OpenSSF Scorecard, secret
    scanner, …) that each otherwise land in ``HUMAN_ISSUE_APPROVAL`` as a
    separate workflow-policy escalation.  This classifies the proposal
    into a canonical tool-class and routes it per
    ``settings.ci_tooling_proposal_policy``: an allowlisted class
    (``auto-ready``) routes straight to implement, a denylisted class
    (``auto-decline``) auto-closes with a note, and an unlisted class
    follows ``settings.ci_tooling_proposal_default`` (``escalate`` — the
    human gate — or ``auto-decline``).

    Returns an :class:`Outcome` to short-circuit, or ``None`` when the
    source is not a CI-tooling proposal source, the draft is not a
    recognised CI-tooling proposal, or the resolved route is
    ``escalate`` (fall through to the normal human-gate path).
    """
    if ticket.source not in _CI_TOOLING_PROPOSAL_SOURCES:
        return None

    class_id = classify_ci_tooling_proposal(ticket.title, draft)
    if class_id is None:
        return None

    route = resolve_ci_tooling_route(class_id, s)

    if route == CIToolingRoute.AUTO_READY:
        log.info(
            "%s: CI-tooling proposal — auto-ready (class %r, allowlisted)",
            ticket.id,
            class_id,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"CI-tooling proposal policy: auto-ready (class {class_id!r}) "
            "— allowlisted, routed to implement without a human gate",
            extract_paths_from_draft=True,
            state=State.READY,
        )

    if route == CIToolingRoute.AUTO_DECLINE:
        log.info(
            "%s: CI-tooling proposal — auto-decline (class %r, denylisted)",
            ticket.id,
            class_id,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"CI-tooling proposal policy: auto-decline (class {class_id!r}) "
            "— denylisted fleet-wide, closed without a human gate per policy",
            state=State.CLOSED,
        )

    # ESCALATE: fall through so the proposal reaches the human gate via
    # its normal triage path (today's behaviour for unlisted classes).
    log.info(
        "%s: CI-tooling proposal — escalate (class %r, unlisted)",
        ticket.id,
        class_id,
    )
    return None


def _scanner_auto_disposition(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
) -> Outcome | None:
    """Apply the per-source scanner bulk auto-disposition policy.

    Scanner / finding sources (audit, module_curator, health, …) fan out
    auto-generated drafts that otherwise stall the human gate.  Each
    covered source is mapped to a deterministic disposition
    (``auto-close-stale`` / ``auto-ready`` / ``hold-for-human``) via
    :func:`resolve_scanner_disposition`.  When
    ``scanner_auto_disposition_dry_run`` is True (the default) the policy
    only logs what it *would* do and returns ``None`` so the ticket
    follows its normal triage path — letting the policy be validated
    against the live backlog before enforcement.

    Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
    through (source not covered, or dry-run).
    """
    disposition = resolve_scanner_disposition(ticket.source, s)
    if disposition is None:
        return None

    if s.scanner_auto_disposition_dry_run:
        log.info(
            "%s: scanner auto-disposition DRY-RUN — source %r would be "
            "dispositioned as %s (not enforced)",
            ticket.id,
            ticket.source,
            disposition.value,
        )
        return None

    if disposition == ScannerDisposition.AUTO_CLOSE_STALE:
        log.info(
            "%s: scanner auto-disposition — auto-close-stale (source %r)",
            ticket.id,
            ticket.source,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"scanner auto-disposition: auto-close-stale "
            f"(source {ticket.source!r}) — closed without human gate per policy",
            state=State.CLOSED,
        )

    if disposition == ScannerDisposition.AUTO_READY:
        log.info(
            "%s: scanner auto-disposition — auto-ready (source %r)",
            ticket.id,
            ticket.source,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"scanner auto-disposition: auto-ready (source {ticket.source!r}) "
            "— routed to implement without human gate per policy",
            extract_paths_from_draft=True,
            state=State.READY,
        )

    # HOLD_FOR_HUMAN: retain the human decision, but as a single
    # consolidated gate item — the draft is already parented to the
    # source's rollup epic (pass_runner._find_or_create_scanner_epic).
    log.info(
        "%s: scanner auto-disposition — hold-for-human (source %r)",
        ticket.id,
        ticket.source,
    )
    return _triage_outcome(
        ctx,
        ws,
        draft,
        ticket.id,
        f"scanner auto-disposition: hold-for-human (source {ticket.source!r}) "
        "— retained under source rollup for a single human decision",
        state=State.HUMAN_ISSUE_APPROVAL,
    )


def deterministic_triage_pre_checks(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
) -> Outcome | None:
    """LLM-free triage pre-checks shared by the legacy triage gate and
    the collapsed pre-refine classifier: throwaway fixture tickets,
    doc-only drafts, a prior triage-SKIP verdict in history, and
    prescriptive (code-complete) specs all route straight to a terminal
    outcome without any LLM spend.

    Returns an :class:`Outcome` to short-circuit, or ``None``.
    """
    # CI/dev-tooling proposal routing runs first: a survey/audit draft
    # proposing a standing CI gate / dev-tooling standard is routed by
    # its tool-class (allowlist → ready, denylist → close) per the
    # configured policy, before it can reach the human gate.  No-op
    # (returns None) for non-CI-tooling drafts or an escalate route.
    ci_tooling_outcome = _ci_tooling_auto_disposition(ctx, ticket, draft, ws, s)
    if ci_tooling_outcome is not None:
        return ci_tooling_outcome

    # Scanner bulk auto-disposition runs next: a covered scanner source
    # under an enforced policy is dispositioned here, before any other
    # pre-check or the refine LLM, keeping scanner findings out of the
    # human gate.  No-op (returns None) for non-scanner sources or in
    # dry-run mode.
    scanner_outcome = _scanner_auto_disposition(ctx, ticket, draft, ws, s)
    if scanner_outcome is not None:
        return scanner_outcome

    # Deterministic pre-check: throwaway/test-fixture tickets. Agents
    # have (mis)used ticket creation to make themselves a live-board
    # fixture (noop-8835, dummy-2218 leaked and one flowed through
    # refine into a wasted implement run). report_issue now refuses to
    # file them, but one created through any other path must not burn a
    # refine/implement run here either. Mirrors the report_issue guard
    # via the shared is_placeholder_ticket detector — same shapes, one
    # source of truth.
    if is_placeholder_ticket(ticket.title, draft):
        log.info(
            "%s: placeholder/test-fixture ticket — closing as DONE "
            "(no refine/implement run)",
            ticket.id,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            "triage SKIP: placeholder/test-fixture ticket — nothing to implement",
            source=ticket.source,
            state=State.DONE,
        )

    # Deterministic pre-check: documentation-only change.
    # When every file path in the draft is under docs/ or is a .md
    # file (and no code files are touched), skip the LLM triage and
    # route directly to implement.  Documentation changes don't
    # benefit from multi-LLM analysis — the spec is self-evident.
    if _is_doc_only_draft(draft):
        log.info(
            "%s: doc-only draft detected — skipping triage + refine",
            ticket.id,
        )
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            "Documentation-only change; no code review needed — "
            "auto-approving (skipped triage + refine)",
            source=ticket.source,
            triage_note="triage SKIP: doc-only",
            extract_paths_from_draft=True,
        )

    # Deterministic pre-check: when a prior refine pass already
    # issued a SKIP verdict for this ticket (recorded in the state
    # transition history), re-issue the SKIP outcome without
    # spending any LLM budget on re-triaging.  The ticket was
    # already classified as implementation-ready; re-running the
    # triage classifier adds no value.
    ticket_history = ctx.service.history(ticket.id)
    for ev in ticket_history:  # type: ignore[attr-defined]
        if ev.note and ev.note.startswith("triage SKIP:"):
            prior_reason = ev.note[len("triage SKIP: ") :]
            # Keep only the triage's own reasoning. The stored note also
            # carries the routing verdict that was appended to it
            # (" | auto-approve: …"), and replaying that verbatim both
            # rewrites it into the new history entry — where it reads as
            # a fresh decision — and feeds it back in as *triage_note*,
            # so an obsolete verdict re-derives itself on every pass and
            # the ticket can never be re-judged. Live, 11 tickets kept
            # replaying an "auto-approve: REJECTED" suffix produced by a
            # gate that no longer exists.
            prior_reason = prior_reason.split(" | auto-approve:", 1)[0].strip()
            log.info(
                "%s: prior triage SKIP found in history — "
                "short-circuiting triage (reason: %s)",
                ticket.id,
                prior_reason,
            )
            return _triage_outcome(
                ctx,
                ws,
                draft,
                ticket.id,
                f"triage SKIP (prior verdict): {prior_reason}",
                source=ticket.source,
                triage_note=prior_reason,
                extract_paths_from_draft=True,
            )

    # Deterministic pre-check: when the draft contains a large number of
    # code-block lines (prescriptive spec — the author already wrote the
    # implementation in fenced code blocks), skip the triage LLM call and
    # route directly to implement.  The expensive refine agent adds no
    # value on a spec that is already code-complete.
    threshold = s.refine_prescriptive_spec_code_lines_threshold
    if threshold > 0:
        code_lines = _count_code_block_lines(draft)
        if code_lines >= threshold:
            log.info(
                "%s: prescriptive spec — code blocks contain %d lines "
                "(threshold %d), skipping triage + refine",
                ticket.id,
                code_lines,
                threshold,
            )
            return _triage_outcome(
                ctx,
                ws,
                draft,
                ticket.id,
                "prescriptive spec — code blocks constitute "
                "implementation-ready spec, skipping refine",
                source=ticket.source,
                extract_paths_from_draft=True,
            )

    return None


def _emit_no_change(
    ctx: StageContext,
    ws: Workspace,
    draft: str,
    ticket: Ticket,
    repo_dir: Path | None,
    reason: str,
) -> Outcome:
    """Emit the NO_CHANGE outcome for *reason* (the discard verdict).

    A TASK-kind (implementation) ticket that hasn't produced a branch, or
    whose branch is not merged to the base branch, must not be auto-closed
    from DRAFT.  Route to READY so implement can verify the "no change"
    claim against the live tree.
    """
    short_reason = reason[:400] + ("…" if len(reason) > 400 else "")
    if ticket.kind == TicketKind.TASK and (
        not ticket.branch or not _verify_branch_merged(repo_dir, ticket)
    ):
        # Deliberately NOT passing triage_note here. A NO_CHANGE
        # reason says, by definition, "this is already done /
        # no change is needed" — which is verbatim what
        # _TRIAGE_REJECTION_PATTERNS looks for. Forwarding it
        # made the rejection gate fire on the premise of this
        # very route, converting "route to READY so implement
        # can verify the claim" into HUMAN_ISSUE_APPROVAL, where
        # the ticket then sat forever: nothing auto-closes it and
        # no human ever approves a ticket whose own note says
        # there is nothing to do. Live, these accumulated for up
        # to six days. The spec still faces the normal
        # auto-approve triage below; only the self-referential
        # rejection match is skipped.
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"triage NO_CHANGE — routing to implement: {short_reason}",
            source=ticket.source,
        )
    return _triage_outcome(
        ctx,
        ws,
        draft,
        ticket.id,
        f"triage NO_CHANGE: {short_reason}",
        state=State.DONE,
    )


def handle_triage_decision(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    repo_dir: Path | None,
    ws: Workspace,
    s: Settings,
    triage: refining.TriageResult,
) -> Outcome | None:
    """Handle a triage verdict — from the legacy triage LLM or the
    collapsed pre-refine classifier: the NO_CHANGE / SKIP / MIGRATE
    short-circuits (with target validation and anti-bounce) and the
    mechanical draft fast-path, all with their original note formats.

    Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
    through to the full refine agent.
    """
    _reconcile.persist_triage_complexity(ws, triage)

    if triage.decision == "NO_CHANGE":
        return _emit_no_change(ctx, ws, draft, ticket, repo_dir, triage.reason)
    if triage.decision == "SKIP":
        # Deterministic guard: a SKIP whose reason asserts there is
        # nothing to implement, or whose draft body is a placeholder,
        # is actually a nothing-to-do discard — coerce to NO_CHANGE so
        # it doesn't route straight to implement with auto-approve
        # (live noop-8835).  SKIP must mean only "the draft is already
        # an implementation-ready spec."
        if _triage_skip_is_noop(triage.reason, draft):
            log.info(
                "%s: SKIP reason asserts no-op / draft is placeholder "
                "(reason=%r) — coercing to NO_CHANGE",
                ticket.id,
                triage.reason[:120],
            )
            return _emit_no_change(ctx, ws, draft, ticket, repo_dir, triage.reason)
        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"triage SKIP: {triage.reason}",
            source=ticket.source,
            triage_note=triage.reason,
            extract_paths_from_draft=True,
        )

    if triage.decision == "MIGRATE":
        from ...config import get_repos_config

        try:
            repos_config = get_repos_config()
            known: dict[str, str] = {"meta": "meta"}
            for rc in repos_config.repos.values():
                known[rc.repo_id] = rc.board_id
                known[rc.board_id] = rc.board_id
        except Exception:
            log.warning(
                "%s: could not load repos config for MIGRATE validation, "
                "escalating to human",
                ticket.id,
                exc_info=True,
            )
            known = {}

        target = (triage.target_board or "").strip()
        resolved_board = known.get(target) if known else None

        if not target or resolved_board is None or resolved_board == ticket.board_id:
            log.info(
                "%s: MIGRATE target invalid (target=%r, resolved=%r, current=%r) "
                "— escalating to human",
                ticket.id,
                target,
                resolved_board,
                ticket.board_id,
            )
            return _triage_outcome(
                ctx,
                ws,
                draft,
                ticket.id,
                f"triage MIGRATE invalid target: {triage.reason}",
                source=ticket.source,
                triage_note=triage.reason,
            )

        anti_bounce = _anti_bounce_escalate(
            ctx, ws, draft, ticket, triage, resolved_board
        )
        if anti_bounce is not None:
            return anti_bounce

        try:
            ctx.service.migrate(
                ticket.id,
                resolved_board,
                note=triage.reason,
            )
        except (KeyError, ValueError) as exc:
            log.warning(
                "%s: MIGRATE call failed: %s — escalating to human",
                ticket.id,
                exc,
            )
            return _triage_outcome(
                ctx,
                ws,
                draft,
                ticket.id,
                f"triage MIGRATE failed: {exc} — {triage.reason}",
                source=ticket.source,
                triage_note=triage.reason,
            )

        return _triage_outcome(
            ctx,
            ws,
            draft,
            ticket.id,
            f"migrated to board {resolved_board!r}: {triage.reason}",
            state=State.DRAFT,
        )

    # --- ops-shaped retrospect follow-up gate ---
    # A retrospect follow-up that asks to RUN something against a deployed
    # service or a high-memory host (e.g. `POST /api/batch/jobs`, "on an
    # environment with adequate memory") cannot be satisfied by a mill
    # implement run — the network-less ~1 GiB sandbox OOMs.  Today the
    # mechanical fast-path auto-approves such a draft precisely BECAUSE it
    # has "no code changes", sending it straight to implement.  Gate it
    # here, ahead of the fast-path (so it takes precedence over both the
    # deterministic-source fast-path and triage_auto_approve, and skips
    # the full refine agent) and route it to a human/ops approval gate.
    # Scoped to retrospect only — user/CI tickets legitimately quote API
    # routes in specs.  Returns an explicit state, bypassing
    # _resolve_next_state, so it fires regardless of require_approval.
    # NOTE: this gate lives on the fast-path seam; when refine_triage is
    # disabled the standard full-refine + require_approval routing applies
    # instead — acceptable, as the incident path is the fast-path.
    if ticket.source == SourceKind.RETROSPECT:
        marker = ops_shape_marker(f"{ticket.title}\n{draft}")
        if marker is not None:
            return _triage_outcome(
                ctx,
                ws,
                draft,
                ticket.id,
                f"ops-shaped retrospect follow-up (matched {marker!r}) — "
                f"requires operator routing, not auto-implement",
                state=State.HUMAN_ISSUE_APPROVAL,
            )

    # --- mechanical draft fast-path ---
    # Empty/whitespace drafts cannot be auto-approved — they'd
    # create an infinite loop (approve → refine produces empty
    # body → fast-path approves again).  Fall through to the
    # full refine agent instead.
    # Deterministic sources (CI) always fast-path.  For other
    # sources, the auto-approve pre-check only runs when
    # require_approval is True (there must be a gate to skip).
    if draft.strip() and (
        ticket.source != "ci"
        or (ticket.source == "ci" and _draft_has_complete_spec(draft))
    ):
        # --- fast-path pre-checks: emptiness + scope ---
        scope_checks = _fast_path_scope_checks(draft)
        any_fail = any(
            isinstance(v, str) and v.startswith("fail:") for v in scope_checks.values()
        )
        if any_fail:
            log.info(
                "%s: mechanical fast-path blocked by pre-checks: %s — "
                "falling through to full refine",
                ticket.id,
                {
                    k: v
                    for k, v in scope_checks.items()
                    if isinstance(v, str) and v.startswith("fail:")
                },
            )
            # Don't return — fall through to full refine below.
        else:
            checks_summary = "; ".join(f"{k}: {v}" for k, v in scope_checks.items())
            try:
                if ticket.source in _AUTO_APPROVE_SOURCES:
                    return _triage_outcome(
                        ctx,
                        ws,
                        draft,
                        ticket.id,
                        f"mechanical draft fast-path "
                        f"(deterministic source {ticket.source!r}) "
                        f"— skipped refine LLM "
                        f"[checks: {checks_summary}]",
                        source=ticket.source,
                        triage_note=(
                            f"{triage.reason} | fast-path checks: {checks_summary}"
                        ),
                        extract_paths_from_draft=True,
                    )

                # Auto-approve only makes sense when there's an
                # approval gate to skip.
                if not s.require_approval:
                    pass  # fall through to full refine
                # Wrong-repo hard gate: when triage detected a
                # repository mismatch, auto-approve MUST NOT
                # override — fall through to full refine which
                # will route to human approval via
                # _resolve_next_state.
                elif _triage_note_signals_wrong_repo(triage.reason):
                    log.info(
                        "%s: mechanical fast-path blocked by "
                        "wrong-repo signal from triage",
                        ticket.id,
                    )
                else:
                    auto = refining.triage_auto_approve(
                        settings=s,
                        spec=_summarize_spec_for_auto_approve(
                            f"{ticket.title}\n\n{draft}"
                        ),
                    )
                    if auto.decision == "APPROVE":
                        return _triage_outcome(
                            ctx,
                            ws,
                            draft,
                            ticket.id,
                            f"mechanical draft fast-path — "
                            f"auto-approve APPROVE: {auto.reason} "
                            f"[checks: {checks_summary}]",
                            source=ticket.source,
                            triage_note=(
                                f"triage REFINE → auto-approve APPROVE: {auto.reason}"
                                f" | fast-path checks: {checks_summary}"
                            ),
                            extract_paths_from_draft=True,
                        )
                    elif auto.decision == "NEEDS_APPROVAL":
                        return _triage_outcome(
                            ctx,
                            ws,
                            draft,
                            ticket.id,
                            f"mechanical draft fast-path — "
                            f"auto-approve NEEDS_APPROVAL (skipped refine): "
                            f"{auto.reason} "
                            f"[checks: {checks_summary}]",
                            source=ticket.source,
                            triage_note=(
                                f"{triage.reason} | fast-path checks: {checks_summary}"
                            ),
                            extract_paths_from_draft=True,
                        )
            except Exception:
                log.warning(
                    "%s: mechanical fast-path auto-approve failed, falling through",
                    ticket.id,
                    exc_info=True,
                )
    return None
