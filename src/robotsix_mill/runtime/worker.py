"""Event-driven worker. No scheduler.

A ticket is enqueued the moment it is emitted (or transitions into an
actionable state). A consumer pulls it and **chains** stages —
``draft → … → done`` — until it hits a terminal state, a stub, or an
error. A bounded **pool** of consumers (``MILL_MAX_CONCURRENCY``) runs
distinct tickets in parallel; a dedupe set guarantees one ticket is
never processed by two consumers at once (one ticket's stages still
run sequentially within its own consumer).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import RepoConfig, get_repos_config
from ..langfuse_client import session_cost
from ..stages import StageContext, get_stage
from ..core.states import STAGE_FOR_STATE, State
from ..core.models import SourceKind
from ..notify import send_notification, _TRIGGER_STATES
from . import tracing
from .tracing import langfuse_trace_url
from .run_registry import RunRegistry

log = logging.getLogger("robotsix_mill.worker")


def _post_trace_event(
    ctx: StageContext,
    ticket_id: str,
    trace_id: str | None,
    stage_name: str,
) -> None:
    """Append the post-stage Langfuse trace URL to the ticket's history.

    Previously this wrote a comment with ``author="mill"``, which
    contaminated the channel refine + implement read for reviewer
    feedback — agents saw the unreadable trace URL and asked the
    operator "what did the reviewer say?". Writing the same breadcrumb
    to ``TicketEvent.note`` instead keeps it visible to humans
    browsing the ticket (the drawer renders history-event notes as
    Markdown so the link stays clickable) without polluting the
    comment stream.

    No-op when *trace_id* is ``None`` or ``langfuse_trace_url`` can't
    build a URL (Langfuse unconfigured). Failures are logged at
    warning level and never propagate.
    """
    if trace_id is None:
        return
    repo_config = ctx.repo_config
    url = langfuse_trace_url(trace_id, repo_config=repo_config)
    if url is None:
        return
    note = f"🔍 [Trace: {stage_name}]({url})"
    try:
        ctx.service.add_history_note(ticket_id, note)
    except Exception:
        log.warning(
            "failed to post trace-link history event for %s (%s)",
            ticket_id,
            stage_name,
            exc_info=True,
        )


# DONE is NOT terminal — retrospect owns it (done -> closed). Only
# closed/errored/blocked stop the chain.
_TERMINAL = {State.CLOSED, State.ERRORED, State.BLOCKED}


async def process_ticket(
    ticket_id: str, ctx: StageContext, active_map: dict | None = None
) -> None:
    """Drive one ticket through as many stages as possible, in order,
    until it reaches a terminal/waiting state or a stub stops the chain."""
    await _process_ticket_inner(ticket_id, ctx, active_map=active_map)


async def _process_ticket_inner(
    ticket_id: str, ctx: StageContext, active_map: dict | None = None
) -> None:
    while True:
        ticket = ctx.service.get(ticket_id)
        if ticket is None:
            log.warning("ticket %s vanished", ticket_id)
            return
        if ticket.state in _TERMINAL:
            return
        # Paused mid-stage awaiting operator reply — do NOT dispatch to
        # any stage runner. The resume path (child 4) will re-enqueue
        # with the reply context once the human replies.
        if ticket.state == State.AWAITING_USER_REPLY:
            log.debug(
                "pausing %s — awaiting user reply (paused_from=%s)",
                ticket_id,
                getattr(ticket, "paused_from", None),
            )
            return
        # Retrying ticket still in backoff — don't open a trace or
        # run any stage; the poll loop re-enqueues later.
        if ticket.next_retry_at is not None and ticket.next_retry_at.replace(
            tzinfo=timezone.utc
        ) > datetime.now(timezone.utc):
            return
        stage_name = STAGE_FOR_STATE.get(ticket.state)
        if stage_name is None:
            log.debug("no stage for %s; pausing %s", ticket.state, ticket_id)
            return
        # Dependency gate at the top of the chain: a ticket waiting on
        # another ticket is not "running" — short-circuit BEFORE the
        # trace span is opened. Otherwise every reconcile sweep would
        # open a Langfuse "ticket" root span, the implement stage would
        # return same-state, and the span closes immediately — empty
        # trace per sweep, accumulating quickly. The wait is resumed
        # naturally by the next sweep once the dep terminates.
        if ctx.service.unmet_dependencies(ticket):
            log.debug(
                "%s: waiting on unmet dependencies — skipping (no trace)",
                ticket_id,
            )
            return
        stage = get_stage(stage_name)
        # Only trace stages that call the model. Poll-driven no-LLM
        # stages (merge, deliver) would otherwise emit an empty "ticket"
        # trace into the Langfuse session on every poll.
        traced = getattr(stage, "traced", True)
        trace_id = None
        try:
            with contextlib.ExitStack() as es:
                root_io = None
                if traced:
                    # One root span per stage call, named after the stage
                    # so Langfuse trace listings read "refine" / "implement"
                    # / "retrospect" instead of a generic "ticket". The
                    # session.id attribute still groups all of a ticket's
                    # stage traces together via Langfuse's session view.
                    root_io = es.enter_context(
                        tracing.start_ticket_root_span(
                            ticket_id, stage_name, repo_config=ctx.repo_config
                        )
                    )
                    # Attach a top-level "input" summary to the root span
                    # so Langfuse's trace view shows what was processed
                    # without drilling into children. Output is set
                    # below, once the stage returns.
                    root_io.set_input(
                        {
                            "ticket_id": ticket_id,
                            "title": ticket.title,
                            "state": ticket.state.value,
                            "stage": stage_name,
                            "source": ticket.source,
                            "priority": bool(getattr(ticket, "priority", False)),
                        }
                    )
                    trace_id = root_io.trace_id if root_io is not None else None
                # stage.run is sync (LLM/tool) — keep the loop responsive
                if active_map is not None:
                    active_map[ticket_id] = {
                        "stage": stage_name,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }
                timeout = ctx.settings.stage_timeout_overrides.get(
                    stage_name, ctx.settings.stage_timeout_seconds
                )
                coro = asyncio.to_thread(stage.run, ticket, ctx)
                try:
                    if timeout > 0:
                        outcome = await asyncio.wait_for(coro, timeout=timeout)
                    else:
                        outcome = await coro
                finally:
                    if active_map is not None:
                        active_map.pop(ticket_id, None)
                # Attach the outcome to the root span — visible at the
                # top of the trace in Langfuse alongside the input.
                if root_io is not None:
                    root_io.set_output(
                        {
                            "next_state": outcome.next_state.value
                            if outcome and outcome.next_state
                            else None,
                            "note": (outcome.note or "") if outcome else "",
                            "no_op": bool(
                                outcome and outcome.next_state == ticket.state
                            ),
                        }
                    )
        except asyncio.TimeoutError:
            timeout = ctx.settings.stage_timeout_overrides.get(
                stage_name, ctx.settings.stage_timeout_seconds
            )
            log.error(
                "%s: %s timed out after %ds — escalating to BLOCKED",
                stage_name,
                ticket_id,
                timeout,
            )
            _post_trace_event(ctx, ticket_id, trace_id, stage_name)
            note = f"stage {stage_name} timed out after {timeout}s"[:200]
            ctx.service.transition(ticket_id, State.BLOCKED, note=note)
            ticket = ctx.service.get(ticket_id)
            if ticket is not None:
                send_notification(ticket, State.BLOCKED, note, ctx.settings)
            return
        except NotImplementedError as e:
            log.warning(
                "%s: stub (%s) — chain paused at %s for %s",
                stage_name,
                e,
                ticket.state,
                ticket_id,
            )
            _post_trace_event(ctx, ticket_id, trace_id, stage_name)
            return
        except Exception as e:  # noqa: BLE001 — any failure fails the ticket
            log.exception("%s: %s failed", stage_name, ticket_id)
            from .transient_errors import classify_stage_error
            from .stage_retry import compute_retry_delay

            classification = classify_stage_error(e)
            if classification == "transient":
                ticket = ctx.service.get(ticket_id)
                if ticket is None:
                    return
                attempt = ticket.retry_attempt + 1
                max_attempts = ctx.settings.stage_retry_max_attempts
                if attempt <= max_attempts:
                    delay = compute_retry_delay(
                        attempt,
                        base=ctx.settings.stage_retry_base_delay,
                        cap=ctx.settings.stage_retry_max_delay,
                    )
                    next_at = datetime.now(timezone.utc).timestamp() + delay
                    next_at_dt = datetime.fromtimestamp(next_at, tz=timezone.utc)
                    ctx.service.set_retry_state(
                        ticket_id,
                        retry_attempt=attempt,
                        last_transient_error=repr(e)[:200],
                        next_retry_at=next_at_dt,
                    )
                    log.warning(
                        "%s: %s transient error (attempt %d/%d) — retry in %.0fs",
                        stage_name,
                        ticket_id,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    _post_trace_event(ctx, ticket_id, trace_id, stage_name)
                    return
                # Retries exhausted — block.
                _post_trace_event(ctx, ticket_id, trace_id, stage_name)
                note = (
                    f"Transient: {type(e).__name__} persisted after "
                    f"{max_attempts} attempts — last: {e}"
                )[:200]
                ctx.service.transition(ticket_id, State.BLOCKED, note=note)
                ticket = ctx.service.get(ticket_id)
                if ticket is not None:
                    send_notification(ticket, State.BLOCKED, note, ctx.settings)
            else:
                # FATAL — block immediately.
                _post_trace_event(ctx, ticket_id, trace_id, stage_name)
                note = f"Fatal: {type(e).__name__}: {e}"[:200]
                ctx.service.transition(ticket_id, State.BLOCKED, note=note)
                ticket = ctx.service.get(ticket_id)
                if ticket is not None:
                    send_notification(ticket, State.BLOCKED, note, ctx.settings)
            return
        # Stage finished without raising — any prior transient-retry
        # breadcrumbs are stale and must clear now, even when the outcome
        # is a no-op (poll stages like merge can succeed-but-wait forever,
        # leaving the chip stuck on the board).
        if ticket.retry_attempt > 0:
            ctx.service.set_retry_state(
                ticket_id,
                retry_attempt=0,
                last_transient_error=None,
                next_retry_at=None,
            )
        if outcome.next_state == ticket.state:
            # no-op (e.g. merge: PR still open) — leave it; the poll
            # re-enqueues later. No transition, no trace, no spam.
            _post_trace_event(ctx, ticket_id, trace_id, stage_name)
            log.debug(
                "%s: %s no-op at %s (awaiting external event)",
                stage_name,
                ticket_id,
                ticket.state,
            )
            return
        # Trace breadcrumb first, then the transition. Keeping the
        # transition as the last event preserves the simple "what
        # state am I in now?" read on ``history[-1]`` for downstream
        # callers (tests, retrospect, UI). The trace event records
        # the stage that produced the transition and sits at the
        # pre-transition state — semantically "work done while in
        # this state".
        _post_trace_event(ctx, ticket_id, trace_id, stage_name)
        # A stage tool may have already moved the ticket to outcome.next_state
        # (e.g. ask_user → AWAITING_USER_REPLY) before returning an Outcome
        # that repeats that state. Re-fetch and skip the redundant transition:
        # transitioning to the current state is a no-op the state machine
        # rejects, and the raised TransitionError used to crash this task.
        _fresh = ctx.service.get(ticket_id)
        if _fresh is not None and _fresh.state == outcome.next_state:
            log.info(
                "%s: %s already at %s (stage tool set it) — skipping transition",
                stage_name,
                ticket_id,
                outcome.next_state,
            )
        else:
            ctx.service.transition(ticket_id, outcome.next_state, outcome.note)
            log.info("%s: %s -> %s", stage_name, ticket_id, outcome.next_state)
        # Best-effort push notification for human-attention states.
        if outcome.next_state in _TRIGGER_STATES:
            ticket = ctx.service.get(ticket_id)
            if ticket is not None:
                send_notification(
                    ticket, outcome.next_state, outcome.note, ctx.settings
                )

        # After a ticket reaches a terminal state, re-evaluate its parent epic if any.
        if outcome.next_state in (State.DONE, State.CLOSED, State.ANSWERED):
            ticket = ctx.service.get(ticket_id)
            if ticket is not None and ticket.parent_id is not None:
                parent = ctx.service.get(ticket.parent_id)
                if parent is not None and parent.kind == "epic":
                    _spawn_epic_reeval(parent.id, ctx)


def _spawn_epic_reeval(epic_id: str, ctx: StageContext) -> None:
    """Fire-and-forget epic re-evaluation in a daemon thread.

    The daemon thread creates a fresh ``TicketService`` from
    ``ctx.settings``, calls the epic-status agent, and transitions
    the epic based on the agent's decision.  Failures are logged at
    warning level and never raised into the worker loop.
    """
    import threading

    t = threading.Thread(
        target=_run_epic_reeval, args=(epic_id, ctx.settings), daemon=True
    )
    t.start()


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
    from ..core.service import TicketService
    from ..agents.epic_status import run_epic_status_agent
    from ..runtime import tracing

    # Discover the epic's board via fanout, then bind the service to
    # it so subsequent transitions / writes go to the right per-repo DB.
    discovery = TicketService(settings)
    epic = discovery.get(epic_id)
    if epic is None:
        log.warning("epic %s vanished before re-evaluation", epic_id)
        return
    svc = TicketService(settings, board_id=epic.board_id)
    try:
        epic = svc.get(epic_id)
        if epic is None:
            log.warning("epic %s vanished before re-evaluation", epic_id)
            return
        if epic.state is State.EPIC_CLOSED:
            log.debug("epic %s: already EPIC_CLOSED — skipping re-evaluation", epic_id)
            return

        epic_desc = svc.workspace(epic).read_description()
        children = svc.list_children(epic_id)

        child_summaries: list[dict] = []
        for child in children:
            child_desc = svc.workspace(child).read_description()
            if len(child_desc) > 2000:
                child_desc = child_desc[:2000] + "\n...(truncated)"
            child_summaries.append(
                {
                    "id": child.id,
                    "title": child.title,
                    "state": child.state.value,
                    "description": child_desc,
                    "depends_on": TicketService._parse_depends_on(child),
                }
            )

        with tracing.start_ticket_root_span(epic_id, "epic-status"):
            result = run_epic_status_agent(
                settings=settings,
                epic_title=epic.title,
                epic_description=epic_desc,
                children=child_summaries,
            )

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
            log.info(
                "epic %s: agent decided close — transitioned to EPIC_CLOSED", epic_id
            )
        elif result.decision == "keep_open":
            log.debug("epic %s: agent decided keep_open — no change", epic_id)
        elif result.decision == "update_description":
            new_hash = svc.workspace(epic).write_description(result.note)
            svc.set_content_hash(epic_id, new_hash)
            log.info("epic %s: agent updated description", epic_id)
        elif result.decision == "update_deps":
            if result.dep_updates is not None:
                log.info(
                    "epic %s: agent requested dependency updates for %d children",
                    epic_id,
                    len(result.dep_updates),
                )
                for child_id, new_deps in result.dep_updates.items():
                    if new_deps is None:
                        new_deps = []
                    svc.set_depends_on(child_id, new_deps)
            if result.note:
                new_hash = svc.workspace(epic).write_description(result.note)
                svc.set_content_hash(epic_id, new_hash)
        # Apply child-ticket changes (new_children, child_rescopes, child_closures).
        _reconcile_child_changes(svc, epic_id, result)

    except Exception:
        log.exception("epic %s: re-evaluation failed", epic_id)


def _fetch_draft_child(svc, child_id: str, operation: str, epic_id: str):
    """Fetch a child ticket and verify it is in DRAFT state.

    Returns the child ticket if safe to mutate, or ``None`` if the
    child is missing or not in DRAFT (with a warning logged).
    """
    from ..core.states import State as S

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
    from ..core.states import State as S

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
                    kind="task",
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
        for child_id in result.child_closures:
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
                svc.transition(
                    child_id,
                    S.CLOSED,
                    note="Obsoleted by epic re-evaluation after sibling merge",
                )
                log.info(
                    "epic %s: closed child %s (obsoleted by sibling merge)",
                    epic_id,
                    child_id,
                )
            except Exception:
                log.exception(
                    "epic %s: failed to close child %s",
                    epic_id,
                    child_id,
                )


def _run_epic_reprocess(epic_id: str, comment_body: str, settings) -> None:
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
    from ..core.service import TicketService
    from ..agents.epic_breakdown import run_epic_breakdown_agent

    # Discover the epic's board via fanout, then bind the service to
    # it so subsequent writes go to the right per-repo DB.
    discovery = TicketService(settings)
    epic = discovery.get(epic_id)
    if epic is None:
        log.warning("epic %s vanished before re-processing", epic_id)
        return
    svc = TicketService(settings, board_id=epic.board_id)
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
        for title, body in zip(result.child_titles, result.child_bodies):
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

        created_ids: list[str] = []
        for title, body in zip(new_titles, new_bodies):
            child = svc.create(
                title=title,
                description=body,
                kind="task",
                parent_id=epic_id,
            )
            created_ids.append(child.id)

        # Build linear dependency chain: new children chained
        # together, appended after the last existing child.
        if existing:
            last_existing_id = existing[-1].id
            if created_ids:
                svc.set_depends_on(created_ids[0], [last_existing_id])
        for i in range(1, len(created_ids)):
            svc.set_depends_on(created_ids[i], [created_ids[i - 1]])

        log.info(
            "epic %s: re-processed — created %d new children: %s",
            epic_id,
            len(created_ids),
            ", ".join(created_ids),
        )
    except Exception:
        log.exception("epic %s: re-processing failed", epic_id)


class Worker:
    """In-process queue + consumer task, owned by the API service."""

    # Default queue key for tickets without a board_id (legacy /
    # repo-less tickets). Always present alongside the per-repo queues.
    _DEFAULT_BOARD = ""

    # Stage-rank by ticket state — used as a secondary sort key in the
    # PriorityQueue (after priority_rank, before FIFO seq). Lower = pops
    # first = closer to CLOSED. The intent is "drain in-flight tickets
    # through to terminal before starting fresh refines" — so the
    # board doesn't pile up with intermediate-state work. States not in
    # STAGE_FOR_STATE never reach the queue (maybe_enqueue gates on
    # state-in-pipeline) but we keep them in the table to avoid KeyError
    # on any drift. _DEFAULT_STAGE_RANK applies to unknown states.
    _DEFAULT_STAGE_RANK: int = 99
    _STAGE_RANK: dict = {
        State.DONE: 0,  # retrospect → CLOSED
        State.DELIVERABLE: 1,  # deliver opens the PR
        State.DOCUMENTING: 2,  # document → DELIVERABLE
        State.CODE_REVIEW: 3,  # review
        State.ADDRESSING_REVIEW: 4,  # merge stage replying to reviewer
        State.FIXING_CI: 5,  # ci_fix retries CI
        State.REBASING: 6,  # merge stage, rebase substep
        State.HUMAN_MR_APPROVAL: 7,  # merge polling (no-LLM)
        State.WAITING_AUTO_MERGE: 8,  # merge polling (no-LLM)
        State.IMPLEMENT_COMPLETE: 9,  # merge polling (no-LLM)
        State.READY: 10,  # implement — fresh code work
        State.DRAFT: 11,  # refine — earliest stage
        State.ASKED: 12,  # answer — inquiry side-channel
    }

    @classmethod
    def _stage_rank(cls, ticket) -> int:
        if ticket is None:
            return cls._DEFAULT_STAGE_RANK
        return cls._STAGE_RANK.get(ticket.state, cls._DEFAULT_STAGE_RANK)

    def __init__(
        self, ctx: StageContext, run_registry: "RunRegistry | None" = None
    ) -> None:
        self.ctx = ctx
        self.run_registry = run_registry
        # Per-repo (board_id) PriorityQueue topology — one queue per
        # repo, so a busy repo can't block another. Items in each queue:
        # (priority_rank, seq, ticket_id). priority_rank = 0 for
        # priority tickets, 1 otherwise → priority tickets pop first;
        # seq breaks ties as FIFO within a rank. The "" key holds the
        # fallback queue for tickets without a matching repo.
        self.queues: dict[str, asyncio.PriorityQueue] = {
            self._DEFAULT_BOARD: asyncio.PriorityQueue(),
        }
        self._enqueue_seq = 0
        # pool of consumer tasks — populated by start(), one or more
        # per repo per its max_concurrency.
        self._tasks: list[asyncio.Task] = []
        self._poll_task: asyncio.Task | None = None
        self._audit_task: asyncio.Task | None = None
        self._trace_health_task: asyncio.Task | None = None
        self._trace_review_task: asyncio.Task | None = None
        self._cost_warmer_task: asyncio.Task | None = None
        self._cost_warmer_fast_task: asyncio.Task | None = None
        # Periodic-pass runs currently executing in worker threads.
        # The to_thread wrapper registers them on entry and removes
        # on exit; ``stop()`` awaits this set (with a grace timeout)
        # before tearing the loops down so a survey / audit / etc.
        # mid-run isn't killed by a container restart.
        self._inflight_passes: set[asyncio.Task] = set()
        self._health_task: asyncio.Task | None = None
        self._agent_check_task: asyncio.Task | None = None
        self._bc_check_task: asyncio.Task | None = None
        self._completeness_check_task: asyncio.Task | None = None
        self._copy_paste_task: asyncio.Task | None = None
        self._module_curator_task: asyncio.Task | None = None
        self._ci_monitor_task: asyncio.Task | None = None
        self._test_gap_task: asyncio.Task | None = None
        self._survey_task: asyncio.Task | None = None
        self._config_sync_task: asyncio.Task | None = None
        self._cost_reconciliation_task: asyncio.Task | None = None
        self._langfuse_cleanup_task: asyncio.Task | None = None
        self._timeout_escalation_task: asyncio.Task | None = None
        self._meta_task: asyncio.Task | None = None
        # board_id -> per-repo bespoke supervisor task. The supervisor
        # itself owns each repo's per-bespoke child tasks; cancelling
        # the supervisor cancels its children.
        self._bespoke_supervisor_tasks: dict[str, asyncio.Task] = {}
        # ticket_id -> consecutive no-progress cycles in a traced stage
        self._stuck: dict[str, int] = {}
        # ids queued OR in-flight — dedupe so the same ticket is never
        # processed by two workers at once (the merge poll, emit, and
        # requeue can all enqueue the same id).
        self._pending: set[str] = set()
        # ticket_id -> {"stage": str, "started_at": str} while stage.run() is executing
        self._active: dict[str, dict] = {}

    def queue_size(self) -> int:
        """Aggregate ticket count across all per-repo queues."""
        return sum(q.qsize() for q in self.queues.values())

    async def queue_join(self) -> None:
        """Wait for every per-repo queue to drain."""
        import asyncio as _asyncio

        await _asyncio.gather(*(q.join() for q in self.queues.values()))

    def _queue_for(self, board_id: str) -> asyncio.PriorityQueue:
        """Return the per-repo queue, creating it on first use.

        Empty/unknown ``board_id`` falls through to the default queue
        (always present at ``self._DEFAULT_BOARD``).
        """
        if not board_id:
            return self.queues[self._DEFAULT_BOARD]
        q = self.queues.get(board_id)
        if q is None:
            q = asyncio.PriorityQueue()
            self.queues[board_id] = q
        return q

    def enqueue(self, ticket_id: str) -> None:
        """Enqueue *ticket_id* on its repo's queue with CURRENT priority
        AND stage rank.

        Queue items are ``(priority_rank, stage_rank, seq, ticket_id)``.
        Sort order: priority tickets (rank 0) first; within priority
        class, later-pipeline tickets (lower stage_rank) first; FIFO
        within a (priority, stage) pair. The stage tie-break drains
        in-flight tickets through to terminal before starting fresh
        refines — keeps the board from piling up with intermediate
        states.

        Priority AND stage are captured at enqueue time and the
        ``_pending`` set de-duplicates concurrent enqueues. If the
        ticket's priority or stage changes after enqueue, the pop-time
        sanity check in :meth:`_run` re-enqueues at the correct rank.
        Operators can also call :meth:`requeue_with_current_priority`
        to force a refresh explicitly.
        """
        # asyncio is single-threaded; enqueue is only ever called from
        # the loop thread, so this set check needs no lock.
        if ticket_id in self._pending:
            return
        self._pending.add(ticket_id)
        self._enqueue_seq += 1
        ticket = self.ctx.service.get(ticket_id)
        prio_rank = (
            0 if (ticket is not None and getattr(ticket, "priority", False)) else 1
        )
        stage_rank = self._stage_rank(ticket)
        board_id = (
            ticket.board_id
            if (ticket is not None and ticket.board_id)
            else self._DEFAULT_BOARD
        )
        self._queue_for(board_id).put_nowait(
            (prio_rank, stage_rank, self._enqueue_seq, ticket_id)
        )

    def requeue_with_current_priority(self, ticket_id: str) -> None:
        """Force a re-enqueue that picks up the ticket's current
        priority from the DB.

        Use this from callers that mutate ``Ticket.priority`` after the
        initial enqueue (the ``POST /tickets/{id}/priority`` route, any
        future stage transition that changes priority). Without it the
        stale enqueue entry stays at the OLD priority rank in the heap.

        The OLD heap entry is NOT removed (Python's ``PriorityQueue``
        doesn't support removal). When it eventually pops, the
        consumer's pop-time priority re-check (see ``_run``) either
        accepts it at the current priority or re-enqueues it again —
        so duplicates are tolerated, not double-processed.
        """
        self._pending.discard(ticket_id)
        self.enqueue(ticket_id)

    def _repo_config_for_ticket(self, ticket_id: str) -> RepoConfig | None:
        """Resolve the ``RepoConfig`` for *ticket_id* from its ``board_id``.

        Returns ``None`` when the ticket has no ``board_id`` or no
        matching repo is found.
        """
        try:
            from ..config import get_repos_config

            ticket = self.ctx.service.get(ticket_id)
            if ticket is None or not ticket.board_id:
                return None
            repos = get_repos_config()
            for rc in repos.repos.values():
                if rc.board_id == ticket.board_id:
                    return rc
            return None
        except Exception:
            return None

    async def _run(self, board_id: str = "") -> None:
        """Consume tickets from one repo's queue.

        Per-repo consumer: each repo gets ``repo.max_concurrency`` of
        these tasks pointed at its own queue, so a busy repo can't
        block another. ``board_id=""`` covers the fallback queue for
        tickets without a matching repo.
        """
        from ..core.service import TicketService

        queue = self._queue_for(board_id)
        # Per-queue service bound to this board's DB — the lifespan's
        # ctx.service is pinned to the first repo, so reads/writes via
        # it would hit the wrong DB for any non-first repo's tickets.
        board_service = (
            TicketService(self.ctx.settings, board_id=board_id)
            if board_id
            else self.ctx.service
        )
        while True:
            popped_prio, popped_stage, _seq, ticket_id = await queue.get()
            try:
                before = board_service.get(ticket_id)
                before_state = before.state if before else None

                # Pop-time sanity check: the entry's (priority, stage)
                # ranks were captured at enqueue time; if either has
                # since changed (priority flip, state transition while
                # queued) the popped entry's order is stale. Re-enqueue
                # at the correct ranks and skip — the fresher entry
                # handles the actual run.
                if before is not None:
                    cur_prio = 0 if getattr(before, "priority", False) else 1
                    cur_stage = self._stage_rank(before)
                    if (cur_prio, cur_stage) != (popped_prio, popped_stage):
                        log.debug(
                            "%s: popped (prio=%d, stage=%d) but current "
                            "(prio=%d, stage=%d); re-enqueuing at correct rank",
                            ticket_id,
                            popped_prio,
                            popped_stage,
                            cur_prio,
                            cur_stage,
                        )
                        # Drop from _pending so requeue isn't deduped away.
                        self._pending.discard(ticket_id)
                        self.enqueue(ticket_id)
                        queue.task_done()
                        continue

                # Resolve per-ticket repo_config from the ticket's board_id.
                ticket_repo_config = self._repo_config_for_ticket(ticket_id)
                per_ticket_ctx = StageContext(
                    settings=self.ctx.settings,
                    service=board_service,
                    repo_config=ticket_repo_config,
                )

                await process_ticket(ticket_id, per_ticket_ctx, active_map=self._active)
                after = board_service.get(ticket_id)
                self._check_progress(
                    ticket_id,
                    before_state,
                    after.state if after else None,
                    repo_config=ticket_repo_config,
                )
            except Exception:  # noqa: BLE001 — never let the consumer die
                log.exception("processing %s crashed", ticket_id)
            finally:
                # drop from in-flight FIRST so a re-enqueue (e.g. next
                # merge-poll cycle) is accepted again.
                self._pending.discard(ticket_id)
                self._active.pop(ticket_id, None)
                queue.task_done()

    def _check_progress(
        self, ticket_id: str, before, after, repo_config: RepoConfig | None = None
    ) -> None:
        """No-progress safety net. A ticket that keeps re-entering the
        same *model-driven* (traced) stage without ever advancing —
        runs interrupted before any checkpoint, or a churning stage —
        would otherwise be re-billed to the LLM on every requeue,
        silently. After ``max_stuck_cycles`` such cycles, escalate to
        BLOCKED (resumable) and notify. Poll stages (merge/deliver,
        traced=False) are exempt: human_mr_approval/rebasing legitimately waits
        on a PR or rebase cycle."""

        # --- retrying-ticket exemption: tickets in explicit backoff
        # must not be counted as stuck (they're waiting on an external
        # outage, not churning). ---
        ticket = self.ctx.service.get(ticket_id)
        if ticket is not None and (ticket.retry_attempt or 0) > 0:
            self._stuck.pop(ticket_id, None)
            return

        # --- dollar-cap safety net: check before the state-change
        # early-return so the cap fires even when the ticket is making
        # forward progress (cost accumulates across all stages). ---
        if self.ctx.settings.max_spend_usd_per_ticket > 0.0:
            cost = session_cost(self.ctx.settings, ticket_id, repo_config=repo_config)
            if cost > self.ctx.settings.max_spend_usd_per_ticket:
                note = (
                    f"Cost cap exceeded: ${cost:.2f} spent "
                    f"(limit ${self.ctx.settings.max_spend_usd_per_ticket:.2f}). "
                    "Escalated to BLOCKED to stop further LLM billing. "
                    "Use resume-blocked to override and continue."
                )
                log.error("%s: %s", ticket_id, note)
                self.ctx.service.transition(ticket_id, State.BLOCKED, note=note[:200])
                self._stuck.pop(ticket_id, None)
                t = self.ctx.service.get(ticket_id)
                if t is not None:
                    send_notification(t, State.BLOCKED, note[:200], self.ctx.settings)
                return

        if after is None or after != before:
            self._stuck.pop(ticket_id, None)
            return
        stage_name = STAGE_FOR_STATE.get(after)
        if stage_name is None:
            return  # terminal / human-wait — not our concern
        if not getattr(get_stage(stage_name), "traced", True):
            return  # poll stage: same-state is by design, never block
        # Dependency-gated ticket: implement.py returns Outcome(READY)
        # when ``unmet_dependencies`` is non-empty — that is the contract,
        # not a stuck state. The ticket is legitimately waiting for
        # another ticket to merge. Counting these toward stuck-cycles
        # would block ANY dependent ticket within ``max_stuck_cycles``
        # poll ticks of being approved, even though nothing is wrong.
        ticket = self.ctx.service.get(ticket_id)
        if ticket is not None and self.ctx.service.unmet_dependencies(ticket):
            self._stuck.pop(ticket_id, None)
            return
        n = self._stuck.get(ticket_id, 0) + 1
        self._stuck[ticket_id] = n
        if n < self.ctx.settings.max_stuck_cycles:
            return
        note = (
            f"no progress after {n} {stage_name} cycles in {after} — "
            "likely interrupted mid-run or non-terminating; escalated "
            "to BLOCKED to stop wasted LLM runs. Use resume-blocked "
            "to re-run this stage, or move to READY/DRAFT to retry "
            "the full chain."
        )
        log.error("%s: %s", ticket_id, note)
        self.ctx.service.transition(ticket_id, State.BLOCKED, note=note[:200])
        self._stuck.pop(ticket_id, None)
        t = self.ctx.service.get(ticket_id)
        if t is not None:
            send_notification(t, State.BLOCKED, note[:200], self.ctx.settings)

    async def _poll_loop(self) -> None:
        """Periodic reconcile sweep: re-enqueue EVERY non-terminal
        ticket that has an automated stage (STAGE_FOR_STATE) and isn't
        already in flight.

        Originally this only re-enqueued human_mr_approval/rebasing for the
        merge/rebase cycle. But drafts created out-of-band — by the
        audit runner, the retrospect stage, and the report_issue tool
        (they call service.create() directly, not the API endpoint that
        enqueues) — were NEVER picked up until a process restart ran
        requeue_unfinished(). The mill's whole self-improvement loop
        (audit/agent → draft → refine → …) silently stalled between
        restarts. This is periodic requeue_unfinished: idempotent
        (enqueue() dedupes via _pending), cheap (the process_ticket
        chain carries each ticket as far as it can in one pass), and
        robust to any current/future draft-creating path. States with
        no automated stage (e.g. human_issue_approval) are untouched —
        they correctly wait for a human."""
        from ..config import get_repos_config
        from ..core.service import TicketService

        interval = max(15, self.ctx.settings.merge_poll_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                # Fan out across every registered repo's DB — the
                # lifespan service is pinned to the lead repo, so
                # without this any non-lead-repo ticket that lands in
                # a poll-relevant state (READY after scope-triage
                # REJECT, HUMAN_MR_APPROVAL, REBASING, …) would never
                # be re-enqueued and would silently stall.
                boards: list[str] = []
                # Always sweep the worker's own context service first
                # — covers single-repo / test setups where the lifespan
                # service is the only one bound and get_repos_config()
                # may not return it.
                if self.ctx.service.board_id not in boards:
                    boards.append(self.ctx.service.board_id)
                try:
                    for rc in get_repos_config().repos.values():
                        if rc.board_id and rc.board_id not in boards:
                            boards.append(rc.board_id)
                except Exception:
                    pass
                for board_id in boards:
                    svc = (
                        self.ctx.service
                        if board_id == self.ctx.service.board_id
                        else TicketService(self.ctx.settings, board_id=board_id)
                    )
                    for t in svc.list():
                        if t.state == State.AWAITING_USER_REPLY:
                            log.debug(
                                "%s: skipping reconcile — awaiting user reply",
                                t.id,
                            )
                            continue
                        if t.state not in STAGE_FOR_STATE:
                            continue
                        # Dep-gated tickets are skipped at the source —
                        # enqueuing them would just trigger _process_ticket_inner
                        # to short-circuit (no trace, no work), but every sweep
                        # would still consume a queue slot + a service.get +
                        # an unmet check. Cheaper to filter here.
                        if svc.unmet_dependencies(t):
                            continue
                        self.enqueue(t.id)
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("reconcile sweep failed")

    def _initial_delay(self, kind: str, interval: int) -> float:
        """Return the seconds to sleep before the first periodic pass.

        Queries ``RunRegistry.most_recent(kind)`` to decide:
        - No registry → full ``interval`` (preserves current behaviour).
        - Never run (``None``) → 1.0 s.
        - Last run overdue (elapsed >= interval) → 1.0 s.
        - Otherwise → ``interval - elapsed`` (remaining time).
        """
        if self.run_registry is None:
            return float(interval)
        entry = self.run_registry.most_recent(kind)
        if entry is None:
            return 1.0
        try:
            from datetime import datetime, timezone

            last_ts = datetime.fromisoformat(entry["started_at"])
            elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
        except Exception:
            return 1.0
        if elapsed >= interval:
            return 1.0
        return interval - elapsed

    _PERIODIC_POLL_TICK_SECONDS = 60

    def _find_config_clone_dir(self, repo_config) -> Path | None:
        """Return any existing clone of *repo_config* usable for
        scheduler-time YAML lookup, or ``None``.

        The scheduler needs to read ``<clone>/.robotsix-mill/agents/
        <name>.yaml`` to honour per-repo periodic overrides, but the
        scheduler does not own a clone — it piggybacks on whichever
        worker clone exists already (bespoke supervisor, or any
        ``<agent>_workspace/repo`` left behind by an earlier run).

        Priority: bespoke_workspace > any *_workspace/repo. When no
        clone exists yet the loader falls back to the built-in YAML.
        """
        if repo_config is None:
            return None
        base = Path(self.ctx.settings.data_dir) / repo_config.repo_id
        if not base.is_dir():
            return None
        bespoke = base / "bespoke_workspace" / "repo"
        if (bespoke / ".git").exists():
            return bespoke
        try:
            for child in base.iterdir():
                if (
                    child.is_dir()
                    and child.name.endswith("_workspace")
                    and (child / "repo" / ".git").exists()
                ):
                    return child / "repo"
        except OSError:
            pass
        return None

    def _resolve_periodic_schedule(
        self,
        label: str,
        repo_config,
        settings_interval_attr: str,
        settings_enabled_attr: str | None = None,
    ) -> tuple[bool, int]:
        """Resolve ``(enabled, interval_seconds)`` for *label* on *repo_config*.

        Lookup order for each field:
          1. The agent YAML loaded via :func:`load_periodic_agent_definition`
             (clone-side override wins over built-in).
          2. The Settings field of the matching name as a fallback.

        Interval is clamped to >= 60s.
        """
        from ..agents.yaml_loader import load_periodic_agent_definition

        settings = self.ctx.settings
        yaml_name = label.replace("-", "_")
        repo_dir = self._find_config_clone_dir(repo_config)
        try:
            definition = load_periodic_agent_definition(yaml_name, repo_dir)
        except FileNotFoundError:
            definition = None

        # Interval: YAML > Settings.
        interval = None
        if definition and definition.interval_seconds is not None:
            interval = definition.interval_seconds
        else:
            interval = getattr(settings, settings_interval_attr, None)
        interval = max(60, int(interval or 86400))

        # Enabled: YAML > Settings.
        enabled = True
        if definition and definition.enabled is not None:
            enabled = bool(definition.enabled)
        elif settings_enabled_attr is not None:
            enabled = bool(getattr(settings, settings_enabled_attr, True))
        return enabled, interval

    async def _run_periodic_pass_per_repo(
        self,
        label: str,
        runner_fn,
        settings_interval_attr: str,
        per_repo_flag: str | None = None,
        settings_enabled_attr: str | None = None,
    ) -> None:
        """Shared per-repo periodic pass loop.

        Each tick (every :attr:`_PERIODIC_POLL_TICK_SECONDS`) the
        scheduler iterates registered repos and decides per-repo
        whether to fire — driven by the agent YAML's
        ``interval_seconds`` + ``enabled`` fields (with override at
        ``<clone>/.robotsix-mill/agents/<name>.yaml``) and the
        Settings field of the matching name as a fallback.

        Args:
            label: Pass identifier (``"audit"``, ``"agent_check"``).
                Also used as the YAML filename after hyphens →
                underscores: ``"copy-paste"`` → ``copy_paste.yaml``.
            runner_fn: Callable accepting ``session_id=`` and
                ``repo_config=`` keywords; returns a result with a
                ``drafts_created`` field.
            settings_interval_attr: Settings field name used as the
                interval fallback (e.g. ``"audit_interval_seconds"``).
            per_repo_flag: Name of the RepoConfig bool field that
                gates this agent for each repo (e.g.
                ``"audit_periodic"``). Repos whose flag is False are
                skipped entirely.
            settings_enabled_attr: Settings field name used as the
                ``enabled`` fallback (e.g. ``"audit_periodic"``).
                ``None`` → assume enabled.
        """
        last_run_by_board: dict[str, datetime] = {}
        # Unseeded boards default to epoch so the first tick fires
        # them as soon as the cadence sleep elapses (matching the
        # legacy ``_initial_delay``'s "fire ASAP when no prior run"
        # semantic). Boards with a registry entry are seeded from it.
        default_seed = datetime(1970, 1, 1, tzinfo=timezone.utc)

        # Seed last-run timestamps from the run registry so a restart
        # doesn't re-fire every repo's pass immediately.
        if self.run_registry is not None:
            for entry in self.run_registry.list_all():
                if entry.get("kind") != label or entry.get("status") != "ok":
                    continue
                board_id = entry.get("repo_id", "") or ""
                if board_id in last_run_by_board:
                    continue
                ts_iso = entry.get("finished_at") or entry.get("started_at")
                if not ts_iso:
                    continue
                try:
                    last_run_by_board[board_id] = datetime.fromisoformat(ts_iso)
                except ValueError:
                    continue

        first_tick = True
        while True:
            # Sleep before checking. The first tick uses a short
            # settling delay so an "overdue" repo fires within a
            # second of startup (matches the legacy ``_initial_delay``
            # behaviour for overdue passes). Subsequent ticks use the
            # poll cadence.
            await asyncio.sleep(1.0 if first_tick else self._PERIODIC_POLL_TICK_SECONDS)
            first_tick = False
            try:
                repos = get_repos_config()
                repo_configs = list(repos.repos.values())
                if not repo_configs:
                    # Single-repo / no repos.yaml: tick the default.
                    repo_configs = [None]  # type: ignore[list-item]
                if per_repo_flag:
                    repo_configs = [
                        rc
                        for rc in repo_configs
                        # Opt-in (9cc9): a registered repo runs this agent only
                        # if its per-repo flag is set. ``rc is None`` is the
                        # single-repo / no-repos.yaml mode, which has no
                        # per-repo config to opt in with, so it still ticks
                        # (governed by the Settings-level master switch).
                        if rc is None or getattr(rc, per_repo_flag, False)
                    ]

                for repo_config in repo_configs:
                    board_id = repo_config.repo_id if repo_config else ""
                    enabled, interval = self._resolve_periodic_schedule(
                        label,
                        repo_config,
                        settings_interval_attr,
                        settings_enabled_attr,
                    )
                    if not enabled:
                        continue

                    now = datetime.now(timezone.utc)
                    last = last_run_by_board.get(board_id, default_seed)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (now - last).total_seconds() < interval:
                        continue

                    await self._fire_periodic_pass(
                        label,
                        runner_fn,
                        repo_config,
                    )
                    last_run_by_board[board_id] = datetime.now(timezone.utc)
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("%s scheduler tick failed", label)

    async def _tracked_to_thread(self, fn, *args, **kwargs):
        """Run *fn* in the default thread pool, tracking the call so
        :meth:`stop` can wait for it before tearing the loop down.

        Difference vs ``asyncio.to_thread``: the underlying future is
        wrapped in a task that's registered in ``_inflight_passes`` and
        ``shield``-ed from cancellation. If the caller (a periodic
        loop) gets cancelled mid-run, the loop task still raises
        ``CancelledError`` immediately, but the thread keeps executing
        and ``stop()`` will await its completion (bounded by
        ``shutdown_grace_seconds``). Without this, a SIGTERM in the
        middle of a survey pass would kill the agent halfway through
        the run and lose the work.
        """
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        self._inflight_passes.add(task)
        task.add_done_callback(self._inflight_passes.discard)
        return await asyncio.shield(task)

    async def _fire_periodic_pass(
        self,
        label: str,
        runner_fn,
        repo_config,
    ) -> None:
        """Run one periodic pass for *repo_config* (or ``None``).

        Wraps the call with run-registry lifecycle + tracing root
        span, mirroring the previous in-line behaviour of
        ``_run_periodic_pass_per_repo``. Errors are logged but do not
        propagate — the caller's loop continues across other repos.
        """
        run_id = None
        repo_label = repo_config.repo_id if repo_config else label
        session_id = tracing.make_session_id(label)
        try:
            log.info(
                "Starting periodic %s pass for repo %s",
                label,
                repo_label,
            )
            if self.run_registry:
                run_id = self.run_registry.start(
                    label,
                    repo_id=repo_config.repo_id if repo_config else "",
                )
            with tracing.start_ticket_root_span(
                session_id,
                label,
                repo_config=repo_config,
            ):
                result = await self._tracked_to_thread(
                    runner_fn,
                    session_id=session_id,
                    repo_config=repo_config,
                )
            log.info(
                "%s pass (%s) completed, created %d draft(s)",
                label.capitalize(),
                repo_label,
                len(result.drafts_created),
            )
            if self.run_registry and run_id:
                runner_summary = (getattr(result, "summary", "") or "").strip()
                if runner_summary:
                    summary = runner_summary
                else:
                    draft_ids = [d["id"] for d in result.drafts_created[:5]]
                    summary = (
                        f"Created {len(result.drafts_created)} drafts: "
                        f"{', '.join(draft_ids)}"
                        f"{'…' if len(result.drafts_created) > 5 else ''}"
                    )
                self.run_registry.finish_ok(run_id, summary)
        except Exception as e:  # noqa: BLE001 — periodic must survive
            log.exception(
                "%s poll failed for repo %s",
                label,
                repo_label,
            )
            if self.run_registry and run_id:
                self.run_registry.finish_error(run_id, str(e))

    async def _run_periodic_pass(
        self,
        label: str,
        runner_fn,
        interval: int,
    ) -> None:
        """Shared periodic pass loop for audit, agent-check, etc.

        Args:
            label: Pass identifier (``"audit"``, ``"agent_check"``).
            runner_fn: Callable accepting ``session_id=`` keyword that
                       returns a result with a ``drafts_created`` field.
            interval: Seconds between passes.
        """
        initial = self._initial_delay(label, interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id(label)
            try:
                log.info("Starting periodic %s pass", label)
                if self.run_registry:
                    run_id = self.run_registry.start(label)
                # runner_fn invokes pydantic-ai's ``agent.run_sync``,
                # which calls ``asyncio.run()`` internally and explodes
                # ("this event loop is already running") when invoked
                # from inside an async task. Offload to a worker thread
                # — same pattern stage handlers use.
                with tracing.start_ticket_root_span(
                    session_id, label, repo_config=None
                ):
                    result = await self._tracked_to_thread(
                        runner_fn,
                        session_id=session_id,
                    )
                log.info(
                    "%s pass completed, created %d draft(s)",
                    label.capitalize(),
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    runner_summary = (getattr(result, "summary", "") or "").strip()
                    if runner_summary:
                        summary = runner_summary
                    else:
                        draft_ids = [d["id"] for d in result.drafts_created[:5]]
                        summary = (
                            f"Created {len(result.drafts_created)} drafts: "
                            f"{', '.join(draft_ids)}"
                            f"{'…' if len(result.drafts_created) > 5 else ''}"
                        )
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("%s poll failed", label)
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _meta_pass_loop(self) -> None:
        """Global meta-agent loop — fires once per interval (not per-repo).

        The meta-agent surveys ALL registered repo clones, identifies
        extraction and alignment opportunities, and files drafts to the
        meta board and per-repo boards respectively.
        """
        from robotsix_mill.meta_runner import MetaPassResult, run_meta_pass

        interval = max(60, self.ctx.settings.meta_interval_seconds)
        initial = self._initial_delay("meta", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id("meta")
            try:
                log.info("Starting periodic meta pass")
                if self.run_registry:
                    run_id = self.run_registry.start("meta")
                with tracing.start_ticket_root_span(
                    session_id, "meta", repo_config=None
                ):
                    result: MetaPassResult = await self._tracked_to_thread(
                        run_meta_pass,
                        session_id=session_id,
                    )
                total_drafts = len(result.extraction_drafts_created) + len(
                    result.alignment_drafts_created
                )
                log.info(
                    "Meta pass completed, created %d extraction + %d alignment = %d total draft(s)",
                    len(result.extraction_drafts_created),
                    len(result.alignment_drafts_created),
                    total_drafts,
                )
                if self.run_registry and run_id:
                    extraction_ids = [
                        d["id"] for d in result.extraction_drafts_created[:3]
                    ]
                    alignment_ids = [
                        d["id"] for d in result.alignment_drafts_created[:3]
                    ]
                    parts = []
                    if extraction_ids:
                        parts.append(f"Extraction: {', '.join(extraction_ids)}")
                    if alignment_ids:
                        parts.append(f"Alignment: {', '.join(alignment_ids)}")
                    summary = "; ".join(parts) if parts else "No drafts created"
                    if total_drafts > 6:
                        summary += " …"
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("Meta pass failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _trace_health_poll_loop(self) -> None:
        """Periodic trace-health check loop. Only runs when
        ``MILL_TRACE_HEALTH_PERIODIC=true``.

        Multi-repo: fans out across all registered repos whose
        ``RepoConfig.trace_health_periodic`` flag is True. When no repos
        are registered, runs once with ``repo_config=None``.
        """
        settings = self.ctx.settings
        interval = max(3600, settings.trace_health_interval_seconds)
        initial = self._initial_delay("trace-health", interval)
        await asyncio.sleep(initial)
        while True:
            repos = get_repos_config()
            repo_configs = list(repos.repos.values())
            if not repo_configs:
                repo_configs = [None]  # type: ignore[list-item]
            else:
                repo_configs = [
                    rc
                    for rc in repo_configs
                    if getattr(rc, "trace_health_periodic", True)
                ]
            for repo_config in repo_configs:
                repo_label = repo_config.repo_id if repo_config else "default"
                try:
                    log.info(
                        "Starting periodic trace-health check for repo %s",
                        repo_label,
                    )
                    from ..trace_health_runner import run_trace_health_check

                    run_id = None
                    if self.run_registry:
                        run_id = self.run_registry.start(
                            "trace-health",
                            repo_id=repo_config.repo_id if repo_config else "",
                        )
                    result = await asyncio.to_thread(
                        run_trace_health_check,
                        repo_config=repo_config,
                    )
                    if result.draft_created:
                        log.info(
                            "Trace-health check (%s): draft created — "
                            "%d/%d traces unsessioned",
                            repo_label,
                            result.unsessioned_count,
                            result.total_traces,
                        )
                    else:
                        log.info(
                            "Trace-health check (%s): no alert "
                            "(%d/%d traces unsessioned)",
                            repo_label,
                            result.unsessioned_count,
                            result.total_traces,
                        )
                    if self.run_registry and run_id:
                        summary = (
                            f"{result.unsessioned_count}/{result.total_traces} "
                            f"traces unsessioned ({result.window_start} to "
                            f"{result.window_end}) — "
                            f"{'draft created' if result.draft_created else 'no alert'}"
                        )
                        self.run_registry.finish_ok(run_id, summary)
                except Exception as e:  # noqa: BLE001 — never let the poll die
                    log.exception(
                        "trace-health poll failed for repo %s",
                        repo_label,
                    )
                    if self.run_registry and run_id:
                        self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _cost_warmer_loop(self) -> None:
        """Background cost-warmer.

        Walks every non-closed ticket on each repo and calls
        ``session_cost`` to refresh its cached Langfuse value. The
        board's /tickets list endpoint reads from the SAME process-
        local cache via ``session_cost_cached`` (cache-only, never
        blocks), so once the warmer has visited a ticket its cost
        column on the board is populated even when the operator
        hasn't opened the ticket drawer.

        Throttling: ``cost_warmer_concurrency`` bounds in-flight
        Langfuse calls (the semaphore replaces the older serial
        ``cost_warmer_pace_ms``); ``cost_warmer_interval_seconds``
        is the wall-time between cycles. With concurrency=4 and a
        ~250ms median Langfuse latency, ~200 open tickets sweep in
        ~12s, so the column refreshes well inside the 60s cache TTL
        and idle tickets never show a $0 dip.

        Closed tickets are skipped entirely — their cost is final
        and the board hides them by default. Each cycle handles
        per-repo failure independently so a Langfuse outage on one
        repo doesn't stall the others.
        """
        from ..core.models import Ticket
        from ..core.service import TicketService
        from ..core.states import State
        from ..langfuse_client import session_cost

        settings = self.ctx.settings
        interval = max(10, settings.cost_warmer_interval_seconds)
        concurrency = max(1, settings.cost_warmer_concurrency)
        terminal = {State.CLOSED, State.EPIC_CLOSED}

        await asyncio.sleep(self._initial_delay("cost-warmer", interval))
        while True:
            cycle_start = time.monotonic()
            repos = get_repos_config()
            repo_configs = [
                rc
                for rc in repos.repos.values()
                if getattr(rc, "cost_warmer_periodic", True)
            ]
            sem = asyncio.Semaphore(concurrency)

            async def _warm_one(ticket: Ticket, repo_config) -> int:
                async with sem:
                    try:
                        await asyncio.to_thread(
                            session_cost,
                            settings,
                            ticket.id,
                            repo_config=repo_config,
                        )
                        return 1
                    except Exception:  # noqa: BLE001
                        log.debug(
                            "cost-warmer: lookup failed for %s",
                            ticket.id,
                            exc_info=True,
                        )
                        return 0

            tasks: list[asyncio.Future] = []
            for repo_config in repo_configs:
                try:
                    svc = TicketService(settings, board_id=repo_config.board_id)
                    tickets: list[Ticket] = svc.list()
                except Exception:  # noqa: BLE001 — survive per-repo errors
                    log.exception(
                        "cost-warmer: listing tickets failed for %s",
                        repo_config.repo_id,
                    )
                    continue

                for ticket in tickets:
                    # Closed tickets never accrue new cost — skip them
                    # entirely. Their cached value (if any) persists,
                    # and the detail-drawer click still does a blocking
                    # fetch for authoritativeness if the operator
                    # opens one.
                    if ticket.state in terminal:
                        continue
                    tasks.append(_warm_one(ticket, repo_config))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            warmed_count = sum(r for r in results if isinstance(r, int))

            cycle_secs = time.monotonic() - cycle_start
            log.debug(
                "cost-warmer cycle: %d tickets warmed in %.1fs",
                warmed_count,
                cycle_secs,
            )
            # Sleep the remainder of the interval if we finished early.
            await asyncio.sleep(max(0.0, interval - cycle_secs))

    async def _cost_warmer_fast_loop(self) -> None:
        """Fast cost-warmer: walks only active-state tickets.

        The slow warmer is comprehensive but takes ~90s+ to cycle on a
        busy board. For tickets that are currently being processed
        (refine, implement, review, …) the operator notices the
        $-amount lagging long before that — the spending climbs while
        the column shows yesterday's value. This loop hits Langfuse for
        every active-state ticket every few seconds with ``force=True``
        so the TTL gate doesn't deflect the call. Throttled by
        interval, not by pace, since active tickets are typically <10
        at a time.

        Active states are read from ``STAGE_FOR_STATE`` so the loop
        automatically tracks any new pipeline stages added.
        """
        from ..core.service import TicketService
        from ..core.states import STAGE_FOR_STATE
        from ..langfuse_client import session_cost

        settings = self.ctx.settings
        interval = max(2, settings.cost_warmer_fast_interval_seconds)
        active_states = set(STAGE_FOR_STATE.keys())

        await asyncio.sleep(self._initial_delay("cost-warmer-fast", interval))
        while True:
            cycle_start = time.monotonic()
            repos = get_repos_config()
            warmed = 0
            for repo_config in repos.repos.values():
                if not getattr(repo_config, "cost_warmer_periodic", True):
                    continue
                try:
                    svc = TicketService(settings, board_id=repo_config.board_id)
                    tickets = svc.list()
                except Exception:  # noqa: BLE001
                    log.exception(
                        "cost-warmer-fast: listing tickets failed for %s",
                        repo_config.repo_id,
                    )
                    continue
                for ticket in tickets:
                    if ticket.state not in active_states:
                        continue
                    try:
                        await asyncio.to_thread(
                            session_cost,
                            settings,
                            ticket.id,
                            repo_config=repo_config,
                            force=True,
                        )
                        warmed += 1
                    except Exception:  # noqa: BLE001
                        log.debug(
                            "cost-warmer-fast: lookup failed for %s",
                            ticket.id,
                            exc_info=True,
                        )

            cycle_secs = time.monotonic() - cycle_start
            log.debug(
                "cost-warmer-fast cycle: %d active tickets warmed in %.1fs",
                warmed,
                cycle_secs,
            )
            await asyncio.sleep(max(0.0, interval - cycle_secs))

    async def _bespoke_supervisor(self, repo_config: RepoConfig) -> None:
        """Per-repo bespoke-agent supervisor loop.

        Owns a clone of the managed repo at
        ``<data_dir>/<board_id>/bespoke_workspace/repo`` and reconciles
        the set of running per-bespoke loop tasks against the YAMLs
        present under ``<clone>/.robotsix-mill/agents/`` on each cycle:

        - new YAML appears   -> spawn a periodic loop on its
                                ``interval_seconds``
        - YAML disappears    -> cancel the loop
        - YAML body changed  -> cancel + respawn so the new prompt /
                                model / interval / web flag take effect
                                without operator intervention

        Cancelling the supervisor also cancels every child loop it
        spawned — that's the worker.stop() contract.
        """
        from ..agents.bespoke_loader import (
            BespokeAgentDefinition,
            load_bespoke_definitions,
        )
        from ..audit_runner import _clone_token
        from ..vcs import git_ops

        settings = self.ctx.settings
        interval = max(60, settings.bespoke_discovery_interval_seconds)
        board_id = repo_config.board_id
        forge_url = repo_config.forge_remote_url or settings.forge_remote_url
        clone_dir = (
            settings.data_dir / repo_config.repo_id / "bespoke_workspace" / "repo"
        )

        # name -> (task, definition)
        running: dict[str, tuple[asyncio.Task, BespokeAgentDefinition]] = {}

        def _cancel_running() -> None:
            for task, _ in running.values():
                task.cancel()
            running.clear()

        try:
            # Skip the random initial delay: spawning bespoke tasks
            # immediately after worker start makes the system feel
            # responsive when an operator commits a new YAML.
            while True:
                try:
                    if forge_url and not (clone_dir / ".git").exists():
                        try:
                            clone_dir.parent.mkdir(
                                parents=True,
                                exist_ok=True,
                            )
                            git_ops.clone(
                                forge_url,
                                clone_dir,
                                settings.forge_target_branch,
                                _clone_token(settings, repo_config),
                            )
                        except Exception:  # noqa: BLE001 — supervisor must survive
                            log.exception(
                                "bespoke supervisor (%s): clone failed",
                                board_id,
                            )
                    elif forge_url and (clone_dir / ".git").exists():
                        try:
                            git_ops.fetch(
                                clone_dir,
                                remote_url=forge_url,
                                token=_clone_token(settings, repo_config),
                                branch=settings.forge_target_branch,
                            )
                            # Hard-reset to the remote so newly committed
                            # .robotsix-mill/ YAMLs land immediately.
                            import subprocess

                            subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(clone_dir),
                                    "reset",
                                    "--hard",
                                    f"origin/{settings.forge_target_branch}",
                                ],
                                check=False,
                                capture_output=True,
                            )
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "bespoke supervisor (%s): refresh failed",
                                board_id,
                            )

                    definitions = {
                        d.name: d for d in load_bespoke_definitions(clone_dir)
                    }

                    # Drop tasks whose YAML disappeared.
                    for name in list(running):
                        if name not in definitions:
                            task, _ = running.pop(name)
                            task.cancel()
                            log.info(
                                "bespoke %s/%s: YAML removed — cancelled",
                                board_id,
                                name,
                            )

                    # Spawn / respawn tasks for current YAMLs.
                    for name, defn in definitions.items():
                        existing = running.get(name)
                        if existing is not None and existing[1] == defn:
                            continue  # unchanged
                        if existing is not None:
                            existing[0].cancel()
                            log.info(
                                "bespoke %s/%s: YAML changed — respawning",
                                board_id,
                                name,
                            )
                        task = asyncio.create_task(
                            self._run_bespoke_loop(
                                repo_config,
                                defn,
                                clone_dir,
                            )
                        )
                        running[name] = (task, defn)
                        log.info(
                            "bespoke %s/%s: scheduled (interval=%ds)",
                            board_id,
                            name,
                            defn.interval_seconds,
                        )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "bespoke supervisor (%s) cycle failed",
                        board_id,
                    )

                await asyncio.sleep(interval)
        finally:
            # Supervisor cancelled (worker.stop() or unexpected) ->
            # tear down every child loop so nothing keeps running.
            _cancel_running()

    async def _run_bespoke_loop(
        self,
        repo_config: RepoConfig,
        definition,
        clone_dir,
    ) -> None:
        """Periodic loop for one bespoke definition.

        Sleeps the YAML's ``interval_seconds`` between passes, then
        invokes :func:`~..bespoke_runner.run_bespoke_pass` against the
        supervisor's clone. Failures in one pass log + continue; the
        loop only exits via cancellation.
        """
        from .. import bespoke_runner
        from .. import tracing

        interval = max(60, definition.interval_seconds)
        label = f"bespoke:{definition.name}"
        # Honour the persisted last-run timestamp so a restarted mill
        # doesn't re-fire every bespoke immediately.
        initial = self._initial_delay(label, interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id(label)
            try:
                log.info(
                    "Starting bespoke pass %r for repo %s",
                    definition.name,
                    repo_config.repo_id,
                )
                if self.run_registry:
                    run_id = self.run_registry.start(
                        label,
                        repo_id=repo_config.repo_id,
                    )
                with tracing.start_ticket_root_span(
                    session_id,
                    label,
                    repo_config=repo_config,
                ):
                    result = await asyncio.to_thread(
                        bespoke_runner.run_bespoke_pass,
                        session_id=session_id,
                        definition=definition,
                        repo_config=repo_config,
                        repo_dir=clone_dir,
                    )
                log.info(
                    "Bespoke %s/%s completed, created %d draft(s)",
                    repo_config.repo_id,
                    definition.name,
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    summary = f"Created {len(result.drafts_created)} drafts"
                    self.run_registry.finish_ok(run_id, summary)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — loop must survive
                log.exception(
                    "bespoke %s/%s pass failed",
                    repo_config.repo_id,
                    definition.name,
                )
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _langfuse_cleanup_poll_loop(self) -> None:
        """Periodic Langfuse trace cleanup: keeps each repo's project at
        most ``langfuse_cleanup_max_traces`` rows by deleting the oldest.

        Multi-repo: iterates all registered repos whose
        ``RepoConfig.langfuse_cleanup_periodic`` flag is True. Pure
        HTTP, no LLM — the cap exists because the self-hosted Langfuse
        instance degrades on large trace tables.
        """
        settings = self.ctx.settings
        interval = max(3600, settings.langfuse_cleanup_interval_seconds)
        initial = self._initial_delay("langfuse-cleanup", interval)
        await asyncio.sleep(initial)
        while True:
            repos = get_repos_config()
            repo_configs = list(repos.repos.values())
            if not repo_configs:
                repo_configs = [None]  # type: ignore[list-item]
            else:
                repo_configs = [
                    rc
                    for rc in repo_configs
                    if getattr(rc, "langfuse_cleanup_periodic", True)
                ]
            for repo_config in repo_configs:
                label = repo_config.repo_id if repo_config else "default"
                try:
                    from ..langfuse_cleanup_runner import run_langfuse_cleanup_pass

                    result = await asyncio.to_thread(
                        run_langfuse_cleanup_pass,
                        settings=settings,
                        repo_config=repo_config,
                        max_traces=settings.langfuse_cleanup_max_traces,
                    )
                    if result.traces_deleted > 0:
                        log.info(
                            "langfuse-cleanup: %s — deleted %d of %d traces (cap %d)",
                            label,
                            result.traces_deleted,
                            result.traces_before,
                            settings.langfuse_cleanup_max_traces,
                        )
                except Exception:  # noqa: BLE001 — periodic sweep must not die
                    log.exception("langfuse-cleanup poll failed for %s", label)
            await asyncio.sleep(interval)

    async def _timeout_escalation_poll_loop(self) -> None:
        """Periodic timeout-escalation: detects AWAITING_USER_REPLY tickets
        stuck beyond the threshold and escalates them to BLOCKED.

        Pure DB query + state transition — no AI agent, no Langfuse tracing.
        Global pass (non-per-repo): AWAITING_USER_REPLY tickets are
        board-agnostic.
        """
        settings = self.ctx.settings
        interval = max(60, settings.timeout_escalation_interval_seconds)
        initial = self._initial_delay("timeout-escalation", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ..timeout_escalation_runner import run_timeout_escalation

                result = await asyncio.to_thread(
                    run_timeout_escalation,
                    settings,
                )
                log.info(
                    "timeout-escalation: pass complete — escalated=%d skipped=%d",
                    result.get("escaped", 0),
                    result.get("skipped", 0),
                )
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("timeout-escalation poll failed")
            await asyncio.sleep(interval)

    async def _ci_monitor_poll_loop(self) -> None:
        """Periodic CI monitor poll: watch the forge target branch for
        completed workflow-run failures and file a ``source="ci"`` draft
        for each new one.

        Per-repo enabled/interval are controlled via ``RepoConfig``
        fields in ``config/repos.yaml``.  The loop runs when *any*
        registered repo has ``ci_monitor_enabled=True``.
        """
        from ..core.service import TicketService
        from ..forge import get_forge

        settings = self.ctx.settings
        ttl_seconds = 30 * 86400  # 30 days

        # ANSI strip for log text (same pattern as forge/github.py).
        _ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Per-repo tracking: last polled timestamp (epoch seconds).
        last_polled: dict[str, float] = {}

        # Determine the minimum interval across all enabled repos so
        # the loop ticks frequently enough to honour the fastest one,
        # but only poll each repo when its own interval has elapsed.
        repos = get_repos_config()
        repo_configs = [rc for rc in repos.repos.values() if rc.ci_monitor_enabled]

        min_interval = 60
        if repo_configs:
            min_interval = max(
                60, min(rc.ci_monitor_interval_seconds for rc in repo_configs)
            )

        await asyncio.sleep(self._initial_delay("ci_monitor", min_interval))
        while True:
            for rc in repo_configs:
                repo_label = rc.repo_id
                interval = max(60, rc.ci_monitor_interval_seconds)

                # Honour per-repo interval.
                now = time.time()
                if (
                    repo_label in last_polled
                    and (now - last_polled[repo_label]) < interval
                ):
                    continue

                try:
                    state_dir = settings.data_dir / rc.repo_id
                    service = TicketService(settings, board_id=rc.board_id)

                    state_dir.mkdir(parents=True, exist_ok=True)
                    state_path = state_dir / "ci_monitor_state.json"
                    log.info("CI monitor poll starting for repo %s", repo_label)

                    # 1. Load dedup state.
                    state: dict = {"seen": {}}
                    if state_path.exists():
                        try:
                            state = json.loads(state_path.read_text("utf-8"))
                        except json.JSONDecodeError, OSError:
                            state = {"seen": {}}
                    seen = state.setdefault("seen", {})

                    # 2. Prune entries older than TTL.
                    stale = [
                        key
                        for key, val in seen.items()
                        if isinstance(val, (int, float)) and (now - val) > ttl_seconds
                    ]
                    for key in stale:
                        del seen[key]

                    # 3. List completed workflow runs on the target branch.
                    forge = get_forge(settings, repo_config=rc)
                    runs = forge.list_workflow_runs(
                        branch=settings.forge_target_branch,
                    )

                    # 4. Only the LATEST run per workflow reflects current
                    # state (the GitHub API returns runs newest-first). Take
                    # one run per workflow_id and act only on that — never
                    # backfill every historical failed run.
                    latest_by_wf: dict = {}
                    for run in runs:
                        wf = run.get("workflow_id")
                        if wf is not None and wf not in latest_by_wf:
                            latest_by_wf[wf] = run

                    existing = service.list()

                    for wf, run in latest_by_wf.items():
                        if run.get("conclusion") != "failure":
                            continue

                        wf_name = run.get("name", "unknown")
                        run_id_val = run.get("id")
                        title = (
                            f"CI failure: {wf_name} on {settings.forge_target_branch}"
                        )

                        # One OPEN ci ticket per workflow: if a non-terminal
                        # source=ci ticket with this title already exists,
                        # don't duplicate.
                        if any(
                            t.source == SourceKind.CI
                            and t.title == title
                            and t.state.value not in ("closed", "done")
                            for t in existing
                        ):
                            continue

                        # Also avoid re-filing for the exact same failing
                        # commit.
                        key = f"{wf}:{run.get('head_sha')}"
                        if key in seen:
                            continue
                        log.info(
                            "CI monitor (%s): new failure — %s (run %s) on %s",
                            repo_label,
                            wf_name,
                            run_id_val,
                            settings.forge_target_branch,
                        )

                        # Fetch job logs.
                        logs = ""
                        try:
                            logs = forge.fetch_workflow_job_logs(run_id=run_id_val)
                        except Exception:
                            log.warning(
                                "CI monitor: failed to fetch logs for run %s",
                                run_id_val,
                            )

                        # Build draft body.
                        body_parts = [
                            f"**Workflow:** {wf_name}",
                            f"**Branch:** {settings.forge_target_branch}",
                            f"**Run:** [{run_id_val}]({run.get('html_url', '')})",
                            f"**Commit:** `{run.get('head_sha', '')}`",
                            f"**Created:** {run.get('created_at', '')}",
                            "",
                        ]
                        if logs:
                            stripped = _ansi_re.sub("", logs)
                            if len(stripped) > 200_000:
                                stripped = stripped[-200_000:]
                            body_parts.append("```")
                            body_parts.append(stripped)
                            body_parts.append("```")

                        body = "\n".join(body_parts)

                        try:
                            service.create(
                                title=title,
                                description=body,
                                source=SourceKind.CI,
                                priority=True,
                            )
                        except Exception:
                            log.exception(
                                "CI monitor: failed to create draft for run %s",
                                run_id_val,
                            )
                            continue

                        # Mark as seen.
                        seen[key] = now

                    # 5. Persist state.
                    state_path.write_text(json.dumps(state), "utf-8")

                    log.info("CI monitor poll completed for repo %s", repo_label)
                except Exception:  # noqa: BLE001 — never let the poll die
                    log.exception("CI monitor poll failed for repo %s", repo_label)

                last_polled[repo_label] = time.time()

            await asyncio.sleep(min_interval)

    # ------------------------------------------------------------------
    # Periodic-pass start helpers
    # ------------------------------------------------------------------

    def _start_periodic_pass(
        self,
        label: str,
        import_path: str,
        settings_interval_attr: str,
        per_repo_flag: str,
        task_attr: str,
        settings_enabled_attr: str | None = None,
    ) -> None:
        """Start a per-repo periodic pass if its settings flag is on and
        the corresponding task attribute is still ``None``.

        *import_path* is a ``"module.path:attr_name"`` string resolved
        lazily via ``importlib`` so monkeypatching in tests still works.
        """
        import importlib

        if settings_enabled_attr is None:
            settings_enabled_attr = per_repo_flag
        if (
            getattr(self.ctx.settings, settings_enabled_attr)
            and getattr(self, task_attr) is None
        ):
            mod_path, attr_name = import_path.rsplit(":", 1)
            mod = importlib.import_module(mod_path)
            runner_fn = getattr(mod, attr_name)
            setattr(
                self,
                task_attr,
                asyncio.create_task(
                    self._run_periodic_pass_per_repo(
                        label,
                        runner_fn,
                        settings_interval_attr=settings_interval_attr,
                        settings_enabled_attr=settings_enabled_attr,
                        per_repo_flag=per_repo_flag,
                    )
                ),
            )
            log.info("Periodic %s enabled (per-repo schedule)", label.replace("_", "-"))

    def _start_poll_loop_pass(
        self,
        label: str,
        poll_loop_fn,
        task_attr: str,
        log_msg: str | None = None,
        log_args: tuple = (),
    ) -> None:
        """Start a dedicated poll-loop periodic pass if its settings flag
        (derived from *label*) is on and the task attribute is still ``None``.

        ``poll_loop_fn`` is a zero-argument async callable (typically a
        bound method like ``self._trace_health_poll_loop``).
        """
        flag = label.replace("-", "_") + "_periodic"
        if getattr(self.ctx.settings, flag) and getattr(self, task_attr) is None:
            setattr(
                self,
                task_attr,
                asyncio.create_task(poll_loop_fn()),
            )
            if log_msg is not None:
                log.info(log_msg, *log_args)

    def start(self) -> None:
        if not self._tasks:
            repos = get_repos_config()
            pool_sizes = [
                (rc.board_id, max(1, rc.max_concurrency)) for rc in repos.repos.values()
            ]
            pool_sizes.append((self._DEFAULT_BOARD, 1))
            for board_id, n in pool_sizes:
                for _ in range(n):
                    self._tasks.append(asyncio.create_task(self._run(board_id)))
            log.info(
                "worker pool started: %s",
                ", ".join(f"{bid or '<default>'}={n}" for bid, n in pool_sizes),
            )
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

        # --- Pattern A: per-repo periodic passes ---
        self._start_periodic_pass(
            "audit",
            "robotsix_mill.audit_runner:run_audit_pass",
            "audit_interval_seconds",
            "audit_periodic",
            "_audit_task",
        )
        self._start_periodic_pass(
            "health",
            "robotsix_mill.health_runner:run_health_pass",
            "health_interval_seconds",
            "health_periodic",
            "_health_task",
        )
        self._start_periodic_pass(
            "agent_check",
            "robotsix_mill.agent_check_runner:run_agent_check_pass",
            "agent_check_interval_seconds",
            "agent_check_periodic",
            "_agent_check_task",
        )
        self._start_periodic_pass(
            "bc_check",
            "robotsix_mill.bc_check_runner:run_bc_check_pass",
            "bc_check_interval_seconds",
            "bc_check_periodic",
            "_bc_check_task",
        )
        self._start_periodic_pass(
            "trace_review",
            "robotsix_mill.trace_review_runner:run_trace_review_pass",
            "trace_review_interval_seconds",
            "trace_review_periodic",
            "_trace_review_task",
        )
        self._start_periodic_pass(
            "completeness_check",
            "robotsix_mill.completeness_check_runner:run_completeness_check_pass",
            "completeness_check_interval_seconds",
            "completeness_check_periodic",
            "_completeness_check_task",
        )
        self._start_periodic_pass(
            "copy-paste",
            "robotsix_mill.copy_paste_runner:run_copy_paste_pass",
            "copy_paste_interval_seconds",
            "copy_paste_periodic",
            "_copy_paste_task",
        )
        self._start_periodic_pass(
            "module_curator",
            "robotsix_mill.module_curator_runner:run_module_curator_pass",
            "module_curator_interval_seconds",
            "module_curator_periodic",
            "_module_curator_task",
        )
        self._start_periodic_pass(
            "test-gap",
            "robotsix_mill.test_gap_runner:run_test_gap_pass",
            "test_gap_interval_seconds",
            "test_gap_periodic",
            "_test_gap_task",
        )
        self._start_periodic_pass(
            "survey",
            "robotsix_mill.survey_runner:run_survey_pass",
            "survey_interval_seconds",
            "survey_periodic",
            "_survey_task",
        )
        self._start_periodic_pass(
            "config-sync",
            "robotsix_mill.config_sync_runner:run_config_sync_pass",
            "config_sync_interval_seconds",
            "config_sync_periodic",
            "_config_sync_task",
        )
        self._start_periodic_pass(
            "cost-reconciliation",
            "robotsix_mill.cost_reconciliation_runner:run_cost_reconciliation_pass",
            "cost_reconciliation_interval_seconds",
            "cost_reconciliation_periodic",
            "_cost_reconciliation_task",
        )

        # --- Pattern B: dedicated poll-loop tasks ---
        self._start_poll_loop_pass(
            "trace-health",
            self._trace_health_poll_loop,
            "_trace_health_task",
            log_msg="Periodic trace-health enabled: interval %ds",
            log_args=(self.ctx.settings.trace_health_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "cost-warmer",
            self._cost_warmer_loop,
            "_cost_warmer_task",
            log_msg="Cost warmer enabled: cycle %ds, concurrency=%d",
            log_args=(
                self.ctx.settings.cost_warmer_interval_seconds,
                self.ctx.settings.cost_warmer_concurrency,
            ),
        )
        self._start_poll_loop_pass(
            "cost-warmer-fast",
            self._cost_warmer_fast_loop,
            "_cost_warmer_fast_task",
            log_msg="Cost warmer (fast, active tickets) enabled: interval %ds",
            log_args=(self.ctx.settings.cost_warmer_fast_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "langfuse-cleanup",
            self._langfuse_cleanup_poll_loop,
            "_langfuse_cleanup_task",
            log_msg="Periodic Langfuse cleanup enabled: interval %ds, cap %d traces/project",
            log_args=(
                self.ctx.settings.langfuse_cleanup_interval_seconds,
                self.ctx.settings.langfuse_cleanup_max_traces,
            ),
        )
        self._start_poll_loop_pass(
            "timeout-escalation",
            self._timeout_escalation_poll_loop,
            "_timeout_escalation_task",
            log_msg="Periodic timeout escalation enabled: interval %ds, threshold %ds",
            log_args=(
                self.ctx.settings.timeout_escalation_interval_seconds,
                self.ctx.settings.timeout_escalation_threshold_seconds,
            ),
        )
        self._start_poll_loop_pass(
            "meta",
            self._meta_pass_loop,
            "_meta_task",
            log_msg="Periodic meta-agent enabled: interval %ds",
            log_args=(self.ctx.settings.meta_interval_seconds,),
        )

        # --- CI monitor (unique: checks repo config, not just settings) ---
        if self._ci_monitor_task is None:
            repos = get_repos_config()
            if any(rc.ci_monitor_enabled for rc in repos.repos.values()):
                self._ci_monitor_task = asyncio.create_task(
                    self._ci_monitor_poll_loop()
                )
                log.info("CI monitor enabled (per-repo config)")

        # --- Bespoke supervisors (unique: iterates repos + guards board_id) ---
        if self.ctx.settings.bespoke_periodic:
            for rc in get_repos_config().repos.values():
                if (
                    not rc.bespoke_periodic
                    or rc.board_id in self._bespoke_supervisor_tasks
                ):
                    continue
                self._bespoke_supervisor_tasks[rc.board_id] = asyncio.create_task(
                    self._bespoke_supervisor(rc)
                )
                log.info(
                    "Bespoke supervisor enabled for repo %s (discovery interval %ds)",
                    rc.repo_id,
                    self.ctx.settings.bespoke_discovery_interval_seconds,
                )

    async def stop(self) -> None:
        # Wait for periodic passes that are mid-run (survey, audit,
        # health, …) to finish before tearing the loops down. Without
        # this a SIGTERM during a survey run would lose the work and
        # leave half-cooked drafts.
        inflight = list(self._inflight_passes)
        if inflight:
            grace = max(0, self.ctx.settings.shutdown_grace_seconds)
            log.info(
                "stop: awaiting %d in-flight periodic pass(es) (grace %ds)",
                len(inflight),
                grace,
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*inflight, return_exceptions=True),
                    timeout=grace if grace > 0 else None,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "stop: in-flight passes did not finish within %ds — "
                    "cancelling remaining %d",
                    grace,
                    sum(1 for t in inflight if not t.done()),
                )
        tasks = list(self._tasks)
        for attr in (
            "_poll_task",
            "_audit_task",
            "_trace_health_task",
            "_trace_review_task",
            "_cost_warmer_task",
            "_cost_warmer_fast_task",
            "_health_task",
            "_ci_monitor_task",
            "_agent_check_task",
            "_bc_check_task",
            "_completeness_check_task",
            "_copy_paste_task",
            "_module_curator_task",
            "_test_gap_task",
            "_survey_task",
            "_config_sync_task",
            "_cost_reconciliation_task",
            "_langfuse_cleanup_task",
            "_timeout_escalation_task",
            "_meta_task",
        ):
            t = getattr(self, attr)
            if t is not None:
                tasks.append(t)
                setattr(self, attr, None)
        # Bespoke supervisors: cancelling each one cancels its child
        # per-bespoke loop tasks via the supervisor's ``finally``.
        for t in self._bespoke_supervisor_tasks.values():
            tasks.append(t)
        self._bespoke_supervisor_tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []
        tracing.flush_tracing()

    def requeue_unfinished(self) -> None:
        """On startup, re-enqueue any ticket left mid-pipeline so a
        restart resumes work (idempotent: stages are re-entrant).

        With per-repo DBs, fan out across every registered repo
        so nothing is missed.
        """
        from ..config import get_repos_config
        from ..core.service import TicketService

        boards: list[str] = []
        try:
            for rc in get_repos_config().repos.values():
                if rc.board_id and rc.board_id not in boards:
                    boards.append(rc.board_id)
        except Exception:
            pass
        for board_id in boards:
            svc = TicketService(self.ctx.settings, board_id=board_id)
            try:
                for ticket in svc.list():
                    if ticket.state in STAGE_FOR_STATE:
                        self.enqueue(ticket.id)
            except Exception:
                log.exception(
                    "requeue_unfinished: failed to enumerate board %r",
                    board_id or "<default>",
                )
