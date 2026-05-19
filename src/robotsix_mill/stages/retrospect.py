"""Retrospect stage: DONE -> CLOSED.

Post-delivery audit. Analyses the finished ticket's workflow (state
history + notes) and its Langfuse session (cost/latency/retries/errors,
workflow-only if Langfuse is unconfigured), records findings, and —
when MILL_RETROSPECT_SPAWN_DRAFTS is on and the agent proposes one —
files an improvement DRAFT linked back via parent_id. Then -> CLOSED.

Agent/analysis failure is BLOCKED-resumable, never terminal.
"""

from __future__ import annotations

import logging

from .. import langfuse_client
from ..agents import retrospecting
from ..core.models import Ticket
from ..core.states import State
from ..core.text_utils import truncate_at_boundary
from ..core.workspace import prune_clone
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.retrospect")

# Phrases that mark a "draft" as a non-actionable "nothing to do"
# report. The retrospect model sometimes sets propose_draft=true with a
# title like "No notable issues - clean run" — that is noise, not a
# ticket. A genuine improvement ticket's title never contains these, so
# the false-positive risk is negligible.
_NOOP_DRAFT_MARKERS = (
    "no notable issue", "no issues", "no issue ", "clean run",
    "nothing to flag", "nothing to report", "no improvement",
    "no action needed", "no concerns", "no notable finding",
    "all good", "no changes needed", "clean ticket", "nothing notable",
)


def _is_noop_draft(title: str | None, body: str | None) -> bool:
    """True if the proposed draft is an empty 'everything is fine'
    report rather than an actionable improvement.

    Keyed on the TITLE only: the noise the model emits is distinctively
    titled ("No notable issues - clean run"), whereas a genuine
    improvement title never contains these phrases. Deliberately does
    NOT use body/title length — legitimate tickets can be terse, and
    length heuristics cause false positives."""
    t = (title or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in _NOOP_DRAFT_MARKERS)


class RetrospectStage(Stage):
    name = "retrospect"
    input_state = State.DONE

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings
        ws = ctx.service.workspace(ticket)

        history = ctx.service.history(ticket.id)
        history_text = "\n".join(
            f"{e.at:%Y-%m-%d %H:%M} {e.state} {e.note or ''}".rstrip()
            for e in history
        )
        desc = ws.read_description()
        if desc:
            desc = truncate_at_boundary(desc, 6000)
        ticket_summary = (
            f"id: {ticket.id}\ntitle: {ticket.title}\n"
            f"branch: {ticket.branch}\n\n{desc}"
        )
        lf = langfuse_client.fetch_session_summary(s, ticket.id)

        # Read current memory — empty string if missing/unreadable.
        memory_text = ""
        memory_file = s.retrospect_memory_file
        try:
            if memory_file.exists():
                memory_text = memory_file.read_text(encoding="utf-8")
        except OSError:
            log.warning("%s: could not read memory file %s", ticket.id, memory_file)

        try:
            res = retrospecting.run_retrospect_agent(
                settings=s,
                ticket_summary=ticket_summary,
                history_text=history_text,
                langfuse_summary=lf,
                memory=memory_text,
            )
        except Exception as e:  # noqa: BLE001 — resumable, never lose the ticket
            log.exception("%s: retrospect agent failed", ticket.id)
            return Outcome(State.BLOCKED, f"retrospect failed — resumable: {e}")

        # Persist the agent's updated memory verbatim.
        if res.updated_memory:
            try:
                memory_file.parent.mkdir(parents=True, exist_ok=True)
                memory_file.write_text(res.updated_memory, encoding="utf-8")
            except OSError:
                log.warning("%s: could not write memory file %s", ticket.id, memory_file)

        spawned = None
        if (
            s.retrospect_spawn_drafts
            and res.propose_draft
            and res.draft_title
            and res.draft_body
        ):
            if _is_noop_draft(res.draft_title, res.draft_body):
                # Model set propose_draft=true on a clean/no-issue run.
                # Don't pollute the board with "no notable issues"
                # tickets — drop it (the analysis is still in findings
                # and the memory ledger).
                log.info(
                    "%s: retrospect proposed a no-op draft %r — skipped",
                    ticket.id, res.draft_title,
                )
            else:
                draft = ctx.service.create(
                    res.draft_title, res.draft_body, source="retrospect"
                )
                ctx.service.set_parent(draft.id, ticket.id)
                spawned = draft.id
                log.info(
                    "%s: retrospect spawned draft %s", ticket.id, spawned
                )

        (ws.artifacts_dir / "retrospect.md").write_text(
            f"# Retrospect\nlangfuse: "
            f"{'yes' if lf else 'workflow-only'}\n"
            f"spawned draft: {spawned or '—'}\n\n{res.findings}\n",
            encoding="utf-8",
        )

        if s.prune_clone_on_close:
            prune_clone(ws)

        note = res.conclusion or "closed"
        if spawned:
            note = f"{note} — improvement draft {spawned}"
        elif res.propose_draft and not s.retrospect_spawn_drafts:
            note = f"{note} — draft proposed (spawning disabled)"
        return Outcome(State.CLOSED, note)
