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
from typing import Any

from ..config import RepoConfig, get_repos_config
from ..langfuse.client import effective_cost, session_cost
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


async def _block_ticket_and_notify(
    ticket_id: str,
    ctx: StageContext,
    stage_name: str,
    note: str,
    trace_id: str | None,
) -> None:
    """Post the trace breadcrumb, transition to BLOCKED, and notify.

    Used by every error path inside :func:`_process_ticket_inner`
    (timeout, transient-exhausted, fatal) so the block-and-notify
    sequence lives in exactly one place.
    """
    _post_trace_event(ctx, ticket_id, trace_id, stage_name)
    ctx.service.transition(ticket_id, State.BLOCKED, note=note[:200])
    ticket = ctx.service.get(ticket_id)
    if ticket is not None:
        send_notification(ticket, State.BLOCKED, note[:200], ctx.settings)


async def _handle_stage_error(
    ticket_id: str,
    ctx: StageContext,
    stage_name: str,
    error: BaseException,
    trace_id: str | None,
) -> None:
    """Absorb the ``except Exception`` body of :func:`_process_ticket_inner`.

    Logs the exception, classifies it via
    :func:`~.transient_errors.classify_stage_error`, and either schedules
    a retry (transient, attempts remaining) or escalates to BLOCKED via
    :func:`_block_ticket_and_notify` (transient-exhausted or fatal).
    """
    log.exception("%s: %s failed", stage_name, ticket_id)
    from .transient_errors import classify_stage_error
    from .stage_retry import compute_retry_delay

    classification = classify_stage_error(error)
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
                last_transient_error=repr(error)[:200],
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
        note = (
            f"Transient: {type(error).__name__} persisted after "
            f"{max_attempts} attempts — last: {error}"
        )[:200]
        await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, trace_id)
    else:
        # FATAL — block immediately.
        note = f"Fatal: {type(error).__name__}: {error}"[:200]
        await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, trace_id)


# Child states that count as "complete" for epic-closing purposes.
_EPIC_CHILD_TERMINAL = frozenset(
    {State.DONE, State.CLOSED, State.ANSWERED, State.EPIC_CLOSED}
)


def _maybe_reevaluate_epic(
    ticket_id: str, ctx: StageContext, next_state: State
) -> None:
    """After a ticket reaches a terminal-ish state, re-evaluate its
    parent epic (if any).

    ``_spawn_epic_reeval`` fires-and-forgets a daemon thread, so this
    helper does not need to be ``async``.
    """
    if next_state in _EPIC_CHILD_TERMINAL:
        ticket = ctx.service.get(ticket_id)
        if ticket is not None and ticket.parent_id is not None:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == "epic":
                _spawn_epic_reeval(parent.id, ctx)


def _root_input_summary(ticket, ticket_id: str, stage_name: str) -> dict:
    """Build the input-summary dict attached to the Langfuse root span."""
    return {
        "ticket_id": ticket_id,
        "title": ticket.title,
        "state": ticket.state.value,
        "stage": stage_name,
        "source": ticket.source,
        "priority": bool(getattr(ticket, "priority", False)),
    }


def _root_output_summary(outcome, ticket) -> dict:
    """Build the output-summary dict attached to the Langfuse root span."""
    return {
        "next_state": outcome.next_state.value
        if outcome and outcome.next_state
        else None,
        "note": (outcome.note or "") if outcome else "",
        "no_op": bool(outcome and outcome.next_state == ticket.state),
    }


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
                        _root_input_summary(ticket, ticket_id, stage_name)
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
                    root_io.set_output(_root_output_summary(outcome, ticket))
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
            note = f"stage {stage_name} timed out after {timeout}s"[:200]
            await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, trace_id)
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
            await _handle_stage_error(ticket_id, ctx, stage_name, e, trace_id)
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
        _maybe_reevaluate_epic(ticket_id, ctx, outcome.next_state)


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
    from ..agents.epic_status import run_epic_status_agent
    from ..runtime import tracing

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
    from ..core.service import TicketService

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


