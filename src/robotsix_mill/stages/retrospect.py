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
import re

from ..langfuse import client as langfuse_client
from ..agents import retrospecting
from ..agents.retrospecting import MemoryEdit, RetrospectResult
from ..config import Settings, get_repo_config
from ..config import ConfigError
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..core.text_noop import is_noop_report
from ..core.text_utils import truncate_at_boundary
from ..core.workspace import prune_clone
from ..core.draft_target import looks_like_mill_internal, resolve_mill_service
from ..forge import get_forge
from ..runtime.tracing import current_session
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.retrospect")


# States that count as "done with" for dedup purposes — ticket titles
# that match an existing ticket in one of these states are considered
# already resolved/filed and won't block re-filing.
_DONE_WITH = {"closed", "done"}

# Word-to-number mapping for parsing count claims like "Eleven tickets".
_WORD_TO_NUM: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "twenty-one": 21,
    "twenty-two": 22,
    "twenty-three": 23,
    "twenty-four": 24,
    "twenty-five": 25,
    "twenty-six": 26,
    "twenty-seven": 27,
    "twenty-eight": 28,
    "twenty-nine": 29,
    "thirty-one": 31,
    "thirty-two": 32,
    "thirty-three": 33,
    "thirty-four": 34,
    "thirty-five": 35,
    "thirty-six": 36,
    "thirty-seven": 37,
    "thirty-eight": 38,
    "thirty-nine": 39,
    "forty-one": 41,
    "forty-two": 42,
    "forty-three": 43,
    "forty-four": 44,
    "forty-five": 45,
    "forty-six": 46,
    "forty-seven": 47,
    "forty-eight": 48,
    "forty-nine": 49,
    "fifty-one": 51,
    "fifty-two": 52,
    "fifty-three": 53,
    "fifty-four": 54,
    "fifty-five": 55,
    "fifty-six": 56,
    "fifty-seven": 57,
    "fifty-eight": 58,
    "fifty-nine": 59,
    "sixty-one": 61,
    "sixty-two": 62,
    "sixty-three": 63,
    "sixty-four": 64,
    "sixty-five": 65,
    "sixty-six": 66,
    "sixty-seven": 67,
    "sixty-eight": 68,
    "sixty-nine": 69,
    "seventy-one": 71,
    "seventy-two": 72,
    "seventy-three": 73,
    "seventy-four": 74,
    "seventy-five": 75,
    "seventy-six": 76,
    "seventy-seven": 77,
    "seventy-eight": 78,
    "seventy-nine": 79,
    "eighty-one": 81,
    "eighty-two": 82,
    "eighty-three": 83,
    "eighty-four": 84,
    "eighty-five": 85,
    "eighty-six": 86,
    "eighty-seven": 87,
    "eighty-eight": 88,
    "eighty-nine": 89,
    "ninety-one": 91,
    "ninety-two": 92,
    "ninety-three": 93,
    "ninety-four": 94,
    "ninety-five": 95,
    "ninety-six": 96,
    "ninety-seven": 97,
    "ninety-eight": 98,
    "ninety-nine": 99,
}


