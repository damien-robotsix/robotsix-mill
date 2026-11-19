"""Stale-thread, retry, and stage-error handling for the ticket processing loop.

Extracted from :mod:`.processing` so the worker orchestration module stays
focused on the per-ticket pipeline.  These helpers own the error/retry
edges of a stage run: closing stale ``[ASK_USER]`` threads on terminal
transitions, classifying and retrying transient stage errors, and blocking
(or closing) a ticket when a stage cannot complete.  They depend on the
``_post_trace_event`` and ``_file_infra_ticket`` helpers that remain in
:mod:`.processing`, imported lazily inside the functions that need them to
avoid a module-import cycle.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ...core.service._helpers import TransitionError
from ...core.service._transition_mixin import _TERMINAL_STATES
from ...core.states import State
from ...notify import send_notification
from ...sandbox import reap_orphan_sandboxes
from ...stages import StageContext
from .. import tracing
from ..transient_errors import first_full_path

log = logging.getLogger("robotsix_mill.worker")


def _is_stale_ask_user_block(next_state: State, err: TransitionError) -> bool:
    """True when *err* is the terminal-state guard refusing *next_state*
    because of an open ``[ASK_USER]`` thread.
    """
    return next_state in _TERMINAL_STATES and "[ASK_USER]" in str(err)


def _close_stale_threads_and_transition(
    ctx: Any,
    ticket_id: str,
    next_state: State,
    note: str | None,
    *,
    stage_name: str,
    block_reason: Any = None,
) -> None:
    """Close every open ``[ASK_USER]`` thread on *ticket_id*, then apply the
    terminal transition to *next_state*.

    The pipeline auto-completing a ticket (merge → DONE once the PR merged,
    retrospect → CLOSED, the retrospect deadline closing a merged ticket)
    must not be blocked by a stale ``[ASK_USER]`` thread — the work shipped,
    so the question is moot. Closing it (record preserved) and retrying
    beats crash-looping the consumer on every poll. Live 2026-09-08 (f15f):
    the retrospect agent posted its own ``[ASK_USER]`` thread, its deadline
    fired, and the timeout close path — which did not have this guard —
    raised ``TransitionError`` and left the merged ticket parked in DONE.
    """
    n = ctx.service.close_open_ask_user_threads(ticket_id)
    log.warning(
        "%s: %s auto-completing to %s — closed %d stale "
        "[ASK_USER] thread(s) that would have blocked it",
        stage_name,
        ticket_id,
        next_state,
        n,
    )
    ctx.service.transition(ticket_id, next_state, note, block_reason=block_reason)


def _transition_closing_stale_threads(
    ctx: Any,
    ticket_id: str,
    next_state: State,
    note: str | None,
    *,
    stage_name: str,
) -> None:
    """``ctx.service.transition`` to a terminal *next_state*, closing stale
    ``[ASK_USER]`` threads on the guard's refusal instead of raising.
    """
    try:
        ctx.service.transition(ticket_id, next_state, note=note)
    except TransitionError as e:
        if not _is_stale_ask_user_block(next_state, e):
            raise
        _close_stale_threads_and_transition(
            ctx, ticket_id, next_state, note, stage_name=stage_name
        )


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
    from .processing import _post_trace_event

    _post_trace_event(ctx, ticket_id, trace_id, stage_name)
    ctx.service.transition(ticket_id, State.BLOCKED, note=note[:200])
    ticket = ctx.service.get(ticket_id)
    if ticket is not None:
        send_notification(ticket, State.BLOCKED, note[:200], ctx.settings)


async def _close_after_retrospect_failure(
    ticket_id: str,
    ctx: StageContext,
    note: str,
    trace_id: str | None,
) -> None:
    """Close a DONE ticket whose retrospect stage could not complete.

    Retrospect is post-merge analysis: the ticket's work is already on
    main, so a failing retrospect has nothing left to protect and nothing
    an operator can "unblock".  Escalating it to BLOCKED (observed
    2026-08-29: five merged tickets flipped to BLOCKED with "Transient:
    UnexpectedModelBehavior persisted after 5 attempts — Exceeded maximum
    output retries") only creates operator work and a phantom failure on
    the board.  Mirror what the stage itself does when it catches its own
    agent error (``Outcome(State.CLOSED, "retrospect failed — …")``).
    """
    from .processing import _post_trace_event

    _post_trace_event(ctx, ticket_id, trace_id, "retrospect")
    ctx.service.transition(
        ticket_id, State.CLOSED, note=f"retrospect failed — {note}"[:200]
    )
    log.warning("retrospect: %s closed without a retrospect — %s", ticket_id, note)


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
    log.error("%s: %s failed", stage_name, ticket_id)
    from ..stage_retry import compute_retry_delay
    from ..transient_errors import (
        MODEL_OUTAGE_MARKER,
        classify_stage_error,
        claude_usage_reset_at,
        is_claude_usage_exhausted,
        is_disk_full_error,
        is_model_unavailable_error,
        is_network_down_error,
        network_available,
    )
    from .processing import _file_infra_ticket, _post_trace_event

    classification = classify_stage_error(error)
    # Populates Langfuse's error message. Without it the span carries only
    # attributes and every failed stage reads as "[ERROR] None".
    tracing.record_exception(error)
    tracing.set_current_span_attribute("error.classification", classification)
    tracing.set_current_span_attribute("error.type", type(error).__name__)
    tracing.set_current_span_attribute(
        "retry.reason", f"{classification}: {type(error).__name__}: {error!s}"[:300]
    )
    if isinstance(error, TimeoutError):
        tracing.set_current_span_attribute("error.subtype", "timeout_inner")
        # --- reap orphan sandboxes on any timeout ---
        # A sandbox exec that triggered the timeout may still be running as
        # an orphaned container. Best-effort reaping here closes the
        # resource leak and ensures trace latency reflects the kill time.
        try:
            reaped = reap_orphan_sandboxes(
                max_age_seconds=2 * ctx.settings.sandbox_op_timeout
            )
            if reaped:
                tracing.set_current_span_attribute(
                    "sandbox.orphans_reaped", str(reaped)
                )
                log.warning("reaped %d orphan sandbox(es) after timeout", reaped)
        except Exception:
            log.warning("failed to reap orphan sandboxes after timeout", exc_info=True)
    if classification == "transient":
        # --- Clear stale implement fingerprint guard ---
        # When a transient infrastructure failure kills an implement run,
        # _finalize(ok=False) has already written the spec-fingerprint
        # guard to artifacts/implement.md. Delete it so the retry doesn't
        # hard-block on "spec unchanged since last implement attempt".
        if stage_name == "implement":
            try:
                ticket_obj = ctx.service.get(ticket_id)
                if ticket_obj is not None:
                    implement_md = (
                        ctx.service.workspace(ticket_obj).artifacts_dir / "implement.md"
                    )
                    implement_md.unlink(missing_ok=True)
            except Exception:
                log.exception(
                    "%s: failed to clear implement fingerprint guard", ticket_id
                )

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
                datetime.now(UTC).timestamp() + outage_delay,
                tz=UTC,
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
        # Data volume full: same shape as the network outage above, and
        # for the same reason. Every board fails identically until space
        # is freed, and the bounded retry envelope expires long before an
        # operator (or the data-dir GC) can act — so park WITHOUT
        # consuming a retry attempt rather than blocking the ticket and
        # requiring a manual resume per ticket.
        if is_disk_full_error(error) and (
            first_full_path(
                [ctx.settings.data_dir, *ctx.settings.disk_check_extra_paths],
                ctx.settings.disk_min_free_mb,
            )
            is not None
        ):
            disk_delay = ctx.settings.disk_full_retry_seconds
            next_at_dt = datetime.fromtimestamp(
                datetime.now(UTC).timestamp() + disk_delay,
                tz=UTC,
            )
            ctx.service.set_retry_state(
                ticket_id,
                retry_attempt=max(ticket.retry_attempt, 1),
                last_transient_error=(
                    "data volume full (parked, retry budget untouched): " + repr(error)
                )[:200],
                next_retry_at=next_at_dt,
            )
            tracing.set_current_span_attribute("retry.disk_full", True)
            tracing.set_current_span_attribute(
                "retry.attempt", max(ticket.retry_attempt, 1)
            )
            log.warning(
                "%s: %s data volume full (%s below %d MB free) — "
                "parked, re-checking in %ds",
                stage_name,
                ticket_id,
                ctx.settings.data_dir,
                ctx.settings.disk_min_free_mb,
                disk_delay,
            )
            _file_infra_ticket(
                ctx,
                "Infrastructure: disk space low — pipeline parked",
                (
                    f"The worker detected low disk space and parked all "
                    f"ticket processing. Path: {ctx.settings.data_dir}, "
                    f"threshold: {ctx.settings.disk_min_free_mb} MB free. "
                    f"The data-dir GC may clear space automatically; if "
                    f"not, operator intervention is required."
                ),
            )
            _post_trace_event(ctx, ticket_id, trace_id, stage_name)
            return
        # LLM-provider model outage ("model unavailable" / overloaded /
        # 503): same shape as the network/disk parks above. A model that
        # is down fails every stage that touches it identically; bounded
        # retries burn the budget in seconds and then block FATALLY,
        # mixing an infrastructure anomaly with genuine content failures.
        # Park WITHOUT consuming a retry attempt — the ticket re-polls
        # every model_outage_retry_seconds until the model recovers.
        #
        # Unlike the network/disk parks (which park indefinitely — DNS
        # always comes back; an operator always frees space), a model
        # outage may be permanent (bad model id, decommissioned model).
        # Track consecutive parks via retry_attempt (normally untouched
        # during parking); escalate to BLOCKED with an infra-framed note
        # when model_outage_max_parks is exceeded.
        if is_model_unavailable_error(error):
            park_count = ticket.retry_attempt + 1
            if park_count > ctx.settings.model_outage_max_parks:
                note = (
                    f"Infrastructure: LLM model outage ({MODEL_OUTAGE_MARKER}) "
                    f"persisted after {park_count - 1} park(s) — "
                    f"may be a permanent condition (bad model id?): "
                    f"{type(error).__name__}: {error}"
                )[:200]
                tracing.set_current_span_attribute("retry.model_outage_exhausted", True)
                tracing.set_current_span_attribute("retry.attempt", park_count)
                await _block_ticket_and_notify(
                    ticket_id, ctx, stage_name, note, trace_id
                )
                return
            outage_delay = ctx.settings.model_outage_retry_seconds
            outage_kind = "model outage"
            if is_claude_usage_exhausted(error):
                # The subscription quota returns at the stated reset; park
                # straight through to it (plus a minute of slack) rather
                # than re-polling a cap that cannot have moved. Without a
                # reset hint, fall back to the configured usage-exhaustion
                # interval — longer than a model blip, shorter than a window.
                outage_kind = "Claude usage exhausted"
                reset_at = claude_usage_reset_at(error)
                if reset_at is not None:
                    outage_delay = max(
                        60,
                        int((reset_at - datetime.now(UTC)).total_seconds()) + 60,
                    )
                    outage_kind += f" — parked until {reset_at:%H:%M}Z"
                else:
                    outage_delay = ctx.settings.claude_usage_exhausted_retry_seconds
                tracing.set_current_span_attribute("retry.claude_usage_exhausted", True)
            next_at_dt = datetime.fromtimestamp(
                datetime.now(UTC).timestamp() + outage_delay,
                tz=UTC,
            )
            ctx.service.set_retry_state(
                ticket_id,
                retry_attempt=park_count,
                last_transient_error=(
                    f"{outage_kind} ({MODEL_OUTAGE_MARKER}, park {park_count}/"
                    f"{ctx.settings.model_outage_max_parks}, "
                    f"retry budget untouched): " + repr(error)
                )[:200],
                next_retry_at=next_at_dt,
            )
            tracing.set_current_span_attribute("retry.model_outage", True)
            tracing.set_current_span_attribute("retry.attempt", park_count)
            log.warning(
                "%s: %s model outage (park %d/%d) — parked, re-checking in %ds",
                stage_name,
                ticket_id,
                park_count,
                ctx.settings.model_outage_max_parks,
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
            next_at = datetime.now(UTC).timestamp() + delay
            next_at_dt = datetime.fromtimestamp(next_at, tz=UTC)
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
        # Retries exhausted — block (or, for retrospect, close: the work is
        # already merged and there is nothing to unblock).
        note = (
            f"Transient: {type(error).__name__} persisted after "
            f"{max_attempts} attempts — last: {error}"
        )[:200]
        tracing.set_current_span_attribute("retry.exhausted", True)
        tracing.set_current_span_attribute("retry.attempt", attempt)
        if stage_name == "retrospect":
            await _close_after_retrospect_failure(ticket_id, ctx, note, trace_id)
            return
        await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, trace_id)
    else:
        # FATAL — block immediately (retrospect: close, see above).
        note = f"Fatal: {type(error).__name__}: {error}"[:200]
        tracing.set_current_span_attribute("error.fatal", True)
        if stage_name == "retrospect":
            await _close_after_retrospect_failure(ticket_id, ctx, note, trace_id)
            return
        await _block_ticket_and_notify(ticket_id, ctx, stage_name, note, trace_id)
