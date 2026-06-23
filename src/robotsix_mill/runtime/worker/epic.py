from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ...core.states import State
from ...core.models import TicketKind

if TYPE_CHECKING:
    from ...forge.base import BranchInfo

log = logging.getLogger("robotsix_mill.worker")


_EPIC_CHILD_TERMINAL = frozenset(
    {State.DONE, State.CLOSED, State.ANSWERED, State.EPIC_CLOSED}
)


def _run_epic_reeval(epic_id: str, settings) -> None:
    """Background runner for epic re-evaluation.

    1. Creates a fresh ``TicketService`` (the worker's ``ctx.service``
       is bound to a shared DB session and not thread-safe).
    2. Fetches the epic, reads its description, gathers all children
       with their descriptions.
    3. Calls :func:`~.agents.epic_status.run_epic_status_agent`.
    4. Transitions the epic (close), updates its description, or does
       nothing (keep_open) based on the agent's decision.
    """
    from ...agents.epic_status import run_epic_status_agent
    from ...runtime import tracing

    bound = _validate_epic_state(settings, epic_id)
    if bound is None:
        return
    svc, epic = bound
    try:
        epic_desc = svc.workspace(epic).read_description()
        child_summaries = _build_child_summaries(svc, epic_id)

        with tracing.start_ticket_root_span(epic_id, "epic-status"):
            result = run_epic_status_agent(
                settings=settings,
                epic_title=epic.title,
                epic_description=epic_desc,
                children=child_summaries,
            )

        _handle_epic_decision(svc, epic_id, epic, result)
        # Apply child-ticket changes (new_children, child_rescopes, child_closures).
        _reconcile_child_changes(svc, epic_id, result)
    except Exception:
        log.exception("epic %s: re-evaluation failed", epic_id)


def _validate_epic_state(settings, epic_id: str):
    """Discover and bind the epic's board-scoped service for re-evaluation.

    Returns the bound ``(svc, epic)`` tuple, or ``None`` (after logging
    the same warning/debug messages) when the epic has vanished or is
    already ``EPIC_CLOSED``.
    """
    from ...core.service import TicketService

    # Discover the epic's board via fanout, then bind the service to
    # it so subsequent transitions / writes go to the right per-repo DB.
    discovery = TicketService(settings)
    epic = discovery.get(epic_id)
    if epic is None:
        log.warning("epic %s vanished before re-evaluation", epic_id)
        return None
    svc = TicketService(settings, board_id=epic.board_id)
    epic = svc.get(epic_id)
    if epic is None:
        log.warning("epic %s vanished before re-evaluation", epic_id)
        return None
    if epic.state is State.EPIC_CLOSED:
        log.debug("epic %s: already EPIC_CLOSED — skipping re-evaluation", epic_id)
        return None
    return svc, epic


