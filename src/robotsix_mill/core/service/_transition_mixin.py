"""State-transition surface of :class:`TicketService` (``_TransitionMixin``)."""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


from ..db import retry_on_db_full
from ..models import (
    Comment,
    Ticket,
)
from ..states import State, can_transition
from ..workspace import Workspace
from ._base import _ServiceBase
from ._helpers import (
    TransitionError,
    _get_ticket,
    _make_event,
    _parse_depends_on_str,
    verify_merge_before_done,
)

log = logging.getLogger("robotsix_mill.service")


def _clear_stale_implement_guard(ws: Workspace) -> None:
    """Delete a stale ``implement.md`` so the stage's stale-respawn
    guard (see ``phase_coordinator.preflight``) doesn't immediately
    re-block a resumed ticket on its own unchanged-spec fingerprint.

    Best-effort and silent when absent — an operator override note is
    the explicit signal that a retry is wanted despite the guard.
    """
    try:
        (ws.artifacts_dir / "implement.md").unlink()
    except FileNotFoundError:
        pass


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
    for w in sorted(set(warnings)):
        lines.append(
            f"⚠️ {w} not found on origin/main at time of closure — verify manually."
        )
    return note.rstrip() + "\n\n" + "\n".join(lines)


def _check_changelog_duplicates(repo_dir: Path | None, ticket_id: str) -> list[str]:
    """Check *repo_dir*'s HEAD for duplicate towncrier fragments for
    *ticket_id*.  Returns a list of the duplicate fragment basenames
    (empty when there are 0 or 1 fragments — no problem).

    Best-effort: returns ``[]`` when *repo_dir* is ``None``, the repo
    has no ``pyproject.toml``, no ``[tool.towncrier]`` config, or any
    git / parsing error occurs.  Never raises.
    """
    if repo_dir is None:
        return []

    pp = repo_dir / "pyproject.toml"
    if not pp.is_file():
        return []

    try:
        import tomllib

        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:
        return []

    tc = (data.get("tool", {}) or {}).get("towncrier")
    if not tc:
        return []

    directory = str(tc.get("directory") or "changes").rstrip("/")

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "ls-tree", "HEAD", "--", f"{directory}/"],
            capture_output=True,
            text=True,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    fragments: list[str] = []
    prefix = f"{ticket_id}."
    for line in result.stdout.splitlines():
        # git ls-tree output: <mode> <type> <sha>\t<path>
        if "\t" not in line:
            continue
        path = line.split("\t", 1)[1]
        name = Path(path).name
        if name.startswith(prefix) and name.endswith(".md"):
            fragments.append(name)

    if len(fragments) > 1:
        return fragments
    return []


