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

from ..config import RepoConfig, get_repos_config
from ..langfuse_client import session_cost
from ..stages import StageContext, get_stage, stage_context_for
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
        # Retrying ticket still in backoff — don't open a trace or
        # run any stage; the poll loop re-enqueues later.
        if ticket.next_retry_at is not None and ticket.next_retry_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
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
                root_io = None
                if traced:
                    # One root span per stage call, named after the stage
                    # so Langfuse trace listings read "refine" / "implement"
                    # / "retrospect" instead of a generic "ticket". The
                    # session.id attribute still groups all of a ticket's
                    # stage traces together via Langfuse's session view.
                    root_io = es.enter_context(
                        tracing.start_ticket_root_span(ticket_id, stage_name, repo_config=ctx.repo_config)
                    )
                    # Attach a top-level "input" summary to the root span
                    # so Langfuse's trace view shows what was processed
                    # without drilling into children. Output is set
                    # below, once the stage returns.
                    root_io.set_input({
                        "ticket_id": ticket_id,
                        "title": ticket.title,
                        "state": ticket.state.value,
                        "stage": stage_name,
                        "source": ticket.source,
                        "priority": bool(getattr(ticket, "priority", False)),
                    })
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
                    root_io.set_output({
                        "next_state": outcome.next_state.value if outcome and outcome.next_state else None,
                        "note": (outcome.note or "") if outcome else "",
                        "no_op": bool(outcome and outcome.next_state == ticket.state),
                    })
        except asyncio.TimeoutError:
            timeout = ctx.settings.stage_timeout_overrides.get(
                stage_name, ctx.settings.stage_timeout_seconds
            )
            log.error(
                "%s: %s timed out after %ds — escalating to BLOCKED",
                stage_name, ticket_id, timeout,
            )
            note = f"stage {stage_name} timed out after {timeout}s"[:200]
            ctx.service.transition(ticket_id, State.BLOCKED, note=note)
            ticket = ctx.service.get(ticket_id)
            if ticket is not None:
                send_notification(ticket, State.BLOCKED, note, ctx.settings)
            return
        except NotImplementedError as e:
            log.warning(
                "%s: stub (%s) — chain paused at %s for %s",
                stage_name, e, ticket.state, ticket_id,
            )
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
                        stage_name, ticket_id, attempt, max_attempts, delay,
                    )
                    return
                # Retries exhausted — block.
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
                ticket_id, retry_attempt=0, last_transient_error=None, next_retry_at=None,
            )
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
            child_summaries.append({
                "id": child.id,
                "title": child.title,
                "state": child.state.value,
                "description": child_desc,
                "depends_on": TicketService._parse_depends_on(child),
            })

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
                epic_id, len(result.new_children),
            )
            result.decision = "keep_open"

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
        State.DONE: 0,                    # retrospect → CLOSED
        State.DELIVERABLE: 1,             # deliver opens the PR
        State.DOCUMENTING: 2,             # document → DELIVERABLE
        State.CODE_REVIEW: 3,             # review
        State.ADDRESSING_REVIEW: 4,       # merge stage replying to reviewer
        State.FIXING_CI: 5,               # ci_fix retries CI
        State.REBASING: 6,                # merge stage, rebase substep
        State.HUMAN_MR_APPROVAL: 7,       # merge polling (no-LLM)
        State.WAITING_AUTO_MERGE: 8,      # merge polling (no-LLM)
        State.IMPLEMENT_COMPLETE: 9,      # merge polling (no-LLM)
        State.READY: 10,                  # implement — fresh code work
        State.DRAFT: 11,                  # refine — earliest stage
        State.ASKED: 12,                  # answer — inquiry side-channel
    }

    @classmethod
    def _stage_rank(cls, ticket) -> int:
        if ticket is None:
            return cls._DEFAULT_STAGE_RANK
        return cls._STAGE_RANK.get(ticket.state, cls._DEFAULT_STAGE_RANK)

    def __init__(self, ctx: StageContext, run_registry: "RunRegistry | None" = None) -> None:
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
        self._health_task: asyncio.Task | None = None
        self._agent_check_task: asyncio.Task | None = None
        self._bc_check_task: asyncio.Task | None = None
        self._completeness_check_task: asyncio.Task | None = None
        self._ci_monitor_task: asyncio.Task | None = None
        self._test_gap_task: asyncio.Task | None = None
        self._survey_task: asyncio.Task | None = None
        self._env_sync_task: asyncio.Task | None = None
        self._cost_reconciliation_task: asyncio.Task | None = None
        self._langfuse_cleanup_task: asyncio.Task | None = None
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
        prio_rank = 0 if (ticket is not None and getattr(ticket, "priority", False)) else 1
        stage_rank = self._stage_rank(ticket)
        board_id = ticket.board_id if (ticket is not None and ticket.board_id) else self._DEFAULT_BOARD
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
                            ticket_id, popped_prio, popped_stage,
                            cur_prio, cur_stage,
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
                    ticket_id, before_state,
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

    def _check_progress(self, ticket_id: str, before, after, repo_config: RepoConfig | None = None) -> None:
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

    async def _run_periodic_pass_per_repo(
        self, label: str, runner_fn, interval: int,
    ) -> None:
        """Shared per-repo periodic pass loop.

        Multi-repo: each periodic agent fans out across all repos.
        One timer per agent type — iterates repos sequentially to avoid
        race conditions on the shared RunRegistry.

        When no repos are registered (single-repo mode), runs the pass
        once with ``repo_config=None`` for backward compatibility.

        Args:
            label: Pass identifier (``"audit"``, ``"agent_check"``).
            runner_fn: Callable accepting ``session_id=`` and ``repo_config=``
                       keywords that returns a result with a ``drafts_created``
                       field.
            interval: Seconds between passes.
        """
        initial = self._initial_delay(label, interval)
        await asyncio.sleep(initial)
        while True:
            session_id = tracing.make_session_id(label)
            repos = get_repos_config()
            repo_configs = list(repos.repos.values())
            if not repo_configs:
                # Single-repo / no repos.yaml: run once without repo_config.
                repo_configs = [None]  # type: ignore[list-item]
            for repo_config in repo_configs:
                run_id = None
                repo_label = repo_config.repo_id if repo_config else label
                try:
                    log.info(
                        "Starting periodic %s pass for repo %s",
                        label, repo_label,
                    )
                    if self.run_registry:
                        run_id = self.run_registry.start(
                            label,
                            repo_id=repo_config.repo_id if repo_config else "",
                        )
                    with tracing.start_ticket_root_span(session_id, label):
                        result = await asyncio.to_thread(
                            runner_fn,
                            session_id=session_id,
                            repo_config=repo_config,
                        )
                    log.info(
                        "%s pass (%s) completed, created %d draft(s)",
                        label.capitalize(), repo_label,
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
                    log.exception(
                        "%s poll failed for repo %s", label, repo_label,
                    )
                    if self.run_registry and run_id:
                        self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _run_periodic_pass(
        self, label: str, runner_fn, interval: int,
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
                with tracing.start_ticket_root_span(session_id, label):
                    result = await asyncio.to_thread(
                        runner_fn, session_id=session_id,
                    )
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
        ``MILL_TRACE_HEALTH_PERIODIC=true``.

        Multi-repo: fans out across all registered repos. When no repos
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
                        run_trace_health_check, repo_config=repo_config,
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

    async def _health_poll_loop(self) -> None:
        """Periodic health pass loop. Only runs when
        ``MILL_HEALTH_PERIODIC=true``."""
        from ..health_runner import run_health_pass
        settings = self.ctx.settings
        interval = max(60, settings.health_interval_seconds)
        await self._run_periodic_pass_per_repo("health", run_health_pass, interval)

    async def _test_gap_poll_loop(self) -> None:
        """Periodic test-gap pass loop. Only runs when
        ``MILL_TEST_GAP_PERIODIC=true``."""
        from ..test_gap_runner import run_test_gap_pass
        settings = self.ctx.settings
        interval = max(60, settings.test_gap_interval_seconds)
        await self._run_periodic_pass_per_repo("test-gap", run_test_gap_pass, interval)

    async def _langfuse_cleanup_poll_loop(self) -> None:
        """Periodic Langfuse trace cleanup: keeps each repo's project at
        most ``langfuse_cleanup_max_traces`` rows by deleting the oldest.

        Multi-repo: iterates all registered repos sequentially. Pure
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
                            "langfuse-cleanup: %s — deleted %d of %d traces "
                            "(cap %d)",
                            label, result.traces_deleted, result.traces_before,
                            settings.langfuse_cleanup_max_traces,
                        )
                except Exception:  # noqa: BLE001 — periodic sweep must not die
                    log.exception("langfuse-cleanup poll failed for %s", label)
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
            min_interval = max(60, min(rc.ci_monitor_interval_seconds for rc in repo_configs))

        await asyncio.sleep(self._initial_delay("ci_monitor", min_interval))
        while True:
            for rc in repo_configs:
                repo_label = rc.repo_id
                interval = max(60, rc.ci_monitor_interval_seconds)

                # Honour per-repo interval.
                now = time.time()
                if repo_label in last_polled and (now - last_polled[repo_label]) < interval:
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
                        except (json.JSONDecodeError, OSError):
                            state = {"seen": {}}
                    seen = state.setdefault("seen", {})

                    # 2. Prune entries older than TTL.
                    stale = [
                        key for key, val in seen.items()
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
                            f"CI failure: {wf_name} on "
                            f"{settings.forge_target_branch}"
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
                            if len(stripped) > 200_000:
                                stripped = stripped[-200_000:]
                            body_parts.append("```")
                            body_parts.append(stripped)
                            body_parts.append("```")

                        body = "\n".join(body_parts)

                        try:
                            service.create(
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
                    state_path.write_text(json.dumps(state), "utf-8")

                    log.info("CI monitor poll completed for repo %s", repo_label)
                except Exception:  # noqa: BLE001 — never let the poll die
                    log.exception("CI monitor poll failed for repo %s", repo_label)

                last_polled[repo_label] = time.time()

            await asyncio.sleep(min_interval)

    def start(self) -> None:
        if not self._tasks:
            # One consumer pool per repo, sized by repo.max_concurrency.
            # Each pool pulls only from its own per-board queue, so a
            # busy repo can't block another. The fallback "" queue
            # (tickets without a board_id) gets a single consumer.
            repos = get_repos_config()
            pool_sizes: list[tuple[str, int]] = []
            for rc in repos.repos.values():
                pool_sizes.append((rc.board_id, max(1, rc.max_concurrency)))
            # Always have a single default-queue consumer for legacy /
            # repo-less tickets — cheap insurance against drift.
            pool_sizes.append((self._DEFAULT_BOARD, 1))
            for board_id, n in pool_sizes:
                for _ in range(n):
                    self._tasks.append(
                        asyncio.create_task(self._run(board_id))
                    )
            log.info(
                "worker pool started: %s",
                ", ".join(
                    f"{bid or '<default>'}={n}" for bid, n in pool_sizes
                ),
            )
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())
        # Opt-in periodic audit
        if self.ctx.settings.audit_periodic and self._audit_task is None:
            from ..audit_runner import run_audit_pass
            self._audit_task = asyncio.create_task(
                self._run_periodic_pass_per_repo(
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
                self._run_periodic_pass_per_repo(
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
                self._run_periodic_pass_per_repo(
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
                self._run_periodic_pass_per_repo(
                    "completeness_check", run_completeness_check_pass,
                    max(60, self.ctx.settings.completeness_check_interval_seconds),
                )
            )
            log.info(
                "Periodic completeness-check enabled: interval %ds",
                self.ctx.settings.completeness_check_interval_seconds,
            )
        # Opt-in periodic Langfuse trace cleanup
        if (
            self.ctx.settings.langfuse_cleanup_periodic
            and self._langfuse_cleanup_task is None
        ):
            self._langfuse_cleanup_task = asyncio.create_task(
                self._langfuse_cleanup_poll_loop()
            )
            log.info(
                "Periodic Langfuse cleanup enabled: interval %ds, cap %d traces/project",
                self.ctx.settings.langfuse_cleanup_interval_seconds,
                self.ctx.settings.langfuse_cleanup_max_traces,
            )
        # CI monitor: enabled when any registered repo has ci_monitor_enabled=True.
        if self._ci_monitor_task is None:
            repos = get_repos_config()
            if any(rc.ci_monitor_enabled for rc in repos.repos.values()):
                self._ci_monitor_task = asyncio.create_task(
                    self._ci_monitor_poll_loop()
                )
                log.info("CI monitor enabled (per-repo config)")
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
                self._run_periodic_pass_per_repo(
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
                self._run_periodic_pass_per_repo(
                    "env-sync", run_env_sync_pass,
                    max(60, self.ctx.settings.env_sync_interval_seconds),
                )
            )
            log.info(
                "Periodic env-sync enabled: interval %ds",
                self.ctx.settings.env_sync_interval_seconds,
            )
        # Opt-in periodic cost-reconciliation
        if (
            self.ctx.settings.cost_reconciliation_periodic
            and self._cost_reconciliation_task is None
        ):
            from ..cost_reconciliation_runner import run_cost_reconciliation_pass
            self._cost_reconciliation_task = asyncio.create_task(
                self._run_periodic_pass(
                    "cost-reconciliation", run_cost_reconciliation_pass,
                    max(60, self.ctx.settings.cost_reconciliation_interval_seconds),
                )
            )
            log.info(
                "Periodic cost-reconciliation enabled: interval %ds",
                self.ctx.settings.cost_reconciliation_interval_seconds,
            )

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for attr in (
            "_poll_task", "_audit_task",
            "_trace_health_task", "_health_task", "_ci_monitor_task",
            "_agent_check_task", "_bc_check_task", "_completeness_check_task", "_test_gap_task", "_survey_task",
            "_env_sync_task", "_cost_reconciliation_task",
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
        restart resumes work (idempotent: stages are re-entrant).

        With per-repo DBs, fan out across every registered repo plus
        the default (legacy / repo-less) DB so nothing is missed.
        """
        from ..config import get_repos_config
        from ..core.service import TicketService

        boards: list[str] = [""]
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
