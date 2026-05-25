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

from ..langfuse_client import session_cost
from ..stages import StageContext, get_stage
from ..core.states import STAGE_FOR_STATE, State
from ..core.models import SourceKind
from ..notify import send_notification, _TRIGGER_STATES
from . import tracing
from .run_registry import RunRegistry

log = logging.getLogger("robotsix_mill.worker")

# DONE is NOT terminal — retrospect owns it (done -> closed). Only
# closed/errored/blocked stop the chain.
_TERMINAL = {State.CLOSED, State.ERRORED, State.BLOCKED}


async def process_ticket(ticket_id: str, ctx: StageContext, active_map: dict | None = None) -> None:
    """Drive one ticket through as many stages as possible, in order,
    until it reaches a terminal/waiting state or a stub stops the chain."""
    await _process_ticket_inner(ticket_id, ctx, active_map=active_map)


async def _process_ticket_inner(ticket_id: str, ctx: StageContext, active_map: dict | None = None) -> None:
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
                if active_map is not None:
                    active_map[ticket_id] = {
                        "stage": stage_name,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }
                try:
                    outcome = await asyncio.to_thread(
                        stage.run, ticket, ctx
                    )
                finally:
                    if active_map is not None:
                        active_map.pop(ticket_id, None)
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

    svc = TicketService(settings)
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
            child_summaries.append({
                "id": child.id,
                "title": child.title,
                "state": child.state.value,
                "description": child_desc,
                "depends_on": TicketService._parse_depends_on(child),
            })

        result = run_epic_status_agent(
            settings=settings,
            epic_title=epic.title,
            epic_description=epic_desc,
            children=child_summaries,
        )

        if result.decision == "close":
            svc.transition(epic_id, State.EPIC_CLOSED, note="[auto-closed] " + (result.note or ""))
            log.info("epic %s: agent decided close — transitioned to EPIC_CLOSED", epic_id)
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
                    epic_id, len(result.dep_updates),
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
            epic_id, operation, child_id,
        )
        return None
    if child.state != S.DRAFT:
        log.warning(
            "epic %s: %s — child %s is in state %s (not DRAFT), skipping",
            epic_id, operation, child_id, child.state.value,
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
                    epic_id, i,
                )
                continue
            title = child_spec.get("title", "")
            body = child_spec.get("body", "")
            if not isinstance(title, str) or not title.strip():
                log.warning(
                    "epic %s: new_children[%d] missing non-empty 'title', skipping",
                    epic_id, i,
                )
                continue
            if not isinstance(body, str) or not body.strip():
                log.warning(
                    "epic %s: new_children[%d] missing non-empty 'body', skipping",
                    epic_id, i,
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
                    epic_id, child.id, title,
                )
            except Exception:
                log.exception(
                    "epic %s: failed to create new child '%s'",
                    epic_id, title,
                )

    # --- child_rescopes ------------------------------------------------
    if result.child_rescopes:
        for child_id, updates in result.child_rescopes.items():
            if not isinstance(updates, dict):
                log.warning(
                    "epic %s: child_rescopes[%s] is not a dict, skipping",
                    epic_id, child_id,
                )
                continue
            new_title = updates.get("title")
            new_body = updates.get("body")
            has_title = isinstance(new_title, str) and new_title.strip()
            has_body = isinstance(new_body, str) and new_body.strip()
            if not has_title and not has_body:
                log.warning(
                    "epic %s: child_rescopes[%s] has no non-empty 'title' or 'body', skipping",
                    epic_id, child_id,
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
                        epic_id, child_id, new_title.strip(),
                    )
                if has_body:
                    new_hash = svc.workspace(child).write_description(new_body.strip())
                    svc.set_content_hash(child_id, new_hash)
                    log.info(
                        "epic %s: rescoped child %s body", epic_id, child_id,
                    )
            except Exception:
                log.exception(
                    "epic %s: failed to rescope child %s", epic_id, child_id,
                )

    # --- child_closures ------------------------------------------------
    if result.child_closures:
        for child_id in result.child_closures:
            if not isinstance(child_id, str) or not child_id.strip():
                log.warning(
                    "epic %s: child_closures entry %r is not a non-empty string, skipping",
                    epic_id, child_id,
                )
                continue
            child = _fetch_draft_child(svc, child_id, "closure", epic_id)
            if child is None:
                continue
            try:
                svc.transition(
                    child_id, S.CLOSED,
                    note="Obsoleted by epic re-evaluation after sibling merge",
                )
                log.info(
                    "epic %s: closed child %s (obsoleted by sibling merge)",
                    epic_id, child_id,
                )
            except Exception:
                log.exception(
                    "epic %s: failed to close child %s", epic_id, child_id,
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

    svc = TicketService(settings)
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
            ts = c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "unknown"
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
        existing_titles_lower = {
            child.title.strip().lower() for child in existing
        }

        new_titles: list[str] = []
        new_bodies: list[str] = []
        for title, body in zip(result.child_titles, result.child_bodies):
            if title.strip().lower() in existing_titles_lower:
                log.debug(
                    "epic %s: skipping duplicate child '%s'", epic_id, title
                )
                continue
            new_titles.append(title)
            new_bodies.append(body)

        if not new_titles:
            log.info(
                "epic %s: re-processed — no new children (all %d proposed "
                "were duplicates)", epic_id, len(result.child_titles),
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
            epic_id, len(created_ids), ", ".join(created_ids),
        )
    except Exception:
        log.exception("epic %s: re-processing failed", epic_id)


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
        self._bc_check_task: asyncio.Task | None = None
        self._completeness_check_task: asyncio.Task | None = None
        self._ci_monitor_task: asyncio.Task | None = None
        self._test_gap_task: asyncio.Task | None = None
        self._survey_task: asyncio.Task | None = None
        self._env_sync_task: asyncio.Task | None = None
        # ticket_id -> consecutive no-progress cycles in a traced stage
        self._stuck: dict[str, int] = {}
        # ids queued OR in-flight — dedupe so the same ticket is never
        # processed by two workers at once (the merge poll, emit, and
        # requeue can all enqueue the same id).
        self._pending: set[str] = set()
        # ticket_id -> {"stage": str, "started_at": str} while stage.run() is executing
        self._active: dict[str, dict] = {}

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
                await process_ticket(ticket_id, self.ctx, active_map=self._active)
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
                self._active.pop(ticket_id, None)
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
        initial = self._initial_delay(label, interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            try:
                log.info("Starting periodic %s pass", label)
                if self.run_registry:
                    run_id = self.run_registry.start(label)
                # runner_fn invokes pydantic-ai's ``agent.run_sync``,
                # which calls ``asyncio.run()`` internally and explodes
                # ("this event loop is already running") when invoked
                # from inside an async task. Offload to a worker thread
                # — same pattern stage handlers use.
                result = await asyncio.to_thread(runner_fn)
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
            await asyncio.sleep(interval)

    async def _trace_health_poll_loop(self) -> None:
        """Periodic trace-health check loop. Only runs when
        ``MILL_TRACE_HEALTH_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(3600, settings.trace_health_interval_seconds)
        initial = self._initial_delay("trace-health", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                log.info("Starting periodic trace-health check")
                from ..trace_health_runner import run_trace_health_check
                run_id = None
                if self.run_registry:
                    run_id = self.run_registry.start("trace-health")
                result = await asyncio.to_thread(run_trace_health_check)
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
            await asyncio.sleep(interval)

    async def _health_poll_loop(self) -> None:
        """Periodic health pass loop. Only runs when
        ``MILL_HEALTH_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(60, settings.health_interval_seconds)
        initial = self._initial_delay("health", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            try:
                log.info("Starting periodic health pass")
                if self.run_registry:
                    run_id = self.run_registry.start("health")
                from ..health_runner import run_health_pass
                result = await asyncio.to_thread(run_health_pass)
                log.info(
                    "Health pass completed, created %d draft(s)",
                    len(result.drafts_created),
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
                log.exception("health poll failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _test_gap_poll_loop(self) -> None:
        """Periodic test-gap pass loop. Only runs when
        ``MILL_TEST_GAP_PERIODIC=true``."""
        settings = self.ctx.settings
        interval = max(60, settings.test_gap_interval_seconds)
        initial = self._initial_delay("test-gap", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            try:
                log.info("Starting periodic test-gap pass")
                if self.run_registry:
                    run_id = self.run_registry.start("test-gap")
                from ..test_gap_runner import run_test_gap_pass
                result = await asyncio.to_thread(run_test_gap_pass)
                log.info(
                    "Test-gap pass completed, created %d draft(s)",
                    len(result.drafts_created),
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
                log.exception("test-gap poll failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

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
                        t.source == SourceKind.CI
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
                            title=title, description=body, source=SourceKind.CI,
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
        # Opt-in periodic bc-check
        if self.ctx.settings.bc_check_periodic and self._bc_check_task is None:
            from ..bc_check_runner import run_bc_check_pass
            self._bc_check_task = asyncio.create_task(
                self._run_periodic_pass(
                    "bc_check", run_bc_check_pass,
                    max(60, self.ctx.settings.bc_check_interval_seconds),
                )
            )
            log.info(
                "Periodic bc-check enabled: interval %ds",
                self.ctx.settings.bc_check_interval_seconds,
            )
        # Opt-in periodic completeness-check
        if self.ctx.settings.completeness_check_periodic and self._completeness_check_task is None:
            from ..completeness_check_runner import run_completeness_check_pass
            self._completeness_check_task = asyncio.create_task(
                self._run_periodic_pass(
                    "completeness_check", run_completeness_check_pass,
                    max(60, self.ctx.settings.completeness_check_interval_seconds),
                )
            )
            log.info(
                "Periodic completeness-check enabled: interval %ds",
                self.ctx.settings.completeness_check_interval_seconds,
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
        # Opt-in periodic survey
        if self.ctx.settings.survey_periodic and self._survey_task is None:
            from ..survey_runner import run_survey_pass
            self._survey_task = asyncio.create_task(
                self._run_periodic_pass(
                    "survey", run_survey_pass,
                    max(60, self.ctx.settings.survey_interval_seconds),
                )
            )
            log.info(
                "Periodic survey enabled: interval %ds",
                self.ctx.settings.survey_interval_seconds,
            )
        # Opt-in periodic env-sync
        if self.ctx.settings.env_sync_periodic and self._env_sync_task is None:
            from ..env_sync_runner import run_env_sync_pass
            self._env_sync_task = asyncio.create_task(
                self._run_periodic_pass(
                    "env-sync", run_env_sync_pass,
                    max(60, self.ctx.settings.env_sync_interval_seconds),
                )
            )
            log.info(
                "Periodic env-sync enabled: interval %ds",
                self.ctx.settings.env_sync_interval_seconds,
            )

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for attr in (
            "_poll_task", "_audit_task",
            "_trace_health_task", "_health_task", "_ci_monitor_task",
            "_agent_check_task", "_bc_check_task", "_completeness_check_task", "_test_gap_task", "_survey_task",
            "_env_sync_task",
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
