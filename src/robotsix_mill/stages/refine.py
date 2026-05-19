"""Refine stage: raw DRAFT -> actionable READY ticket.

Rewrites the file-canonical ``description.md`` into a precise spec the
implement agent can act on unattended; the original draft is kept as an
artifact for traceability. Empty draft or missing OpenRouter key ->
BLOCKED with a clear note (not a crash).

When ``require_approval`` is true (the default), the refined ticket
enters ``awaiting_approval`` instead of ``ready`` — a human must approve
before the implement stage picks it up.

Before the expensive refine agent runs, a cheap **dedup / already-done
check** inspects the draft against existing tickets and recent commits.
If the draft is a clear duplicate or the change is already committed,
the ticket is short-circuited to ``CLOSED`` — no refiner, no human
approval gate, no wasted cost.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone

from ..agents import dedup
from ..agents import refining
from ..core.models import Ticket
from ..core.states import State
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.refine")


def _as_utc(dt: datetime) -> datetime:
    """Coerce a possibly tz-naive datetime to aware UTC.

    SQLite/SQLModel round-trips ``updated_at``/``created_at`` WITHOUT
    tzinfo even though we store them from ``datetime.now(timezone.utc)``.
    Comparing such a naive value against an aware cutoff raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    — which broke the dedup guard (hence refine) for every draft as soon
    as any CLOSED ticket existed. Treat naive DB datetimes as UTC.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class RefineStage(Stage):
    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        ws = ctx.service.workspace(ticket)
        draft = ws.read_description().strip()
        if not draft:
            return Outcome(State.BLOCKED, "empty draft — nothing to refine")

        # Ground the spec in the ACTUAL repo: clone it locally so the
        # refine agent uses explore/read_file instead of web-fetching
        # the project's own files. Best-effort — a clone failure (or no
        # forge configured) just falls back to draft-only refinement.
        s = ctx.settings
        repo_dir = None
        if s.forge_remote_url:
            cand = ws.dir / "repo"
            if (cand / ".git").exists():
                repo_dir = cand  # idempotent: reuse an existing clone
            else:
                try:
                    git_ops.clone(
                        s.forge_remote_url, cand,
                        s.forge_target_branch, s.forge_token,
                    )
                    repo_dir = cand
                except subprocess.CalledProcessError as e:
                    log.warning(
                        "%s: refine clone failed, draft-only: %s",
                        ticket.id, (e.stderr or "")[:200],
                    )

        # --- dedup / already-done guard (best-effort) ---
        # Gather candidate tickets: all non-terminal + recently closed.
        all_tickets = ctx.service.list()
        now = datetime.now(timezone.utc)
        lookback_cutoff = datetime.fromtimestamp(
            now.timestamp() - s.dedup_lookback_days * 86400, tz=timezone.utc
        )
        non_terminal = {State.CLOSED, State.ERRORED}
        candidates = [
            t for t in all_tickets
            if t.id != ticket.id and (
                t.state not in non_terminal
                or (
                    t.state == State.CLOSED
                    and _as_utc(t.updated_at) >= lookback_cutoff
                )
            )
        ]
        candidates_json = json.dumps(
            [{"id": t.id, "title": t.title, "state": t.state.value, "source": t.source}
             for t in candidates],
            default=str,
        )

        # Gather recent commits (only when we have a clone).
        recent_commits_json: str | None = None
        if repo_dir is not None:
            try:
                commits = git_ops.recent_commits(repo_dir, s.dedup_lookback_commits)
                recent_commits_json = json.dumps(
                    [{"sha": c["sha"], "subject": c["subject"]} for c in commits]
                )
            except Exception:
                log.warning("%s: recent_commits failed, skipping commit dedup", ticket.id)

        try:
            verdict = dedup.run_dedup_check(
                settings=s,
                draft_title=ticket.title,
                draft_body=draft,
                candidates_json=candidates_json,
                recent_commits_json=recent_commits_json,
            )
        except Exception:
            log.warning(
                "%s: dedup check failed, proceeding with refine", ticket.id,
                exc_info=True,
            )
            verdict = {
                "duplicate_of": None,
                "already_done": None,
                "reason": "dedup check failed",
            }

        if verdict.get("duplicate_of"):
            return Outcome(
                State.CLOSED,
                f"duplicate of {verdict['duplicate_of']}: {verdict.get('reason', 'no reason')}",
            )
        if verdict.get("already_done"):
            return Outcome(
                State.CLOSED,
                f"already implemented in {verdict['already_done']}: {verdict.get('reason', 'no reason')}",
            )
        # --- end dedup guard ---

        try:
            spec = refining.run_refine_agent(
                settings=s, title=ticket.title, draft=draft,
                repo_dir=repo_dir,
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            return Outcome(State.BLOCKED, str(e))

        if not spec.strip():
            return Outcome(State.BLOCKED, "refiner produced an empty spec")

        # preserve the raw draft, then make the refined spec canonical
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft, encoding="utf-8"
        )
        new_hash = ws.write_description(spec)
        ctx.service.set_content_hash(ticket.id, new_hash)

        next_state = (
            State.AWAITING_APPROVAL if ctx.settings.require_approval
            else State.READY
        )
        return Outcome(next_state, "refined")
