"""State-transition surface of :class:`TicketService` (``_TransitionMixin``)."""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ...config import Settings
from ...vcs import git_ops
from ..db import retry_on_db_full
from ..models import (
    Comment,
    SourceKind,
    Ticket,
)
from ..states import State, can_transition
from ..workspace import (
    Workspace,
    clear_spawn_exhaustion_marker,
    read_counter,
    read_spawn_exhaustion_marker,
)
from ._base import _ServiceBase
from ._comments import _sanitize_log_value
from ._helpers import (
    TransitionError,
    _get_ticket,
    _make_event,
    _parse_depends_on_str,
    verify_merge_before_done,
)

log = logging.getLogger("robotsix_mill.service")


# States a ticket can only reach by the implement stage having actually
# produced deliverable work. Reaching any of them proves the spawn budget
# was spent productively, so it is returned to full.
_IMPLEMENT_PROGRESS_STATES: frozenset[State] = frozenset(
    {
        State.CODE_REVIEW,
        State.DOCUMENTING,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
    }
)


def _reset_implement_spawn_counter(ws: Workspace) -> None:
    """Clear ``artifacts/implement_spawn_count`` after productive work.

    The counter caps implement-stage invocations so a ticket looping
    through BLOCKED→READY→BLOCKED cannot burn unbounded LLM quota. But
    nothing reset it on success, so it was monotonic across the ticket's
    whole life: a ticket got three implement invocations *ever*, and a
    fourth pass — an entirely normal outcome after review feedback —
    dead-ended it at ``spawn limit reached (3/3)`` no matter how much
    genuine progress it had made. That became the largest BLOCKED class
    on the live board, on tickets whose own summaries reported every gate
    passing.

    Resetting here does not weaken the loop protections: the
    implement↔review ping-pong is separately bounded by
    ``ticket.implement_cycles`` against ``max_implement_review_cycles``,
    and an unproductive ticket never reaches these states at all, so its
    budget still runs out.

    Best-effort and silent when absent.
    """
    with contextlib.suppress(OSError):
        (ws.artifacts_dir / "implement_spawn_count").unlink(missing_ok=True)
    # Also clear the recurrence marker — a reset granted by genuine
    # implement progress (or an explicit operator rework request)
    # forgets the repeat-exhaustion history so the next exhaustion
    # starts a fresh first-exhaustion cycle.
    clear_spawn_exhaustion_marker(ws)
    # Also clear the in-flight spawn marker and abort log so a
    # resumed ticket starts with a clean spawn-state ledger — a stale
    # in-flight marker would otherwise be absorbed as a "killed by
    # shutdown" abort on the next preflight even though the operator
    # deliberately reset the budget.
    with contextlib.suppress(OSError):
        (ws.artifacts_dir / "implement_spawn_state.json").unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        (ws.artifacts_dir / "implement_spawn_aborts.jsonl").unlink(missing_ok=True)


def _reset_tripped_ci_fix_guards(ws: Workspace, settings: Settings) -> list[str]:
    """Reset the ci_fix guard counters that are *at or above* their ceiling.

    Two counters bound the merge-side CI loop, both stored in the
    ticket's artifacts dir and both compared against a ceiling BEFORE
    the ci-fix agent is allowed to run:

    * ``ci_identical_failure_count.txt`` vs ``ci_fix_max_identical_failures``
      — the same CI failure fingerprint repeating without progress.
    * ``auto_fix_cycles.txt`` vs ``auto_fix_max_cycles`` — the combined
      rebase+ci_fix dispatch ceiling.

    Nothing ever cleared them on an operator resume, which made both
    guards *terminal*: the gate re-evaluates before the agent phase, so
    a resumed ticket re-blocks on the very next poll with the counter
    one higher than before, and no amount of resuming can change that.
    Live on 2026-08-11 one robotsix-ui ticket carried five consecutive
    resume→re-block pairs against the identical fingerprint, its block
    note advising "Resume to retry" each time — advice the code could
    not honour without manual workspace surgery.

    Only a counter that has actually reached its ceiling is reset, so a
    resume for an unrelated reason preserves the loop state faithfully —
    the same rule ``_reset_implement_spawn_counter``'s caller applies to
    ``implement_spawn_count``. The operator resume IS the intervention
    that breaks the "consecutive without progress" chain; after the
    reset the ticket gets a full budget of genuine agent attempts again
    before the guard re-arms.

    Returns the labels of the counters that were reset, for the resume
    event note.  Best-effort and silent when absent.
    """
    reset: list[str] = []
    ceilings = (
        ("ci_identical_failure_count.txt", settings.ci_fix_max_identical_failures),
        ("auto_fix_cycles.txt", settings.auto_fix_max_cycles),
    )
    for fname, ceiling in ceilings:
        if ceiling <= 0:
            continue  # guard disabled — nothing to reset
        path = ws.artifacts_dir / fname
        if read_counter(path) < ceiling:
            continue
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        reset.append(fname)
    if "ci_identical_failure_count.txt" in reset:
        # The stored fingerprint is the counter's comparison baseline;
        # leaving it behind would re-increment from zero to the ceiling
        # against the same failure without the agent ever being asked.
        with contextlib.suppress(OSError):
            (ws.artifacts_dir / "ci_failure_fingerprint.txt").unlink(missing_ok=True)
    return reset


