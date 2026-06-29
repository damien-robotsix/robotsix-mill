"""Triage skip logic for the refine stage.

Pre-agent classification: detects maintenance / no-change / skip /
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
from ...core.workspace import Workspace
from ..base import Outcome, StageContext
from . import _reconcile
from . import _result_paths
from .helpers import (
    OPERATOR_SENDBACK_PREFIX,
    _AUTO_APPROVE_SOURCES,
    _draft_has_complete_spec,
    _summarize_spec_for_auto_approve,
    log,
)


# ---------------------------------------------------------------------------
# module-level triage helpers
# ---------------------------------------------------------------------------

_MIGRATE_NOTE_PREFIX = "migrated from board "

# Regex that matches a fenced code block line — three or more backticks
# optionally followed by a language hint.  Used by
# :func:`_count_code_block_lines` to count lines inside code fences.
_CODE_FENCE_RE = re.compile(r"^\s*```")


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
) -> Outcome:
    """Write draft-original.md and file_map.json, then return a resolved Outcome.

    Encapsulates the repeated 3-statement pattern found across triage
    decision handlers: write draft-original.md, write file_map.json,
    and return a ``resolved_outcome``.
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
    ``OPERATOR_SENDBACK_PREFIX``."""
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
# phase: triage skip / maintenance
# ---------------------------------------------------------------------------


def triage_skip(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    repo_dir: Path | None,
    extra_roots: list[Path] | None,
    title: str,
    ws: Workspace,
    s: Settings,
    reviewer_comments: str | None,
) -> Outcome | None:
    """Triage phase 1: LLM classifier (3-way: SKIP / MAINTENANCE / REFINE).

    A single cheap LLM call classifies the draft.  If it's
    already a precise, implementation-ready spec, skip the
    expensive refine agent entirely.  If it's a maintenance
    (operational) request the keyword classifier missed, route
    to MAINTENANCE.  ONLY run when:
    - the feature flag is enabled, AND
    - no reviewer sendback (human-flagged changes always refine).

    Also captures the complexity verdict from the triage classifier
    and persists it to ``ws.artifacts_dir / "triage_complexity.json"``
    so ``_run_and_collect`` can read it and pass it to
    ``run_refine_agent`` for exploration gating.

    Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
    through to the full refine agent.
    """
    if not (s.refine_triage_enabled and not reviewer_comments):
        return None

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

    try:
        triage = refining.triage_refine(
            settings=s,
            title=title,
            draft=draft,
            repo_dir=repo_dir,
            extra_roots=extra_roots,
        )
        _reconcile.persist_triage_complexity(ws, triage)

        if (
            triage.decision == "MAINTENANCE"
            and s.maintenance_triage_enabled
            and ticket.source != SourceKind.CI
        ):
            (ws.artifacts_dir / "draft-original.md").write_text(
                draft if draft else "(title-only ticket, no body provided)",
                encoding="utf-8",
            )
            return Outcome(
                State.MAINTENANCE,
                f"maintenance triage (LLM): {triage.reason} — {title}",
            )
        if triage.decision == "NO_CHANGE":
            short_reason = triage.reason[:400] + (
                "…" if len(triage.reason) > 400 else ""
            )
            # A TASK-kind (implementation) ticket that hasn't produced a
            # branch must not be auto-closed from DRAFT.  Route to READY
            # so implement can verify the "no change" claim against the
            # live tree.
            if ticket.kind == TicketKind.TASK and not ticket.branch:
                return _triage_outcome(
                    ctx,
                    ws,
                    draft,
                    ticket.id,
                    f"triage NO_CHANGE — routing to implement: {short_reason}",
                    source=ticket.source,
                    triage_note=triage.reason,
                )
            (ws.artifacts_dir / "draft-original.md").write_text(
                draft if draft else "(title-only ticket, no body provided)",
                encoding="utf-8",
            )
            _reconcile.write_file_map(ws, [], only_if_absent=True)
            return Outcome(
                State.DONE,
                f"triage NO_CHANGE: {short_reason}",
            )
        if triage.decision == "SKIP":
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

            if (
                not target
                or resolved_board is None
                or resolved_board == ticket.board_id
            ):
                log.info(
                    "%s: MIGRATE target invalid (target=%r, resolved=%r, current=%r) "
                    "— escalating to human",
                    ticket.id,
                    target,
                    resolved_board,
                    ticket.board_id,
                )
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                _reconcile.write_file_map(ws, [], only_if_absent=True)
                return _result_paths.resolved_outcome(
                    ctx,
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
                (ws.artifacts_dir / "draft-original.md").write_text(
                    draft if draft else "(title-only ticket, no body provided)",
                    encoding="utf-8",
                )
                _reconcile.write_file_map(ws, [], only_if_absent=True)
                return _result_paths.resolved_outcome(
                    ctx,
                    draft,
                    ticket.id,
                    f"triage MIGRATE failed: {exc} — {triage.reason}",
                    source=ticket.source,
                    triage_note=triage.reason,
                )

            (ws.artifacts_dir / "draft-original.md").write_text(
                draft if draft else "(title-only ticket, no body provided)",
                encoding="utf-8",
            )
            _reconcile.write_file_map(ws, [], only_if_absent=True)
            return Outcome(
                State.DRAFT,
                f"migrated to board {resolved_board!r}: {triage.reason}",
            )

        # --- mechanical draft fast-path ---
        if s.auto_approve_enabled and (
            ticket.source not in ("user", "ci")
            or (ticket.source == "ci" and _draft_has_complete_spec(draft))
        ):
            try:
                if ticket.source in _AUTO_APPROVE_SOURCES:
                    (ws.artifacts_dir / "draft-original.md").write_text(
                        draft if draft else "(title-only ticket, no body provided)",
                        encoding="utf-8",
                    )
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
                    return _result_paths.resolved_outcome(
                        ctx,
                        draft,
                        ticket.id,
                        f"mechanical draft fast-path "
                        f"(deterministic source {ticket.source!r}) "
                        f"— skipped refine LLM",
                        source=ticket.source,
                        triage_note=triage.reason,
                    )

                auto = refining.triage_auto_approve(
                    settings=s,
                    spec=_summarize_spec_for_auto_approve(f"{ticket.title}\n\n{draft}"),
                )
                if auto.decision == "APPROVE":
                    (ws.artifacts_dir / "draft-original.md").write_text(
                        draft if draft else "(title-only ticket, no body provided)",
                        encoding="utf-8",
                    )
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
                    return _result_paths.resolved_outcome(
                        ctx,
                        draft,
                        ticket.id,
                        f"mechanical draft fast-path — "
                        f"auto-approve APPROVE: {auto.reason}",
                        source=ticket.source,
                        triage_note=(
                            f"triage REFINE → auto-approve APPROVE: {auto.reason}"
                        ),
                    )
                elif auto.decision == "NEEDS_APPROVAL":
                    (ws.artifacts_dir / "draft-original.md").write_text(
                        draft if draft else "(title-only ticket, no body provided)",
                        encoding="utf-8",
                    )
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
                    return _result_paths.resolved_outcome(
                        ctx,
                        draft,
                        ticket.id,
                        f"mechanical draft fast-path — "
                        f"auto-approve NEEDS_APPROVAL (skipped refine): {auto.reason}",
                        source=ticket.source,
                        triage_note=triage.reason,
                    )
            except Exception:
                log.warning(
                    "%s: mechanical fast-path auto-approve failed, falling through",
                    ticket.id,
                    exc_info=True,
                )
    except Exception:
        log.warning(
            "%s: triage failed, falling through to full refine",
            ticket.id,
            exc_info=True,
        )
    return None
