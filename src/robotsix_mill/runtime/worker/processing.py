from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from datetime import datetime, timezone

from ...stages import StageContext, get_stage
from ...core.states import STAGE_FOR_STATE, State
from ...core.models import TicketKind
from ...core.service._helpers import TransitionError
from ...core.service._lifecycle import _TERMINAL_STATES
from ...notify import send_notification, _TRIGGER_STATES
from .. import tracing
from ..tracing import langfuse_trace_url
from .epic import _EPIC_CHILD_TERMINAL, _run_epic_reeval

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
    from ..transient_errors import (
        classify_stage_error,
        is_network_down_error,
        network_available,
    )
    from ..stage_retry import compute_retry_delay

    classification = classify_stage_error(error)
    tracing.set_current_span_attribute("error.classification", classification)
    tracing.set_current_span_attribute("error.type", type(error).__name__)
    if classification == "transient":
        ticket = ctx.service.get(ticket_id)
        if ticket is None:
            return
        # Global network outage: every network-touching stage is about
        # to fail identically, and an outage longer than the bounded
        # retry envelope (~1 min) would mass-block the board. Park the
        # ticket WITHOUT consuming a retry attempt; it re-polls until
        # connectivity returns, then normal bounded retries apply.
        if is_network_down_error(error) and not network_available(
            ctx.settings.network_probe_host
        ):
            outage_delay = ctx.settings.network_outage_retry_seconds
            next_at_dt = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + outage_delay,
                tz=timezone.utc,
            )
            ctx.service.set_retry_state(
                ticket_id,
                # Floor at 1 so the board shows the retry chip; never
                # incremented here, so parking can't exhaust the budget.
                retry_attempt=max(ticket.retry_attempt, 1),
                last_transient_error=(
                    "network outage (parked, retry budget untouched): " + repr(error)
                )[:200],
                next_retry_at=next_at_dt,
            )
            tracing.set_current_span_attribute("retry.network_outage", True)
            tracing.set_current_span_attribute(
                "retry.attempt", max(ticket.retry_attempt, 1)
            )
            log.warning(
                "%s: %s network outage (%s unresolvable) — parked, re-checking in %ds",
                stage_name,
                ticket_id,
                ctx.settings.network_probe_host,
                outage_delay,
            )
            _post_trace_event(ctx, ticket_id, trace_id, stage_name)
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
            tracing.set_current_span_attribute("retry.attempt", attempt)
            tracing.set_current_span_attribute("retry.max_attempts", max_attempts)
            tracing.set_current_span_attribute("retry.next_at", next_at_dt.isoformat())
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
        tracing.set_current_span_attribute("retry.exhausted", True)
        tracing.set_current_span_attribute("retry.attempt", attempt)
        await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, trace_id)
    else:
        # FATAL — block immediately.
        note = f"Fatal: {type(error).__name__}: {error}"[:200]
        tracing.set_current_span_attribute("error.fatal", True)
        await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, trace_id)


# Child states that count as "complete" for epic-closing purposes.
def _maybe_reevaluate_epic(
    ticket_id: str, ctx: StageContext, next_state: State
) -> None:
    """After a ticket reaches a terminal-ish state, re-evaluate its
    parent epic (if any).

    The parent epic may live on a DIFFERENT board than the child
    (cross-repo epics).  Use a fan-out ``get`` (``TicketService``
    with empty ``board_id``, whose ``get`` fans out via
    ``_get_anywhere``) so a child on board A finds its epic on
    board B.

    ``_spawn_epic_reeval`` fires-and-forgets a daemon thread, so this
    helper does not need to be ``async``.
    """
    if next_state in _EPIC_CHILD_TERMINAL:
        ticket = ctx.service.get(ticket_id)
        if ticket is not None and ticket.parent_id is not None:
            # Use a fan-out service (empty board_id) for parent lookup
            # so cross-board epic links are resolved.
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == TicketKind.EPIC:
                _spawn_epic_reeval(parent.id, ctx)


def _root_span_attributes(
    ticket, stage_name: str, dispatch_counts: Counter[str]
) -> dict[str, str]:
    """Build span attributes for Langfuse searchability from ticket metadata.

    Returns string-keyed values only — OTel span attributes must be
    scalar strings, bools, ints, or floats.
    """
    return {
        "ticket.state": ticket.state.value,
        "ticket.kind": (
            ticket.kind.value if hasattr(ticket, "kind") and ticket.kind else ""
        ),
        "ticket.retry_attempt": str(getattr(ticket, "retry_attempt", 0)),
        "ticket.review_rounds": str(getattr(ticket, "review_rounds", 0)),
        "ticket.blocked_from": ticket.blocked_from or "",
        "ticket.paused_from": ticket.paused_from or "",
        "ticket.dispatch_count": str(dispatch_counts.get(stage_name, 0)),
        "ticket.source": ticket.source or "",
        "stage.name": stage_name,
    }


