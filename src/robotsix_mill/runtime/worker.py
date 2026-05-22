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

from ..langfuse_client import session_cost
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
        try:
            with contextlib.ExitStack() as es:
                if traced:
                    # One root span per stage call, named after the stage
                    # so Langfuse trace listings read "refine" / "implement"
                    # / "retrospect" instead of a generic "ticket". The
                    # session.id attribute still groups all of a ticket's
                    # stage traces together via Langfuse's session view.
                    es.enter_context(
                        tracing.start_ticket_root_span(ticket_id, stage_name)
                    )
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
        self._trace_health_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._agent_check_task: asyncio.Task | None = None
        self._ci_monitor_task: asyncio.Task | None = None
        self._test_gap_task: asyncio.Task | None = None
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
        traced=False) are exempt: human_mr_approval/rebasing legitimately waits
        on a PR or rebase cycle."""

        # --- dollar-cap safety net: check before the state-change
        # early-return so the cap fires even when the ticket is making
        # forward progress (cost accumulates across all stages). ---
        if self.ctx.settings.max_spend_usd_per_ticket > 0.0:
            cost = session_cost(self.ctx.settings, ticket_id)
            if cost > self.ctx.settings.max_spend_usd_per_ticket:
                note = (
                    f"Cost cap exceeded: ${cost:.2f} spent "
                    f"(limit ${self.ctx.settings.max_spend_usd_per_ticket:.2f}). "
                    "Escalated to BLOCKED to stop further LLM billing. "
                    "Use resume-blocked to override and continue."
                )
                log.error("%s: %s", ticket_id, note)
                self.ctx.service.transition(
                    ticket_id, State.BLOCKED, note=note[:200]
                )
                self._stuck.pop(ticket_id, None)
                t = self.ctx.service.get(ticket_id)
                if t is not None:
                    send_notification(
                        t, State.BLOCKED, note[:200], self.ctx.settings
                    )
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
        interval = max(15, self.ctx.settings.merge_poll_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                for t in self.ctx.service.list():
                    if t.state not in STAGE_FOR_STATE:
                        continue
                    # Dep-gated tickets are skipped at the source —
                    # enqueuing them would just trigger _process_ticket_inner
                    # to short-circuit (no trace, no work), but every sweep
                    # would still consume a queue slot + a service.get +
                    # an unmet check. Cheaper to filter here.
                    if self.ctx.service.unmet_dependencies(t):
                        continue
                    self.enqueue(t.id)
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("reconcile sweep failed")

    async def _run_periodic_pass(
        self, label: str, runner_fn, interval: int,
    ) -> None:
        """Shared periodic pass loop for audit, agent-check, etc.

        Args:
            label: Pass identifier (``"audit"``, ``"agent_check"``).
            runner_fn: Zero-arg callable that returns a result with a
                       ``drafts_created`` field.
            interval: Seconds between passes.
        """
        while True:
            await asyncio.sleep(interval)
            run_id = None
            try:
                log.info("Starting periodic %s pass", label)
                if self.run_registry:
                    run_id = self.run_registry.start(label)
                result = runner_fn()
                log.info(
                    "%s pass completed, created %d draft(s)",
                    label.capitalize(), len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    draft_ids = [
                        d["id"] for d in result.drafts_created[:5]
                    ]
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

    async def _test_gap_poll_loop(self) -> None:
        """Periodic test-gap pass loop. Only runs when
        ``MILL_TEST_GAP_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(60, settings.test_gap_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                log.info("Starting periodic test-gap pass")
                from ..test_gap_runner import run_test_gap_pass
                result = run_test_gap_pass()
                log.info(
                    "Test-gap pass completed, created %d draft(s)",
                    len(result.drafts_created),
                )
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("test-gap poll failed")

    async def _ci_monitor_poll_loop(self) -> None:
        """Periodic CI monitor poll: watch the forge target branch for
        completed workflow-run failures and file a ``source="ci"`` draft
        for each new one.  Only runs when ``MILL_CI_MONITOR_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(60, settings.ci_monitor_interval_seconds)
        state_path = settings.ci_monitor_memory_path
        ttl_seconds = 30 * 86400  # 30 days

        # ANSI strip for log text (same pattern as forge/github.py).
        _ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        while True:
            try:
                log.info("CI monitor poll starting")
                # 1. Load dedup state.
                state: dict = {"seen": {}}
                if state_path.exists():
                    try:
                        state = json.loads(state_path.read_text("utf-8"))
                    except (json.JSONDecodeError, OSError):
                        state = {"seen": {}}
                seen = state.setdefault("seen", {})

                # 2. Prune entries older than TTL.
                now = int(time.time())
                stale = [
                    key for key, val in seen.items()
                    if isinstance(val, (int, float)) and (now - val) > ttl_seconds
                ]
                for key in stale:
                    del seen[key]

                # 3. List completed workflow runs on the target branch.
                from ..forge import get_forge
                forge = get_forge(settings)
                runs = forge.list_workflow_runs(
                    branch=settings.forge_target_branch,
                )

                # 4. Only the LATEST run per workflow reflects current
                # state (the GitHub API returns runs newest-first). Take
                # one run per workflow_id and act only on that — never
                # backfill every historical failed run (that filed one
                # ticket per commit -> board flood).
                latest_by_wf: dict = {}
                for run in runs:
                    wf = run.get("workflow_id")
                    if wf is not None and wf not in latest_by_wf:
                        latest_by_wf[wf] = run

                existing = self.ctx.service.list()

                for wf, run in latest_by_wf.items():
                    if run.get("conclusion") != "failure":
                        continue

                    wf_name = run.get("name", "unknown")
                    run_id_val = run.get("id")
                    title = (
                        f"CI failure: {wf_name} on "
                        f"{settings.forge_target_branch}"
                    )

                    # One OPEN ci ticket per workflow: if a non-terminal
                    # source=ci ticket with this title already exists,
                    # don't duplicate (the recurring failure is already
                    # being worked).
                    if any(
                        t.source == "ci"
                        and t.title == title
                        and t.state.value not in ("closed", "done")
                        for t in existing
                    ):
                        continue

                    # Also avoid re-filing for the exact same failing
                    # commit (e.g. a prior ticket was closed but CI is
                    # still red at that sha).
                    key = f"{wf}:{run.get('head_sha')}"
                    if key in seen:
                        continue
                    log.info(
                        "CI monitor: new failure — %s (run %s) on %s",
                        wf_name, run_id_val, settings.forge_target_branch,
                    )

                    # Fetch job logs.
                    logs = ""
                    try:
                        logs = forge.fetch_workflow_job_logs(
                            run_id=run_id_val
                        )
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
                        # Cap total body log text at ~200 KB for the
                        # draft description (sanity limit).
                        if len(stripped) > 200_000:
                            stripped = stripped[-200_000:]
                        body_parts.append("```")
                        body_parts.append(stripped)
                        body_parts.append("```")

                    title = f"CI failure: {wf_name} on {settings.forge_target_branch}"
                    body = "\n".join(body_parts)

                    try:
                        self.ctx.service.create(
                            title=title, description=body, source="ci",
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
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(state), "utf-8")

                log.info("CI monitor poll completed")
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("CI monitor poll failed")
            await asyncio.sleep(interval)

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
            from ..audit_runner import run_audit_pass
            self._audit_task = asyncio.create_task(
                self._run_periodic_pass(
                    "audit", run_audit_pass,
                    max(60, self.ctx.settings.audit_interval_seconds),
                )
            )
            log.info(
                "Periodic audit enabled: interval %ds",
                self.ctx.settings.audit_interval_seconds,
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
        # Opt-in periodic agent-check
        if (
            self.ctx.settings.agent_check_periodic
            and self._agent_check_task is None
        ):
            from ..agent_check_runner import run_agent_check_pass
            self._agent_check_task = asyncio.create_task(
                self._run_periodic_pass(
                    "agent_check", run_agent_check_pass,
                    max(60, self.ctx.settings.agent_check_interval_seconds),
                )
            )
            log.info(
                "Periodic agent-check enabled: interval %ds",
                self.ctx.settings.agent_check_interval_seconds,
            )
        # Opt-in CI monitor
        if self.ctx.settings.ci_monitor_periodic and self._ci_monitor_task is None:
            self._ci_monitor_task = asyncio.create_task(
                self._ci_monitor_poll_loop()
            )
            log.info(
                "CI monitor enabled: interval %ds",
                self.ctx.settings.ci_monitor_interval_seconds,
            )
        # Opt-in periodic test-gap
        if self.ctx.settings.test_gap_periodic and self._test_gap_task is None:
            self._test_gap_task = asyncio.create_task(self._test_gap_poll_loop())
            log.info(
                "Periodic test-gap enabled: interval %ds",
                self.ctx.settings.test_gap_interval_seconds,
            )

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for attr in (
            "_poll_task", "_audit_task",
            "_trace_health_task", "_health_task", "_ci_monitor_task",
            "_agent_check_task", "_test_gap_task",
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