class _TransitionMixin(_ServiceBase):
    """State transitions, resume, retry, request-changes, and mark-done."""

    def transition(self, ticket_id: str, dst: State, note: str | None = None) -> Ticket:
        """Move a ticket to *dst* state.

        Returns the updated :class:`Ticket`. Raises :class:`KeyError` if
        the ticket does not exist and :class:`TransitionError` if the
        transition is not allowed by the state machine.

        When transitioning to :class:`State.BLOCKED`, the originating
        state is recorded in ``blocked_from`` so it can be resumed later.

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
            if dst in _TERMINAL_STATES:
                open_threads = self._has_open_ask_user_threads(ticket_id, s)
                if open_threads:
                    ids = ", ".join(str(t.id) for t in open_threads)
                    raise TransitionError(
                        f"{ticket_id}: cannot transition to {dst} while "
                        f"{len(open_threads)} [ASK_USER] thread(s) are "
                        f"open (IDs: {ids})"
                    )
            # Refuse transition to DONE when duplicate changelog
            # fragments exist on the ticket's branch.  This gate
            # prevents a BLOCKED ticket from being force-closed
            # while the fragment conflict is still live on HEAD.
            if dst is State.DONE:
                repo_dir = self.workspace(ticket).repo_dir
                dupes = _check_changelog_duplicates(repo_dir, ticket_id)
                if dupes:
                    raise TransitionError(
                        f"{ticket_id}: cannot transition to {dst} — "
                        f"duplicate changelog fragments on branch: "
                        f"{', '.join(sorted(dupes))}"
                    )
            # Record originating state when blocking; clear when leaving
            # BLOCKED (regardless of resume or override path).
            if dst is State.BLOCKED:
                ticket.blocked_from = ticket.state.value
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
            # leaving AWAITING_USER_REPLY (resume path).
            if dst is State.AWAITING_USER_REPLY:
                ticket.paused_from = ticket.state.value
            elif ticket.state is State.AWAITING_USER_REPLY:
                ticket.paused_from = None
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=dst, note=note))
            s.commit()
            s.refresh(ticket)
            # Purge oldest terminal tickets if we just crossed the cap.
            if dst in self._ARCHIVABLE_STATES:
                self._maybe_purge_archived()
            if self._on_transition is not None:
                self._on_transition(ticket)
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
            ticket.retry_attempt = 0
            ticket.last_transient_error = None
            ticket.next_retry_at = None
            ticket.pre_redraft_trace_count = -1  # sentinel: set baseline on next poll
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
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
            counter_path = None
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
            event_note = f"resumed from blocked (was blocked from {dst.value})"
            if note:
                event_note += f"; override: {note}"
            if spawn_reset:
                event_note += "; spawn counter reset via resume-blocked"
            s.add(_make_event(s, ticket_id=ticket_id, state=dst, note=event_note))
            s.commit()
            s.refresh(ticket)
            if note and dst is State.READY:
                _clear_stale_implement_guard(self.workspace(ticket))
            # Clear any stale implement conversation state so that a
            # blocked→READY resume starts a fresh agent conversation
            # instead of replaying the prior transcript (which would
            # drown out corrective feedback loaded from comments).
            if dst is State.READY:
                from ...stages.pause import clear_conversation_state

                clear_conversation_state(self.workspace(ticket), "implement")
            if spawn_reset and counter_path is not None:
                try:
                    counter_path.unlink()
                except FileNotFoundError:
                    pass  # best-effort; file may already be gone
            if self._on_transition is not None:
                self._on_transition(ticket)
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
            ticket.updated_at = datetime.now(timezone.utc)
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
            ticket.state = State.DRAFT
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=State.DRAFT, note=note))
            s.commit()
            if comment is not None:
                s.refresh(comment)
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket)
            return comment, ticket

    def close_tracker(self, ticket_id: str, note: str = "") -> Ticket:
        """Close a tracking ticket from any non-terminal state.

        Escape hatch for tracker tickets (source=ORPHANED_PR_CHECK): unlike
        mark_done, works from BLOCKED and skips all merge/branch/changelog
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
            ticket.paused_from = None
            ticket.state = State.CLOSED
            ticket.updated_at = datetime.now(timezone.utc)
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
                self._on_transition(ticket)
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
            is_force_close = ticket.state in force_close_states
            if is_force_close:
                reason = note if note.strip() else "operator mark-done"
                note = f"[force-closed from {ticket.state}] {reason}"
            # Refuse mark-done when duplicate changelog fragments
            # exist on the ticket's branch.
            repo_dir = self.workspace(ticket).repo_dir
            dupes = _check_changelog_duplicates(repo_dir, ticket_id)
            if dupes:
                raise TransitionError(
                    f"{ticket_id}: cannot mark done — "
                    f"duplicate changelog fragments on branch: "
                    f"{', '.join(sorted(dupes))}"
                )
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
                now = datetime.now(timezone.utc)
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
            ticket.state = State.DONE
            ticket.updated_at = datetime.now(timezone.utc)
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
                self._on_transition(ticket)
            return comment, ticket
