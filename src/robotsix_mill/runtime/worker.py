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
import logging

from ..stages import StageContext, get_stage
from ..core.states import STAGE_FOR_STATE, State
from ..notify import send_notification, _TRIGGER_STATES
from . import tracing
from .run_registry import RunRegistry

log = logging.getLogger("robotsix_mill.worker")

# DONE is NOT terminal — retrospect owns it (done -> closed). Only
# closed/errored/blocked stop the chain.
_TERMINAL = {State.CLOSED, State.ERRORED, State.BLOCKED}


async def process_ticket(ticket_id: str, ctx: StageContext) -> None:
    """Drive one ticket through as many stages as possible, in order,
    until it reaches a terminal/waiting state or a stub stops the chain."""
    await _process_ticket_inner(ticket_id, ctx)


async def _process_ticket_inner(ticket_id: str, ctx: StageContext) -> None:
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

    def __init__(self, ctx: StageContext, run_registry: "RunRegistry | None" = None) -> None:
        self.ctx = ctx
        self.run_registry = run_registry
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        # pool of consumer tasks — tickets run concurrently, not serially
        self._tasks: list[asyncio.Task] = []
        self._poll_task: asyncio.Task | None = None
        self._audit_task: asyncio.Task | None = None
        self._scout_task: asyncio.Task | None = None
        self._trace_health_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        # ticket_id -> consecutive no-progress cycles in a traced stage
        self._stuck: dict[str, int] = {}
        # ids queued OR in-flight — dedupe so the same ticket is never
        # processed by two workers at once (the merge poll, emit, and
        # requeue can all enqueue the same id).
        self._pending: set[str] = set()

    def enqueue(self, ticket_id: str) -> None:
        # asyncio is single-threaded; enqueue is only ever called from
        # the loop thread, so this set check needs no lock.
        if ticket_id in self._pending:
            return
        self._pending.add(ticket_id)
        self.queue.put_nowait(ticket_id)

    async def _run(self) -> None:
        while True:
            ticket_id = await self.queue.get()
            try:
                before = self.ctx.service.get(ticket_id)
                before_state = before.state if before else None
                await process_ticket(ticket_id, self.ctx)
                after = self.ctx.service.get(ticket_id)
                self._check_progress(
                    ticket_id, before_state,
                    after.state if after else None,
                )
            except Exception:  # noqa: BLE001 — never let the consumer die
                log.exception("processing %s crashed", ticket_id)
            finally:
                # drop from in-flight FIRST so a re-enqueue (e.g. next
                # merge-poll cycle) is accepted again.
                self._pending.discard(ticket_id)
                self.queue.task_done()

    def _check_progress(self, ticket_id: str, before, after) -> None:
        """No-progress safety net. A ticket that keeps re-entering the
        same *model-driven* (traced) stage without ever advancing —
        runs interrupted before any checkpoint, or a churning stage —
        would otherwise be re-billed to the LLM on every requeue,
        silently. After ``max_stuck_cycles`` such cycles, escalate to
        BLOCKED (resumable) and notify. Poll stages (merge/deliver,
        traced=False) are exempt: in_review/rebasing legitimately waits
        on a PR or rebase cycle."""
        if after is None or after != before:
            self._stuck.pop(ticket_id, None)
            return
        stage_name = STAGE_FOR_STATE.get(after)
        if stage_name is None:
            return  # terminal / human-wait — not our concern
        if not getattr(get_stage(stage_name), "traced", True):
            return  # poll stage: same-state is by design, never block
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

        Originally this only re-enqueued in_review/rebasing for the
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
        no automated stage (e.g. awaiting_approval) are untouched —
        they correctly wait for a human."""
        interval = max(15, self.ctx.settings.merge_poll_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                for t in self.ctx.service.list():
                    if t.state in STAGE_FOR_STATE:
                        self.enqueue(t.id)
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("reconcile sweep failed")

    async def _audit_poll_loop(self) -> None:
        """Periodic audit pass loop. Only runs when
        ``MILL_AUDIT_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(60, settings.audit_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                log.info("Starting periodic audit pass")
                from ..audit_runner import run_audit_pass
                run_id = None
                if self.run_registry:
                    run_id = self.run_registry.start("audit")
                result = run_audit_pass()
                log.info(
                    "Audit pass completed, created %d draft(s)",
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    draft_ids = [d["id"] for d in result.drafts_created[:5]]
                    summary = (
                        f"Created {len(result.drafts_created)} drafts: "
                        f"{', '.join(draft_ids)}"
                        f"{'…' if len(result.drafts_created) > 5 else ''}"
                    )
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("audit poll failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))

    async def _scout_poll_loop(self) -> None:
        """Periodic scout pass loop. Only runs when
        ``MILL_SCOUT_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(60, settings.scout_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                log.info("Starting periodic scout pass")
                from ..scout_runner import run_scout_pass
                run_id = None
                if self.run_registry:
                    run_id = self.run_registry.start("scout")
                result = run_scout_pass()
                log.info(
                    "Scout pass completed, created %d draft(s)",
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    draft_ids = [d["id"] for d in result.drafts_created[:5]]
                    summary = (
                        f"Created {len(result.drafts_created)} drafts: "
                        f"{', '.join(draft_ids)}"
                        f"{'…' if len(result.drafts_created) > 5 else ''}"
                    )
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("scout poll failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))

    async def _trace_health_poll_loop(self) -> None:
        """Periodic trace-health check loop. Only runs when
        ``MILL_TRACE_HEALTH_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(3600, settings.trace_health_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                log.info("Starting periodic trace-health check")
                from ..trace_health_runner import run_trace_health_check
                run_id = None
                if self.run_registry:
                    run_id = self.run_registry.start("trace-health")
                result = run_trace_health_check()
                if result.draft_created:
                    log.info(
                        "Trace-health check: draft created — "
                        "%d/%d traces unsessioned",
                        result.unsessioned_count,
                        result.total_traces,
                    )
                else:
                    log.info(
                        "Trace-health check: no alert "
                        "(%d/%d traces unsessioned)",
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
                log.exception("trace-health poll failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))

    async def _health_poll_loop(self) -> None:
        """Periodic health pass loop. Only runs when
        ``MILL_HEALTH_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(60, settings.health_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                log.info("Starting periodic health pass")
                from ..health_runner import run_health_pass
                result = run_health_pass()
                log.info(
                    "Health pass completed, created %d draft(s)",
                    len(result.drafts_created),
                )
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("health poll failed")

    def start(self) -> None:
        if not self._tasks:
            n = max(1, self.ctx.settings.max_concurrency)
            self._tasks = [
                asyncio.create_task(self._run()) for _ in range(n)
            ]
            log.info("worker pool started: concurrency=%d", n)
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())
        # Opt-in periodic audit
        if self.ctx.settings.audit_periodic and self._audit_task is None:
            self._audit_task = asyncio.create_task(self._audit_poll_loop())
            log.info(
                "Periodic audit enabled: interval %ds",
                self.ctx.settings.audit_interval_seconds,
            )
        # Opt-in periodic scout
        if self.ctx.settings.scout_periodic and self._scout_task is None:
            self._scout_task = asyncio.create_task(self._scout_poll_loop())
            log.info(
                "Periodic scout enabled: interval %ds",
                self.ctx.settings.scout_interval_seconds,
            )
        # Opt-in periodic trace-health
        if self.ctx.settings.trace_health_periodic and self._trace_health_task is None:
            self._trace_health_task = asyncio.create_task(
                self._trace_health_poll_loop()
            )
            log.info(
                "Periodic trace-health enabled: interval %ds",
                self.ctx.settings.trace_health_interval_seconds,
            )
        # Opt-in periodic health
        if self.ctx.settings.health_periodic and self._health_task is None:
            self._health_task = asyncio.create_task(self._health_poll_loop())
            log.info(
                "Periodic health enabled: interval %ds",
                self.ctx.settings.health_interval_seconds,
            )

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for attr in (
            "_poll_task", "_audit_task", "_scout_task",
            "_trace_health_task", "_health_task",
        ):
            t = getattr(self, attr)
            if t is not None:
                tasks.append(t)
                setattr(self, attr, None)
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
        restart resumes work (idempotent: stages are re-entrant)."""
        for ticket in self.ctx.service.list():
            if ticket.state in STAGE_FOR_STATE:
                self.enqueue(ticket.id)