def _resolve_delivery(
    svc: Any, ticket_id: str, _seen: set[str] | None = None
) -> dict[str, Any]:
    """Classify a ticket's *delivery* state by scanning its history.

    Returns a small dict with at least ``delivered`` (bool), ``label``
    (human-readable), and ``canonical`` (resolved dedup-chain id or
    ``None``). A ticket counts as *delivered* only when it has a terminal
    ``DONE`` event whose note is **not** a non-implementation close (i.e.
    the genuine ``"merged: …"`` path). Dedup-closed tickets are followed
    to their canonical ticket — a dedup chain whose end never merged is
    *not* delivered. The function never raises and guards against
    cycles/self-reference via the ``_seen`` set.
    """
    from ...stages.refine.helpers import (
        DEDUP_ALREADY_DONE_PREFIX,
        DEDUP_DUPLICATE_PREFIX,
        NON_IMPLEMENTATION_CLOSE_PREFIXES,
    )

    if _seen is None:
        _seen = set()
    if ticket_id in _seen or len(_seen) > 20:
        return {"delivered": False, "label": "not delivered", "canonical": None}
    _seen.add(ticket_id)

    ticket = svc.get(ticket_id)
    if ticket is None:
        return {"delivered": False, "label": "not delivered", "canonical": None}

    try:
        history = svc.history(ticket_id)
    except Exception:
        history = []

    # Last event with state DONE is the terminal delivery decision.
    done_event = None
    for ev in history or []:
        if getattr(ev, "state", None) is State.DONE:
            done_event = ev

    if done_event is None:
        # Never reached DONE — unstarted (DRAFT) or in-progress.
        is_draft = getattr(ticket, "state", None) is State.DRAFT
        return {
            "delivered": False,
            "label": "unstarted" if is_draft else "in_progress",
            "canonical": None,
        }

    note = getattr(done_event, "note", None) or ""
    matched_prefix = next(
        (p for p in NON_IMPLEMENTATION_CLOSE_PREFIXES if note.startswith(p)),
        None,
    )
    if matched_prefix is None:
        # A genuine DONE that is not a non-implementation close → merged.
        return {"delivered": True, "label": "merged", "canonical": None}

    # Non-implementation close. For dedup closes, follow the chain to the
    # canonical ticket and inherit its delivery verdict.
    if matched_prefix in (DEDUP_DUPLICATE_PREFIX, DEDUP_ALREADY_DONE_PREFIX):
        canonical = note[len(matched_prefix) :].split(":", 1)[0].strip()
        if canonical and canonical != ticket_id and canonical not in _seen:
            sub = _resolve_delivery(svc, canonical, _seen)
            sub_label = "merged" if sub["delivered"] else "not delivered"
            return {
                "delivered": sub["delivered"],
                "label": f"dedup-closed → {canonical} ({sub_label})",
                "canonical": canonical,
            }
        # Missing / self / looping canonical → not delivered.
        return {
            "delivered": False,
            "label": f"dedup-closed → {canonical or '?'} (not delivered)",
            "canonical": canonical or None,
        }

    # Freshness/obsolescence non-implementation close — shipped nothing.
    return {"delivered": False, "label": "closed (not delivered)", "canonical": None}


def _build_child_summaries(svc, epic_id: str) -> list[dict]:
    """Build the per-child summary dicts passed to the epic-status agent.

    Each child's description is read and truncated to 500 chars (with a
    ``"\\n...(truncated)"`` suffix); the summary carries ``id``,
    ``title``, ``state``, ``description``, ``depends_on`` and
    ``delivery`` (a delivery-evidence label from :func:`_resolve_delivery`).
    """
    from ...core.service import TicketService

    child_summaries: list[dict] = []
    for child in svc.list_children(epic_id):
        child_desc = svc.workspace(child).read_description()
        if len(child_desc) > 500:
            child_desc = child_desc[:500] + "\n...(truncated)"
        child_summaries.append(
            {
                "id": child.id,
                "title": child.title,
                "state": child.state.value,
                "description": child_desc,
                "depends_on": TicketService._parse_depends_on(child),
                "delivery": _resolve_delivery(svc, child.id)["label"],
            }
        )
    return child_summaries


def _apply_dep_updates(svc, epic_id: str, dep_updates) -> None:
    """Apply the agent's per-child dependency replacements.

    ``None`` entries are normalized to an empty list before writing.
    """
    log.info(
        "epic %s: agent requested dependency updates for %d children",
        epic_id,
        len(dep_updates),
    )
    for child_id, new_deps in dep_updates.items():
        if new_deps is None:
            new_deps = []
        svc.set_depends_on(child_id, new_deps)


def _handle_epic_decision(svc, epic_id: str, epic, result) -> None:
    """Apply the agent's epic-level decision to the bound epic.

    Handles the close-vs-new-children downgrade safety net and the
    close / keep_open / update_description / update_deps dispatch.
    """
    # Safety net for the close-vs-new-children coupling enforced in
    # the prompt: if the agent says `close` but also proposes new
    # follow-up work, treat it as `keep_open` so the new children
    # get created and run before the epic is sealed. The next
    # re-eval (after those children land) gets another chance.
    has_new_children = bool(result.new_children)
    if result.decision == "close" and has_new_children:
        log.warning(
            "epic %s: agent returned close + %d new_children — "
            "downgrading to keep_open until follow-up work lands",
            epic_id,
            len(result.new_children),
        )
        result.decision = "keep_open"

    if result.decision == "close":
        svc.transition(
            epic_id, State.EPIC_CLOSED, note="[auto-closed] " + (result.note or "")
        )
        log.info("epic %s: agent decided close — transitioned to EPIC_CLOSED", epic_id)
    elif result.decision == "keep_open":
        log.debug("epic %s: agent decided keep_open — no change", epic_id)
    elif result.decision == "update_description":
        new_hash = svc.workspace(epic).write_description(result.note)
        svc.set_content_hash(epic_id, new_hash)
        log.info("epic %s: agent updated description", epic_id)
    elif result.decision == "update_deps":
        if result.dep_updates is not None:
            _apply_dep_updates(svc, epic_id, result.dep_updates)
        if result.note:
            new_hash = svc.workspace(epic).write_description(result.note)
            svc.set_content_hash(epic_id, new_hash)


