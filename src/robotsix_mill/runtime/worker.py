"""Event-driven worker. No scheduler.

A ticket is enqueued the moment it is emitted (or transitions into an
actionable state). The worker pulls it and **chains** stages —
``draft → … → done`` — until it hits a terminal state, a stub, or an
error. One worker, sequential, for v1.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..stages import StageContext, get_stage
from ..core.states import STAGE_FOR_STATE, State
from ..notify import send_notification, _TRIGGER_STATES
from . import tracing

log = logging.getLogger("robotsix_mill.worker")

# DONE is NOT terminal — retrospect owns it (done -> closed). Only
# closed/errored/blocked stop the chain.
_TERMINAL = {State.CLOSED, State.ERRORED, State.BLOCKED}


async def process_ticket(ticket_id: str, ctx: StageContext) -> None:
    """Drive one ticket through as many stages as possible, in order,
    until it reaches a terminal/waiting state or a stub stops the chain."""
    while True:
        ticket = ctx.service.get(ticket_id)
        if ticket is None:
            log.warning("ticket %s vanished", ticket_id)
            return
        if ticket.state in _TERMINAL:
            return
        stage_name = STAGE_FOR_STATE.get(ticket.state)
        if stage_name is None:
            log.debug("no stage for %s; pausing %s", ticket.state, ticket_id)
            return
        stage = get_stage(stage_name)
        # Only trace stages that call the model. Poll-driven no-LLM
        # stages (merge, deliver) would otherwise emit an empty "ticket"
        # trace into the Langfuse session on every poll.
        traced = getattr(stage, "traced", True)
        try:
            with contextlib.ExitStack() as es:
                if traced:
                    es.enter_context(tracing.start_ticket_root_span(ticket_id))
                    es.enter_context(tracing.trace_stage(stage_name))
                # stage.run is sync (LLM/tool) — keep the loop responsive
                outcome = await asyncio.to_thread(stage.run, ticket, ctx)
        except NotImplementedError as e:
            log.warning(
                "%s: stub (%s) — chain paused at %s for %s",
                stage_name, e, ticket.state, ticket_id,
            )
            return
        except Exception as e:  # noqa: BLE001 — any failure fails the ticket
            log.exception("%s: %s failed", stage_name, ticket_id)
            ctx.service.transition(ticket_id, State.ERRORED, note=repr(e)[:200])
            # Best-effort notification for the errored transition.
            ticket = ctx.service.get(ticket_id)
            if ticket is not None:
                send_notification(ticket, State.ERRORED, repr(e)[:200], ctx.settings)
            return
        if outcome.next_state == ticket.state:
            # no-op (e.g. merge: PR still open) — leave it; the poll
            # re-enqueues later. No transition, no trace, no spam.
            log.debug(
                "%s: %s no-op at %s (awaiting external event)",
                stage_name, ticket_id, ticket.state,
            )
            return
        ctx.service.transition(ticket_id, outcome.next_state, outcome.note)
        log.info("%s: %s -> %s", stage_name, ticket_id, outcome.next_state)
        # Best-effort push notification for human-attention states.
        if outcome.next_state in _TRIGGER_STATES:
            ticket = ctx.service.get(ticket_id)
            if ticket is not None:
                send_notification(ticket, outcome.next_state, outcome.note, ctx.settings)


class Worker:
    """In-process queue + consumer task, owned by the API service."""

    def __init__(self, ctx: StageContext) -> None:
        self.ctx = ctx
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

    def enqueue(self, ticket_id: str) -> None:
        self.queue.put_nowait(ticket_id)

    async def _run(self) -> None:
        while True:
            ticket_id = await self.queue.get()
            try:
                await process_ticket(ticket_id, self.ctx)
            except Exception:  # noqa: BLE001 — never let the consumer die
                log.exception("processing %s crashed", ticket_id)
            finally:
                self.queue.task_done()

    async def _poll_loop(self) -> None:
        """Lightweight merge poll: periodically re-enqueue in_review
        tickets so the merge stage re-checks the PR. mill has no
        scheduler; this timer exists solely for the external merge event."""
        interval = max(15, self.ctx.settings.merge_poll_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                for t in self.ctx.service.list(state=State.IN_REVIEW):
                    self.enqueue(t.id)
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("merge poll failed")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        for attr in ("_task", "_poll_task"):
            t = getattr(self, attr)
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        tracing.flush_tracing()

    def requeue_unfinished(self) -> None:
        """On startup, re-enqueue any ticket left mid-pipeline so a
        restart resumes work (idempotent: stages are re-entrant)."""
        for ticket in self.ctx.service.list():
            if ticket.state in STAGE_FOR_STATE:
                self.enqueue(ticket.id)
