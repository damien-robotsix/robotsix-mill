"""Refine stage: raw DRAFT -> actionable READY ticket.

Rewrites the file-canonical ``description.md`` into a precise spec the
implement agent can act on unattended; the original draft is kept as an
artifact for traceability. Empty draft or missing OpenRouter key ->
BLOCKED with a clear note (not a crash).

When ``require_approval`` is true (the default), the refined ticket
enters ``human_issue_approval`` instead of ``ready`` — a human must approve
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
from ..pass_runner import load_memory, persist_memory
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.refine")


def _resolve_next_state(
    ctx: StageContext, spec: str, ticket_id: str,
) -> tuple[State, str | None]:
    """Return (next_state, auto_approve_note_or_None).

    Encapsulates the decision: if approval is not required → READY;
    if auto-approve is disabled → HUMAN_ISSUE_APPROVAL; otherwise run
    the cheap auto-approve triage on the spec → READY on OBVIOUS,
    HUMAN_ISSUE_APPROVAL otherwise (or on error).  Empty/whitespace
    specs skip the triage entirely and go to HUMAN_ISSUE_APPROVAL
    when gated, mirroring the original behaviour.

    Every triage outcome carries a structured note so the auto-approve
    decision is visible in ticket history.  Triage failures or
    unexpected errors in note assembly fall through to
    HUMAN_ISSUE_APPROVAL with a fallback note — the transition is
    never blocked.
    """
    if not ctx.settings.require_approval:
        return State.READY, None
    if not spec or not spec.strip():
        return State.HUMAN_ISSUE_APPROVAL, None
    if not ctx.settings.auto_approve_enabled:
        return State.HUMAN_ISSUE_APPROVAL, None
    try:
        result = refining.triage_auto_approve(
            settings=ctx.settings, spec=spec,
        )
        if result.decision == "OBVIOUS":
            return State.READY, f"auto-approve: OBVIOUS — {result.reason}"
        # NEEDS_APPROVAL — return the reason as a structured history
        # note (no side-effect comment; this is the sole surface).
        return State.HUMAN_ISSUE_APPROVAL, f"auto-approve: NEEDS_APPROVAL — {result.reason}"
    except Exception:
        log.warning(
            "auto-approve triage failed, falling back to human approval",
            exc_info=True,
        )
    return State.HUMAN_ISSUE_APPROVAL, "auto-approve: triage failed — falling back to human approval"


def _build_candidates_json(candidates: list[Ticket], ctx: StageContext) -> str:
    """Serialize candidates for the dedup check, including ticket bodies."""
    entries: list[dict] = []
    for t in candidates:
        try:
            body = ctx.service.workspace(t).read_description()
        except Exception:
            body = ""
        entries.append({
            "id": t.id,
            "title": t.title,
            "state": t.state.value,
            "source": t.source,
            "body": body,
        })
    return json.dumps(entries, default=str)


class RefineStage(Stage):
    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:  # noqa: C901  # TODO: split dedup, clone, refine into sub-functions (ticket: split_refine_stage)
        ws = ctx.service.workspace(ticket)
        draft = ws.read_description().strip()
        epic_ctx = ctx.service.get_epic_context(ticket)
        title = ticket.title.strip()
        if not title and not draft:
            return Outcome(State.BLOCKED, "empty title and draft — nothing to refine")

        # --- dependency gate: refuse to refine until all deps are
        # terminal (CLOSED/DONE). Same-state no-op → the reconcile
        # sweep re-enqueues this ticket each poll cycle.
        unmet = ctx.service.unmet_dependencies(ticket)
        if unmet:
            log.debug(
                "%s: unmet dependencies — deferring refine: %s",
                ticket.id, unmet,
            )
            return Outcome(State.DRAFT)

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
        candidates_json = _build_candidates_json(candidates, ctx)

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
                repo_dir=repo_dir,
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
                    next_state, auto_note = _resolve_next_state(ctx, spec, ticket.id)
                    note = "split child — spec already refined"
                    if auto_note:
                        note += f" | {auto_note}"
                    return Outcome(next_state, note)

        # --- gather reviewer comments (sendback guard) ---
        reviewer_comments: str | None = None
        try:
            comments = ctx.service.list_comments(ticket.id)
            if comments:
                reviewer_comments = "\n".join(
                    f"[{c.created_at.isoformat()}] {c.body}" for c in comments
                )
        except Exception:
            log.warning("%s: list_comments failed, proceeding without", ticket.id)

        # --- triage: skip full refine for already-precise drafts ---
        # A single cheap LLM call classifies the draft.  If it's
        # already a precise, implementation-ready spec, skip the
        # expensive refine agent entirely.  ONLY skip when:
        # - the feature flag is enabled, AND
        # - no reviewer sendback (human-flagged changes always refine), AND
        # - the triage model says SKIP.
        if (
            s.refine_triage_enabled
            and not reviewer_comments
        ):
            try:
                triage = refining.triage_refine(
                    settings=s, title=title, draft=draft,
                )
                if triage.decision == "SKIP":
                    # The draft IS the spec — preserve it unchanged.
                    (ws.artifacts_dir / "draft-original.md").write_text(
                        draft if draft else "(title-only ticket, no body provided)",
                        encoding="utf-8",
                    )
                    next_state, auto_note = _resolve_next_state(ctx, draft, ticket.id)
                    note = f"triage SKIP: {triage.reason}"
                    if auto_note:
                        note += f" | {auto_note}"
                    return Outcome(next_state, note)
            except Exception:
                log.warning(
                    "%s: triage failed, falling through to full refine",
                    ticket.id, exc_info=True,
                )
        # --- end triage ---

        # --- run the refine agent ---
        try:
            memory_text = load_memory(s.refine_memory_file, max_chars=s.max_memory_chars)

            result = refining.run_refine_agent(
                settings=s, title=ticket.title, draft=draft,
                repo_dir=repo_dir,
                reviewer_comments=reviewer_comments,
                memory=memory_text,
                epic_context=epic_ctx,
            )

            if result.updated_memory:
                persist_memory(s.refine_memory_file, result.updated_memory)

            if result.title and result.title.strip():
                ctx.service.set_title(ticket.id, result.title.strip())
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            return Outcome(State.BLOCKED, str(e))

        # --- epic body handling (non-split path) ---
        # In autonomous mode: apply immediately to the epic.
        # In gated mode: store as artifact in child workspace for
        # later application on approval.
        if result.epic_body and result.epic_body.strip() and epic_ctx:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == "epic":
                if not ctx.settings.require_approval:
                    new_hash = ctx.service.workspace(parent).write_description(
                        result.epic_body.strip()
                    )
                    ctx.service.set_content_hash(parent.id, new_hash)
                else:
                    (ws.artifacts_dir / "epic-body-proposed.md").write_text(
                        result.epic_body.strip(), encoding="utf-8"
                    )

        # --- preserve the raw draft (always, for traceability) ---
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )

        # --- write file map artifact ---
        if result.file_map:
            (ws.artifacts_dir / "file_map.json").write_text(
                json.dumps(
                    [{"file": e.file, "note": e.note} for e in result.file_map],
                    indent=2,
                ),
                encoding="utf-8",
            )

        # --- normal single-scope path ---
        if not result.split:
            spec = result.spec_markdown or ""
            if not spec or not spec.strip():
                log.warning(
                    "%s: refiner produced an empty spec — "
                    "proceeding with original draft",
                    ticket.id,
                )
                next_state, _auto_reason = _resolve_next_state(ctx, "", ticket.id)
                return Outcome(next_state, "refined (empty spec — kept original draft)")

            # --- spec review (conciseness pass) ---
            if s.spec_review_enabled and not reviewer_comments:
                try:
                    review_result = refining.review_spec_for_conciseness(
                        settings=s, spec_markdown=spec,
                    )
                    (ws.artifacts_dir / "refine-verbose.md").write_text(
                        spec, encoding="utf-8",
                    )
                    concise = review_result.concise_spec
                    if not concise or not concise.strip():
                        log.warning(
                            "%s: spec review returned empty concise spec, "
                            "using verbose spec",
                            ticket.id,
                        )
                    else:
                        spec = concise
                        log.info(
                            "%s: spec review: %s",
                            ticket.id, review_result.stripped_summary,
                        )
                except Exception:
                    log.warning(
                        "%s: spec review failed, using verbose spec",
                        ticket.id, exc_info=True,
                    )

            new_hash = ws.write_description(spec)
            ctx.service.set_content_hash(ticket.id, new_hash)

            next_state, auto_note = _resolve_next_state(ctx, spec, ticket.id)
            note = "refined"
            if auto_note:
                note += f" | {auto_note}"
            return Outcome(next_state, note)

        # --- multi-scope split path ---
        children_raw = result.children
        if not children_raw or len(children_raw) == 0:
            # Degrade gracefully: treat as single-spec with whatever we got.
            spec = result.spec_markdown or ""
            if not spec or not spec.strip():
                log.warning(
                    "%s: refiner produced an empty spec "
                    "(split with no children) — "
                    "proceeding with original draft",
                    ticket.id,
                )
                next_state, _auto_reason = _resolve_next_state(ctx, "", ticket.id)
                return Outcome(
                    next_state,
                    "refined (empty spec, split degraded — kept original draft)",
                )
            new_hash = ws.write_description(spec)
            ctx.service.set_content_hash(ticket.id, new_hash)
            next_state, auto_note = _resolve_next_state(ctx, spec, ticket.id)
            note = "refined (split degraded — no valid children)"
            if auto_note:
                note += f" | {auto_note}"
            return Outcome(next_state, note)

        # Validate and collect valid children.
        valid_children: list[dict] = []
        for child in children_raw:
            child_title = (child.title or "").strip()
            spec_md = (child.spec_markdown or "").strip()
            if not child_title or not spec_md:
                continue
            deps = child.depends_on or []
            if not isinstance(deps, list):
                deps = []
            # Keep only non-negative integer indices.
            deps = [d for d in deps if isinstance(d, int) and d >= 0]
            valid_children.append({
                "title": child_title,
                "spec_markdown": spec_md,
                "depends_on": deps,
            })

        if len(valid_children) == 0:
            return Outcome(State.BLOCKED, "refiner produced no valid split children")

        # --- spec review for split children (conciseness pass) ---
        if s.spec_review_enabled and not reviewer_comments:
            for i, child in enumerate(valid_children):
                try:
                    review_result = refining.review_spec_for_conciseness(
                        settings=s, spec_markdown=child["spec_markdown"],
                    )
                    (ws.artifacts_dir / f"refine-verbose-child-{i + 1}.md").write_text(
                        child["spec_markdown"], encoding="utf-8",
                    )
                    concise = review_result.concise_spec
                    if not concise or not concise.strip():
                        log.warning(
                            "%s: spec review child %d returned empty concise spec, "
                            "using verbose spec",
                            ticket.id, i + 1,
                        )
                    else:
                        child["spec_markdown"] = concise
                        log.info(
                            "%s: spec review child %d: %s",
                            ticket.id, i + 1, review_result.stripped_summary,
                        )
                except Exception:
                    log.warning(
                        "%s: spec review failed for child %d, using verbose spec",
                        ticket.id, i + 1, exc_info=True,
                    )

        if len(valid_children) == 1:
            # Only one valid child — fall back to single-spec path.
            child = valid_children[0]
            new_hash = ws.write_description(child["spec_markdown"])
            ctx.service.set_content_hash(ticket.id, new_hash)
            # Update the ticket title: agent's explicit title beats
            # the child's title (which is a fallback).
            if not (result.title and result.title.strip()):
                ctx.service.set_title(ticket.id, child["title"])
            next_state, auto_note = _resolve_next_state(ctx, child["spec_markdown"], ticket.id)
            note = "refined (single child, no split)"
            if auto_note:
                note += f" | {auto_note}"
            return Outcome(next_state, note)

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

        # Transition each child to HUMAN_ISSUE_APPROVAL or READY.
        for i, cid in enumerate(child_ids):
            child_state, auto_note = _resolve_next_state(
                ctx, valid_children[i]["spec_markdown"], cid,
            )
            child_note = f"split from {ticket.id}"
            if auto_note:
                child_note += f" | {auto_note}"
            ctx.service.transition(cid, child_state, note=child_note)

        # Apply epic body immediately in split path regardless of
        # require_approval — the children each go through their own
        # approval flow, and the original ticket is closed so there
        # is no single approval event to gate on.
        if result.epic_body and result.epic_body.strip() and epic_ctx:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == "epic":
                new_hash = ctx.service.workspace(parent).write_description(
                    result.epic_body.strip()
                )
                ctx.service.set_content_hash(parent.id, new_hash)

        # Close the original ticket.
        ids_note = ", ".join(child_ids)
        return Outcome(
            State.CLOSED,
            f"split into {ids_note}",
        )