def _fetch_draft_child(svc, child_id: str, operation: str, epic_id: str):
    """Fetch a child ticket and verify it is in DRAFT state.

    Returns the child ticket if safe to mutate, or ``None`` if the
    child is missing or not in DRAFT (with a warning logged).
    """
    from ...core.states import State as S

    child = svc.get(child_id)
    if child is None:
        log.warning(
            "epic %s: %s — child %s not found, skipping",
            epic_id,
            operation,
            child_id,
        )
        return None
    if child.state != S.DRAFT:
        log.warning(
            "epic %s: %s — child %s is in state %s (not DRAFT), skipping",
            epic_id,
            operation,
            child_id,
            child.state.value,
        )
        return None
    return child


def _reconcile_child_changes(svc, epic_id: str, result) -> None:
    """Apply proposed child-ticket changes with safe reconciliation.

    - *new_children* are always created.
    - *child_rescopes* and *child_closures* only apply to DRAFT children;
      in-flight / terminal children are skipped with a warning.
    - Each child operation is wrapped in its own try/except so one
      failure does not halt the rest.
    """
    from ...core.states import State as S

    # --- new_children --------------------------------------------------
    if result.new_children:
        for i, child_spec in enumerate(result.new_children):
            if not isinstance(child_spec, dict):
                log.warning(
                    "epic %s: new_children[%d] is not a dict, skipping",
                    epic_id,
                    i,
                )
                continue
            title = child_spec.get("title", "")
            body = child_spec.get("body", "")
            if not isinstance(title, str) or not title.strip():
                log.warning(
                    "epic %s: new_children[%d] missing non-empty 'title', skipping",
                    epic_id,
                    i,
                )
                continue
            if not isinstance(body, str) or not body.strip():
                log.warning(
                    "epic %s: new_children[%d] missing non-empty 'body', skipping",
                    epic_id,
                    i,
                )
                continue
            try:
                child = svc.create(
                    title=title.strip(),
                    description=body.strip(),
                    kind=TicketKind.TASK,
                    parent_id=epic_id,
                )
                log.info(
                    "epic %s: created new child %s ('%s')",
                    epic_id,
                    child.id,
                    title,
                )
            except Exception:
                log.exception(
                    "epic %s: failed to create new child '%s'",
                    epic_id,
                    title,
                )

    # --- child_rescopes ------------------------------------------------
    if result.child_rescopes:
        for child_id, updates in result.child_rescopes.items():
            if not isinstance(updates, dict):
                log.warning(
                    "epic %s: child_rescopes[%s] is not a dict, skipping",
                    epic_id,
                    child_id,
                )
                continue
            new_title = updates.get("title")
            new_body = updates.get("body")
            has_title = isinstance(new_title, str) and new_title.strip()
            has_body = isinstance(new_body, str) and new_body.strip()
            if not has_title and not has_body:
                log.warning(
                    "epic %s: child_rescopes[%s] has no non-empty 'title' or 'body', skipping",
                    epic_id,
                    child_id,
                )
                continue

            child = _fetch_draft_child(svc, child_id, "rescope", epic_id)
            if child is None:
                continue

            try:
                if has_title:
                    svc.set_title(child_id, new_title.strip())
                    log.info(
                        "epic %s: rescoped child %s title -> '%s'",
                        epic_id,
                        child_id,
                        new_title.strip(),
                    )
                if has_body:
                    new_hash = svc.workspace(child).write_description(new_body.strip())
                    svc.set_content_hash(child_id, new_hash)
                    log.info(
                        "epic %s: rescoped child %s body",
                        epic_id,
                        child_id,
                    )
            except Exception:
                log.exception(
                    "epic %s: failed to rescope child %s",
                    epic_id,
                    child_id,
                )

    # --- child_closures ------------------------------------------------
    if result.child_closures:
        # Normalize to (child_id, covering_id) pairs. The agent should
        # emit a ``dict`` mapping child -> covering merged sibling, but a
        # legacy bare list (no named sibling) is accepted and each entry
        # is treated as a closure with NO covering sibling (so it is
        # refused by the verification gate below).
        if isinstance(result.child_closures, dict):
            closure_pairs = list(result.child_closures.items())
        else:
            closure_pairs = [(cid, None) for cid in result.child_closures]

        for child_id, covering_id in closure_pairs:
            if not isinstance(child_id, str) or not child_id.strip():
                log.warning(
                    "epic %s: child_closures entry %r is not a non-empty string, skipping",
                    epic_id,
                    child_id,
                )
                continue
            child = _fetch_draft_child(svc, child_id, "closure", epic_id)
            if child is None:
                continue
            try:
                # Verify a genuinely-merged covering sibling before
                # obsoleting an unstarted child. Without this gate a
                # dedup-close or an unrelated sibling merge would wipe
                # un-delivered Tier-1 work (incident on epic 4564).
                covering = covering_id.strip() if isinstance(covering_id, str) else ""
                if not covering:
                    log.warning(
                        "epic %s: closure of child %s refused — no covering "
                        "sibling named",
                        epic_id,
                        child_id,
                    )
                    continue
                if covering == child_id:
                    log.warning(
                        "epic %s: closure of child %s refused — covering "
                        "sibling equals the child itself",
                        epic_id,
                        child_id,
                    )
                    continue
                delivery = _resolve_delivery(svc, covering)
                if not delivery.get("delivered"):
                    log.warning(
                        "epic %s: closure of child %s refused — covering "
                        "sibling %s is not a merged delivery (%s)",
                        epic_id,
                        child_id,
                        covering,
                        delivery.get("label"),
                    )
                    continue
                svc.transition(
                    child_id,
                    S.CLOSED,
                    note=f"Obsoleted: scope delivered by merged sibling {covering}",
                )
                log.info(
                    "epic %s: closed child %s (scope delivered by merged sibling %s)",
                    epic_id,
                    child_id,
                    covering,
                )
            except Exception:
                log.exception(
                    "epic %s: failed to close child %s",
                    epic_id,
                    child_id,
                )


