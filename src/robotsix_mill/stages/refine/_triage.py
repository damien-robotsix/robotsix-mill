) -> Outcome | None:
    """Skip re-refinement for split children.

    A child ticket created from a split already has a refined
    spec in its description.md.  Detect this by checking whether
    the parent is CLOSED with a "split into" note — the canonical
    signal that this ticket's description is already the refined
    output.  When children are reparented to an umbrella epic
    the direct parent is no longer CLOSED, so also check the
    ticket's own history for a "split from" transition note.
    Also detect children of EPIC_OPEN epics (created by implement
    stage's scope-split EXPAND cap) as split children.
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
        if parent is not None:
            # Detect split children from multi-scope refine splits
            # (parent is CLOSED with "split into" note in history).
            if parent.state == State.CLOSED:
                parent_history = ctx.service.history(parent.id)
                is_split_child = any(
                    ev.state == State.CLOSED
                    and ev.note
                    and ev.note.startswith("split into")
                    for ev in parent_history  # type: ignore[attr-defined]
                )
            # Also detect scope-split children from implement stage:
            # children grouped under an EPIC_OPEN epic (the canonical
            # marker for implement-stage scope-split groupings created by
            # the scope-triage EXPAND cap).
            elif parent.kind == TicketKind.EPIC and parent.state == State.EPIC_OPEN:
                is_split_child = True
    if not is_split_child:
        # Final fallback: check own history for "split from" note (both
        # refine-stage splits and implement-stage scope-splits mark their
        # description this way).
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
