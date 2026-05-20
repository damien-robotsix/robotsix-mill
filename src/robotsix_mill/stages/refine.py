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
from ..core.datetime_utils import _as_utc
from ..core.models import Ticket
from ..core.states import State
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.refine")


class RefineStage(Stage):
    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:  # noqa: C901  # TODO: split dedup, clone, refine into sub-functions (ticket: split_refine_stage)
        ws = ctx.service.workspace(ticket)
        draft = ws.read_description().strip()
        title = ticket.title.strip()
        if not title and not draft:
            return Outcome(State.BLOCKED, "empty title and draft — nothing to refine")

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

        # Discarded drafts go to DONE (not directly CLOSED) so retrospect
        # still analyses them — sanity-check the dedup verdict, capture
        # any lesson in the memory ledger, and keep the audit trail
        # consistent with every other terminal-ish ticket.
        if verdict.get("duplicate_of"):
            return Outcome(
                State.DONE,
                f"duplicate of {verdict['duplicate_of']}: {verdict.get('reason', 'no reason')}",
            )
        if verdict.get("already_done"):
            return Outcome(
                State.DONE,
                f"already implemented in {verdict['already_done']}: {verdict.get('reason', 'no reason')}",
            )
        # --- end dedup guard ---

        # --- skip re-refinement for split children ---
        # A child ticket created from a split already has a refined
        # spec in its description.md.  Detect this by checking whether
        # the parent is CLOSED with a "split into" note — the canonical
        # signal that this ticket's description is already the refined
        # output.  We must NOT short-circuit for retrospect-spawned
        # drafts (whose parent is also CLOSED but for a different
        # reason and whose description is a raw draft, not a spec).
        if ticket.parent_id is not None:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.state == State.CLOSED:
                # Only short-circuit if the parent was closed by a
                # split — otherwise (e.g. retrospect spawn) the
                # draft still needs refinement.
                parent_history = ctx.service.history(parent.id)
                is_split_child = any(
                    ev.state == State.CLOSED
                    and ev.note
                    and ev.note.startswith("split into")
                    for ev in parent_history
                )
                if is_split_child:
                    spec = draft
                    if not spec.strip():
                        return Outcome(State.BLOCKED, "split child has empty description")
                    # Preserve the raw draft if not already preserved.
                    draft_original = ws.artifacts_dir / "draft-original.md"
                    if not draft_original.exists():
                        draft_original.write_text(
                            "(split child — spec written by parent's refine agent)",
                            encoding="utf-8",
                        )
                    next_state = (
                        State.AWAITING_APPROVAL if ctx.settings.require_approval
                        else State.READY
                    )
                    return Outcome(next_state, "split child — spec already refined")

        # --- run the refine agent ---
        try:
            # Gather reviewer comments (if the ticket was sent back to draft).
            comments = ctx.service.list_comments(ticket.id)
            reviewer_comments: str | None = None
            if comments:
                reviewer_comments = "\n".join(
                    f"[{c.created_at.isoformat()}] {c.body}" for c in comments
                )

            result = refining.run_refine_agent(
                settings=s, title=ticket.title, draft=draft,
                repo_dir=repo_dir,
                reviewer_comments=reviewer_comments,
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            return Outcome(State.BLOCKED, str(e))

        # --- preserve the raw draft (always, for traceability) ---
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )

        # --- normal single-scope path ---
        if not result.get("split"):
            spec = result.get("spec", "")
            if not spec or not spec.strip():
                return Outcome(State.BLOCKED, "refiner produced an empty spec")

            new_hash = ws.write_description(spec)
            ctx.service.set_content_hash(ticket.id, new_hash)

            next_state = (
                State.AWAITING_APPROVAL if ctx.settings.require_approval
                else State.READY
            )
            return Outcome(next_state, "refined")

        # --- multi-scope split path ---
        children_raw: list = result.get("children", [])
        if not isinstance(children_raw, list) or len(children_raw) == 0:
            # Degrade gracefully: treat as single-spec with whatever we got.
            spec = result.get("spec", "")
            if not spec or not spec.strip():
                return Outcome(State.BLOCKED, "refiner produced an empty spec (split with no children)")
            new_hash = ws.write_description(spec)
            ctx.service.set_content_hash(ticket.id, new_hash)
            next_state = (
                State.AWAITING_APPROVAL if ctx.settings.require_approval
                else State.READY
            )
            return Outcome(next_state, "refined (split degraded — no valid children)")

        # Validate and collect valid children.
        valid_children: list[dict] = []
        for child in children_raw:
            if not isinstance(child, dict):
                continue
            title = (child.get("title") or "").strip()
            spec_md = (child.get("spec_markdown") or "").strip()
            if not title or not spec_md:
                continue
            deps = child.get("depends_on", [])
            if not isinstance(deps, list):
                deps = []
            # Keep only non-negative integer indices.
            deps = [d for d in deps if isinstance(d, int) and d >= 0]
            valid_children.append({
                "title": title,
                "spec_markdown": spec_md,
                "depends_on": deps,
            })

        if len(valid_children) == 0:
            return Outcome(State.BLOCKED, "refiner produced no valid split children")
        if len(valid_children) == 1:
            # Only one valid child — fall back to single-spec path.
            child = valid_children[0]
            new_hash = ws.write_description(child["spec_markdown"])
            ctx.service.set_content_hash(ticket.id, new_hash)
            # Also update the ticket title to the child's title.
            ctx.service.set_title(ticket.id, child["title"])
            next_state = (
                State.AWAITING_APPROVAL if ctx.settings.require_approval
                else State.READY
            )
            return Outcome(next_state, "refined (single child, no split)")

        # Create child tickets.
        child_ids: list[str] = []
        for i, child in enumerate(valid_children):
            child_ticket = ctx.service.create(
                title=child["title"],
                description=child["spec_markdown"],
                source=ticket.source,
            )
            child_ids.append(child_ticket.id)
            ctx.service.set_parent(child_ticket.id, ticket.id)

        # Resolve depends_on indices → real ticket IDs.
        for i, child in enumerate(valid_children):
            if child["depends_on"]:
                resolved = []
                for idx in child["depends_on"]:
                    if 0 <= idx < i and idx < len(child_ids):
                        resolved.append(child_ids[idx])
                if resolved:
                    ctx.service.set_depends_on(child_ids[i], resolved)

        # Transition each child to AWAITING_APPROVAL or READY.
        child_next_state = (
            State.AWAITING_APPROVAL if ctx.settings.require_approval
            else State.READY
        )
        for cid in child_ids:
            ctx.service.transition(
                cid, child_next_state,
                note=f"split from {ticket.id}",
            )

        # Close the original ticket.
        ids_note = ", ".join(child_ids)
        return Outcome(
            State.CLOSED,
            f"split into {ids_note}",
        )
