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

from .. import langfuse_client
from ..agents import retrospecting
from ..core.models import Ticket
from ..core.states import State
from ..core.text_noop import is_noop_report
from ..core.text_utils import truncate_at_boundary
from ..core.workspace import prune_clone
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.retrospect")

# Word-to-number mapping for parsing count claims like "Eleven tickets".
_WORD_TO_NUM: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}


def _parse_numeric_count(text: str) -> int | None:
    """Extract a numeric ticket-count claim from text.

    Looks for patterns like ``3 tickets``, ``Eleven tickets``, or
    ``ten tickets now demonstrate…``.  Returns the integer count or
    *None* if no claim is found.
    """
    # Word numbers (case-insensitive): "Eleven tickets"
    pattern_words = r'\b(' + '|'.join(_WORD_TO_NUM) + r')\b\s+tickets?\b'
    m = re.search(pattern_words, text, re.IGNORECASE)
    if m:
        return _WORD_TO_NUM[m.group(1).lower()]
    # Digit numbers: "3 tickets"
    m = re.search(r'\b(\d+)\s+tickets?\b', text)
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
    for m in re.finditer(r'^\s*[-*]\s+`([^`]+)`', text, re.MULTILINE):
        ids.add(m.group(1))
    # Bare ID starting a bullet line (fallback): - TKT-001: note
    for m in re.finditer(
        r'^\s*[-*]\s+([A-Za-z][A-Za-z0-9_.-]*\d+[A-Za-z0-9_.-]*)\b', text, re.MULTILINE
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
    sections = re.split(r'\n(?=## )', memory_text)

    for section in sections:
        heading_match = re.match(r'##\s+(.+)', section)
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
                preview_str = ', '.join(preview_ids) + ', …'
            else:
                preview_str = ', '.join(preview_ids) if preview_ids else '(none)'

            warnings.append(
                f"Memory count drift in issue '{issue_heading}': "
                f"Assessment claims {count_claim} ticket(s), "
                f"evidence list has {actual_count} distinct ID(s) "
                f"[{preview_str}]"
            )

    return warnings

# No-op detection (markers + logic) lives in core.text_noop — a single
# source of truth shared with the report_issue tool so the two can't
# drift. Kept as a thin title-only shim here (the spawn guard and the
# existing test call it as _is_noop_draft(title, body); body is and was
# ignored — title-only by design, no length heuristics).
def _is_noop_draft(title: str | None, body: str | None = None) -> bool:
    """The retrospect model sometimes sets propose_draft=true with a
    "No notable issues - clean run" title — noise, not a ticket. Defers
    to the shared :func:`is_noop_report` (title-only)."""
    return is_noop_report(title)


class RetrospectStage(Stage):
    name = "retrospect"
    input_state = State.DONE

    # ------------------------------------------------------------------
    # deep-analysis frequency gate
    # ------------------------------------------------------------------

    @staticmethod
    def _deep_counter_path(settings: Settings) -> Path:
        return settings.data_dir / "retrospect_deep_counter"

    @staticmethod
    def _read_deep_counter(settings: Settings) -> int:
        """Read the deep-analysis counter file. Returns 0 if missing
        or corrupted (logs a warning for the corrupted case)."""
        path = RetrospectStage._deep_counter_path(settings)
        try:
            if path.exists():
                raw = path.read_text(encoding="utf-8").strip()
                return int(raw)
        except (ValueError, OSError) as e:
            log.warning(
                "retrospect deep counter corrupted (%s) — resetting to 0", e
            )
        return 0

    @staticmethod
    def _write_deep_counter(settings: Settings, value: int) -> None:
        """Write the deep-analysis counter file."""
        path = RetrospectStage._deep_counter_path(settings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(value), encoding="utf-8")
        except OSError:
            log.warning("could not write deep counter to %s", path)

    # ------------------------------------------------------------------

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

        # --- deep-analysis frequency gate ------------------------------
        deep_analysis = False
        trace_ids: list[str] = []
        counter = self._read_deep_counter(s)
        frequency = s.retrospect_deep_analysis_frequency
        if counter >= frequency:
            deep_analysis = True
            self._write_deep_counter(s, 0)
            # Fetch the session trace list to extract trace IDs.
            traces_data = langfuse_client._langfuse_api_get(
                s,
                "/api/public/traces",
                params={"sessionId": ticket.id, "limit": 100},
            )
            if traces_data:
                for t in traces_data.get("data", []):
                    tid = t.get("id")
                    if tid:
                        trace_ids.append(tid)
        else:
            self._write_deep_counter(s, counter + 1)
        # ---------------------------------------------------------------

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
                deep_analysis=deep_analysis,
                trace_ids=trace_ids,
            )
        except Exception as e:  # noqa: BLE001 — resumable, never lose the ticket
            log.exception("%s: retrospect agent failed", ticket.id)
            return Outcome(State.BLOCKED, f"retrospect failed — resumable: {e}")

        # Advisory consistency check: warn on count drift between
        # Assessment claims and evidence lists (non-blocking).
        drift_warnings = _check_memory_count_consistency(res.updated_memory)
        for w in drift_warnings:
            log.warning("%s: %s", ticket.id, w)

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