def _root_input_summary(
    ticket, ticket_id: str, stage_name: str, dispatch_count: int = 0
) -> dict:
    """Build the input-summary dict attached to the Langfuse root span.

    Includes ticket identity, current state, retry/review counters,
    and a dispatch counter that serves as an early-loop-detection
    trigger — a stage re-running many times in one pass signals a
    potential runaway.
    """
    return {
        "ticket_id": ticket_id,
        "title": ticket.title,
        "state": ticket.state.value,
        "kind": (
            ticket.kind.value if hasattr(ticket, "kind") and ticket.kind else None
        ),
        "stage": stage_name,
        "source": ticket.source,
        "priority": bool(getattr(ticket, "priority", False)),
        "retry_attempt": getattr(ticket, "retry_attempt", 0),
        "last_transient_error": getattr(ticket, "last_transient_error", None),
        "review_rounds": getattr(ticket, "review_rounds", 0),
        "blocked_from": getattr(ticket, "blocked_from", None),
        "paused_from": getattr(ticket, "paused_from", None),
        "dispatch_count": dispatch_count,
        "workspace_path": getattr(ticket, "workspace_path", None),
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
    dispatch_counts: Counter[str] = Counter()
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
        limit = ctx.settings.ticket_state_cycle_limit
        if traced and limit > 0:
            dispatch_counts[stage_name] += 1
            if dispatch_counts[stage_name] > limit:
                note = (
                    f"Cycle ceiling: '{stage_name}' re-ran "
                    f"{dispatch_counts[stage_name]} times this pass "
                    f"(limit {limit}) — pausing for human review to "
                    f"avoid an unbounded implement/review/ci_fix re-run "
                    f"loop."
                )[:200]
                log.warning("%s: %s — %s", stage_name, ticket_id, note)
                await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, None)
                return
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
                    extra_attrs = _root_span_attributes(
                        ticket, stage_name, dispatch_counts
                    )
                    root_io = es.enter_context(
                        tracing.start_ticket_root_span(
                            ticket_id,
                            stage_name,
                            extra_attributes=extra_attrs,
                            repo_config=ctx.repo_config,
                        )
                    )
                    # Attach a top-level "input" summary to the root span
                    # so Langfuse's trace view shows what was processed
                    # without drilling into children. Output is set
                    # below, once the stage returns.
                    root_io.set_input(
                        _root_input_summary(
                            ticket,
                            ticket_id,
                            stage_name,
                            dispatch_count=dispatch_counts.get(stage_name, 0),
                        )
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
            tracing.set_current_span_attribute("error.classification", "timeout")
            tracing.set_current_span_attribute("error.timeout_seconds", str(timeout))
            if root_io is not None:
                root_io.set_output(
                    {
                        "error": f"stage {stage_name} timed out after {timeout}s",
                        "next_state": "BLOCKED",
                    }
                )
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
            tracing.set_current_span_attribute(
                "error.classification", "not_implemented"
            )
            if root_io is not None:
                root_io.set_output({"error": f"stub: {e}"})
            _post_trace_event(ctx, ticket_id, trace_id, stage_name)
            return
        except Exception as e:  # noqa: BLE001 — any failure fails the ticket
            if root_io is not None:
                root_io.set_output({"error": f"{type(e).__name__}: {str(e)[:200]}"})
            tracing.set_current_span_attribute(
                "ticket.retry_attempt", str(getattr(ticket, "retry_attempt", 0) + 1)
            )
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
        tracing.set_current_span_attribute(
            "outcome.next_state", outcome.next_state.value
        )
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
            try:
                ctx.service.transition(ticket_id, outcome.next_state, outcome.note)
            except TransitionError as e:
                # The pipeline auto-completing a ticket (e.g. merge → DONE
                # once the PR merged) must not be blocked by a stale
                # [ASK_USER] thread — the work shipped, so the question is
                # moot. Closing it (record preserved) and retrying beats
                # crash-looping the consumer on every poll.
                if outcome.next_state in _TERMINAL_STATES and "[ASK_USER]" in str(e):
                    n = ctx.service.close_open_ask_user_threads(ticket_id)
                    log.warning(
                        "%s: %s auto-completing to %s — closed %d stale "
                        "[ASK_USER] thread(s) that would have blocked it",
                        stage_name,
                        ticket_id,
                        outcome.next_state,
                        n,
                    )
                    ctx.service.transition(ticket_id, outcome.next_state, outcome.note)
                else:
                    raise
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