def _build_child_summaries(svc, epic_id: str) -> list[dict]:
    """Build the per-child summary dicts passed to the epic-status agent.

    Each child's description is read and truncated to 500 chars (with a
    ``"\\n...(truncated)"`` suffix); the summary carries ``id``,
    ``title``, ``state``, ``description`` and ``depends_on``.
    """
    from ..core.service import TicketService

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

        # Advisory pre-filing dedup: flag (never drop) children whose
        # scope overlaps a recent ticket or an earlier sibling in this
        # batch. Runs after the existing-children title filter above.
        # Best-effort — a failure must not block filing.
        from ..dedup import annotate_child_body, find_child_overlaps

        overlap_notes = find_child_overlaps(
            svc,
            epic_id,
            new_titles,
            new_bodies,
            settings,
            datetime.now(timezone.utc),
        )

        created_ids: list[str] = []
        for title, body, dup_note in zip(new_titles, new_bodies, overlap_notes):
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
    # The synthetic cross-repo meta board is not a registered repo, but its
    # tickets ARE worked (refine builds a multi-repo workspace via triage —
    # see meta.workspace.build_triaged_meta_workspace). Consume it like a board.
    _META_BOARD = "meta"

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
        self,
        ctx: StageContext,
        run_registry: "RunRegistry | None" = None,
        run_registries: "dict[str, RunRegistry] | None" = None,
    ) -> None:
        self.ctx = ctx
        # ``run_registry`` is the default/fallback (board-less ticks).
        # ``run_registries`` maps board_id -> that repo's RunRegistry so a
        # periodic pass records into — and reads its cadence from — the SAME
        # per-repo runs.json the per-repo /runs API serves. Without this every
        # periodic run landed in the lead repo's registry and was invisible in
        # other repos' run lists (e.g. audit on robotsix-llmio).
        self.run_registry = run_registry
        self.run_registries = run_registries or {}
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
        self._data_dir_audit_task: asyncio.Task | None = None
        self._langfuse_cleanup_task: asyncio.Task | None = None
        self._timeout_escalation_task: asyncio.Task | None = None
        self._meta_task: asyncio.Task | None = None
        self._cost_analyst_task: asyncio.Task | None = None
        self._run_health_task: asyncio.Task | None = None
        # board_id -> per-repo bespoke supervisor task. The supervisor
        # itself owns each repo's per-bespoke child tasks; cancelling
        # the supervisor cancels its children.
        self._periodic_supervisor_tasks: dict[str, asyncio.Task] = {}
        # ticket_id -> consecutive no-progress cycles in a traced stage
        self._stuck: dict[str, int] = {}
        # Epic-sweep dedup: epic_id → child count at last sweep re-eval, so the
        # safety-net sweep re-evaluates an all-children-terminal epic at most
        # once per stable child set (re-eval again only when children change).
        self._epic_sweep_seen: dict[str, int] = {}
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

    def _maybe_sweep_orphaned_epic(self, epic, svc) -> None:
        """Re-evaluate an EPIC_OPEN epic whose children are ALL terminal but
        which is still open (a missed child-close trigger orphaned it).

        Idempotent: re-evaluates at most once per stable terminal child set
        (keyed on child count), so a healthy epic isn't re-billed every poll.
        Re-evaluates again only when the child count changes (e.g. epic_status
        spawned new children). The epic_status agent itself decides whether to
        actually close — this just ensures it gets the chance."""
        children = svc.list_children(epic.id)
        if not children:
            return
        if not all(c.state in _EPIC_CHILD_TERMINAL for c in children):
            return
        if self._epic_sweep_seen.get(epic.id) == len(children):
            return  # already swept this stable terminal child set
        self._epic_sweep_seen[epic.id] = len(children)
        log.info(
            "epic %s: all %d children terminal but still EPIC_OPEN — "
            "sweep-triggering re-evaluation (missed child-close trigger)",
            epic.id,
            len(children),
        )
        _spawn_epic_reeval(epic.id, self.ctx)

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
            # Exclude the pre-redraft baseline so the cap restarts at
            # zero after a redraft: only spend accrued since the most
            # recent redraft counts toward the limit. ``ticket`` was
            # fetched above for the retry-attempt check.
            baseline = (
                getattr(ticket, "pre_redraft_cost_usd", 0.0) or 0.0
                if ticket is not None
                else 0.0
            )
            effective = effective_cost(cost, baseline)
            if effective > self.ctx.settings.max_spend_usd_per_ticket:
                note = (
                    f"Cost cap exceeded: ${effective:.2f} spent "
                    f"(limit ${self.ctx.settings.max_spend_usd_per_ticket:.2f}; "
                    "pre-redraft cost excluded). "
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
                # Seed with the synthetic cross-repo meta board: its
                # tickets ARE worked (refine builds a multi-repo
                # workspace), but it is not a registered repo, so without
                # this a meta ticket that becomes READY *after* startup
                # (e.g. its dependency closes) would never be re-enqueued
                # and would sit READY until the next restart —
                # requeue_unfinished() seeds meta but only runs once.
                boards: list[str] = [self._META_BOARD]
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
                        # Safety net: an EPIC_OPEN epic is normally re-evaluated
                        # by the child-close hook (_maybe_reevaluate_epic). If
                        # that trigger is ever missed (a child closes via a path
                        # that bypasses it, a race, or an error), the epic is
                        # orphaned in EPIC_OPEN forever — epics are NOT in
                        # STAGE_FOR_STATE, so the requeue sweep below skips them.
                        # Catch it here.
                        if t.kind == "epic" and t.state == State.EPIC_OPEN:
                            self._maybe_sweep_orphaned_epic(t, svc)
                            continue
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

    def _registry_for(self, repo_config) -> "RunRegistry | None":
        """The RunRegistry a periodic pass should read/write for *repo_config*.

        Per-repo registry (``run_registries[board_id]``) when available so the
        run is recorded in — and its cadence measured from — the same
        ``<data_dir>/<board_id>/runs.json`` the per-repo /runs API serves.
        Falls back to the default registry for board-less ticks.
        """
        if repo_config is not None and self.run_registries:
            return self.run_registries.get(repo_config.board_id, self.run_registry)
        return self.run_registry

    def _initial_delay(
        self, kind: str, interval: int, repo_id: str = "", registry=None
    ) -> float:
        """Return the seconds to sleep before the first periodic pass.

        Queries ``RunRegistry.most_recent(kind, repo_id)`` to decide:
        - No registry → full ``interval`` (preserves current behaviour).
        - Never run (``None``) → 1.0 s.
        - Last run overdue (elapsed >= interval) → 1.0 s.
        - Otherwise → ``interval - elapsed`` (remaining time).

        *repo_id* scopes the lookup to one repo's own history. Per-repo loops
        (the periodic-workflow + bespoke supervisors) MUST pass it: without it
        ``most_recent`` returns the newest run of *kind* across ALL repos, so a
        repo that has never run the agent inherits another repo's recent
        timestamp and waits a near-full interval before its first run — every
        restart resetting that wait. With a 24 h interval + frequent restarts
        the first run then never fires (the symptom: audit never ran on
        robotsix-llmio because mill's daily audit kept the shared clock warm).

        *registry* selects which store to read (per-repo loops pass the repo's
        own registry so the cadence matches where the run is recorded); defaults
        to the worker's fallback registry.
        """
        reg = registry if registry is not None else self.run_registry
        if reg is None:
            return float(interval)
        entry = reg.most_recent(kind, repo_id=repo_id or None)
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

        Priority: periodic_workspace (legacy: bespoke_workspace) > any
        *_workspace/repo. When no clone exists yet the loader falls back to
        the built-in YAML.
        """
        if repo_config is None:
            return None
        base = Path(self.ctx.settings.data_dir) / repo_config.repo_id
        if not base.is_dir():
            return None
        periodic = base / "periodic_workspace" / "repo"
        if (periodic / ".git").exists():
            return periodic
        bespoke = base / "bespoke_workspace" / "repo"  # legacy name (pre-rename)
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
                    # Presence wins over flag: if the repo ships a
                    # .robotsix-mill/periodic/<label>.yaml the periodic
                    # supervisor owns this agent for this repo — skip here so
                    # it never double-fires during the migration window.
                    if self._has_periodic_presence(repo_config, label):
                        continue
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
        # Record into the per-repo registry so the run shows up in that repo's
        # /runs list (not the lead repo's).
        reg = self._registry_for(repo_config)
        repo_label = repo_config.repo_id if repo_config else label
        session_id = tracing.make_session_id(label)
        try:
            log.info(
                "Starting periodic %s pass for repo %s",
                label,
                repo_label,
            )
            if reg:
                run_id = reg.start(
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
            if reg and run_id:
                runner_summary = (getattr(result, "summary", "") or "").strip()
                n = len(result.drafts_created)
                if runner_summary:
                    # The agent's own account + the draft count, so the count is
                    # always visible alongside its reasoning.
                    summary = f"{runner_summary} | {n} draft(s) filed"
                else:
                    draft_ids = [d["id"] for d in result.drafts_created[:5]]
                    summary = (
                        f"Created {n} drafts: "
                        f"{', '.join(draft_ids)}"
                        f"{'…' if n > 5 else ''}"
                    )
                reg.finish_ok(run_id, summary)
        except Exception as e:  # noqa: BLE001 — periodic must survive
            log.exception(
                "%s poll failed for repo %s",
                label,
                repo_label,
            )
            if reg and run_id:
                reg.finish_error(run_id, str(e))

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
        from robotsix_mill.meta.runner import MetaPassResult, run_meta_pass

        interval = max(60, self.ctx.settings.meta_interval_seconds)
        initial = self._initial_delay("meta", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id("meta")
            # Record into the dedicated meta-board registry (tagged
            # repo_id="meta") so the run shows on the meta board's runs
            # drawer, not the lead repo's. Falls back to the default
            # registry if the meta registry is somehow absent.
            registry = self.run_registries.get(self._META_BOARD) or self.run_registry
            try:
                log.info("Starting periodic meta pass")
                if registry:
                    run_id = registry.start("meta", repo_id=self._META_BOARD)
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
                if registry and run_id:
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
                    registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("Meta pass failed")
                if registry and run_id:
                    registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _run_health_pass_loop(self) -> None:
        """Global run-health loop — fires once per interval (not per-repo).

        Reads every board's run registry over the window, flags failed/
        degraded runs deterministically, runs one LLM pass to separate real
        failures from legitimate empties, and files high-confidence draft
        tickets to the mill board.
        """
        from robotsix_mill.runners.run_health_runner import (
            RunHealthPassResult,
            run_run_health_pass,
        )

        interval = max(60, self.ctx.settings.run_health_interval_seconds)
        initial = self._initial_delay("run-health", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id("run_health")
            try:
                log.info("Starting periodic run-health pass")
                if self.run_registry:
                    run_id = self.run_registry.start("run_health")
                with tracing.start_ticket_root_span(
                    session_id, "run_health", repo_config=None
                ):
                    result: RunHealthPassResult = await self._tracked_to_thread(
                        run_run_health_pass,
                        session_id=session_id,
                    )
                log.info(
                    "Run-health pass completed, created %d draft(s)",
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    ids = [d["id"] for d in result.drafts_created[:3]]
                    summary = (
                        f"{len(result.drafts_created)} draft(s): {', '.join(ids)}"
                        if ids
                        else "No drafts created"
                    )
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("Run-health pass failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _cost_analyst_pass_loop(self) -> None:
        """Global cost-analyst loop — fires once per interval (not per-repo).

        Studies the fleet's aggregate cost-by-stage distribution + the four
        significant trace/ticket specimens and files high-confidence
        cost-reduction drafts to the mill board.
        """
        from robotsix_mill.runners.cost_analyst_runner import (
            CostAnalystPassResult,
            run_cost_analyst_pass,
        )

        interval = max(60, self.ctx.settings.cost_analyst_interval_seconds)
        initial = self._initial_delay("cost-analyst", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id("cost_analyst")
            try:
                log.info("Starting periodic cost-analyst pass")
                if self.run_registry:
                    run_id = self.run_registry.start("cost_analyst")
                with tracing.start_ticket_root_span(
                    session_id, "cost_analyst", repo_config=None
                ):
                    result: CostAnalystPassResult = await self._tracked_to_thread(
                        run_cost_analyst_pass,
                        session_id=session_id,
                    )
                log.info(
                    "Cost-analyst pass completed, created %d draft(s)",
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    ids = [d["id"] for d in result.drafts_created[:3]]
                    summary = (
                        f"{len(result.drafts_created)} draft(s): {', '.join(ids)}"
                        if ids
                        else "No drafts created"
                    )
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("Cost-analyst pass failed")
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
                    if self._has_periodic_presence(rc, "trace_health")
                ]
            for repo_config in repo_configs:
                repo_label = repo_config.repo_id if repo_config else "default"
                try:
                    log.info(
                        "Starting periodic trace-health check for repo %s",
                        repo_label,
                    )
                    from ..runners.trace_health_runner import run_trace_health_check

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

    async def _periodic_supervisor(self, repo_config: RepoConfig) -> None:
        """Per-repo periodic-workflow supervisor loop.

        Owns a clone of the managed repo at
        ``<data_dir>/<repo_id>/periodic_workspace/repo`` (legacy name:
        ``bespoke_workspace`` — auto-migrated below) and reconciles the
        set of running per-workflow loop tasks against the files the repo
        ships, on each cycle:

        - ``<clone>/.robotsix-mill/periodic/<name>.yaml`` (the unified
          per-repo periodic-workflow path): presence enables the workflow;
          it partial-merges over the built-in of the same name. ``llm_agent``
          and ``schedule_only`` kinds are scheduled here; ``maintenance`` is
          handled by the global poll loops; brand-new ``bespoke`` workflows
          via this dir are deferred to the legacy bespoke path below.
        - ``<clone>/.robotsix-mill/agents/<name>.yaml`` (legacy bespoke path,
          gated on ``settings.bespoke_periodic``): brand-new repo agents.

        Reconcile semantics per file: appear -> spawn a loop on its interval;
        disappear -> cancel; body change -> cancel + respawn (so the new
        prompt/model/interval take effect without operator intervention).
        Cancelling the supervisor cancels every child loop (worker.stop()).
        """
        from ..agents.bespoke_loader import load_bespoke_definitions
        from ..agents.periodic_loader import discover_periodic_workflows
        from ..runners.audit_runner import _clone_token
        from ..vcs import git_ops

        settings = self.ctx.settings
        interval = max(60, settings.bespoke_discovery_interval_seconds)
        board_id = repo_config.board_id
        forge_url = repo_config.forge_remote_url or settings.forge_remote_url
        repo_data_dir = settings.data_dir / repo_config.repo_id
        periodic_ws = repo_data_dir / "periodic_workspace"
        clone_dir = periodic_ws / "repo"

        # One-time migration of the legacy ``bespoke_workspace`` name (the
        # supervisor was the "bespoke supervisor" before it was generalized to
        # own all per-repo periodic-workflow discovery). Rename rather than
        # re-clone so the existing fetch history is preserved.
        legacy_ws = repo_data_dir / "bespoke_workspace"
        if legacy_ws.is_dir() and not periodic_ws.exists():
            try:
                legacy_ws.rename(periodic_ws)
                log.info(
                    "periodic supervisor (%s): migrated bespoke_workspace -> "
                    "periodic_workspace",
                    board_id,
                )
            except OSError:
                log.exception(
                    "periodic supervisor (%s): workspace rename failed", board_id
                )

        # namespaced key -> (task, comparison object). The comparison object
        # (a ResolvedPeriodicWorkflow or BespokeAgentDefinition) drives
        # respawn-on-change via ``==``.
        running: dict[str, tuple[asyncio.Task, Any]] = {}

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

                    # Build the DESIRED set of loops keyed by a namespaced
                    # id, each carrying a comparison object (for respawn) and
                    # a zero-arg spawn closure.
                    desired: dict[str, tuple[Any, Any]] = {}

                    # (a) Unified per-repo periodic workflows.
                    for wf in discover_periodic_workflows(clone_dir):
                        if not wf.enabled:
                            continue
                        if wf.kind in ("llm_agent", "schedule_only"):
                            # Global per-agent kill-switch (fleet-wide off).
                            if not getattr(settings, f"{wf.name}_periodic", True):
                                continue
                            key = f"periodic:{wf.name}"
                            desired[key] = (
                                wf,
                                (
                                    lambda wf=wf: self._run_periodic_workflow_loop(
                                        repo_config, wf, clone_dir
                                    )
                                ),
                            )
                        elif wf.kind == "bespoke":
                            # Brand-new agent via the unified dir — deferred to
                            # the legacy bespoke path for now (bespoke
                            # unification into .robotsix-mill/periodic/ is a
                            # follow-up).
                            log.debug(
                                "periodic %s/%s: bespoke kind via periodic dir "
                                "not yet scheduled here — use .robotsix-mill/"
                                "agents/ for now",
                                board_id,
                                wf.name,
                            )
                        # maintenance kind: handled by the global poll loops.

                    # (b) Legacy bespoke definitions (gated on the master switch).
                    if settings.bespoke_periodic:
                        for defn in load_bespoke_definitions(clone_dir):
                            key = f"bespoke:{defn.name}"
                            desired[key] = (
                                defn,
                                (
                                    lambda defn=defn: self._run_bespoke_loop(
                                        repo_config, defn, clone_dir
                                    )
                                ),
                            )

                    # Drop tasks whose source file disappeared.
                    for key in list(running):
                        if key not in desired:
                            task, _ = running.pop(key)
                            task.cancel()
                            log.info(
                                "periodic %s/%s: removed — cancelled", board_id, key
                            )

                    # Spawn / respawn tasks for the current desired set.
                    for key, (cmp_obj, spawn) in desired.items():
                        existing = running.get(key)
                        if existing is not None and existing[1] == cmp_obj:
                            continue  # unchanged
                        if existing is not None:
                            existing[0].cancel()
                            log.info(
                                "periodic %s/%s: changed — respawning", board_id, key
                            )
                        task = asyncio.create_task(spawn())
                        running[key] = (task, cmp_obj)
                        log.info("periodic %s/%s: scheduled", board_id, key)
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
        from ..runners import bespoke_runner
        from .. import tracing

        interval = max(60, definition.interval_seconds)
        label = f"bespoke:{definition.name}"
        # Honour the persisted last-run timestamp so a restarted mill
        # doesn't re-fire every bespoke immediately. Scope to this repo so a
        # repo that has never run this bespoke fires promptly instead of
        # inheriting another repo's recent timestamp.
        initial = self._initial_delay(
            label,
            interval,
            repo_id=repo_config.repo_id,
            registry=self._registry_for(repo_config),
        )
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

    # Schedule-only periodic workflows (no prompt yaml / own runner). Their
    # module-level ``run_<name>_pass(session_id, repo_config)`` stub is used
    # directly — no definition override is applicable.
    _SCHEDULE_ONLY_RUNNERS: dict[str, str] = {
        "trace_review": "robotsix_mill.runners.trace_review_runner:run_trace_review_pass",
        "config_sync": "robotsix_mill.runners.config_sync_runner:run_config_sync_pass",
        "cost_reconciliation": (
            "robotsix_mill.runners.cost_reconciliation_runner:run_cost_reconciliation_pass"
        ),
        "data_dir_audit": (
            "robotsix_mill.runners.data_dir_audit_runner:run_data_dir_audit_pass"
        ),
    }

    def _build_periodic_workflow_runner(self, wf):
        """Return a ``runner_fn(session_id, repo_config)`` for *wf*, or None.

        ``llm_agent`` → a closure that runs the matching periodic pass with
        the merged definition threaded in as ``definition_override``.
        ``schedule_only`` → the workflow's module-level pass stub.
        """
        if wf.kind == "llm_agent":
            from ..config import Settings

            definition = wf.definition

            # board_cleanup is an llm_agent but has a BESPOKE runner (it
            # operates on the board, not the code tree, so it needs the
            # full board snapshot injected) — route it before the generic
            # PERIODIC_PASS_CONFIGS lookup.
            if wf.name == "board_cleanup":
                from ..runners.periodic_runner import run_board_cleanup_pass

                def _run_board_cleanup(*, session_id, repo_config):
                    return run_board_cleanup_pass(
                        session_id,
                        repo_config,
                        settings=Settings(),
                        definition_override=definition,
                    )

                return _run_board_cleanup

            from ..runners.periodic_runner import (
                PERIODIC_PASS_CONFIGS,
                run_periodic_pass,
            )

            cfg = PERIODIC_PASS_CONFIGS.get(wf.name)
            if cfg is None:
                return None

            def _run(*, session_id, repo_config):
                return run_periodic_pass(
                    session_id,
                    repo_config,
                    cfg,
                    settings=Settings(),
                    definition_override=definition,
                )

            return _run
        if wf.kind == "schedule_only":
            import importlib

            path = self._SCHEDULE_ONLY_RUNNERS.get(wf.name)
            if path is None:
                return None
            mod_path, attr = path.rsplit(":", 1)
            return getattr(importlib.import_module(mod_path), attr)
        return None

    async def _run_periodic_workflow_loop(self, repo_config, wf, clone_dir) -> None:
        """Periodic loop for one resolved per-repo periodic workflow.

        Sleeps the resolved interval (file override > Settings fallback) and
        fires the matching runner. Failures log + continue; exits only via
        cancellation by the supervisor.
        """
        settings = self.ctx.settings
        label = wf.name
        interval = wf.interval_seconds
        if interval is None:
            interval = getattr(settings, f"{wf.name}_interval_seconds", 86400)
        interval = max(60, int(interval or 86400))

        runner_fn = self._build_periodic_workflow_runner(wf)
        if runner_fn is None:
            log.warning(
                "periodic workflow %s (%s): no runner for kind %r — not scheduling",
                wf.name,
                repo_config.repo_id,
                wf.kind,
            )
            return

        await asyncio.sleep(
            self._initial_delay(
                label,
                interval,
                repo_id=repo_config.repo_id,
                registry=self._registry_for(repo_config),
            )
        )
        while True:
            await self._fire_periodic_pass(label, runner_fn, repo_config)
            await asyncio.sleep(interval)

    def _maintenance_enabled_for(self, repo_config, name: str) -> bool:
        """Whether a non-LLM maintenance loop (langfuse_cleanup) runs for
        *repo_config*: enabled iff the repo ships a
        ``.robotsix-mill/periodic/<name>.yaml`` presence file. (The legacy
        ``RepoConfig.<name>_periodic`` flags were removed — presence is the
        single source of truth.)
        """
        return self._has_periodic_presence(repo_config, name)

    def _has_periodic_presence(self, repo_config, label: str) -> bool:
        """True when *repo_config*'s clone ships ``.robotsix-mill/periodic/
        <label>.yaml`` — meaning the periodic supervisor owns that workflow
        for this repo and the legacy flag-based loop must NOT also fire it.
        """
        from ..agents.periodic_loader import PERIODIC_DIR

        clone = self._find_config_clone_dir(repo_config)
        if clone is None:
            return False
        name = label.replace("-", "_")
        return clone.joinpath(*PERIODIC_DIR, f"{name}.yaml").is_file()

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
                    if self._maintenance_enabled_for(rc, "langfuse_cleanup")
                ]
            for repo_config in repo_configs:
                label = repo_config.repo_id if repo_config else "default"
                try:
                    from ..runners.langfuse_cleanup_runner import (
                        run_langfuse_cleanup_pass,
                    )

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
                from ..runners.timeout_escalation_runner import run_timeout_escalation

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
            pool_sizes.append((self._META_BOARD, 1))
            for board_id, n in pool_sizes:
                for _ in range(n):
                    self._tasks.append(asyncio.create_task(self._run(board_id)))
            log.info(
                "worker pool started: %s",
                ", ".join(f"{bid or '<default>'}={n}" for bid, n in pool_sizes),
            )
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

        # --- Per-repo periodic LLM agents + schedule-only passes ---
        # These now run via the per-repo periodic supervisor (spawned
        # below), driven by .robotsix-mill/periodic/<name>.yaml presence
        # files in each managed repo. The legacy repos.yaml enable-flags
        # were removed.

        # --- Pattern B: dedicated poll-loop tasks ---
        self._start_poll_loop_pass(
            "trace-health",
            self._trace_health_poll_loop,
            "_trace_health_task",
            log_msg="Periodic trace-health enabled: interval %ds",
            log_args=(self.ctx.settings.trace_health_interval_seconds,),
        )
        # Cost-cache warming is no longer a backend daemon — it's driven by
        # the board's /tickets poll (runtime/cost_warm.py). See PR removing
        # _cost_warmer_loop.
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
        self._start_poll_loop_pass(
            "cost-analyst",
            self._cost_analyst_pass_loop,
            "_cost_analyst_task",
            log_msg="Periodic cost-analyst enabled: interval %ds",
            log_args=(self.ctx.settings.cost_analyst_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "run-health",
            self._run_health_pass_loop,
            "_run_health_task",
            log_msg="Periodic run-health enabled: interval %ds",
            log_args=(self.ctx.settings.run_health_interval_seconds,),
        )

        # --- CI monitor (unique: checks repo config, not just settings) ---
        if self._ci_monitor_task is None:
            repos = get_repos_config()
            if any(rc.ci_monitor_enabled for rc in repos.repos.values()):
                self._ci_monitor_task = asyncio.create_task(
                    self._ci_monitor_poll_loop()
                )
                log.info("CI monitor enabled (per-repo config)")

        # --- Periodic supervisors: one per repo (owns the clone; discovers
        # .robotsix-mill/periodic/ presence files AND legacy bespoke files).
        # Always spawned — the unified periodic-workflow path does not depend
        # on the bespoke master switch (that switch still gates legacy bespoke
        # files inside the supervisor).
        for rc in get_repos_config().repos.values():
            if rc.board_id in self._periodic_supervisor_tasks:
                continue
            self._periodic_supervisor_tasks[rc.board_id] = asyncio.create_task(
                self._periodic_supervisor(rc)
            )
            log.info(
                "Periodic supervisor enabled for repo %s (discovery interval %ds)",
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
            "_data_dir_audit_task",
            "_langfuse_cleanup_task",
            "_timeout_escalation_task",
            "_meta_task",
            "_cost_analyst_task",
            "_run_health_task",
        ):
            t = getattr(self, attr)
            if t is not None:
                tasks.append(t)
                setattr(self, attr, None)
        # Periodic supervisors: cancelling each one cancels its child
        # per-workflow loop tasks via the supervisor's ``finally``.
        for t in self._periodic_supervisor_tasks.values():
            tasks.append(t)
        self._periodic_supervisor_tasks.clear()
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

        boards: list[str] = [self._META_BOARD]
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
