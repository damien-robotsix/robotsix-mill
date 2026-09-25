"""Blocked auto-resume runner — retries BLOCKED tickets whose block note says
the condition is resumable, once, after a cooldown.

A BLOCKED ticket waits for a human ``resume-blocked``.  Live (7 days to
2026-08-29) 77 blocks landed on 56 tickets and 31 were resumed by hand;
the notes were overwhelmingly infrastructure or budget shaped — "agent
error — resumable" (provider output retries, session limit), "ci fix agent
could not turn CI green within its iteration budget", stage timeouts,
"clone missing — resumable", "pr_urls.json corrupted — resumable".  The
operator's playbook for every one of them was: wait a bit, click resume.
This pass does that click, deterministically and bounded:

* only the LATEST ``BLOCKED`` history note decides (same rule as the
  upstream-CI recovery runner), and only when it matches one of
  ``settings.blocked_auto_resume_patterns`` and none of the hard excludes
  (spec-fingerprint blocks need a description change; upstream-CI parks
  have their own runner);
* only after the ticket has been BLOCKED for
  ``blocked_auto_resume_cooldown_seconds`` — an immediate retry of a
  provider failure just fails again;
* at most ``blocked_auto_resume_max_per_ticket`` times per ticket, counted
  from the ``[auto-resume`` comments the resume itself leaves behind, so
  a ticket that keeps re-blocking ends up with a human exactly as before.

No LLM, no forge calls: history reads + ``resume_blocked``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...core.models import TicketEvent
from ...core.service import TicketService
from ...core.states import State
from ...stages.ci_fix_helpers import UPSTREAM_CI_BLOCK_MARKER
from .timeout_escalation_runner import _boards_to_scan

if TYPE_CHECKING:
    from ...config import Settings

log = logging.getLogger("robotsix_mill.blocked_auto_resume")

AUTO_RESUME_MARKER = "[auto-resume"

# Leading marker of the comment left on every ticket escalated as part of a
# dependency deadlock.  It doubles as the idempotency key: a member that
# already carries a ``CYCLE_MARKER`` comment is not re-reported, so repeated
# passes over a still-unbroken cycle stay silent.
CYCLE_MARKER = "[cycle-detected]"

# Never auto-resumed regardless of the pattern list: these need a human or
# another pass.
_HARD_EXCLUDES = (
    "spec unchanged",  # implement fingerprint guard — description must change
    UPSTREAM_CI_BLOCK_MARKER,  # upstream_ci_recovery_runner owns these
    "refusing to close",  # retrospect: PRs not merged
    "not merged",
)


def _latest_blocked_event(service: TicketService, ticket_id: str) -> TicketEvent | None:
    events: list[TicketEvent] = service.history(ticket_id, order="desc")
    for ev in events:
        if ev.state is State.BLOCKED:
            return ev
    return None


def _matches(note: str, patterns: list[str]) -> bool:
    head = note[:400]
    if any(x.lower() in head.lower() for x in _HARD_EXCLUDES):
        return False
    for pat in patterns:
        try:
            if re.search(pat, head, re.IGNORECASE):
                return True
        except re.error:
            log.warning("blocked_auto_resume: invalid pattern %r ignored", pat)
    return False


def _prior_auto_resumes(service: TicketService, ticket_id: str) -> int:
    comments: list[Any] = list(service.list_comments(ticket_id) or [])
    return len(
        [
            c
            for c in comments
            if str(getattr(c, "body", "") or "").startswith(AUTO_RESUME_MARKER)
        ]
    )


def _age_seconds(at: datetime, now: datetime) -> float:
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (now - at).total_seconds()


def _parse_edge_ids(raw: Any) -> list[str]:
    """Parse a ``depends_on``/``unblocks`` JSON payload into a list of ids.

    Returns ``[]`` for a null/blank field, malformed JSON, or a non-list
    payload; drops non-string edges — dependency wiring must never crash the
    pass.
    """
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError, TypeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [e for e in parsed if isinstance(e, str)]


def _build_dependency_graph(tickets: list[Any]) -> dict[str, set[str]]:
    """Directed "waits-for" graph over *tickets*, restricted to their own ids.

    An edge ``id -> dep`` means *id* waits for *dep* to finish: either *id*
    declares ``depends_on`` *dep*, or *dep* declares ``unblocks`` *id* (the
    inverse edge a dependency park leaves on the solver).  Edges pointing
    outside the BLOCKED set are ignored — a cycle can only exist among
    tickets that are all still blocked.
    """
    ids = {t.id for t in tickets}
    graph: dict[str, set[str]] = {t.id: set() for t in tickets}
    for t in tickets:
        for dep in _parse_edge_ids(getattr(t, "depends_on", None)):
            if dep in ids:
                graph[t.id].add(dep)
        for target in _parse_edge_ids(getattr(t, "unblocks", None)):
            if target in ids:
                graph[target].add(t.id)
    return graph


def _dfs_finish_order(graph: dict[str, set[str]]) -> list[str]:
    """Iterative post-order (finish times) over *graph* — Kosaraju pass 1."""
    order: list[str] = []
    seen: set[str] = set()
    for start, neighbours in graph.items():
        if start in seen:
            continue
        seen.add(start)
        stack: list[tuple[str, Any]] = [(start, iter(neighbours))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, iter(graph.get(nxt, set()))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()
    return order


def _transpose(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    """Reverse every edge of *graph* — Kosaraju pass 2 uses the transpose."""
    t: dict[str, set[str]] = {n: set() for n in graph}
    for node, deps in graph.items():
        for dep in deps:
            t.setdefault(dep, set()).add(node)
    return t


def _collect_component(
    transposed: dict[str, set[str]], start: str, assigned: set[str]
) -> list[str]:
    """Iterative DFS over *transposed* collecting one SCC rooted at *start*."""
    comp: list[str] = []
    stack = [start]
    assigned.add(start)
    while stack:
        node = stack.pop()
        comp.append(node)
        for nxt in transposed.get(node, set()):
            if nxt not in assigned:
                assigned.add(nxt)
                stack.append(nxt)
    return comp


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Kosaraju's algorithm — all SCCs of *graph* (iterative, stack-safe)."""
    order = _dfs_finish_order(graph)
    transposed = _transpose(graph)
    assigned: set[str] = set()
    components: list[list[str]] = []
    for node in reversed(order):
        if node in assigned:
            continue
        components.append(_collect_component(transposed, node, assigned))
    return components