def _clear_stale_implement_guard(ws: Workspace) -> None:
    """Delete a stale ``implement.md`` so the stage's stale-respawn
    guard (see ``phase_coordinator.preflight``) doesn't immediately
    re-block a resumed ticket on its own unchanged-spec fingerprint.

    Best-effort and silent when absent — an operator override note is
    the explicit signal that a retry is wanted despite the guard.

    Before deleting, the stall-detection state (summary-fingerprint)
    is extracted from ``implement.md`` and persisted to
    ``implement_stall_state.json`` so the cross-spawn stall guard keeps
    its comparison baseline across operator-initiated resume/reset
    cycles.  The counter itself is reset to zero: the note IS the
    override, and a stall count that survives it made the guard
    terminal — nothing but a progressing implement pass clears the
    counter, and the guard blocks before that pass can run.  The
    fingerprint is retained so an attempt that once again returns a
    byte-identical summary re-trips the guard on its very next cycle.

    The spec-fingerprint is persisted to ``implement_spec_override`` so
    the stale-spec guard stays suppressed for this exact spec — re-arming
    as soon as it moves.
    """
    _persist_stall_state_from_implement_md(ws, reset_count=True)
    _persist_spec_fingerprint_override(ws)
    with contextlib.suppress(FileNotFoundError):
        (ws.artifacts_dir / "implement.md").unlink()


def _persist_spec_fingerprint_override(ws: Workspace) -> None:
    """Extract the ``spec-fingerprint`` from ``artifacts/implement.md``
    and persist it to ``artifacts/implement_spec_override``.

    This marker tells the preflight stale-spec guard that the operator
    has explicitly overridden the guard for this exact spec fingerprint
    — the guard will skip re-blocking until the spec changes (i.e. the
    current fingerprint no longer matches the stored override).

    Best-effort — silently no-ops when ``implement.md`` is absent or
    has no spec-fingerprint line.
    """
    md_path = ws.artifacts_dir / "implement.md"
    if not md_path.exists():
        return
    try:
        md_content = md_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in md_content.splitlines():
        if line.startswith("spec-fingerprint: "):
            spec_fp = line.split("spec-fingerprint: ", 1)[1].strip()
            if spec_fp:
                with contextlib.suppress(OSError):
                    (ws.artifacts_dir / "implement_spec_override").write_text(
                        spec_fp, encoding="utf-8"
                    )
            return


def _persist_stall_state_from_implement_md(
    ws: Workspace, *, reset_count: bool = False
) -> None:
    """Extract ``summary-fingerprint`` and ``stall-count`` from
    ``artifacts/implement.md`` and persist them to
    ``artifacts/implement_stall_state.json``.

    With *reset_count* the counter is zeroed while the fingerprint is
    kept — the operator override path, which wants a fresh attempt but
    still wants an identical re-run caught immediately.  The override
    also records that fingerprint as ``resume_fingerprint`` so the next
    stall can tell whether it survived a refresh (byte-identical to the
    summary present at resume time) or is a first-time stall.

    Best-effort — silently no-ops when ``implement.md`` is absent or
    unreadable.  The JSON file provides stall-detection continuity
    across operator-initiated resume-blocked cycles where
    ``implement.md`` is cleared by ``_clear_stale_implement_guard``.
    """
    import json

    state_path = ws.artifacts_dir / "implement_stall_state.json"
    md_path = ws.artifacts_dir / "implement.md"
    md_content = ""
    if md_path.exists():
        try:
            md_content = md_path.read_text(encoding="utf-8")
        except OSError:
            md_content = ""
    if not md_content:
        # Nothing to extract.  An override still has to zero any
        # counter a previous cycle left behind in the JSON — that
        # leftover is the whole reason the stall guard could outlive
        # every resume-blocked and pin a ticket to BLOCKED for good.
        if reset_count and state_path.exists():
            try:
                prev = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError, OSError:  # fmt: skip
                prev = {}
            if prev.get("stall_count"):
                prev["stall_count"] = 0
                with contextlib.suppress(OSError):
                    state_path.write_text(json.dumps(prev), encoding="utf-8")
        return
    summary_fp = ""
    stall_count = 0
    for line in md_content.splitlines():
        if line.startswith("summary-fingerprint: "):
            summary_fp = line.split("summary-fingerprint: ", 1)[1].strip()
        elif line.startswith("stall-count: "):
            try:
                stall_count = int(line.split("stall-count: ", 1)[1].strip())
            except ValueError:
                stall_count = 0
    # A non-reset re-persist must not launder away a refresh marker a
    # prior resume recorded in the JSON — carry it forward unless this
    # is itself a resume (which re-marks the current fingerprint).
    resume_fp = ""
    if reset_count:
        resume_fp = summary_fp
    elif state_path.exists():
        try:
            _prev_state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:  # fmt: skip
            _prev_state = {}
        resume_fp = _prev_state.get("resume_fingerprint", "")
    if reset_count:
        stall_count = 0
    if summary_fp or stall_count or reset_count:
        with contextlib.suppress(OSError):
            state_path.write_text(
                json.dumps(
                    {
                        "summary_fingerprint": summary_fp,
                        "stall_count": stall_count,
                        "resume_fingerprint": resume_fp,
                    }
                ),
                encoding="utf-8",
            )


