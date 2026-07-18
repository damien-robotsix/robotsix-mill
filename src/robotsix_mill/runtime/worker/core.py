"""Worker consumer — in-process queue, stage chaining, and lifecycle.

The ``Worker`` class assembles ``PeriodicPassesMixin`` and
``PollLoopsMixin`` via multiple inheritance to form the complete
event-driven consumer.  It owns ticket queuing (per-board priority
queues), consumer-pool lifecycle (start / stop / reconcile), and
drives the stage chain for each dequeued ticket.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...config import RepoConfig, get_repos_config
from ...langfuse.client import effective_cost, session_cost, session_traces
from ...stages import StageContext, get_stage
from ...core.states import STAGE_FOR_STATE, State
from ...core.models import TicketKind
from ...notify import send_notification

if TYPE_CHECKING:
    from ...core.service import TicketService
from .. import tracing
from ..run_registry import RunRegistry

from .epic import _EPIC_CHILD_TERMINAL
from .processing import (
    process_ticket,
    _spawn_epic_reeval,
)
from .periodic_passes import PeriodicPassesMixin
from .poll_loops import PollLoopsMixin

log = logging.getLogger("robotsix_mill.worker")

# States counting toward the per-repo in-flight-PR cap.
# A ticket in any of these states has an open PR / branch in flight.
# See _CAP_GATED_STATES for the states gated by the cap.
_IN_FLIGHT_PR_STATES: frozenset[State] = frozenset(
    {
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.WAITING_AUTO_MERGE,
        State.REBASING,
        State.FIXING_CI,
        State.ADDRESSING_REVIEW,
    }
)

# States blocked by the in-flight-PR cap — only READY (implement)
# and DRAFT (refine) are gated.  All other states (including every
# member of _IN_FLIGHT_PR_STATES) are ALWAYS processed regardless
# of the cap count.
_CAP_GATED_STATES: frozenset[State] = frozenset({State.READY, State.DRAFT})


# An in-flight-state ticket whose state has not advanced in this long is
# stuck — its PR is conflicting, was merged/closed without the ticket
# advancing, or the ticket lost its pr_url. It is no longer an *active*
# PR slot, so it must NOT keep counting toward the cap (that would freeze
# the board's fresh drafts/ready behind a phantom PR). The in-flight states
# are all AUTOMATED pipeline states that normally advance within one CI
# cycle (~minutes); 3 h of no movement means genuinely stuck. (Human-wait
# states like HUMAN_MR_APPROVAL are NOT in _IN_FLIGHT_PR_STATES, so a slow
# human review never trips this.)
_INFLIGHT_PR_STALE_SECONDS = 3 * 60 * 60


def _count_inflight_prs(service: "TicketService") -> int:
    """Count tickets ACTIVELY holding an in-flight PR slot on this board.

    A ticket counts only if it is in an :data:`_IN_FLIGHT_PR_STATES` state
    AND has advanced within :data:`_INFLIGHT_PR_STALE_SECONDS`. Tickets
    wedged in an in-flight state far longer than that are stuck (conflicting
    PR, merged-but-not-advanced, lost pr_url) — they are phantom slots, and
    counting them by raw state inflates the cap and freezes the board's
    fresh work indefinitely (observed live: the chat board froze at 4/3,
    one of the four stuck ~6 h with a conflicting PR, another with its
    pr_url lost). Fail-safe: a ticket whose ``updated_at`` is missing or
    unreadable is COUNTED, so a parsing edge case can never silently
    disable the cap."""
    now = datetime.now(timezone.utc)
    n = 0
    for t in service.list():
        if t.state not in _IN_FLIGHT_PR_STATES:
            continue
        updated = getattr(t, "updated_at", None)
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if (now - updated).total_seconds() > _INFLIGHT_PR_STALE_SECONDS:
                # stuck/stale → not an active slot; skip (don't gate fresh work)
                continue
        n += 1
    return n


class Worker(PeriodicPassesMixin, PollLoopsMixin):
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
        State.READY: 11,  # implement — fresh code work
        State.DRAFT: 12,  # refine — earliest stage
        State.ASKED: 13,  # answer — inquiry side-channel
    }
    # Every STAGE_FOR_STATE state MUST appear above: a missing entry falls
    # to _DEFAULT_STAGE_RANK (99) and is starved indefinitely on a busy
    # board — every newly arriving draft/ready outranks it forever (live
    # case: 4 resumed MAINTENANCE tickets sat 75+ min with zero pickup
    # while later drafts refined). Guarded by a registry test.

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
        self._tasks: dict[str, list[asyncio.Task]] = {}
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
        self._data_dir_gc_task: asyncio.Task | None = None
        self._langfuse_cleanup_task: asyncio.Task | None = None
        self._timeout_escalation_task: asyncio.Task | None = None
        self._meta_task: asyncio.Task | None = None
        self._run_health_task: asyncio.Task | None = None
        self._diagnostic_task: asyncio.Task[None] | None = None
        self._stale_branch_task: asyncio.Task | None = None
        self._orphaned_pr_check_task: asyncio.Task[None] | None = None
        self._db_maintenance_task: asyncio.Task | None = None
        self._sandbox_reaper_task: asyncio.Task[None] | None = None
        self._ci_debt_recheck_task: asyncio.Task[None] | None = None
        self._credit_balance_task: asyncio.Task[None] | None = None
        self._dependabot_ingest_task: asyncio.Task[None] | None = None
        self._requeue_task: asyncio.Task[None] | None = None
        # board_id -> per-repo bespoke supervisor task. The supervisor
        # itself owns each repo's per-bespoke child tasks; cancelling
        # the supervisor cancels its children.
        self._periodic_supervisor_tasks: dict[str, asyncio.Task[None]] = {}
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
        # Tickets whose priority_rank was temporarily demoted because
        # they hit the in-flight PR cap.  When such a ticket is popped
        # the pop-time priority correction must be skipped — the
        # demotion was intentional, not stale state.  Cleared when the
        # ticket is actually processed (cap check passed) or when an
        # explicit priority flip calls requeue_with_current_priority.
        self._cap_deferred: set[str] = set()
        # ticket_id -> {"stage": str, "started_at": str} while stage.run() is executing
        self._active: dict[str, dict] = {}
        # Global semaphore capping total concurrently-running stages across
        # all boards. Created in start() to bind to the running event loop.
        self._global_semaphore: asyncio.Semaphore | None = None

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
        self._cap_deferred.discard(ticket_id)
        self.enqueue(ticket_id)

    def _repo_config_for_ticket(self, ticket_id: str) -> RepoConfig | None:
        """Resolve the ``RepoConfig`` for *ticket_id* from its ``board_id``.

        Returns ``None`` when the ticket has no ``board_id`` or no
        matching repo is found.
        """
        try:
            from ...config import get_repos_config

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
        from ...core.service import TicketService

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
                    if ticket_id in self._cap_deferred:
                        # Priority rank was intentionally demoted by a
                        # prior cap deferral — skip correction so the
                        # demotion isn't immediately undone.
                        pass
                    elif (cur_prio, cur_stage) != (popped_prio, popped_stage):
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
                        continue

                # Resolve per-ticket repo_config from the ticket's board_id.
                ticket_repo_config = self._repo_config_for_ticket(ticket_id)
                per_ticket_ctx = StageContext(
                    settings=self.ctx.settings,
                    service=board_service,
                    repo_config=ticket_repo_config,
                )

                # --- In-flight PR cap ---
                if (
                    before_state in _CAP_GATED_STATES
                    and ticket_repo_config is not None
                    and ticket_repo_config.max_inflight_prs > 0
                ):
                    in_flight = _count_inflight_prs(board_service)
                    if in_flight >= ticket_repo_config.max_inflight_prs:
                        log.debug(
                            "repo %s at in-flight cap (%d/%d); deferring %s",
                            ticket_repo_config.repo_id,
                            in_flight,
                            ticket_repo_config.max_inflight_prs,
                            ticket_id,
                        )
                        self._pending.discard(ticket_id)
                        # Demote the priority rank so this ticket sorts
                        # behind non-priority merge-pipeline work
                        # (IMPLEMENT_COMPLETE etc.).  Without this a
                        # priority READY/DRAFT ticket deadlocks the board
                        # when the cap is saturated — priority dominates
                        # stage_rank, so it always sits at the queue head
                        # and the merge-poll tickets behind it never get
                        # a chance to drain a cap slot.
                        demoted_prio = max(1, popped_prio)
                        self._enqueue_seq += 1
                        self._pending.add(ticket_id)
                        self._cap_deferred.add(ticket_id)
                        queue.put_nowait(
                            (demoted_prio, popped_stage, self._enqueue_seq, ticket_id)
                        )
                        await asyncio.sleep(15)
                        # NOTE: do NOT call queue.task_done() here. `continue`
                        # still runs the `finally` block below, which calls
                        # task_done() exactly once per get(). Calling it here
                        # too double-counts the same get() → the queue raises
                        # ValueError("task_done() called too many times"),
                        # which propagates out of the while-loop and KILLS the
                        # board consumer. Every cap-gated board that deferred a
                        # ticket lost its consumer this way (silent board stall).
                        continue

                # The ticket passed the in-flight cap check (or isn't
                # cap-gated).  Clear any prior cap-deferral demotion so
                # the next enqueue (e.g. requeue from merge poll) uses
                # its real priority rank.
                self._cap_deferred.discard(ticket_id)

                # --- Global concurrency cap ---
                if self._global_semaphore is not None:
                    if self._global_semaphore.locked():
                        log.debug(
                            "global concurrency cap (%d) saturated; waiting for slot",
                            self.ctx.settings.max_global_concurrency,
                        )
                    async with self._global_semaphore:
                        await process_ticket(
                            ticket_id, per_ticket_ctx, active_map=self._active
                        )
                else:
                    await process_ticket(
                        ticket_id, per_ticket_ctx, active_map=self._active
                    )
                after = board_service.get(ticket_id)
                # _check_progress calls session_cost/session_traces, which
                # hit Langfuse via a synchronous httpx.Client (20s timeout).
                # Called directly this would block the whole event loop —
                # and every HTTP route mill serves — for up to 20s after
                # every single ticket-stage completion. Offload to a thread.
                await self._tracked_to_thread(
                    self._check_progress,
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
        actually close — this just ensures it gets the chance.

        Uses :meth:`~.TicketService.list_children_across_boards` so children
        on other boards (cross-repo epic) are visible to the orphan sweep.
        """
        children = svc.list_children_across_boards(epic.id)
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

        # --- trace-count / OpenRouter marginal-spend circuit breaker ---
        if (
            self.ctx.settings.max_traces_per_ticket > 0
            or self.ctx.settings.max_openrouter_marginal_usd_per_ticket > 0.0
        ):
            traces = session_traces(
                self.ctx.settings, ticket_id, repo_config=repo_config
            )
            if traces is not None:
                n = len(traces)
                # Sentinel -1 means "set baseline on next poll" — an
                # operator just resumed this ticket.  Capture the current
                # trace count as the baseline so pre-resume traces are
                # excluded from the cap, then skip the block.
                trace_baseline = (
                    getattr(ticket, "pre_redraft_trace_count", 0)
                    if ticket is not None
                    else 0
                )
                if trace_baseline == -1:
                    self.ctx.service.set_pre_redraft_trace_count(ticket_id, n)
                    # After setting the baseline, effective count is
                    # n - n == 0 — well under any cap, so skip the
                    # breaker for this poll.  Still compute OpenRouter
                    # cost for the note below (informational only).
                effective_n = max(0, n - trace_baseline) if trace_baseline > 0 else n
                openrouter_cost = sum(
                    t["cost"]
                    for t in traces
                    if "openrouter" in (t.get("model") or "").lower()
                )
                if trace_baseline == -1:
                    # Baseline just set — skip blocking on this poll.
                    pass
                elif (
                    self.ctx.settings.max_traces_per_ticket > 0
                    and effective_n > self.ctx.settings.max_traces_per_ticket
                ) or (
                    self.ctx.settings.max_openrouter_marginal_usd_per_ticket > 0.0
                    and openrouter_cost
                    > self.ctx.settings.max_openrouter_marginal_usd_per_ticket
                ):
                    note = (
                        f"Circuit breaker tripped: {effective_n} effective traces "
                        f"({n} total, "
                        f"baseline {trace_baseline}; "
                        f"limit {self.ctx.settings.max_traces_per_ticket}), "
                        f"OpenRouter spend ${openrouter_cost:.2f} "
                        f"(limit ${self.ctx.settings.max_openrouter_marginal_usd_per_ticket:.2f}). "
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
        from ...config import get_repos_config
        from ...core.service import TicketService

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
                        if t.kind == TicketKind.EPIC and t.state == State.EPIC_OPEN:
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

    def start(self) -> None:
        if not self._tasks:
            repos = get_repos_config()
            cap = max(1, self.ctx.settings.max_global_concurrency)
            self._global_semaphore = asyncio.Semaphore(cap)
            log.info("global concurrency cap: %d", cap)
            pool_sizes = [
                (rc.board_id, max(1, rc.max_concurrency)) for rc in repos.repos.values()
            ]
            pool_sizes.append((self._DEFAULT_BOARD, 1))
            pool_sizes.append((self._META_BOARD, 1))
            for board_id, n in pool_sizes:
                self._tasks[board_id] = []
                for _ in range(n):
                    self._tasks[board_id].append(
                        asyncio.create_task(self._run(board_id))
                    )
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
            "run-health",
            self._run_health_pass_loop,
            "_run_health_task",
            log_msg="Periodic run-health enabled: interval %ds",
            log_args=(self.ctx.settings.run_health_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "diagnostic",
            self._diagnostic_pass_loop,
            "_diagnostic_task",
            log_msg="Periodic diagnostic enabled: interval %ds",
            log_args=(self.ctx.settings.diagnostic_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "stale-branch-cleanup",
            self._stale_branch_cleanup_loop,
            "_stale_branch_task",
            log_msg="Periodic stale-branch cleanup enabled: interval %ds",
            log_args=(self.ctx.settings.stale_branch_cleanup_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "orphaned-pr-check",
            self._orphaned_pr_check_loop,
            "_orphaned_pr_check_task",
            log_msg="Periodic orphaned-PR check enabled: interval %ds",
            log_args=(self.ctx.settings.orphaned_pr_check_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "db-maintenance",
            self._db_maintenance_poll_loop,
            "_db_maintenance_task",
            log_msg=(
                "Periodic DB maintenance enabled: interval %ds, "
                "max_events_per_ticket=%d"
            ),
            log_args=(
                self.ctx.settings.db_maintenance_interval_seconds,
                self.ctx.settings.max_events_per_ticket,
            ),
        )
        self._start_poll_loop_pass(
            "sandbox-reaper",
            self._sandbox_reaper_loop,
            "_sandbox_reaper_task",
            log_msg="Periodic sandbox reaper enabled: interval %ds",
            log_args=(self.ctx.settings.sandbox_reaper_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "ci-debt-recheck",
            self._ci_debt_recheck_loop,
            "_ci_debt_recheck_task",
            log_msg="Periodic CI-debt recheck enabled: interval %ds",
            log_args=(self.ctx.settings.ci_debt_recheck_interval_seconds,),
        )
        self._start_poll_loop_pass(
            "dependabot-ingest",
            self._dependabot_ingest_poll_loop,
            "_dependabot_ingest_task",
            log_msg="Periodic Dependabot ingest enabled: interval %ds",
            log_args=(self.ctx.settings.dependabot_ingest_interval_seconds,),
        )

        # --- Credit balance poll (gated on its own flag, not _periodic) ---
        if (
            self.ctx.settings.low_credit_poll_enabled
            and self._credit_balance_task is None
        ):
            self._credit_balance_task = asyncio.create_task(
                self._credit_balance_poll_loop()
            )
            log.info(
                "Periodic credit-balance poll enabled: interval %ds, threshold $%.2f",
                self.ctx.settings.low_credit_poll_interval_seconds,
                self.ctx.settings.low_credit_threshold_usd,
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

    async def reconcile_consumers(self) -> None:
        """Spawn consumer tasks for newly registered repos and cancel
        consumers for deregistered ones.

        Called from POST /repos and DELETE /repos/{repo_id} so that
        runtime-registered boards are picked up immediately — without
        this a board registered after ``start()`` gets a queue (via the
        reconcile sweep's lazy ``_queue_for``) but no consumer tasks,
        and tickets sit in DRAFT forever.
        """
        repos = get_repos_config()
        current_boards = set(self._tasks.keys())
        desired_boards = {rc.board_id for rc in repos.repos.values()}
        desired_boards.add(self._DEFAULT_BOARD)
        desired_boards.add(self._META_BOARD)

        for board_id in sorted(desired_boards - current_boards):
            n = 1
            for rc in repos.repos.values():
                if rc.board_id == board_id:
                    n = max(1, rc.max_concurrency)
                    break
            self._tasks[board_id] = []
            for _ in range(n):
                self._tasks[board_id].append(asyncio.create_task(self._run(board_id)))
            log.info(
                "reconcile: spawned %d consumer(s) for board %r",
                n,
                board_id,
            )

        for board_id in sorted(current_boards - desired_boards):
            tasks = self._tasks.pop(board_id, [])
            for t in tasks:
                t.cancel()
            self.queues.pop(board_id, None)
            log.info(
                "reconcile: cancelled %d consumer(s) for deregistered board %r",
                len(tasks),
                board_id,
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
        tasks = [t for tasks_list in self._tasks.values() for t in tasks_list]
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
            "_data_dir_gc_task",
            "_langfuse_cleanup_task",
            "_timeout_escalation_task",
            "_meta_task",
            "_run_health_task",
            "_diagnostic_task",
            "_stale_branch_task",
            "_db_maintenance_task",
            "_sandbox_reaper_task",
            "_ci_debt_recheck_task",
            "_credit_balance_task",
            "_requeue_task",
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
        self._tasks = {}
        tracing.flush_tracing()

    def requeue_unfinished(self) -> None:
        """On startup, re-enqueue any ticket left mid-pipeline so a
        restart resumes work (idempotent: stages are re-entrant).

        Spawns a background drip-feed task that enqueues matching
        tickets in batches (with a pause between batches) to avoid
        saturating the freshly-booted event loop.  Returns immediately
        so lifespan startup is not blocked.
        """
        self._requeue_task = asyncio.create_task(self._requeue_unfinished_drip())

    async def _requeue_unfinished_drip(self) -> None:
        """Background coroutine: enumerate all boards, collect
        unfinished ticket ids, then enqueue them in rate-limited
        batches."""
        from ...config import get_repos_config
        from ...core.service import TicketService

        boards: list[str] = [self._META_BOARD]
        try:
            for rc in get_repos_config().repos.values():
                if rc.board_id and rc.board_id not in boards:
                    boards.append(rc.board_id)
        except Exception:
            pass

        # Collect all matching ticket ids first (the enumeration is
        # blocking svc.list() — see epic children 1-2 for moving this
        # off the event loop).
        to_enqueue: list[str] = []
        for board_id in boards:
            svc = TicketService(self.ctx.settings, board_id=board_id)
            try:
                for ticket in svc.list():
                    if ticket.state in STAGE_FOR_STATE:
                        to_enqueue.append(ticket.id)
            except Exception:
                log.exception(
                    "requeue_unfinished: failed to enumerate board %r",
                    board_id or "<default>",
                )

        # Drip-feed enqueues in batches with a pause between each batch.
        batch_size = max(1, self.ctx.settings.requeue_batch_size)
        pause = self.ctx.settings.requeue_batch_pause_seconds
        for i in range(0, len(to_enqueue), batch_size):
            batch = to_enqueue[i : i + batch_size]
            for tid in batch:
                self.enqueue(tid)
            # Pause between batches (skip after the last batch).
            if i + batch_size < len(to_enqueue):
                await asyncio.sleep(pause)
