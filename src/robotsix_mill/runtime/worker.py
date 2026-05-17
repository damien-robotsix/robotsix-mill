"""Event-driven worker. No scheduler.

A ticket is enqueued the moment it is emitted (or transitions into an
actionable state). The worker pulls it and **chains** stages —
``draft → … → done`` — until it hits a terminal state, a stub, or an
error. One worker, sequential, for v1.
"""

from __future__ import annotations

import asyncio
import logging

from ..stages import StageContext, get_stage
from ..core.states import STAGE_FOR_STATE, State

log = logging.getLogger("robotsix_mill.worker")

_TERMINAL = {State.DONE, State.FAILED, State.BLOCKED}


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
            log.debug("no stage for state %s; pausing %s", ticket.state, ticket_id)
            return
        stage = get_stage(stage_name)
        try:
            # stage.run is sync (LLM/tool calls) — keep the loop responsive
            outcome = await asyncio.to_thread(stage.run, ticket, ctx)
        except NotImplementedError as e:
            log.warning(
                "%s: stub (%s) — chain paused at %s for %s",
                stage_name, e, ticket.state, ticket_id,
            )
            return
        except Exception as e:  # noqa: BLE001 — any failure fails the ticket
            log.exception("%s: %s failed", stage_name, ticket_id)
            ctx.service.transition(ticket_id, State.FAILED, note=repr(e)[:200])
            return
        ctx.service.transition(ticket_id, outcome.next_state, outcome.note)
        log.info("%s: %s -> %s", stage_name, ticket_id, outcome.next_state)


class Worker:
    """In-process queue + consumer task, owned by the API service."""

    def __init__(self, ctx: StageContext) -> None:
        self.ctx = ctx
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

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

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def requeue_unfinished(self) -> None:
        """On startup, re-enqueue any ticket left mid-pipeline so a
        restart resumes work (idempotent: stages are re-entrant)."""
        for ticket in self.ctx.service.list():
            if ticket.state in STAGE_FOR_STATE:
                self.enqueue(ticket.id)