# A ticket auto-unblocks its ``unblocks`` targets when it reaches one of
# these completion states (DONE = merged/auto-merged; CLOSED = retrospected;
# EPIC_CLOSED = all epic children done). Firing on both DONE and CLOSED is
# idempotent — targets are only moved if still BLOCKED.
_UNBLOCK_TRIGGER_STATES: set[State] = {
    State.DONE,
    State.CLOSED,
    State.EPIC_CLOSED,
}

# States that represent a terminal pipeline outcome — transitions to
# these are gated on having no open [ASK_USER] threads.
_TERMINAL_STATES: set[State] = {
    State.DONE,
    State.CLOSED,
    State.ERRORED,
}


# --- PR/commit citation verification for mark_done -----------------------

# Matches "#NNNNN" or "PR #NNNNN" — PR number references in free-text notes.
_PR_CITATION_RE = re.compile(r"(?:PR\s+)?#(\d{1,5})", re.IGNORECASE)

# Matches 7–40 hex SHA-like tokens (same pattern as refine's _COMMIT_SHA_RE).
_COMMIT_CITATION_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _verify_citations(note: str, repo_dir: Path | None) -> str:
    """Best-effort: check cited PRs / commit SHAs against *repo_dir*'s
    ``origin/main`` and append ⚠️ warnings for any that can't be verified.

    Returns *note* unchanged when *repo_dir* is ``None`` or missing,
    when *note* is empty, or when no citations are detected.
    """
    if not repo_dir or not repo_dir.exists():
        return note
    if not note or not note.strip():
        return note

    # A "not found on origin/main" warning is only trustworthy after a
    # fresh fetch.  Refresh origin/main first so a stale workspace clone
    # cannot turn a merged PR/commit into a false citation warning.
    origin_main_sha = git_ops.refresh_origin_target(repo_dir, "main")

    warnings: list[str] = []

    # --- PR citations: git log --grep="#N" origin/main ------------------
    for m in _PR_CITATION_RE.finditer(note):
        pr_num = m.group(1)
        grep = f"#{pr_num}"
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "log",
                    "--oneline",
                    f"--grep={grep}",
                    "origin/main",
                    "-1",
                ],
                capture_output=True,
                text=True,
            )
        except Exception:
            # If git itself is broken, skip verification entirely.
            return note
        if result.returncode != 0 or not result.stdout.strip():
            warnings.append(f"PR #{pr_num}")

    # --- Commit SHA citations: git cat-file -e + merge-base ------------
    for m in _COMMIT_CITATION_RE.finditer(note):
        sha = m.group(0)
        # Skip SHAs that are embedded inside PR references already handled above.
        try:
            type_check = subprocess.run(
                ["git", "-C", str(repo_dir), "cat-file", "-e", sha],
                capture_output=True,
                text=True,
            )
        except Exception:
            return note
        if type_check.returncode != 0:
            warnings.append(f"commit {sha}")
            continue
        try:
            anc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "merge-base",
                    "--is-ancestor",
                    sha,
                    "origin/main",
                ],
                capture_output=True,
                text=True,
            )
        except Exception:
            return note
        if anc.returncode != 0:
            warnings.append(f"commit {sha}")

    if not warnings:
        return note

    lines: list[str] = []
    if origin_main_sha:
        lines.append(
            f"origin/main verified @ {origin_main_sha[:12]} before citation check."
        )
    for w in sorted(set(warnings)):
        lines.append(
            f"⚠️ {w} not found on origin/main at time of closure — verify manually."
        )
    return note.rstrip() + "\n\n" + "\n".join(lines)