def _already_cycle_reported(service: TicketService, ticket_id: str) -> bool:
    comments: list[Any] = list(service.list_comments(ticket_id) or [])
    return any(
        str(getattr(c, "body", "") or "").startswith(CYCLE_MARKER) for c in comments
    )


def _detect_and_escalate_cycles(
    service: TicketService, blocked: list[Any], counts: dict[str, int]
) -> set[str]:
    """Find SCCs of size > 1 among *blocked* and escalate each unreported
    member with a ``CYCLE_MARKER`` comment naming the cycle path.

    Returns the set of *all* member ids (reported or not) so the caller can
    skip them: a ticket stuck in a dependency deadlock must be escalated for
    manual intervention, not counted as ``not_matched`` (nor auto-resumed).
    Bumps ``counts['cycles_escalated']`` once per newly-escalated ticket, so
    repeated passes over an unbroken cycle report zero.
    """
    graph = _build_dependency_graph(blocked)
    members: set[str] = set()
    for comp in _strongly_connected_components(graph):
        if len(comp) < 2:
            continue
        members.update(comp)
        path = " → ".join(sorted(comp))
        for tid in comp:
            if _already_cycle_reported(service, tid):
                continue
            note = (
                f"{CYCLE_MARKER} dependency deadlock: this ticket is in a "
                f"strongly-connected cycle of {len(comp)} BLOCKED tickets "
                f"({path}). Auto-resume cannot break it — needs manual "
                "intervention: break one edge or close a member. See "
                "docs/cycles.md."
            )
            try:
                service.add_comment(tid, note, author="blocked-auto-resume")
            except Exception:
                log.exception("blocked_auto_resume: %s cycle escalation failed", tid)
                continue
            counts["cycles_escalated"] += 1
        log.warning("blocked_auto_resume: dependency cycle detected among %s", path)
    return members


def _resume_one(
    service: TicketService,
    t: Any,
    *,
    patterns: list[str],
    cooldown: int,
    max_per: int,
    now: datetime,
) -> str:
    """Classify one BLOCKED ticket and, if eligible, resume it.

    Returns the ``counts`` key to bump — ``"not_matched"``, ``"cooling"``,
    ``"budget_exhausted"`` or ``"resumed"`` — or ``""`` when the resume call
    itself failed (already logged; nothing to count).
    """
    ev = _latest_blocked_event(service, t.id)
    if ev is None or not t.blocked_from:
        return "not_matched"
    note = ev.note or ""
    if not _matches(note, patterns):
        return "not_matched"
    age = _age_seconds(ev.at, now)
    if age < cooldown:
        return "cooling"
    prior = _prior_auto_resumes(service, t.id)
    if prior >= max_per:
        return "budget_exhausted"
    resume_note = (
        f"{AUTO_RESUME_MARKER} {prior + 1}/{max_per}] automatic retry after "
        f"{int(age // 60)} min BLOCKED (back to {t.blocked_from}); "
        f"block note was: {note[:200]}"
    )
    try:
        service.resume_blocked(t.id, note=resume_note)
    except Exception:
        log.exception("blocked_auto_resume: %s resume failed", t.id)
        return ""
    log.info(
        "blocked_auto_resume: %s resumed to %s after %d min (%d/%d): %s",
        t.id,
        t.blocked_from,
        int(age // 60),
        prior + 1,
        max_per,
        note[:120],
    )
    return "resumed"


def run_blocked_auto_resume(
    settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """One auto-resume pass over every board's BLOCKED tickets.

    Returns counts: ``resumed``, ``cooling`` (matched, not old enough yet),
    ``budget_exhausted`` (matched, already auto-resumed the maximum number
    of times), ``not_matched``, ``cycles_escalated`` (BLOCKED tickets newly
    escalated because they sit in a dependency deadlock).
    """
    now = now or datetime.now(UTC)
    cooldown = max(0, int(settings.blocked_auto_resume_cooldown_seconds))
    max_per = max(0, int(settings.blocked_auto_resume_max_per_ticket))
    patterns = list(settings.blocked_auto_resume_patterns or [])
    counts = {
        "resumed": 0,
        "cooling": 0,
        "budget_exhausted": 0,
        "not_matched": 0,
        "cycles_escalated": 0,
    }
    if not patterns or max_per == 0:
        return counts
    for board_id in _boards_to_scan(settings):
        service = TicketService(settings, board_id=board_id)
        try:
            blocked = service.list(state=State.BLOCKED)
        except Exception:
            log.exception("blocked_auto_resume: board=%r list failed", board_id)
            continue
        # Escalate dependency deadlocks first, then skip their members: a
        # ticket stuck in a cycle must be surfaced for manual intervention,
        # never auto-resumed nor buried in ``not_matched``.
        cycle_members = _detect_and_escalate_cycles(service, blocked, counts)
        for t in blocked:
            if t.id in cycle_members:
                continue
            key = _resume_one(
                service,
                t,
                patterns=patterns,
                cooldown=cooldown,
                max_per=max_per,
                now=now,
            )
            if key:
                counts[key] += 1
    return counts
