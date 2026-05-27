"""Refine stage: raw DRAFT -> actionable READY ticket.

Rewrites the file-canonical ``description.md`` into a precise spec the
implement agent can act on unattended; the original draft is kept as an
artifact for traceability. Empty draft or missing OpenRouter key ->
BLOCKED with a clear note (not a crash).

When ``require_approval`` is true (the default), the refined ticket
enters ``human_issue_approval`` instead of ``ready`` — a human must approve
before the implement stage picks it up.

Before the expensive refine agent runs, a cheap **dedup / already-done
check** inspects the draft against existing tickets. If the draft is a
clear duplicate or the change is already covered by a recently-closed
ticket, the ticket is short-circuited to ``CLOSED`` — no refiner, no
human approval gate, no wasted cost.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..agents import dedup
from ..agents import refining
from ..core.datetime_utils import _as_utc
from ..core.models import Ticket
from ..core.states import State
from ..forge.auth import _resolve_remote_url, github_token
from ..pass_runner import load_memory, persist_memory
from ..vcs import git_ops
from .pause import (
    check_for_pause, save_conversation_state,
    load_conversation_state, build_resume_message_history,
)
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.refine")


def _resolve_next_state(
    ctx: StageContext, spec: str, ticket_id: str,
) -> tuple[State, str | None]:
    """Return (next_state, auto_approve_note_or_None).

    Encapsulates the decision: if approval is not required → READY;
    if auto-approve is disabled → HUMAN_ISSUE_APPROVAL; otherwise run
    the auto-approve triage on the spec → READY on APPROVE (no design
    decision found), HUMAN_ISSUE_APPROVAL otherwise (or on error).
    Empty/whitespace specs skip the triage entirely and go to
    HUMAN_ISSUE_APPROVAL when gated, mirroring the original behaviour.

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
        if result.decision == "APPROVE":
            return State.READY, f"auto-approve: APPROVE — {result.reason}"
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
        # Honour the per-ticket repo_config so multi-repo tickets clone
        # their own repo, not the mill's own (same bug fixed for
        # implement/deliver in 4518cd1).
        s = ctx.settings
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        repo_dir = None
        if remote_url:
            cand = ws.dir / "repo"
            if (cand / ".git").exists():
                repo_dir = cand  # idempotent: reuse an existing clone
            else:
                try:
                    try:
                        token = github_token(s, repo_config=ctx.repo_config)
                    except RuntimeError:
                        token = None  # no credentials configured — clone will fail
                    git_ops.clone(
                        remote_url, cand,
                        s.forge_target_branch, token,
                    )
                    repo_dir = cand
                except subprocess.CalledProcessError as e:
                    # Escalate clone failure to BLOCKED — running refine
                    # with no repo grounds the agent's system prompt
                    # against tools that aren't registered (the
                    # `tools=[]` path in refining.py:385). The result is
                    # an inconsistent, tool-less refine that wastes
                    # tokens. Surface the cause to the operator instead.
                    reason = (
                        f"refine clone failed: {(e.stderr or '').strip()[:200]}"
                    )
                    log.warning("%s: %s", ticket.id, reason)
                    ctx.service.add_comment(
                        ticket.id,
                        f"[refine] {reason}\n\n"
                        "The refine stage needs a working repo clone to "
                        "ground the spec in actual code. Fix the underlying "
                        "cause (permissions, credentials, disk space, "
                        "network), then `resume-blocked` to re-run refine.",
                        author="refine",
                    )
                    return Outcome(State.BLOCKED, reason)

        # --- dedup / already-done guard (best-effort) ---
        # Skip dedup entirely for trivial drafts — a title-only or
        # near-empty draft has near-zero chance of being a duplicate
        # worth detecting and the dedup LLM call's cost dwarfs the value.
        if len(draft) < 100:
            log.debug(
                "%s: trivial draft (%d chars), skipping dedup",
                ticket.id, len(draft),
            )
        else:
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

            try:
                verdict = dedup.run_dedup_check(
                    settings=s,
                    draft_title=ticket.title,
                    draft_body=draft,
                    candidates_json=candidates_json,
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
        # output.  When children are reparented to an umbrella epic
        # the direct parent is no longer CLOSED, so also check the
        # ticket's own history for a "split from" transition note.
        # We must NOT short-circuit for retrospect-spawned drafts
        # (whose parent is also CLOSED but for a different reason and
        # whose description is a raw draft, not a spec).
        is_split_child = False
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
        if not is_split_child:
            # Fallback: check the ticket's own history for a
            # "split from" note (children reparented to an epic).
            own_history = ctx.service.history(ticket.id)
            is_split_child = any(
                ev.note
                and ev.note.startswith("split from")
                for ev in own_history
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
            # Split children skip the refine agent — but implement still
            # demands a file_map.json. Write an empty one so the
            # downstream gate treats this as scope-free mode rather
            # than "refine broken" → BLOCKED.
            file_map_path = ws.artifacts_dir / "file_map.json"
            if not file_map_path.exists():
                file_map_path.write_text("[]", encoding="utf-8")
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
                # Only count non-closed top-level threads for sendback detection.
                open_threads = [
                    c for c in comments
                    if c.parent_id is None and c.closed_at is None
                ]
                if open_threads:
                    closed_ids = {c.id for c in comments if c.closed_at is not None}
                    reviewer_comments = "\n".join(
                        f"[id={c.id} @ {c.created_at.isoformat()}] {c.body}"
                        for c in comments
                        if c.id not in closed_ids and c.parent_id not in closed_ids
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
                    # Try to extract backtick-quoted file paths from
                    # the draft so the implement stage can enforce
                    # scope even when we skip the refine agent.
                    # Pattern: backtick-quoted strings that look like
                    # file paths (contain a '/' directory separator
                    # and a file extension).
                    _PATH_RE = re.compile(
                        r'`([^`]*/[^`]*\.[a-zA-Z]{1,10})`'
                    )
                    extracted = _PATH_RE.findall(draft)
                    if extracted:
                        file_map_path = ws.artifacts_dir / "file_map.json"
                        if not file_map_path.exists():
                            file_map_path.write_text(
                                json.dumps(
                                    [{"file": p, "note": "from draft"}
                                     for p in extracted],
                                    indent=2,
                                ),
                                encoding="utf-8",
                            )
                        next_state, auto_note = _resolve_next_state(
                            ctx, draft, ticket.id,
                        )
                        note = f"triage SKIP: {triage.reason}"
                        if auto_note:
                            note += f" | {auto_note}"
                        return Outcome(next_state, note)
                    # No paths extracted — fall through to the refine
                    # agent (do NOT write an empty file_map).  The
                    # refine agent will explore the codebase and
                    # produce a proper file_map with real file
                    # exploration behind it.
                    log.info(
                        "%s: triage SKIP but no file paths in draft "
                        "— falling through to refine agent for "
                        "file_map",
                        ticket.id,
                    )
            except Exception:
                log.warning(
                    "%s: triage failed, falling through to full refine",
                    ticket.id, exc_info=True,
                )
        # --- end triage ---

        # --- run the refine agent ---
        refine_memory_path = s.memory_file_for(
            "refine", ctx.repo_config.board_id if ctx.repo_config else ""
        )
        memory_text = load_memory(refine_memory_path, max_chars=s.max_memory_chars)

        extra_roots: list[Path] | None = None

        # --- resume awareness: detect if returning from a pause ---
        resume_history: list | None = None
        saved_state = load_conversation_state(ws)
        if saved_state is not None:
            # Check whether the ticket is resuming from a pause by
            # looking for a prior AWAITING_USER_REPLY event in the
            # ticket history.
            own_history = ctx.service.history(ticket.id)
            was_paused = any(
                ev.state == State.AWAITING_USER_REPLY
                for ev in own_history
            )
            if was_paused:
                # Find the operator's reply: the most recent non-closed
                # comment that is NOT an [ASK_USER] marker.
                reply_text: str | None = None
                try:
                    comments = ctx.service.list_comments(ticket.id)
                    for c in reversed(comments):
                        if c.closed_at is not None:
                            continue
                        body = (c.body or "").strip()
                        if body.startswith("[ASK_USER]"):
                            continue
                        reply_text = body
                        break
                except Exception:
                    log.warning(
                        "%s: list_comments failed during resume, "
                        "proceeding without operator reply",
                        ticket.id,
                    )
                if reply_text is None:
                    reply_text = "(no operator reply found)"
                resume_history = build_resume_message_history(
                    saved_state, reply_text,
                )
                log.info(
                    "%s: resuming refine from pause — "
                    "loaded %d-byte conversation state",
                    ticket.id, len(saved_state),
                )

        try:
            result = refining.run_refine_agent(
                settings=s, title=ticket.title, draft=draft,
                repo_dir=repo_dir,
                reviewer_comments=reviewer_comments,
                memory=memory_text,
                epic_context=epic_ctx,
                extra_roots=extra_roots,
                message_history=resume_history,
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            return Outcome(State.BLOCKED, str(e))

        # --- pause detection ---
        if check_for_pause(result.conversation_state):
            save_conversation_state(ws, result.conversation_state)
            ctx.service.transition(
                ticket.id, State.AWAITING_USER_REPLY,
                note="paused — agent asked a clarifying question",
            )
            log.info(
                "%s: paused refine — agent invoked ask_user",
                ticket.id,
            )
            return Outcome(State.AWAITING_USER_REPLY)

        if result.updated_memory:
            persist_memory(refine_memory_path, result.updated_memory)

        if result.title and result.title.strip():
            ctx.service.set_title(ticket.id, result.title.strip())

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

        # --- write reference_files artifact ---
        if result.reference_files:
            ref_path = ws.artifacts_dir / "reference_files.json"
            ref_path.write_text(
                json.dumps(
                    [{"path": p} for p in result.reference_files],
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

        # Reparent children: if the ticket already belongs to an
        # epic, reparent to that epic; otherwise create a new
        # umbrella epic so children appear under a visible grouping
        # entity rather than a closed parent.
        existing_epic_id: str | None = None
        if ticket.parent_id is not None:
            parent_candidate = ctx.service.get(ticket.parent_id)
            if parent_candidate is not None and parent_candidate.kind == "epic":
                existing_epic_id = ticket.parent_id
                for cid in child_ids:
                    ctx.service.set_parent(cid, existing_epic_id)
        if existing_epic_id is None:
            epic_title = (
                (result.title and result.title.strip())
                or ticket.title.strip()
            )
            epic_desc = (
                (result.spec_markdown and result.spec_markdown.strip())
                or draft
            )
            epic = ctx.service.create(
                title=epic_title,
                description=epic_desc,
                kind="epic",
                source=ticket.source,
            )
            for cid in child_ids:
                ctx.service.set_parent(cid, epic.id)

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