def _tail_truncate_log(text: str, max_chars: int) -> str:
    """Cap *text* to its most-recent ``max_chars`` characters.

    History and comment logs are chronological — the recent tail is what
    matters for a retrospective — so the oldest lines are dropped and the
    newest kept (mirroring the tail-keep semantics of ``load_memory``).
    Truncation aligns to a line boundary and prepends a short omission
    note.  ``max_chars <= 0`` disables capping (returns *text* unchanged).
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut_point = len(text) - max_chars
    nl_idx = text.find("\n", cut_point)
    kept = text[nl_idx + 1 :] if nl_idx != -1 else text[cut_point:]
    omitted_lines = text[: len(text) - len(kept)].count("\n") + 1
    return f"[... {omitted_lines} earlier lines omitted]\n{kept}"


def _parse_numeric_count(text: str) -> int | None:
    """Extract a numeric ticket-count claim from text.

    Looks for patterns like ``3 tickets``, ``Eleven tickets``, or
    ``ten tickets now demonstrate…``.  Returns the integer count or
    *None* if no claim is found.
    """
    # Word numbers (case-insensitive): "Eleven tickets"
    pattern_words = r"\b(" + "|".join(_WORD_TO_NUM) + r")\b\s+tickets?\b"
    m = re.search(pattern_words, text, re.IGNORECASE)
    if m:
        return _WORD_TO_NUM[m.group(1).lower()]
    # Digit numbers: "3 tickets"
    m = re.search(r"\b(\d+)\s+tickets?\b", text)
    if m:
        return int(m.group(1))
    return None


def _extract_ticket_ids(text: str) -> set[str]:
    """Extract distinct ticket IDs from evidence-list bullets.

    Primary format: ``- `TKT-001``` (backtick-wrapped ID on a bullet).
    Fallback: ``- TKT-001: some note`` (bare ID on a bullet line).
    """
    ids: set[str] = set()
    # Backtick-wrapped: - `TKT-001`
    for m in re.finditer(r"^\s*[-*]\s+`([^`]+)`", text, re.MULTILINE):
        ids.add(m.group(1))
    # Bare ID starting a bullet line (fallback): - TKT-001: note
    for m in re.finditer(
        r"^\s*[-*]\s+([A-Za-z][A-Za-z0-9_.-]*\d+[A-Za-z0-9_.-]*)\b", text, re.MULTILINE
    ):
        ids.add(m.group(1))
    return ids


def _check_memory_count_consistency(memory_text: str) -> list[str]:
    """Check the memory ledger for Assessment-vs-evidence count drift.

    For each issue section (``## …`` heading) in *memory_text*, extracts
    any numeric ticket-count claim from the section body and compares it
    to the number of distinct ticket IDs found in the evidence bullets.
    Returns a (possibly empty) list of human-readable warning strings,
    one per drifted issue.
    """
    if not memory_text or not memory_text.strip():
        return []

    warnings: list[str] = []
    # Split on "## " at line start — each issue section begins with a
    # level-2 Markdown heading.
    sections = re.split(r"\n(?=## )", memory_text)

    for section in sections:
        heading_match = re.match(r"##\s+(.+)", section)
        if not heading_match:
            continue
        issue_heading = heading_match.group(1).strip()

        count_claim = _parse_numeric_count(section)
        if count_claim is None:
            continue  # No numeric claim → nothing to check

        ticket_ids = _extract_ticket_ids(section)
        actual_count = len(ticket_ids)

        if count_claim != actual_count:
            preview_ids = sorted(ticket_ids)
            if len(preview_ids) > 5:
                preview_ids = preview_ids[:5]
                preview_str = ", ".join(preview_ids) + ", …"
            else:
                preview_str = ", ".join(preview_ids) if preview_ids else "(none)"

            warnings.append(
                f"Memory count drift in issue '{issue_heading}': "
                f"Assessment claims {count_claim} ticket(s), "
                f"evidence list has {actual_count} distinct ID(s) "
                f"[{preview_str}]"
            )

    return warnings


def _apply_memory_edits(
    existing: str, edits: list[MemoryEdit]
) -> tuple[str, list[str]]:
    """Apply targeted ``memory_edits`` to *existing* ledger text.

    Processes edits in list order against a running string seeded with
    *existing*. Matching is exact verbatim substring (first occurrence);
    no fuzzy matching, regex, or normalization. Never raises — returns
    ``(new_text, failures)`` where *failures* is a list of human-readable
    strings, one per edit that could not be applied.
    """
    running = existing
    failures: list[str] = []
    for i, edit in enumerate(edits):
        if edit.op == "append":
            if running.rstrip():
                running = running.rstrip() + "\n\n" + edit.text
            else:
                running = edit.text
        elif edit.op == "replace":
            if not edit.find or edit.find not in running:
                failures.append(f"edit {i} (replace): find text not found or empty")
                continue
            running = running.replace(edit.find, edit.text, 1)
        elif edit.op == "remove":
            if not edit.find or edit.find not in running:
                failures.append(f"edit {i} (remove): find text not found or empty")
                continue
            running = running.replace(edit.find, "", 1)
            # Collapse triple-or-more newlines left by the removal so
            # blank-line spacing stays clean.
            running = re.sub(r"\n{3,}", "\n\n", running)
        else:  # pragma: no cover — Literal type forbids other values
            failures.append(f"edit {i}: unknown op {edit.op!r}")
    return running, failures


# No-op detection (markers + logic) lives in core.text_noop — a single
# source of truth shared with the report_issue tool so the two can't
# drift.
def _is_noop_draft(title: str | None) -> bool:
    """The retrospect model sometimes sets propose_draft=true with a
    "No notable issues - clean run" title — noise, not a ticket. Defers
    to the shared :func:`is_noop_report` (title-only)."""
    return is_noop_report(title)


class RetrospectStage(Stage):
    """Run a deep-analysis retrospective on completed tickets and optionally spawn follow-up draft tickets."""

    name = "retrospect"
    input_state = State.DONE

    # ------------------------------------------------------------------
    # deep-analysis frequency gate
    # ------------------------------------------------------------------

    def _maybe_spawn_draft(
        self,
        res: RetrospectResult,
        ticket: Ticket,
        settings: Settings,
        ctx: StageContext,
    ) -> str | None:
        """Conditionally spawn a draft ticket from the agent's proposal.

        Returns the spawned draft ID, or None if no draft was created.
        """
        # First guard — spawn conditions not met.
        if not (
            settings.retrospect_spawn_drafts
            and res.propose_draft
            and res.draft_title
            and res.draft_body
        ):
            return None

        # Second guard — model proposed a no-op / clean-run draft.
        if _is_noop_draft(res.draft_title):
            log.info(
                "%s: retrospect proposed a no-op draft %r — skipped",
                ticket.id,
                res.draft_title,
            )
            return None

        # Happy path: build body, create ticket on the target board,
        # set parent if on the same board, log, and return the ID.
        body = res.draft_body
        if res.draft_gap_id:
            body += f"\n\n<!-- retrospect-gap-id: {res.draft_gap_id} -->"
        # Safety net: override ``"current"`` → ``"mill"`` when the
        # draft's title+body names mill internals. See
        # looks_like_mill_internal docstring for the rationale.
        draft_target = res.draft_target
        if draft_target == "current" and looks_like_mill_internal(
            res.draft_title, body
        ):
            log.info(
                "%s: retrospect draft_target auto-corrected current→mill "
                "(draft body names mill-internal symbols)",
                ticket.id,
            )
            draft_target = "mill"
        if draft_target == "mill":
            target_service = (
                resolve_mill_service(settings, ctx.service, caller_label="retrospect")
                or ctx.service
            )
        else:
            target_service = ctx.service
        draft = target_service.create(
            res.draft_title,
            body,
            source=SourceKind.RETROSPECT,
            origin_session=current_session(),
        )
        # Only set the parent link when the draft lives on the same
        # board as the originating ticket — cross-board parents would
        # dangle (the parent lookup is per-board).
        if target_service.board_id == ctx.service.board_id:
            ctx.service.set_parent(draft.id, ticket.id)
        log.info(
            "%s: retrospect spawned draft %s on %s",
            ticket.id,
            draft.id,
            target_service.board_id or "<default>",
        )
        return draft.id

    def _maybe_spawn_follow_up(
        self,
        res: RetrospectResult,
        ticket: Ticket,
        settings: Settings,
        ctx: StageContext,
    ) -> str | None:
        """Conditionally file a concrete incomplete-work follow-up ticket.

        Returns the spawned draft ID, or None if no follow-up was created.
        """
        if not res.follow_up_title or not res.follow_up_body:
            return None

        follow_up_title = res.follow_up_title.strip()
        follow_up_body = res.follow_up_body

        # Same auto-correction as the draft path: a follow-up that
        # names mill internals belongs on the mill board.
        follow_up_target = res.follow_up_target
        if follow_up_target == "current" and looks_like_mill_internal(
            follow_up_title, follow_up_body
        ):
            log.info(
                "%s: retrospect follow_up_target auto-corrected "
                "current→mill (body names mill-internal symbols)",
                ticket.id,
            )
            follow_up_target = "mill"
        if follow_up_target == "mill":
            target_service = (
                resolve_mill_service(settings, ctx.service, caller_label="retrospect")
                or ctx.service
            )
        else:
            target_service = ctx.service

        # Dedup: skip if an open (non-closed, non-done) ticket with the
        # same case-insensitive title already exists ON THE TARGET
        # BOARD. Dedup is per-board because two ticket-databases can
        # legitimately carry identically-titled follow-ups for
        # different concerns; routing to "mill" should still dedupe
        # against mill's own backlog.
        norm = follow_up_title.casefold()
        for t in target_service.list():
            if t.title.strip().casefold() == norm and t.state.value not in _DONE_WITH:
                log.info(
                    "%s: retrospect follow-up already filed as %s (state=%s) — not duplicating",
                    ticket.id,
                    t.id,
                    t.state.value,
                )
                return None

        draft = target_service.create(
            follow_up_title,
            follow_up_body,
            source=SourceKind.RETROSPECT,
            origin_session=current_session(),
        )
        if target_service.board_id == ctx.service.board_id:
            ctx.service.set_parent(draft.id, ticket.id)
        log.info(
            "%s: retrospect spawned follow-up %s on %s",
            ticket.id,
            draft.id,
            target_service.board_id or "<default>",
        )
        return draft.id

    def _suppress_duplicate_agented_proposals(
        self,
        res: RetrospectResult,
        ticket: Ticket,
        settings: Settings,
        ctx: StageContext,
    ) -> None:
        """Drop AGENT.md proposals that duplicate a recently-filed or
        in-flight proposal before the ticket-filing sink writes.

        Runs once over ``res.agented_md_proposals`` and reassigns it to
        the surviving (non-duplicate) proposals so the downstream
        ticket-filing call sees only the survivors. Each suppression is
        recorded in ``res.findings`` (persisted into retrospect.md).

        Reuses the shared
        :func:`robotsix_mill.core.dedup.find_agent_md_proposal_overlap` seam
        — no second matcher. Best-effort: a dedup-query failure inside
        the helper logs and returns ``None`` (fall through to filing).
        """
        if not settings.retrospect_spawn_agented_proposals:
            return
        proposals = res.agented_md_proposals
        if not proposals:
            return

        from datetime import datetime, timezone

        from ..core.dedup import find_agent_md_proposal_overlap

        now = datetime.now(timezone.utc)
        kept: list[dict] = []
        for prop in proposals:
            section = prop.get("section", "")
            rule = prop.get("rule", "")
            matched = find_agent_md_proposal_overlap(
                ctx.service,
                section,
                rule,
                settings,
                now,
                exclude_ids={ticket.id},
            )
            if matched is None:
                kept.append(prop)
                continue
            rule_snippet = " ".join(rule.split())
            if len(rule_snippet) > 80:
                rule_snippet = rule_snippet[:79].rstrip() + "…"
            res.findings += (
                f"\nSuppressed duplicate AGENT.md proposal "
                f"({section} — {rule_snippet}) — scope-equivalent to "
                f"{matched.id}."
            )
            log.info(
                "%s: suppressed duplicate AGENT.md proposal "
                "(%s — %s) — scope-equivalent to %s",
                ticket.id,
                section,
                rule_snippet,
                matched.id,
            )
        res.agented_md_proposals = kept

    def _maybe_spawn_agented_proposal_tickets(
        self,
        res: RetrospectResult,
        ticket: Ticket,
        settings: Settings,
        ctx: StageContext,
    ) -> list[str]:
        """File a draft ticket per AGENT.md proposal on the originating
        repo's board.

        Gated by the ``retrospect_spawn_agented_proposals`` flag.
        AGENT.md proposals are always
        relative to the repo retrospect just audited, so they are filed
        on ``ctx.service`` — for a registered-repo run
        ``ctx.service.board_id == ctx.repo_config.board_id``, i.e. *this*
        repo's board. No mill-routing is applied: a proposal made for
        repo X must land on repo X's board, never on mill.

        Returns the list of filed draft IDs (possibly empty).
        """
        if not settings.retrospect_spawn_agented_proposals:
            return []
        proposals = res.agented_md_proposals
        if not proposals:
            return []

        # File on the originating repo's board only — proposals are
        # board-scoped via ctx; no looks_like_mill_internal routing.
        target_service = ctx.service
        spawned: list[str] = []
        for prop in proposals:
            section = prop.get("section", "")
            rule = prop.get("rule", "")
            rationale = prop.get("rationale", "")

            section_label = section.lstrip("#").strip() or "AGENT.md"
            rule_snippet = " ".join(rule.split())  # collapse whitespace
            if len(rule_snippet) > 80:
                rule_snippet = rule_snippet[:79].rstrip() + "…"
            if rule_snippet:
                title = f"AGENT.md: {section_label} — {rule_snippet}"
            else:
                title = f"AGENT.md: {section_label} — proposed rule"

            body = (
                f"### Proposed addition to {section}\n\n"
                f"> **Rule:** {rule}\n\n"
                f"**Rationale:** {rationale}\n\n"
                f"**Provenance:** proposed by retrospect from {ticket.id}\n"
            )

            # Dedup: skip if an open (non-done/non-closed) ticket with the
            # same case-insensitive title already exists on the target
            # board — same pattern as _maybe_spawn_follow_up. Re-querying
            # list() each iteration also dedupes proposals filed earlier
            # in this same loop.
            norm = title.strip().casefold()
            existing = next(
                (
                    t
                    for t in target_service.list()
                    if t.title.strip().casefold() == norm
                    and t.state.value not in _DONE_WITH
                ),
                None,
            )
            if existing is not None:
                log.info(
                    "%s: AGENT.md proposal already filed as %s (state=%s) "
                    "— not duplicating",
                    ticket.id,
                    existing.id,
                    existing.state.value,
                )
                continue

            draft = target_service.create(
                title,
                body,
                source=SourceKind.RETROSPECT,
                origin_session=current_session(),
            )
            # Same board as the originating ticket → parent link is safe.
            ctx.service.set_parent(draft.id, ticket.id)
            spawned.append(draft.id)
            log.info(
                "%s: retrospect filed AGENT.md proposal ticket %s on %s",
                ticket.id,
                draft.id,
                target_service.board_id or "<default>",
            )
        return spawned

    # ------------------------------------------------------------------

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Run a retrospective over a DONE ticket's history, comments, and description; emit findings and optionally spawn follow-up draft tickets."""
        s = ctx.settings
        ws = ctx.service.workspace(ticket)

        # Defensive multi-repo verification: the merge stage's
        # aggregator already gated this — but a manual BLOCKED→DONE
        # override could skip it.  When ``pr_urls.json`` exists,
        # re-confirm every listed PR is actually merged before the
        # retrospect agent runs.  Forge-call exceptions during this
        # check do NOT block — transient retry is the merge stage's
        # job, not retrospect's.
        from ..stages.merge import _load_pr_urls

        try:
            pr_entries = _load_pr_urls(ws.artifacts_dir)
        except ValueError as e:
            return Outcome(
                State.BLOCKED,
                f"pr_urls.json corrupted in retrospect — resumable: {e}",
            )
        if pr_entries:
            unmerged: list[str] = []
            for entry in pr_entries:
                try:
                    rc = get_repo_config(entry["repo_id"])
                except ConfigError:
                    unmerged.append(f"{entry['repo_id']} (unknown repo)")
                    continue
                try:
                    pr = get_forge(s, repo_config=rc).pr_status(
                        source_branch=entry["branch"]
                    )
                except Exception:  # noqa: BLE001 — transient is merge's job
                    continue
                if pr is None or not pr.get("merged"):
                    unmerged.append(f"{entry['repo_id']}: {entry['url']}")
            if unmerged:
                return Outcome(
                    State.BLOCKED,
                    f"retrospect refusing to close — {len(unmerged)} PR(s) "
                    "not merged: " + "; ".join(unmerged),
                )

        history = ctx.service.history(ticket.id)
        history_text = "\n".join(
            f"{e.at:%Y-%m-%d %H:%M} {e.state} {e.note or ''}".rstrip() for e in history
        )
        # Cap to the most-recent tail — every state transition ever
        # recorded is otherwise fed in uncapped.
        history_text = _tail_truncate_log(history_text, s.retrospect_log_max_chars)
        # Fetch comments
        comments = ctx.service.list_comments(ticket.id)
        if comments:
            comments_text = "\n".join(
                f"{c.created_at:%Y-%m-%d %H:%M} | {c.body}".rstrip() for c in comments
            )
        else:
            comments_text = ""
        # Cap to the most-recent tail — every comment body is otherwise
        # fed in verbatim.
        comments_text = _tail_truncate_log(comments_text, s.retrospect_log_max_chars)

        desc = ws.read_description()
        if desc:
            desc = truncate_at_boundary(desc, 6000)
        ticket_summary = (
            f"id: {ticket.id}\ntitle: {ticket.title}\nbranch: {ticket.branch}\n\n{desc}"
        )
        # Per-trace deep inspection is now handled by the periodical
        # pipeline (trace_health_runner + expensive-item detector).
        # The retrospect only receives the pre-computed session summary.
        lf = langfuse_client.fetch_session_summary(
            s, ticket.id, repo_config=ctx.repo_config
        )

        # Retrieve epic context and sibling sub-issues so the agent can
        # cross-reference incomplete-work findings against the parent
        # epic's scope and planned sibling work.
        epic_ctx = ctx.service.get_epic_context(ticket)
        sibling_ctx = ""
        if ticket.parent_id:
            siblings = ctx.service.list_children(ticket.parent_id)
            others = [sib for sib in siblings if sib.id != ticket.id]
            if others:
                lines = ["<epic_siblings>"]
                for sib in others:
                    title = sib.title.strip()
                    if len(title) > 80:
                        title = title[:77] + "..."
                    lines.append(f"- `{sib.id}` [{sib.state.value}] {title}")
                lines.append("</epic_siblings>")
                sibling_ctx = "\n".join(lines)

        # Read current memory through the shared helper — returns "" on
        # missing/unreadable files and tail-truncates to max_memory_chars
        # (keeps the most-recent entries), matching every other stage.
        from ..runners.pass_runner import load_memory, persist_memory

        memory_file = s.memory_file_for("retrospect", ctx.memory_board_id(ticket))
        memory_text = load_memory(memory_file, max_chars=s.max_memory_chars)

        # Verify prior proposals and prepend verified-state table.
        from ..runners.pass_runner import (
            _verify_prior_proposals,
            _render_verified_summary,
            _format_recent_proposals,
        )

        # Render a one-line verified-state summary as an EPHEMERAL kwarg
        # passed to the agent separately from memory. Concatenating it
        # into memory_text would round-trip through ``updated_memory`` and
        # bake the DB-derived data into the persisted ledger — see the
        # matching note in ``pass_runner.run_agent_pass``.
        verified = _verify_prior_proposals(ctx.service, s, SourceKind.RETROSPECT)
        verified_block = _render_verified_summary(verified) if verified else ""

        # Build recent-proposals block for prompt injection.
        recent = ctx.service.recent_proposals_for(SourceKind.RETROSPECT, limit=100)
        rp_block = _format_recent_proposals(recent)

        repo_dir = ws.repo_dir if ws.repo_dir.exists() else None
        try:
            res = retrospecting.run_retrospect_agent(
                settings=s,
                ticket_summary=ticket_summary,
                history_text=history_text,
                langfuse_summary=lf,
                memory=memory_text,
                comments_text=comments_text,
                recent_proposals=rp_block,
                verified_proposals=verified_block,
                epic_context=epic_ctx,
                sibling_context=sibling_ctx,
                repo_dir=repo_dir,
            )
        except Exception as e:  # noqa: BLE001 — resumable, never lose the ticket
            log.exception("%s: retrospect agent failed", ticket.id)
            # Transient model blips get a fresh stage re-run via the
            # worker's stage-retry rather than a hard BLOCK — same fix as
            # implement.py / review.py.
            from ..runtime.transient_errors import reraise_if_transient

            reraise_if_transient(e)
            # Non-transient failure: the defensive PR-merge check already
            # confirmed all PRs are merged, so the deliverable is done.
            # Degrade to CLOSED instead of BLOCKED — the ticket's work is
            # delivered; only the post-merge audit failed.
            (ws.artifacts_dir / "retrospect.md").write_text(
                f"# Retrospect\nretrospect failed — {e!r}\n",
                encoding="utf-8",
            )
            if s.prune_clone_on_close:
                prune_clone(ws)
            return Outcome(State.CLOSED, f"retrospect failed — {e!r}")

        # Resolve the memory document to persist across the three output
        # paths (full rewrite / append-only delta / no change), stripping
        # the ephemeral verified-state table if the agent copied it back in
        # (it is injected fresh each run from the DB and must never accrete
        # in the ledger).
        from ..runners.pass_runner import strip_ephemeral_sections

        if res.updated_memory:
            # Case 3: full rewrite (existing behavior — agent modified the ledger).
            persisted = strip_ephemeral_sections(res.updated_memory)
        elif res.memory_edits:
            # Case 2b: targeted edits against the re-read ledger — the
            # preferred path for modifications (resolve / move / repair /
            # remove) without re-emitting the whole document.
            existing = ""
            try:
                if memory_file.exists():
                    existing = memory_file.read_text(encoding="utf-8")
            except OSError:
                log.warning("%s: could not re-read memory file for edits", ticket.id)
            new_text, failures = _apply_memory_edits(existing, res.memory_edits)
            for f in failures:
                log.warning("%s: memory edit failed: %s", ticket.id, f)
            persisted = strip_ephemeral_sections(new_text)
        elif res.memory_delta:
            # Case 2: append new observations to the stored ledger.
            existing = ""
            try:
                if memory_file.exists():
                    existing = memory_file.read_text(encoding="utf-8")
            except OSError:
                log.warning(
                    "%s: could not re-read memory file for delta merge", ticket.id
                )
            # Ensure clean separation: blank line between existing content and delta.
            if existing.rstrip():
                merged = existing.rstrip() + "\n\n" + res.memory_delta
            else:
                merged = res.memory_delta
            persisted = strip_ephemeral_sections(merged)
        else:
            # Case 1: no changes — nothing to write.
            persisted = ""

        # Advisory consistency check: warn on count drift between
        # Assessment claims and evidence lists (non-blocking).
        drift_warnings = _check_memory_count_consistency(persisted)
        for w in drift_warnings:
            log.warning("%s: %s", ticket.id, w)

        if persisted:
            persist_memory(memory_file, persisted, max_chars=s.max_memory_chars)

        spawned = self._maybe_spawn_draft(res, ticket, s, ctx)
        follow_up = self._maybe_spawn_follow_up(res, ticket, s, ctx)
        self._suppress_duplicate_agented_proposals(res, ticket, s, ctx)
        # File a draft ticket per AGENT.md proposal on the originating
        # repo's board so the change enters the normal refine → implement
        # pipeline.
        self._maybe_spawn_agented_proposal_tickets(res, ticket, s, ctx)

        (ws.artifacts_dir / "retrospect.md").write_text(
            f"# Retrospect\nlangfuse: "
            f"{'yes' if lf else 'workflow-only'}\n"
            f"spawned draft: {spawned or '—'}\n"
            f"follow-up: {follow_up or '—'}\n\n{res.findings}\n",
            encoding="utf-8",
        )

        if s.prune_clone_on_close:
            prune_clone(ws)

        note = res.conclusion or "closed"
        if spawned:
            note = f"{note} — improvement draft {spawned}"
        elif res.propose_draft and not s.retrospect_spawn_drafts:
            note = f"{note} — draft proposed (spawning disabled)"
        if follow_up:
            note = f"{note} — follow-up {follow_up}"
        return Outcome(State.CLOSED, note)
