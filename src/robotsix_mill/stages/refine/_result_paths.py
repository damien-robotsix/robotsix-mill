"""Result-path handlers for the refine stage.

Handles the four outcome modes returned by the refine agent:
no-change-needed, promote-to-epic, single-scope, and multi-scope split.
Also includes the shared ``resolved_outcome`` builder, ``ack_threads``
helper, and ``review_spec_conciseness`` used by single/multi paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...agents import refining
from ...config.settings import Settings
from ...core.models import Ticket, TicketKind
from ...core.states import State
from ...core.workspace import Workspace
from ..base import Outcome, StageContext
from .helpers import (
    UNMERGED_BRANCH_PREFIX,
    _COMMIT_SHA_RE,
    _TICKET_ID_RE,
    _rationale_claims_external_fix,
    _resolve_next_state,
    _spec_is_degenerate,
    _verify_cited_fix_at_head,
    log,
)


# -- shared outcome / thread / artifact helpers -------------------------


def resolved_outcome(
    ctx: StageContext,
    spec: str,
    ticket_id: str,
    base_note: str,
    *,
    source: str | None = None,
    triage_note: str | None = None,
) -> Outcome:
    """Resolve the next state for *spec* and build the closing Outcome.

    Encapsulates the repeated ``_resolve_next_state`` → "append the
    auto-approve note when present" → ``Outcome`` pattern shared by the
    split-child, triage-skip, single-scope, and split paths.
    """
    next_state, auto_note = _resolve_next_state(
        ctx, spec, ticket_id, source=source, triage_note=triage_note
    )
    note = base_note
    if auto_note:
        note += f" | {auto_note}"
    return Outcome(next_state, note)


def ack_threads(
    ctx: StageContext,
    ticket: Ticket,
    reviewer_comments: str | None,
    open_thread_ids: set[int],
) -> None:
    """Acknowledge any open reviewer threads after the agent ran.

    No-op unless there were reviewer comments *and* open threads — the
    guard the original repeated at every outcome return.
    """
    if reviewer_comments and open_thread_ids:
        # Late import from orchestration so test monkeypatches on
        # orchestration.acknowledge_unanswered_threads take effect here.
        from . import orchestration as _orch

        _orch.acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)


# -- spec conciseness review --------------------------------------------


def review_spec_conciseness(
    s: Settings,
    ws: Workspace,
    ticket: Ticket,
    spec: str,
    verbose_filename: str,
    child_index: int | None = None,
) -> str:
    """Run the conciseness review on *spec*, returning the concise spec.

    Saves the verbose original to ``ws.artifacts_dir / verbose_filename``
    and returns the reviewed concise spec.  On a degenerate
    (empty/placeholder) review result or any failure, returns the
    original verbose *spec* unchanged.  When *child_index* (1-based) is
    given, log messages name the child — preserving the two original
    message variants exactly.
    """
    try:
        review_result = refining.review_spec_for_conciseness(
            settings=s,
            spec_markdown=spec,
        )
        (ws.artifacts_dir / verbose_filename).write_text(
            spec,
            encoding="utf-8",
        )
        concise = review_result.concise_spec
        if _spec_is_degenerate(concise):
            if child_index is None:
                log.warning(
                    "%s: spec review returned empty/placeholder "
                    "concise spec, using verbose spec",
                    ticket.id,
                )
            else:
                log.warning(
                    "%s: spec review child %d returned empty/placeholder "
                    "concise spec, using verbose spec",
                    ticket.id,
                    child_index,
                )
            return spec
        if child_index is None:
            log.info(
                "%s: spec review: %s",
                ticket.id,
                review_result.stripped_summary,
            )
        else:
            log.info(
                "%s: spec review child %d: %s",
                ticket.id,
                child_index,
                review_result.stripped_summary,
            )
        return concise
    except Exception:
        if child_index is None:
            log.warning(
                "%s: spec review failed, using verbose spec",
                ticket.id,
                exc_info=True,
            )
        else:
            log.warning(
                "%s: spec review failed for child %d, using verbose spec",
                ticket.id,
                child_index,
                exc_info=True,
            )
        return spec


# -- phase: no-change-needed --------------------------------------------


def no_change_path(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    repo_dir: Path | None,
    title: str,
    ws: Workspace,
    result: refining.RefineResult,
) -> Outcome | None:
    """Handle the ``no_change_needed`` result mode.

    When refine concludes the spec is informational — full
    investigation already in the body, acceptance criteria are
    "post a comment explaining why no change is needed", or a
    parallel ticket already shipped the fix — it returns
    no_change_needed=true. The stage files the rationale as a
    top-level comment on the ticket and transitions
    DRAFT → DONE, skipping implement / review / document /
    deliver / merge. This is the bypass that catches the
    d129-style "implement gets stuck because there's nothing
    to write" failure mode.

    Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
    through (no-change does not apply / degrades to the normal paths).
    """
    from robotsix_mill.stages import refine as _facade

    if not (
        result.no_change_needed and not result.split and not result.promote_to_epic
    ):
        return None

    rationale = (result.no_change_rationale or "").strip()
    if not rationale:
        log.warning(
            "%s: no_change_needed but no rationale — "
            "degrading to normal single-spec path",
            ticket.id,
        )
        return None

    if ticket.branch and not _facade._verify_branch_merged(repo_dir, ticket):
        return Outcome(
            State.BLOCKED,
            f"{UNMERGED_BRANCH_PREFIX} '{ticket.branch}' "
            "but is not merged to main. "
            "Merge the PR or manually close.",
        )

    if _rationale_claims_external_fix(rationale):
        cited_refs = _TICKET_ID_RE.findall(rationale) + _COMMIT_SHA_RE.findall(
            rationale.lower()
        )
        cited = (
            ", ".join(dict.fromkeys(cited_refs))
            or "the prior ticket / commit named in the rationale"
        )
        ancestry_ok = _verify_cited_fix_at_head(repo_dir, rationale)
        log.info(
            "%s: no_change_needed rationale claims an external "
            "fix (%s) — routing to implement for live re-check "
            "(cited-commit ancestry check: %s)",
            ticket.id,
            cited,
            "passed (NOT sufficient — see revert subtlety)"
            if ancestry_ok
            else "not proven",
        )
        verification_spec = (
            "## Problem\n\n"
            "A prior refine pass concluded this ticket needs no "
            "change because the fix was already shipped elsewhere "
            f"({cited}). That claim was NOT verified against the "
            "live tree. A `git revert` re-introduces a bug while "
            "leaving the original fix commit as an ancestor of "
            "`origin/main`, so the cited fix may not actually be "
            "present at HEAD — re-verify before closing.\n\n"
            f"Original ticket: {title}\n\n"
            "Original problem / draft:\n\n"
            f"{draft or '(no draft body)'}\n\n"
            "Refine's unverified rationale:\n\n"
            f"{rationale}\n\n"
            "## Scope\n\n"
            "Inspect the relevant file(s) / condition named in the "
            "original problem at the current HEAD and determine "
            "whether the bug condition is still present.\n\n"
            "## Acceptance criteria\n\n"
            "- If the bug condition is still present at HEAD (e.g. "
            "the cited fix was reverted or overwritten), re-apply "
            "the fix so the condition is resolved (with a test "
            "where appropriate).\n"
            "- If the condition is genuinely already resolved at "
            "HEAD, make no change — the implement empty-diff path "
            "will close the ticket.\n\n"
            "## Out of scope / constraints\n\n"
            "- Do not expand scope beyond verifying and (if needed) "
            f"re-applying the fix for: {title}.\n"
            "- Ancestry of the cited commit is NOT sufficient proof "
            "(a revert leaves it an ancestor); verify the actual "
            "bug condition against the working tree.\n"
        )
        new_hash = ws.write_description(verification_spec)
        ctx.service.set_content_hash(ticket.id, new_hash)
        return resolved_outcome(
            ctx,
            verification_spec,
            ticket.id,
            "refined | unverified 'already implemented' claim "
            "routed to implement for live re-check",
            source=ticket.source,
        )

    short = rationale[:400] + ("…" if len(rationale) > 400 else "")
    return Outcome(
        State.DONE,
        f"no change needed — {short}",
    )


# -- phase: promote-to-epic ---------------------------------------------


def promote_to_epic_path(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
    result: refining.RefineResult,
) -> Outcome:
    """Handle the ``promote_to_epic`` result mode.

    When refine decides the spec is too varied for one pass
    (manifest-driven, ≥6 children, per-item deep specs needed),
    it returns promote_to_epic=True. The stage converts the
    ticket to an epic, writes the strategic epic_body to the
    workspace description, and synchronously invokes
    epic-breakdown to spawn the children. After that the epic
    sits in EPIC_OPEN — its children flow through refine
    individually on their own cycles.
    """
    from ...agents.epic_breakdown import (
        plan_child_dependencies,
        run_epic_breakdown_agent,
    )

    epic_body = (result.epic_body or result.spec_markdown or "").strip()
    if not epic_body:
        log.warning(
            "%s: promote_to_epic but no epic_body — falling back to original draft",
            ticket.id,
        )
        epic_body = draft or ticket.title
    new_hash = ws.write_description(epic_body)
    ctx.service.set_content_hash(ticket.id, new_hash)
    ctx.service.promote_to_epic(ticket.id)
    try:
        breakdown = run_epic_breakdown_agent(
            settings=s,
            epic_title=ticket.title,
            epic_description=epic_body,
        )
        from ...core.dedup import annotate_child_body, find_child_overlaps

        child_titles = list(breakdown.child_titles)
        child_bodies = list(breakdown.child_bodies)
        overlap_notes = find_child_overlaps(
            ctx.service,
            ticket.id,
            child_titles,
            child_bodies,
            s,
            datetime.now(timezone.utc),
        )
        created_children: list[tuple[str, str, str]] = []
        for child_title, child_body, dup_note in zip(
            child_titles,
            child_bodies,
            overlap_notes,
            strict=True,
        ):
            if dup_note:
                log.warning(
                    "epic %s: child '%s' flagged as possible duplicate — %s",
                    ticket.id,
                    child_title,
                    dup_note,
                )
                child_body = annotate_child_body(child_body, dup_note)
            child = ctx.service.create(
                title=child_title,
                description=child_body,
                kind=TicketKind.TASK,
                parent_id=ticket.id,
            )
            created_children.append((child.id, child_title, child_body))
        created_ids = [cid for cid, _t, _b in created_children]
        for child_id, deps in plan_child_dependencies(
            created_children,
            child_board_id=lambda cid: (
                _t.board_id
                if (_t := ctx.service.get(cid)) is not None
                else ctx.service.board_id
            ),
            create_child=lambda title, body: (
                ctx.service.create(
                    title=title,
                    description=body,
                    kind=TicketKind.TASK,
                    parent_id=ticket.id,
                ).id
            ),
        ).items():
            ctx.service.set_depends_on(child_id, deps)
        if breakdown.epic_body and breakdown.epic_body.strip():
            revised_hash = ws.write_description(
                breakdown.epic_body.strip(),
            )
            ctx.service.set_content_hash(ticket.id, revised_hash)
        note = f"promoted to epic; spawned {len(created_ids)} child(ren)"
    except Exception:
        log.exception(
            "%s: epic-breakdown after promote_to_epic failed — "
            "epic body is in place, children left for "
            "/generate-children",
            ticket.id,
        )
        note = "promoted to epic; breakdown failed — use /generate-children to retry"
    return Outcome(State.EPIC_OPEN, note)


# -- phase: normal single-scope -----------------------------------------


def single_scope_path(
    ctx: StageContext,
    ticket: Ticket,
    ws: Workspace,
    s: Settings,
    result: refining.RefineResult,
    reviewer_comments: str | None,
    open_thread_ids: set[int],
) -> Outcome:
    """Handle the normal (non-split) single-scope result."""
    spec = result.spec_markdown or ""
    if _spec_is_degenerate(spec):
        log.warning(
            "%s: refiner produced no usable spec (empty or "
            "placeholder %r) — proceeding with original draft",
            ticket.id,
            spec[:60],
        )
        next_state, _auto_reason = _resolve_next_state(
            ctx, "", ticket.id, source=ticket.source
        )
        return Outcome(next_state, "refined (no usable spec — kept original draft)")

    if s.spec_review_enabled and not reviewer_comments:
        spec = review_spec_conciseness(s, ws, ticket, spec, "refine-verbose.md")

    new_hash = ws.write_description(spec)
    ctx.service.set_content_hash(ticket.id, new_hash)

    ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)

    return resolved_outcome(ctx, spec, ticket.id, "refined", source=ticket.source)


# -- phase: multi-scope split -------------------------------------------


def multi_scope_path(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
    epic_ctx: str,
    result: refining.RefineResult,
    reviewer_comments: str | None,
    open_thread_ids: set[int],
) -> Outcome:
    """Handle the multi-scope split result (validate, split, reparent)."""
    children_raw = result.children
    if not children_raw or len(children_raw) == 0:
        spec = result.spec_markdown or ""
        if _spec_is_degenerate(spec):
            log.warning(
                "%s: refiner produced no usable spec "
                "(split with no children) — "
                "proceeding with original draft",
                ticket.id,
            )
            next_state, _auto_reason = _resolve_next_state(
                ctx, "", ticket.id, source=ticket.source
            )
            ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)
            return Outcome(
                next_state,
                "refined (empty spec, split degraded — kept original draft)",
            )

        if s.spec_review_enabled and not reviewer_comments:
            spec = review_spec_conciseness(s, ws, ticket, spec, "refine-verbose.md")

        new_hash = ws.write_description(spec)
        ctx.service.set_content_hash(ticket.id, new_hash)

        ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)

        return resolved_outcome(
            ctx,
            spec,
            ticket.id,
            "refined (split degraded — no valid children)",
            source=ticket.source,
        )

    # Validate and collect valid children.
    valid_children: list[dict[str, Any]] = []
    for spec_child in children_raw:
        child_title = (spec_child.title or "").strip()
        spec_md = (spec_child.spec_markdown or "").strip()
        if not child_title or not spec_md:
            continue
        deps = spec_child.depends_on or []
        if not isinstance(deps, list):
            deps = []
        deps = [d for d in deps if isinstance(d, int) and d >= 0]
        valid_children.append(
            {
                "title": child_title,
                "spec_markdown": spec_md,
                "depends_on": deps,
            }
        )

    if len(valid_children) == 0:
        ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)
        return Outcome(State.BLOCKED, "refiner produced no valid split children")

    if s.spec_review_enabled and not reviewer_comments:
        for i, child in enumerate(valid_children):
            child["spec_markdown"] = review_spec_conciseness(
                s,
                ws,
                ticket,
                child["spec_markdown"],
                f"refine-verbose-child-{i + 1}.md",
                child_index=i + 1,
            )

    if len(valid_children) == 1:
        child = valid_children[0]
        new_hash = ws.write_description(child["spec_markdown"])
        ctx.service.set_content_hash(ticket.id, new_hash)
        if not (result.title and result.title.strip()):
            ctx.service.set_title(ticket.id, child["title"])

        ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)

        return resolved_outcome(
            ctx,
            child["spec_markdown"],
            ticket.id,
            "refined (single child, no split)",
            source=ticket.source,
        )

    # Create child tickets.
    child_ids: list[str] = []
    for _i, child in enumerate(valid_children):
        child_ticket = ctx.service.create(
            title=child["title"],
            description=child["spec_markdown"],
            source=ticket.source,
            board_id=ticket.board_id,
        )
        child_ids.append(child_ticket.id)

    # Reparent children.
    existing_epic_id: str | None = None
    if ticket.parent_id is not None:
        parent_candidate = ctx.service.get(ticket.parent_id)
        if parent_candidate is not None and parent_candidate.kind == TicketKind.EPIC:
            existing_epic_id = ticket.parent_id
            for cid in child_ids:
                ctx.service.set_parent(cid, existing_epic_id)
    if existing_epic_id is None:
        epic_title = (result.title and result.title.strip()) or ticket.title.strip()
        epic_desc = (result.spec_markdown and result.spec_markdown.strip()) or draft
        epic = ctx.service.create(
            title=epic_title,
            description=epic_desc,
            kind=TicketKind.EPIC,
            source=ticket.source,
            board_id=ticket.board_id,
        )
        for cid in child_ids:
            ctx.service.set_parent(cid, epic.id)

    # Resolve depends_on indices → real ticket IDs.
    for i, child in enumerate(valid_children):
        if child["depends_on"]:
            resolved = []
            for idx in child["depends_on"]:
                if 0 <= idx < i and idx < len(child_ids):
                    resolved.append(child_ids[idx])
            if resolved:
                ctx.service.set_depends_on(child_ids[i], resolved)

    # Transition each child to HUMAN_ISSUE_APPROVAL or READY.
    for i, cid in enumerate(child_ids):
        child_state, auto_note = _resolve_next_state(
            ctx,
            valid_children[i]["spec_markdown"],
            cid,
        )
        child_note = f"split from {ticket.id}"
        if auto_note:
            child_note += f" | {auto_note}"
        ctx.service.transition(cid, child_state, note=child_note)

    if result.epic_body and result.epic_body.strip() and epic_ctx:
        parent = ctx.service.get(ticket.parent_id)
        if parent is not None and parent.kind == TicketKind.EPIC:
            new_hash = ctx.service.workspace(parent).write_description(
                result.epic_body.strip()
            )
            ctx.service.set_content_hash(parent.id, new_hash)

    ids_note = ", ".join(child_ids)

    ack_threads(ctx, ticket, reviewer_comments, open_thread_ids)

    return Outcome(
        State.CLOSED,
        f"split into {ids_note}",
    )