class _TransitionMixin(_ServiceBase):
    """State transitions, resume, retry, request-changes, and mark-done."""

    def transition(
        self,
        ticket_id: str,
        dst: State,
        note: str | None = None,
        block_reason: str | None = None,
    ) -> Ticket:
        """Move a ticket to *dst* state.

        Returns the updated :class:`Ticket`. Raises :class:`KeyError` if
        the ticket does not exist and :class:`TransitionError` if the
        transition is not allowed by the state machine.

        When transitioning to :class:`State.BLOCKED`, the originating
        state is recorded in ``blocked_from`` so it can be resumed later,
        and *block_reason* (if given) is recorded as the machine-checkable
        structured reason (see :mod:`~robotsix_mill.core.block_reason`).
        Leaving BLOCKED clears both.  *block_reason* is ignored when the
        destination is not BLOCKED.

        Transitions to terminal states — :class:`State.DONE`,
        :class:`State.CLOSED`, or :class:`State.ERRORED` — are rejected
        when the ticket has any open ``[ASK_USER]`` comment threads.
        """
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            blocked_from = State(ticket.blocked_from) if ticket.blocked_from else None
            paused_from = State(ticket.paused_from) if ticket.paused_from else None
            if not can_transition(ticket.state, dst, blocked_from, paused_from):
                raise TransitionError(
                    f"{ticket_id}: {ticket.state} -> {dst} not allowed"
                )
            # Refuse to transition to a terminal state while any
            # [ASK_USER] threads remain open — those questions must be
            # resolved (thread closed) before the pipeline completes.
            #
            # Exception: BLOCKED → CLOSED for ask_user-blocked tickets.
            # When the operator force-closes an abandoned ask_user
            # ticket, auto-close the open threads with a system note
            # rather than blocking the transition.
            if dst in _TERMINAL_STATES:
                open_threads = self._has_open_ask_user_threads(ticket_id, s)
                if open_threads:
                    if (
                        ticket.state is State.BLOCKED
                        and dst is State.CLOSED
                        and ticket.blocked_from == State.AWAITING_USER_REPLY.value
                    ):
                        now = datetime.now(UTC)
                        for t in open_threads:
                            t.closed_at = now
                            s.add(t)
                        ids = ", ".join(str(t.id) for t in open_threads)
                        log.info(
                            "%s: BLOCKED → CLOSED — auto-closed %d open [ASK_USER] "
                            "thread(s) (IDs: %s)",
                            _sanitize_log_value(ticket_id),
                            len(open_threads),
                            ids,
                        )
                    else:
                        ids = ", ".join(str(t.id) for t in open_threads)
                        raise TransitionError(
                            f"{ticket_id}: cannot transition to {dst} while "
                            f"{len(open_threads)} [ASK_USER] thread(s) are "
                            f"open (IDs: {ids})"
                        )
            # Record originating state when blocking; clear when leaving
            # BLOCKED (regardless of resume or override path).
            if dst is State.BLOCKED:
                ticket.blocked_from = ticket.state.value
                ticket.block_reason = block_reason
                # Guard: every BLOCKED transition must carry a reason in
                # the history event.  A blocked ticket with no note is an
                # unrecoverable diagnostic gap — default to a generic note
                # that surfaces the originating state so operators can
                # understand why the ticket was blocked.
                if not note or not note.strip():
                    note = f"blocked from {ticket.state.value} (no reason recorded)"
                    log.warning(
                        "%s: BLOCKED transition with no note — "
                        "defaulting to generic reason",
                        ticket_id,
                    )
            elif ticket.state is State.BLOCKED:
                ticket.blocked_from = None
                ticket.block_reason = None
                # When an operator forces a blocked ticket back into
                # READY with an explicit justification note, clear the
                # implement stage's stale-spec guard so the fingerprint-
                # collision refusal (phase_coordinator.preflight guard
                # #4) doesn't silently re-block the ticket.  This
                # mirrors resume_blocked's note-gated clearing and
                # ensures ANY operator-forced transition into READY
                # (not just the resume-blocked endpoint) satisfies the
                # "operator-authorized retry" requirement.
                if dst is State.READY and note and note.strip():
                    _clear_stale_implement_guard(self.workspace(ticket))
            # Record originating state when pausing mid-stage; clear when
            # leaving AWAITING_USER_REPLY (resume path), except when
            # escalating to BLOCKED — paused_from must survive so the
            # operator can answer and resume to the original state.
            if dst is State.AWAITING_USER_REPLY:
                # Preserve an existing paused_from (e.g. when resuming
                # from BLOCKED back through AWAITING_USER_REPLY).
                if not ticket.paused_from:
                    ticket.paused_from = ticket.state.value
            elif ticket.state is State.AWAITING_USER_REPLY and dst is not State.BLOCKED:
                ticket.paused_from = None
            # Leaving READY for a later stage means the implement stage
            # actually delivered — reset its spawn budget.
            if ticket.state is State.READY and dst in _IMPLEMENT_PROGRESS_STATES:
                _reset_implement_spawn_counter(self.workspace(ticket))
            old_state = ticket.state.value
            ticket.state = dst
            ticket.updated_at = datetime.now(UTC)
            s.add(ticket)
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=dst, note=note))
            s.commit()
            s.refresh(ticket)
            # Purge oldest terminal tickets if we just crossed the cap.
            if dst in self._ARCHIVABLE_STATES:
                self._maybe_purge_archived()
            if self._on_transition is not None:
                self._on_transition(ticket, old_state)
            # Capture unblock targets to fire AFTER this session closes
            # (cross-board: each target may live on another board's DB; we
            # must not hold this session open while transitioning them).
            unblock_targets = (
                _parse_depends_on_str(ticket.unblocks)
                if dst in _UNBLOCK_TRIGGER_STATES
                else []
            )
        if unblock_targets:
            self._fire_unblocks(ticket_id, unblock_targets)
        return self.get(ticket_id) or ticket

    def _fire_unblocks(self, solver_id: str, target_ids: list[str]) -> None:
        """Transition each BLOCKED ticket in *target_ids* to DRAFT.

        Called when *solver_id* completes. Best-effort and idempotent: a
        target that is missing or not currently BLOCKED is skipped (so
        re-firing on DONE then CLOSED is a no-op the second time). Targets
        may live on other boards — ``transition`` resolves each via
        ``_board_for``.
        """
        note = f"auto-unblocked: solver {solver_id} completed"
        for tid in target_ids:
            try:
                target = self.get(tid)
                if target is None or target.state is not State.BLOCKED:
                    continue
                self.transition(tid, State.DRAFT, note=note)
                log.info("unblock: %s -> DRAFT (solver %s completed)", tid, solver_id)
            except Exception:
                log.warning(
                    "unblock: failed to re-open %s (solver %s)",
                    tid,
                    solver_id,
                    exc_info=True,
                )

    def resume_blocked(self, ticket_id: str, note: str = "") -> Ticket:
        """Resume a blocked ticket to the state it was blocked from.

        Reads ``ticket.blocked_from`` and transitions the ticket back to
        that state so only the failed stage is re-run.

        When *note* is non-empty it is recorded as a comment on the
        ticket and, if resuming back into READY, clears the implement
        stage's stale-spec guard (``artifacts/implement.md``) — an
        explicit operator justification is treated as sufficient reason
        to retry even though the spec itself is unchanged, instead of
        requiring manual workspace surgery to reset the guard.

        When the ticket was blocked from READY due to the implement
        spawn limit (``artifacts/implement_spawn_count`` ≥
        ``implement_max_spawns_per_ticket``), the counter file is
        deleted so the ticket gets a fresh attempt budget, and the
        reset is recorded in the event history as "spawn counter reset
        via resume-blocked". Tickets blocked from READY for other
        reasons keep their counter intact.

        The ticket-lifetime implement↔review cap
        (``ticket.implement_cycles`` ≥ ``max_implement_review_cycles``)
        follows the same rule: it is checked in implement's preflight
        before any work runs, so a ticket at the ceiling would otherwise
        re-block on the next poll having done nothing, making resume a
        silent no-op.

        The merge-side ci_fix guard counters follow the same rule via
        :func:`_reset_tripped_ci_fix_guards` — one that has reached its
        ceiling is cleared (and recorded in the event note), one below
        its ceiling is preserved.  Without this the identical-failure
        and auto-fix-cycle guards were terminal: they re-evaluate before
        the agent phase, so a resume re-blocked on the next poll no
        matter what the operator had fixed.
        """
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            if ticket.state is not State.BLOCKED:
                raise TransitionError(
                    f"{ticket_id}: cannot resume — not BLOCKED (currently {ticket.state})"
                )
            if not ticket.blocked_from:
                raise TransitionError(
                    f"{ticket_id}: cannot resume — no blocked_from recorded; "
                    "use a manual transition (READY or DRAFT) instead"
                )
            dst = State(ticket.blocked_from)
            if not can_transition(ticket.state, dst, dst):
                raise TransitionError(
                    f"{ticket_id}: {ticket.state} -> {dst} not allowed"
                )
            ticket.blocked_from = None
            ticket.block_reason = None
            ticket.retry_attempt = 0
            ticket.last_transient_error = None
            ticket.next_retry_at = None
            ticket.pre_redraft_trace_count = -1  # sentinel: set baseline on next poll
            old_state = ticket.state.value
            ticket.state = dst
            ticket.updated_at = datetime.now(UTC)
            s.add(ticket)
            note = note.strip()
            if note:
                s.add(Comment(ticket_id=ticket_id, body=note, author="operator"))
            s.flush()
            # Detect spawn-limit block: only reset the counter when it's
            # actually at/above the limit — tickets blocked from READY
            # for other reasons keep their counter so the state is
            # faithfully preserved across the resume.
            spawn_reset = False
            spawn_hold = False  # recurring exhaustion — see below
            counter_path = None
            # The ticket-lifetime implement↔review cap is checked in
            # implement's preflight, BEFORE any work happens, so a ticket
            # sitting at the ceiling re-blocks on the very next poll having
            # done nothing: no implement, no review, no artifacts. Without
            # clearing it here "resume-blocked" is a silent no-op and the
            # ticket is terminal, even though its block note invites a human
            # to inspect and resume. Live, auto-mail 590f and central-deploy
            # de52 each absorbed two operator resumes that way.
            #
            # Same rule as the spawn counter and the ci_fix guards: reset it
            # only when it is actually AT the ceiling, so a ticket blocked
            # for some other reason keeps its count faithfully.
            cycles_reset = False
            if dst is State.READY and self.settings.max_implement_review_cycles > 0:
                cycles_reset = (
                    ticket.implement_cycles >= self.settings.max_implement_review_cycles
                )
            if dst is State.READY and self.settings.implement_max_spawns_per_ticket > 0:
                counter_path = (
                    self.workspace(ticket).artifacts_dir / "implement_spawn_count"
                )
                spawn_limit = self.settings.implement_max_spawns_per_ticket
                spawn_count = 0
                if counter_path.exists():
                    try:
                        spawn_count = int(
                            counter_path.read_text(encoding="utf-8").strip()
                        )
                    except ValueError, OSError:
                        spawn_count = 0
                spawn_reset = spawn_count >= spawn_limit
                if spawn_reset:
                    # Recurring-exhaustion hold: when this ticket has
                    # ALREADY exhausted its spawn budget at least twice
                    # on the CURRENT spec fingerprint and the resume
                    # carries neither a spec change nor an explicit
                    # justification note, do NOT auto-grant another
                    # full budget.  The ticket returns to READY but the
                    # counter stays at/above the limit — the next
                    # preflight re-blocks it free of charge (no trace,
                    # no spawn slot) and the RECURRING_SPAWN_EXHAUSTION
                    # diagnostic stays visible until a human changes
                    # the spec or resumes with a note.
                    marker = read_spawn_exhaustion_marker(self.workspace(ticket))
                    current_fp = self._compute_spec_fingerprint(ticket)
                    if (
                        marker is not None
                        and marker[1] >= 2
                        and marker[0] == current_fp
                        and not note.strip()
                    ):
                        spawn_reset = False
                        spawn_hold = True
            # Same rule on the merge side: a ci_fix guard counter that has
            # reached its ceiling is terminal until something clears it,
            # and the resume is that something.  Applies to every dst —
            # the CI loop is reachable from fixing_ci, implement_complete
            # and rebasing alike.
            ci_guards_reset = _reset_tripped_ci_fix_guards(
                self.workspace(ticket), self.settings
            )
            if cycles_reset:
                ticket.implement_cycles = 0
                s.add(ticket)
            event_note = f"resumed from blocked (was blocked from {dst.value})"
            if note:
                event_note += f"; override: {note}"
            if spawn_reset:
                event_note += "; spawn counter reset via resume-blocked"
            if spawn_hold:
                event_note += (
                    "; recurring spawn exhaustion — counter NOT reset "
                    "(spec unchanged and no resume note); change the "
                    "spec or resume with a note to grant a fresh budget"
                )
            if cycles_reset:
                event_note += (
                    "; implement-review cycle counter reset via resume-blocked "
                    "(was at the ceiling, which blocks in preflight before any "
                    "work runs)"
                )
            if ci_guards_reset:
                event_note += (
                    f"; ci_fix guard(s) reset via resume-blocked: "
                    f"{', '.join(ci_guards_reset)}"
                )
            s.add(_make_event(s, ticket_id=ticket_id, state=dst, note=event_note))
            s.commit()
            s.refresh(ticket)
            if note and dst is State.READY:
                _clear_stale_implement_guard(self.workspace(ticket))
            # Clear any stale implement conversation state so that a
            # blocked→READY resume starts a fresh agent conversation
            # instead of replaying the prior transcript (which would
            # drown out corrective feedback loaded from comments).
            # Also clear the cached implement summary and reference-files
            # list — feeding the agent its own prior summary biases it
            # toward producing byte-identical output instead of reading
            # the updated (corrective) spec.  The stall state is
            # persisted to implement_stall_state.json so the cross-spawn
            # stall guard survives this reset.
            if dst is State.READY:
                from ...stages.pause import clear_conversation_state

                clear_conversation_state(self.workspace(ticket), "implement")
                ws = self.workspace(ticket)
                # Persist stall state from implement.md before we
                # potentially delete it above — and also when no note
                # is provided (implement.md survives, but the JSON
                # ensures continuity across future resets).
                _persist_stall_state_from_implement_md(ws)
                for _fname in ("implement_summary.md", "reference_files.json"):
                    _p = ws.artifacts_dir / _fname
                    with contextlib.suppress(FileNotFoundError):
                        _p.unlink()
            if spawn_reset and counter_path is not None:
                try:
                    counter_path.unlink()
                except FileNotFoundError:
                    pass  # best-effort; file may already be gone
                # The resume grants a fresh spawn budget — clear the
                # spawn-state ledger (in-flight marker + abort log)
                # with it so stale kill evidence doesn't outlive the
                # operator's intervention.
                _ws = self.workspace(ticket)
                for _fname in (
                    "implement_spawn_state.json",
                    "implement_spawn_aborts.jsonl",
                ):
                    with contextlib.suppress(FileNotFoundError):
                        (_ws.artifacts_dir / _fname).unlink()
            if self._on_transition is not None:
                self._on_transition(ticket, old_state)
            return ticket

    def set_retry_state(
        self,
        ticket_id: str,
        *,
        retry_attempt: int,
        last_transient_error: str | None,
        next_retry_at: datetime | None,
    ) -> None:
        """Set transient-error retry metadata on a ticket.

        Does NOT create a ``TicketEvent`` — the workflow state hasn't changed.
        """
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.retry_attempt = retry_attempt
            ticket.last_transient_error = last_transient_error
            ticket.next_retry_at = next_retry_at
            ticket.updated_at = datetime.now(UTC)
            s.add(ticket)
            s.commit()

    def request_changes(
        self, ticket_id: str, body: str, author: str = "user"
    ) -> tuple[Comment | None, Ticket]:
        """Transition from ``human_issue_approval`` to ``draft`` in one
        atomic operation.  When ``body`` is non-empty a ``Comment`` is
        also created.

        Returns the ``(Comment | None, Ticket)`` pair. Raises
        ``KeyError`` if the ticket does not exist, ``TransitionError``
        if it is not in ``human_issue_approval``.
        """
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            if ticket.state is not State.HUMAN_ISSUE_APPROVAL:
                raise TransitionError(
                    f"{ticket_id}: cannot request changes — "
                    f"not human_issue_approval (currently {ticket.state})"
                )
            comment = None
            if body.strip():
                comment = Comment(ticket_id=ticket_id, body=body, author=author)
                s.add(comment)
            note = f"changes requested: {body}"
            old_state = ticket.state.value
            ticket.state = State.DRAFT
            ticket.updated_at = datetime.now(UTC)
            s.add(ticket)
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=State.DRAFT, note=note))
            s.commit()
            if comment is not None:
                s.refresh(comment)
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket, old_state)
            return comment, ticket

    def request_implementation_changes(
        self, ticket_id: str, body: str, author: str = "user"
    ) -> tuple[Comment, Ticket]:
        """Send a ticket awaiting merge approval back to implement.

        The operator has reviewed the open PR and wants the
        implementation adjusted.  The spec itself is fine, so this
        re-runs implement rather than re-refining from DRAFT the way
        :meth:`request_changes` does.

        *body* is required and becomes a :class:`Comment` — that is the
        channel the implement stage reads operator feedback from
        (comments whose author is neither ``mill`` nor ``system`` are
        collected into the agent's ``feedback`` input).  A transition
        without it would silently re-run implement with no idea what to
        change.

        Two guards would otherwise defeat the request and are cleared:
        the stale-spec fingerprint guard (the spec is deliberately
        unchanged — the operator's note is the new information), and the
        implement spawn counter (an operator asking for rework must not
        be refused because earlier attempts used the budget).

        Returns ``(Comment, Ticket)``.  Raises ``KeyError`` if the ticket
        does not exist, ``TransitionError`` if it is not in
        ``human_mr_approval`` or if *body* is empty.
        """
        if not body.strip():
            raise TransitionError(
                f"{ticket_id}: cannot request implementation changes — "
                "a non-empty body is required; it is the only thing that "
                "tells the implement agent what to change"
            )
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            if ticket.state is not State.HUMAN_MR_APPROVAL:
                raise TransitionError(
                    f"{ticket_id}: cannot request implementation changes — "
                    f"not human_mr_approval (currently {ticket.state})"
                )
            comment = Comment(ticket_id=ticket_id, body=body, author=author)
            s.add(comment)
            note = f"implementation changes requested: {body}"
            old_state = ticket.state.value
            ticket.state = State.READY
            ticket.updated_at = datetime.now(UTC)
            s.add(ticket)
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=State.READY, note=note))
            s.commit()
            s.refresh(comment)
            s.refresh(ticket)
        ws = self.workspace(ticket)
        _clear_stale_implement_guard(ws)
        _reset_implement_spawn_counter(ws)
        if self._on_transition is not None:
            self._on_transition(ticket, old_state)
        return comment, ticket

    def close_tracker(self, ticket_id: str, note: str = "") -> Ticket:
        """Close a tracking ticket from any non-terminal state.

        Escape hatch for tracker tickets (source=ORPHANED_PR_CHECK): unlike
        mark_done, works from BLOCKED and skips all merge/branch
        verification (tracker tickets have no mill-authored commits).
        Transitions directly to CLOSED — no retrospect stage.

        Raises TransitionError when the ticket is already terminal.
        """
        _NON_CLOSEABLE = {State.DONE, State.CLOSED, State.ANSWERED, State.EPIC_CLOSED}
        try:
            board = self._board_for(ticket_id)
        except ValueError:
            board = self.board_id or ""
        with retry_on_db_full(self.settings, board) as s:
            ticket = _get_ticket(s, ticket_id)
            if ticket.state in _NON_CLOSEABLE:
                raise TransitionError(
                    f"{ticket_id}: cannot close tracker — "
                    f"state {ticket.state} is already terminal"
                )
            ticket.blocked_from = None
            ticket.block_reason = None
            ticket.paused_from = None
            old_state = ticket.state.value
            ticket.state = State.CLOSED
            ticket.updated_at = datetime.now(UTC)
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(
                    s,
                    ticket_id=ticket_id,
                    state=State.CLOSED,
                    note=note,
                )
            )
            s.commit()
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket, old_state)
        # Purge oldest terminal tickets if we just crossed the cap.
        self._maybe_purge_archived()
        return self.get(ticket_id) or ticket

    def mark_done(
        self, ticket_id: str, note: str = "", author: str = "user"
    ) -> tuple[Comment | None, Ticket]:
        """Mark a ticket as DONE from any non-terminal state.

        This is an escape hatch that bypasses ``can_transition()`` —
        similar to ``redraft()`` and ``request_changes()``.  Terminal
        states (DONE, CLOSED, ANSWERED, EPIC_CLOSED) and EPIC_OPEN are
        rejected.

        Before persisting, the ticket's feature branch is verified to
        have reached origin/main (via ancestor check, log grep for the
        ticket ID, and content-level grep).  If the merge cannot be
        confirmed the transition is refused with ``TransitionError``.

        Cited PR numbers and commit SHAs in *note* are also verified
        against ``origin/main``; unverifiable citations get a ⚠️
        warning appended (soft warning — the closure still proceeds).

        Returns ``(Comment | None, Ticket)``.  Raises ``KeyError`` if
        the ticket does not exist, ``TransitionError`` if the state is
        not eligible or the merge cannot be confirmed.
        """
        _NON_MARK_DONEABLE: set[State] = {
            State.DONE,
            State.CLOSED,
            State.ANSWERED,
            State.EPIC_CLOSED,
            State.EPIC_OPEN,
        }
        try:
            board = self._board_for(ticket_id)
        except ValueError:
            board = self.board_id or ""
        with retry_on_db_full(self.settings, board) as s:
            ticket = _get_ticket(s, ticket_id)
            if ticket.state in _NON_MARK_DONEABLE:
                raise TransitionError(
                    f"{ticket_id}: cannot mark done — "
                    f"state {ticket.state} is not eligible for mark-done"
                )
            # Force‑close marker for stuck tickets so operators know
            # this was a deliberate override. BLOCKED and REBASING are
            # the "stuck" states the escape hatch exists for — a no-op
            # ticket that loops in BLOCKED, or a ticket wedged in the
            # rebase agent — so both get the marker.
            force_close_states = {State.BLOCKED, State.REBASING}
            # ci-source tickets are main-branch workflow-failure reports —
            # they never open a feature branch, so there is nothing to
            # merge-verify. The ci-auto-close pass closes them via mark_done
            # once the workflow turns green, from any non-terminal state.
            is_force_close = (
                ticket.state in force_close_states or ticket.source == SourceKind.CI
            )
            if is_force_close:
                reason = note if note.strip() else "operator mark-done"
                note = f"[force-closed from {ticket.state}] {reason}"
            repo_dir = self.workspace(ticket).repo_dir
            # Refuse mark-done when the ticket's branch hasn't been
            # merged to origin/main (best-effort — skipped when the
            # workspace clone or branch isn't available).
            #
            # Escape-hatch exemption: a deliberate operator force-close
            # of a stuck BLOCKED/REBASING ticket bypasses the merge
            # verification. These are exactly the states where a no-op
            # ticket loops — its branch was never merged (there was
            # nothing to merge), so the merge check would 409 forever
            # and there would be no way to close the stuck ticket. The
            # operator is explicitly deciding to terminate it.
            if not is_force_close:
                verify_merge_before_done(
                    ticket_id=ticket_id,
                    repo_dir=repo_dir,
                    branch_prefix=self.settings.branch_prefix,
                    forge_target_branch=self.settings.forge_target_branch,
                    branch_name=ticket.branch,
                )
            # Close any open [ASK_USER] threads before force-closing —
            # the operator's mark-done means the question is moot.
            # Record the fact in the note so it's visible in history.
            open_ask = self._has_open_ask_user_threads(ticket_id, s)
            if open_ask:
                now = datetime.now(UTC)
                for c in open_ask:
                    c.closed_at = now
                    s.add(c)
                prefix = (
                    f"[force-closed with {len(open_ask)} open [ASK_USER] "
                    f"thread(s) — automatically closed]"
                )
                note = f"{prefix} {note}" if note.strip() else prefix
            # Augment the note with citation warnings before persisting.
            note = _verify_citations(note, repo_dir)
            comment = None
            if note.strip():
                comment = Comment(ticket_id=ticket_id, body=note, author=author)
                s.add(comment)
            event_note = f"mark done: {note}" if note else "mark done"
            old_state = ticket.state.value
            ticket.state = State.DONE
            ticket.updated_at = datetime.now(UTC)
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(s, ticket_id=ticket_id, state=State.DONE, note=event_note)
            )
            s.commit()
            if comment is not None:
                s.refresh(comment)
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket, old_state)
            # Capture unblock targets to fire AFTER this session closes,
            # mirroring transition(): mark-done is a completion (DONE) so
            # dependents parked on this ticket must not stay BLOCKED —
            # e.g. a ci_fix dependency closed as moot must still resume
            # the ticket it parked.
            unblock_targets = _parse_depends_on_str(ticket.unblocks)
        if unblock_targets:
            self._fire_unblocks(ticket_id, unblock_targets)
        return comment, ticket