def _run_epic_reprocess(
    epic_id: str, comment_body: str, settings, board_id: str = ""
) -> None:
    """Background runner for epic re-processing triggered by a comment.

    1. Creates a fresh ``TicketService`` (the route's ``svc`` is bound
       to a request-scoped session and not thread-safe).
    2. Fetches the epic, reads its description, and gathers the full
       comment history.
    3. Calls :func:`~.agents.epic_breakdown.run_epic_breakdown_agent`
       with the operator comments included in the prompt.
    4. Reconciles the agent's proposed children against existing
       children: skips duplicates (case-insensitive title match),
       creates only net-new children.
    5. Chains new children linearly, appended after the last existing
       child.
    """
    from ...core.service import TicketService
    from ...agents.epic_breakdown import (
        plan_child_dependencies,
        run_epic_breakdown_agent,
    )

    # Discover the epic's board via fanout, then bind the service to
    # it so subsequent writes go to the right per-repo DB.
    # When *board_id* is known at spawn time (the route already holds
    # the ticket object), skip the fanout step to avoid a race between
    # the discovery read and the bound lookup.
    if not board_id:
        discovery = TicketService(settings)
        found = discovery.get(epic_id)
        if found is None:
            log.warning("epic %s vanished before re-processing", epic_id)
            return
        board_id = found.board_id

    svc = TicketService(settings, board_id=board_id)
    try:
        epic = svc.get(epic_id)
        if epic is None:
            log.warning("epic %s vanished before re-processing", epic_id)
            return

        epic_desc = svc.workspace(epic).read_description()

        # Build chronological comment history for the agent prompt.
        all_comments = svc.list_comments(epic_id)
        comment_lines: list[str] = []
        for c in all_comments:
            ts = (
                c.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if c.created_at
                else "unknown"
            )
            if c.parent_id is None:
                comment_lines.append(f"[{ts}] {c.author}: {c.body}")
            else:
                comment_lines.append(f"[{ts}]   ↳ {c.author}: {c.body}")
        comments_prompt = "\n".join(comment_lines)

        result = run_epic_breakdown_agent(
            settings=settings,
            epic_title=epic.title,
            epic_description=epic_desc,
            comments=comments_prompt,
        )

        # Reconcile: compare proposed titles against existing children.
        existing = svc.list_children(epic_id)
        existing_titles_lower = {child.title.strip().lower() for child in existing}

        new_titles: list[str] = []
        new_bodies: list[str] = []
        for title, body in zip(result.child_titles, result.child_bodies, strict=True):
            if title.strip().lower() in existing_titles_lower:
                log.debug("epic %s: skipping duplicate child '%s'", epic_id, title)
                continue
            new_titles.append(title)
            new_bodies.append(body)

        if not new_titles:
            log.info(
                "epic %s: re-processed — no new children (all %d proposed "
                "were duplicates)",
                epic_id,
                len(result.child_titles),
            )
            return

        # Advisory pre-filing dedup: flag (never drop) children whose
        # scope overlaps a recent ticket or an earlier sibling in this
        # batch. Runs after the existing-children title filter above.
        # Best-effort — a failure must not block filing.
        from ...core.dedup import annotate_child_body, find_child_overlaps

        overlap_notes = find_child_overlaps(
            svc,
            epic_id,
            new_titles,
            new_bodies,
            settings,
            datetime.now(timezone.utc),
        )

        created_children: list[tuple[str, str, str]] = []
        for title, body, dup_note in zip(
            new_titles, new_bodies, overlap_notes, strict=True
        ):
            if dup_note:
                log.warning(
                    "epic %s: child '%s' flagged as possible duplicate — %s",
                    epic_id,
                    title,
                    dup_note,
                )
                body = annotate_child_body(body, dup_note)
            child = svc.create(
                title=title,
                description=body,
                kind=TicketKind.TASK,
                parent_id=epic_id,
            )
            created_children.append((child.id, title, body))
        created_ids = [cid for cid, _t, _b in created_children]

        # Dependency wiring: a linear chain appended after the last
        # existing child — unless the batch includes a create/initialize-
        # repo child, in which case repo-populating siblings depend on it
        # so they cannot run before the repo exists.  Cross-repo
        # producer→consumer edges and bump-child synthesis are also
        # applied when children target different repos.
        predecessor_id = existing[-1].id if existing else None
        for child_id, deps in plan_child_dependencies(
            created_children,
            predecessor_id=predecessor_id,
            child_board_id=lambda cid: (
                _t.board_id if (_t := svc.get(cid)) is not None else svc.board_id
            ),
            create_child=lambda title, body: (
                svc.create(
                    title=title,
                    description=body,
                    kind=TicketKind.TASK,
                    parent_id=epic_id,
                ).id
            ),
        ).items():
            svc.set_depends_on(child_id, deps)

        log.info(
            "epic %s: re-processed — created %d new children: %s",
            epic_id,
            len(created_ids),
            ", ".join(created_ids),
        )
    except Exception:
        log.exception("epic %s: re-processing failed", epic_id)


def _branch_is_stale(
    b: "BranchInfo",
    *,
    now: "datetime",
    max_age_days: int,
    target_branch: str,
    open_pr: "set[str]",
    prefix_only: bool,
    branch_prefix: str,
) -> bool:
    """Return True when *b* is eligible for deletion under all guards."""
    from datetime import timedelta

    if b.name == target_branch:
        return False
    if b.is_protected:
        return False
    if b.name in open_pr:
        return False
    cutoff = now - timedelta(days=max_age_days)
    if b.last_commit_at >= cutoff:
        return False
    if prefix_only and not b.name.startswith(branch_prefix):
        return False
    return True
