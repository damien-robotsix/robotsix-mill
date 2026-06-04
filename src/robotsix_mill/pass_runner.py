"""Shared agent-pass runner.

Extracts the common boilerplate shared by the periodic-pass runners
(audit, health, agent-check): read memory, invoke agent, write memory,
create draft tickets.
Agent modules are NOT imported here — the caller provides a callable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .core.models import SourceKind, Ticket, ProposedActionStatus
from .core.service import TicketService
from .core.states import State
from .core.workspace import Workspace
from .draft_target import looks_like_mill_internal, resolve_mill_service

log = logging.getLogger("robotsix_mill.pass_runner")

# Matches <!-- {label}-gap-id: foo_bar --> style markers in ticket descriptions.
# Label is a non-whitespace run with the optional `bespoke:<name>` shape that
# bespoke agents emit, plus `trace-health` / `trace-review` /
# `cost_reconciliation` and any future SourceKind that writes a marker. The
# label-vs-source_kind comparison below filters the matches to the caller's
# scope; matching here is intentionally permissive to avoid silent drift as
# new SourceKinds are added (was a hardcoded alternation of 11 labels that
# left bespoke + 3 others silently unmatched).
_GAP_ID_RE = re.compile(r"<!--\s*(\S+)-gap-id:\s*(\S+)\s*-->")


def _verify_prior_proposals(
    service: TicketService,
    settings: Settings,
    source_label: SourceKind,
) -> dict[str, dict]:
    """Query the ticket store for drafts previously spawned by the
    agent identified by *source_label*, check their state, and return a
    mapping from ``gap_id`` → ``{ticket_id, state, resolution, branch}``.

    Only tickets whose description contains a ``<!-- {label}-gap-id:
    ... -->`` marker matching *source_label* are included.  Pre-rollout
    drafts without markers are silently skipped.
    """
    result: dict[str, dict] = {}

    # 1. List all tickets; filter client-side to matching source.
    try:
        all_tickets = service.list()
    except Exception:
        log.debug(
            "_verify_prior_proposals: service.list() failed — "
            "returning empty mapping (DB may not be initialised)"
        )
        return result
    for ticket in all_tickets:
        if ticket.source != source_label:
            continue

        # 2. Read description and parse marker.
        desc = Workspace(
            settings.workspaces_dir_for(ticket.board_id), ticket.id
        ).read_description()
        for m in _GAP_ID_RE.finditer(desc):
            marker_label, gap_id = m.group(1), m.group(2)
            if marker_label != source_label:
                continue

            # 3. Determine resolution.
            state_str = ticket.state.name
            if ticket.state == State.CLOSED:
                history = service.history(ticket.id)
                if any(ev.state == State.DONE for ev in history):
                    resolution = "merged"
                else:
                    resolution = "declined"
            elif ticket.state == State.DONE:
                resolution = "merged"
            else:
                resolution = "in-flight"

            result[gap_id] = {
                "ticket_id": ticket.id,
                "state": state_str,
                "resolution": resolution,
                "branch": ticket.branch,
            }

    return result


def _render_verified_table(verified: dict[str, dict]) -> str:
    """Render a Markdown table from the verified mapping for agent input."""
    lines = [
        "## Prior proposals — verified state",
        "",
        "| gap_id | ticket_id | state | resolution |",
        "|--------|-----------|-------|------------|",
    ]
    for gap_id, info in verified.items():
        tid = info["ticket_id"]
        if info.get("branch"):
            tid = f"{tid} (branch: {info['branch']})"
        resolution = info["resolution"]
        if resolution == "merged":
            resolution_str = "merged (via DONE)"
        elif resolution == "declined":
            resolution_str = "declined (closed directly)"
        else:
            resolution_str = "in-flight"
        lines.append(f"| {gap_id} | {tid} | {info['state']} | {resolution_str} |")
    return "\n".join(lines)


def _verify_proposed_actions(
    service: TicketService,
    source_label: SourceKind,
) -> list:
    """Query the DB for PENDING proposed actions from *source_label*.

    Returns the list of :class:`ProposedAction` rows (empty list on
    any exception, following the defensive pattern of
    ``_verify_prior_proposals``).
    """
    try:
        return service.list_proposed_actions(
            source=str(source_label),
            status=ProposedActionStatus.PENDING,
            limit=200,
        )
    except Exception:
        log.debug(
            "_verify_proposed_actions: list_proposed_actions failed — "
            "returning empty list"
        )
        return []


def _render_proposed_actions_table(actions: list) -> str:
    """Render a Markdown table of pending proposed actions for agent context.

    Returns ``""`` when *actions* is empty.
    """
    if not actions:
        return ""
    lines = [
        "## Proposed actions — pending",
        "",
        "| id | target_ticket | action | rationale | created |",
        "|----|---------------|--------|-----------|---------|",
    ]
    for pa in actions:
        created = pa.created_at.strftime("%Y-%m-%d") if pa.created_at else "—"
        lines.append(
            f"| {pa.id} | {pa.target_ticket_id} | {pa.action_type} "
            f"| {pa.rationale[:80]} | {created} |"
        )
    return "\n".join(lines)


# The verified-state table (above) is injected fresh into the agent prompt every
# run as an EPHEMERAL block. When an agent copies it into its ``updated_memory``
# output it bakes per-ticket state (gap_id/ticket_id/state) into the cross-run
# ledger, which then accretes stale ticket rows forever. Memory is for
# cross-ticket patterns + things to monitor, not a per-ticket diary — ticket
# history lives in the DB. Strip the section on persist so the invariant holds
# regardless of whether the agent obeyed the prompt.
# Match ONLY the heading line + its blank lines + the contiguous Markdown table
# rows (``| … |``). Deliberately NOT ``.*?`` to the next heading — that would
# also swallow any prose the agent wrote AFTER the table when no later ``##``
# heading bounds it (observed wiping a whole ledger to empty). Stop at the first
# line that is neither blank nor a table row.
_EPHEMERAL_MEMORY_SECTION_RE = re.compile(
    r"(?m)^[ \t]*##\s*Prior proposals\b[^\n]*\n"  # heading line
    r"(?:[ \t]*\n)*"  # optional blank lines
    r"(?:[ \t]*\|[^\n]*\n?)+"  # one or more table rows (| … |)
)

_EPHEMERAL_PROPOSED_ACTIONS_SECTION_RE = re.compile(
    r"(?m)^[ \t]*##\s*Proposed actions\b[^\n]*\n"  # heading line
    r"(?:[ \t]*\n)*"  # optional blank lines
    r"(?:[ \t]*\|[^\n]*\n?)+"  # one or more table rows (| … |)
)


def strip_ephemeral_sections(memory_text: str) -> str:
    """Remove DB-derived ephemeral tables (``## Prior proposals`` and
    ``## Proposed actions``) from a memory document before it is
    persisted to the cross-run ledger.

    Removes only the heading + the contiguous table rows — any
    surrounding cross-ticket notes the agent wrote are preserved.
    """
    # Fast path: leave text byte-for-byte unchanged when there is nothing to
    # strip (the vast majority of memory documents) — only normalise whitespace
    # when a section is actually removed.
    if not memory_text or (
        "## Prior proposals" not in memory_text
        and "## Proposed actions" not in memory_text
    ):
        return memory_text
    cleaned = _EPHEMERAL_MEMORY_SECTION_RE.sub("", memory_text)
    cleaned = _EPHEMERAL_PROPOSED_ACTIONS_SECTION_RE.sub("", cleaned)
    # collapse the blank-line gap a removed section leaves behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned + "\n" if cleaned else ""


def _format_recent_proposals(tickets: list[Ticket]) -> str:
    """Format a ``<recent_proposals>`` block for agent prompt injection.

    One line per ticket: ``[STATE] short_id | title``, most recent first.
    """
    if not tickets:
        return "<recent_proposals>\n(no recent proposals)\n</recent_proposals>"
    lines = ["<recent_proposals>"]
    for t in tickets:
        short_id = t.id[:7]
        state_val = t.state.value
        lines.append(f"[{state_val}] {short_id} | {t.title}")
    lines.append("</recent_proposals>")
    return "\n".join(lines)


def load_memory(memory_file: Path, max_chars: int | None = None) -> str:
    """Read a memory ledger file; returns ``""`` if missing/unreadable.

    When *max_chars* is set and the file exceeds that limit, the oldest
    entries are dropped — only the last *max_chars* characters (most
    recent) are kept, adjusted to a newline boundary so entries aren't
    split mid-line.  A ``[... memory truncated: N chars omitted]`` note
    is prepended and a warning is logged.
    """
    try:
        if memory_file.exists():
            text = memory_file.read_text(encoding="utf-8")
            if max_chars is not None and len(text) > max_chars:
                original_size = len(text)
                # Find the cut point (keep the last max_chars), then
                # advance to the next newline so the first kept line is
                # a complete line.
                cut_point = original_size - max_chars
                nl_idx = text.find("\n", cut_point)
                if nl_idx != -1:
                    kept = text[nl_idx + 1 :]  # start after the newline
                else:
                    kept = text[cut_point:]  # fallback (no newline found)
                omitted = original_size - len(kept)
                text = f"[... memory truncated: {omitted} chars omitted]\n\n{kept}"
                log.warning(
                    "memory file %s truncated: %d → %d chars",
                    memory_file,
                    original_size,
                    len(text),
                )
            return text
    except OSError:
        log.warning("could not read memory file %s", memory_file)
    return ""


def persist_memory(memory_file: Path, text: str) -> None:
    """Write *text* to *memory_file*, creating parent dirs as needed.

    Strips the ephemeral ``## Prior proposals — verified state`` and
    ``## Proposed actions — pending`` tables an agent may have copied
    back into its memory output — those blocks are injected fresh each
    run from the DB and must never accrete in the cross-run ledger.
    """
    text = strip_ephemeral_sections(text)
    if text or not memory_file.exists():
        try:
            memory_file.parent.mkdir(parents=True, exist_ok=True)
            memory_file.write_text(text, encoding="utf-8")
        except OSError:
            log.warning("could not write memory file %s", memory_file)


@dataclass
class AgentPassResult:
    """Internal result of running an agent pass."""

    updated_memory: str
    drafts_created: list[dict]  # [{"id": ..., "title": ...}, ...]
    session_id: str = ""
    # The agent's one-line account of what it examined + the basis for the
    # number of drafts it filed. Surfaced as the run-registry summary so an
    # operator can tell a legitimate 0-draft run from a no-op.
    summary: str = ""
    # Proposed actions extracted from agent output and persisted.
    # [{"id": ..., "action_type": ..., "target_ticket_id": ..., "status": ...}, ...]
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class ProposedActionItem:
    """A single proposed action extracted from agent output."""

    action_type: str  # one of "close", "transition", "comment", "relabel"
    target_ticket_id: str
    rationale: str
    payload: str | None = None  # JSON string, schema varies by action_type


def _test_file_exists_for_gap(repo_dir: Path, title: str) -> bool:
    """Return ``True`` if the expected test file(s) for a draft
    already exist on disk.

    For **test-gap** drafts, parses titles of the form
    ``test gap: add unit tests for <module_path>`` and derives the
    expected test-file path under ``repo_dir / "tests"``.

    For **health** drafts, parses titles of the form
    ``Add tests/<dir>/ test subdirectory for ...`` and checks whether
    any ``test_*.py`` file already exists under that test directory.

    Returns ``False`` (conservative: don't block) on any parse failure.
    """
    # TEST_GAP pattern: "test gap: add unit tests for <module_path>"
    m = re.match(r"^test gap: add unit tests for (.+)", title)
    if m:
        module_path = m.group(1).strip()

        # Strip leading src/robotsix_mill/ prefix if present (the system
        # prompt example uses the short form, but guard against the LLM
        # emitting the full path).
        prefix = "src/robotsix_mill/"
        if module_path.startswith(prefix):
            module_path = module_path[len(prefix) :]

        # Must end with .py to derive a test file.
        if not module_path.endswith(".py"):
            return False

        # Split into directory and basename: foo.py → test_foo.py
        parts = module_path.rsplit("/", 1)
        if len(parts) == 2:
            directory, basename = parts
            test_path = repo_dir / "tests" / directory / f"test_{basename}"
        else:
            basename = module_path
            test_path = repo_dir / "tests" / f"test_{basename}"

        return test_path.exists()

    # HEALTH pattern: "Add tests/<dir>/ test subdirectory for ..."
    m = re.match(r"^Add tests/(.+?)/", title)
    if m:
        test_dir = repo_dir / "tests" / m.group(1).strip()
        if test_dir.is_dir():
            return any(test_dir.glob("test_*.py"))
        return False

    return False


def run_agent_pass(
    agent_fn: Callable[..., Any],
    *,
    memory_file: Path,
    source_label: SourceKind,
    service: TicketService,
    settings: Settings,
    origin_session: str | None = None,
    max_drafts: int | None = None,
    repo_dir: Path | None = None,
) -> AgentPassResult:
    """Execute one agent pass with shared boilerplate.

    Args:
        agent_fn: Callable invoked as ``agent_fn(settings=settings,
                  memory=memory_text)``.  The caller pre-bakes extra
                  kwargs (e.g. ``repo_dir``) via ``functools.partial``.
        memory_file: Path to the memory/ledger file.
        source_label: Label for draft ticket ``source`` field (e.g.
                      ``SourceKind.AUDIT``, ``SourceKind.AGENT``).
        service: ``TicketService`` for creating draft tickets.
        settings: Mill settings (passed through to the agent callable).
        origin_session: Value for ``origin_session`` on created tickets.
        max_drafts: If set, limit the number of draft tickets created
                    (clips ``draft_titles``, ``draft_bodies``, and
                    ``gap_ids`` before the creation loop).  Defaults to
                    ``None`` (no limit).

    Returns:
        ``AgentPassResult`` with updated memory and created draft info.
    """
    # 1. Read current memory — empty string if missing/unreadable.
    memory_text = load_memory(memory_file, max_chars=settings.max_memory_chars)

    # 2. Verify prior proposals; render an ephemeral verified-state table
    #    passed to the agent as a SEPARATE kwarg (not concatenated onto
    #    memory_text). The table is recomputed from the DB every pass —
    #    persisting it into the memory ledger would cause a self-
    #    perpetuating leak (the agent echoes memory back verbatim, the
    #    runner persists it, the next tick re-prepends a fresh table on
    #    top, …).
    verified = _verify_prior_proposals(service, settings, source_label)
    verified_block = _render_verified_table(verified) if verified else ""

    # Build proposed-actions table for agent context.
    pending_actions = _verify_proposed_actions(service, source_label)
    actions_block = (
        _render_proposed_actions_table(pending_actions) if pending_actions else ""
    )

    # Combine both ephemeral sections into one block.
    parts = [b for b in [verified_block, actions_block] if b]
    combined_verified = "\n\n".join(parts) if parts else ""

    # 3. Build the recent-proposals block for prompt injection.
    recent = service.recent_proposals_for(source_label, limit=100)
    rp_block = _format_recent_proposals(recent)

    # 4. Invoke the agent callable.
    #
    # Resilience: periodic agents emit a structured Result via
    # PromptedOutput (the providers reject forced tool_choice, so the
    # model must produce schema-valid JSON in free text). A
    # flash-class model occasionally finishes its analysis but never
    # emits a parseable Result — pydantic-ai then raises
    # UnexpectedModelBehavior ("Exceeded maximum output retries")
    # even after the agent's own ``retries`` budget. A periodic pass
    # is BEST-EFFORT: a malformed final emit must NOT hard-error the
    # whole run (which discards the work AND shows up as a scary error
    # on the board). Degrade to a clean no-op instead — zero drafts,
    # memory preserved untouched — and let the next scheduled tick try
    # again. We catch ONLY the output-emit failure class, not arbitrary
    # exceptions (clone/forge/etc. failures happen earlier in
    # run_periodic_pass and must still surface).
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    try:
        res = agent_fn(
            settings=settings,
            memory=memory_text,
            recent_proposals=rp_block,
            verified_proposals=combined_verified,
        )
    except UnexpectedModelBehavior as e:
        log.warning(
            "%s: agent did not emit a parseable structured Result "
            "(%s) — degrading this pass to a no-op (0 drafts, memory "
            "preserved); will retry next tick",
            source_label,
            e,
        )
        return AgentPassResult(
            updated_memory=memory_text,
            drafts_created=[],
            session_id=origin_session or "",
            summary=(
                "⚠ agent did not emit a parseable structured result — pass "
                "degraded to a no-op (this is NOT a clean 0-draft run)"
            ),
            proposed_actions=[],
        )

    # 5. Persist the agent's updated memory verbatim.
    if res.updated_memory:
        persist_memory(memory_file, res.updated_memory)

    # 5b. Extract and persist proposed actions from agent output.
    import json as _json

    proposed_actions_raw = getattr(res, "proposed_actions", None) or []
    proposed_created: list[dict] = []
    for pa in proposed_actions_raw:
        try:
            if not isinstance(pa, dict):
                continue
            action_type = str(pa.get("action_type", "")).strip()
            target = str(pa.get("target_ticket_id", "")).strip()
            rationale = str(pa.get("rationale", "")).strip()
            if not action_type or not target or not rationale:
                continue
            payload = pa.get("payload")
            if payload is not None and not isinstance(payload, str):
                payload = _json.dumps(payload)
            created = service.create_proposed_action(
                source=str(source_label),
                target_ticket_id=target,
                action_type=action_type,
                rationale=rationale,
                payload=payload,
            )
            proposed_created.append(
                {
                    "id": created.id,
                    "action_type": str(created.action_type),
                    "target_ticket_id": created.target_ticket_id,
                    "status": str(created.status),
                }
            )
        except Exception:
            log.exception("failed to persist proposed action from %s", source_label)

    # 6. Create draft tickets for each proposal.
    gap_ids = getattr(res, "gap_ids", [])
    created: list[dict] = []
    limit = min(len(res.draft_titles), len(res.draft_bodies))
    if max_drafts is not None:
        limit = min(limit, max_drafts)
    for i in range(limit):
        title = res.draft_titles[i]
        body = res.draft_bodies[i]
        if not title or not body:
            continue
        # Live-filesystem guard: skip drafts whose expected test
        # file(s) already exist on disk.
        if (
            source_label in (SourceKind.TEST_GAP, SourceKind.HEALTH)
            and repo_dir is not None
        ):
            if _test_file_exists_for_gap(repo_dir, title):
                log.warning(
                    "%s draft skipped — test file(s) already exist on disk: %s",
                    source_label,
                    title,
                )
                continue
        # Append gap-id marker if available.
        if i < len(gap_ids) and gap_ids[i]:
            body += f"\n\n<!-- {source_label}-gap-id: {gap_ids[i]} -->"
        # Mill-internal routing: if the draft names mill-internal
        # symbols, file on the mill maintenance board instead of the
        # audited repo's board — same heuristic the retrospect stage
        # uses (see ``draft_target.looks_like_mill_internal``). A
        # misconfigured mill target (unset / unknown repo) falls back
        # to the audited board so a draft is never lost.
        target_service = service
        if looks_like_mill_internal(title, body):
            mill_svc = resolve_mill_service(
                settings, service, caller_label=str(source_label)
            )
            if mill_svc is not None:
                log.info(
                    "%s: draft routed to mill board (mill-internal "
                    "symbols detected): %s",
                    source_label,
                    title,
                )
                target_service = mill_svc
        try:
            ticket = target_service.create(
                title,
                body,
                source=source_label,
                origin_session=origin_session,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            log.info(
                "%s spawned draft %s: %s",
                source_label,
                ticket.id,
                title,
            )
        except Exception:
            log.exception("failed to create draft ticket: %s", title)

    return AgentPassResult(
        updated_memory=res.updated_memory or memory_text,
        drafts_created=created,
        session_id=origin_session or "",
        summary=(getattr(res, "summary", "") or "").strip(),
        proposed_actions=proposed_created,
    )
