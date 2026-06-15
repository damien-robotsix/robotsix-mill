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
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field

from ..config import Settings
from ..core.models import (
    SourceKind,
    Ticket,
)
from ..core.service import TicketService
from ..core.states import State
from ..core.text_utils import tail_keep
from ..core.workspace import Workspace
from ..core.dedup import _extract_paths, annotate_child_body, find_inflight_overlap
from ..core.draft_target import looks_like_mill_internal, resolve_mill_service

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


class ProposedActionItem(BaseModel):
    """A single action proposal emitted by a periodic agent.

    Mirrors the fields of the ``ProposedAction`` DB row that the agent
    controls — ``source``, ``status``, and lifecycle timestamps are set
    by the runner/service, not by the agent.
    """

    target_ticket_id: str = Field(description="Ticket ID the action applies to")
    action_type: str = Field(description="One of: close, transition, comment, relabel")
    payload: str | None = Field(
        default=None,
        description="JSON string whose schema varies by action_type "
        "(e.g. target state for transition, comment body for comment)",
    )
    rationale: str = Field(
        description="Why the agent believes this action is warranted"
    )


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

        # A single stale / orphaned ticket whose board can no longer be
        # resolved (service.history -> _board_for raises ValueError) must
        # NOT abort the whole verification pass — skip it and continue.
        try:
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
        except Exception as exc:
            log.debug(
                "_verify_prior_proposals: skipping ticket %s — %s",
                ticket.id,
                exc,
            )
            continue

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
) -> list[dict]:
    """Return decided (non-PENDING) ProposedAction rows for *source_label*.

    Each dict: {id, target_ticket_id, action_type, status, rationale,
    decided_at, decided_by}.
    """
    try:
        rows = service.list_proposed_actions(source=str(source_label))
    except Exception:
        log.debug(
            "_verify_proposed_actions: list_proposed_actions failed — "
            "returning empty list (DB may not be initialised)"
        )
        return []

    result: list[dict] = []
    for pa in rows:
        result.append(
            {
                "id": pa.id,
                "target_ticket_id": pa.target_ticket_id,
                "action_type": pa.action_type,
                "status": pa.status,
                "rationale": pa.rationale,
                "decided_at": pa.decided_at.isoformat() if pa.decided_at else "",
                "decided_by": pa.decided_by or "",
            }
        )
    return result


def _render_proposed_actions_table(decided: list[dict]) -> str:
    """Render a Markdown table of decided proposed actions for agent context."""
    if not decided:
        return ""
    lines = [
        "## Prior proposed actions — decided",
        "",
        "| id | target_ticket | action | status | decided_by | rationale |",
        "|----|---------------|--------|--------|------------|-----------|",
    ]
    for pa in decided:
        tid_short = pa["target_ticket_id"][:7]
        rationale_short = pa["rationale"][:80].replace("|", "\\|")
        lines.append(
            f"| {pa['id']} | {tid_short} | {pa['action_type']} "
            f"| {pa['status']} | {pa['decided_by']} | {rationale_short} |"
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
    r"(?m)^[ \t]*##\s*Prior proposed actions\b[^\n]*\n"
    r"(?:[ \t]*\n)*"
    r"(?:[ \t]*\|[^\n]*\n?)+"
)

# The ``<recent_proposals>…</recent_proposals>`` block is injected fresh into
# the agent prompt every run as transient DB-surfaced data (see
# ``_format_recent_proposals``). An agent that echoes that block into its
# ``updated_memory`` permanently contaminates the cross-run ledger — every
# subsequent run re-reads the phantom reference and treats it as real repo
# code. Strip any echoed block on persist. Non-greedy + DOTALL so it matches
# the full block across newlines, tolerant of surrounding whitespace.
_EPHEMERAL_RECENT_PROPOSALS_RE = re.compile(
    r"[ \t]*<recent_proposals>.*?</recent_proposals>[ \t]*\n?",
    re.DOTALL,
)


def strip_ephemeral_sections(memory_text: str) -> str:
    """Remove the DB-derived ``## Prior proposals — verified state`` and
    ``## Prior proposed actions — decided`` tables, plus any echoed
    ``<recent_proposals>…</recent_proposals>`` block, from a memory document
    before it is persisted to the cross-run ledger.

    Removes only the heading + the contiguous table rows (and the XML block) —
    any surrounding cross-ticket notes the agent wrote are preserved.
    """
    # Fast path: leave text byte-for-byte unchanged when there is nothing to
    # strip (the vast majority of memory documents) — only normalise whitespace
    # when a section is actually removed.
    if not memory_text or (
        "## Prior proposals" not in memory_text
        and "## Prior proposed actions" not in memory_text
        and "<recent_proposals>" not in memory_text
    ):
        return memory_text
    cleaned: str | None = None
    if "## Prior proposals" in memory_text:
        cleaned = _EPHEMERAL_MEMORY_SECTION_RE.sub("", memory_text)
    if "## Prior proposed actions" in (cleaned or memory_text):
        cleaned = _EPHEMERAL_PROPOSED_ACTIONS_SECTION_RE.sub("", cleaned or memory_text)
    if "<recent_proposals>" in (cleaned if cleaned is not None else memory_text):
        cleaned = _EPHEMERAL_RECENT_PROPOSALS_RE.sub(
            "", cleaned if cleaned is not None else memory_text
        )
    # collapse the blank-line gap a removed section leaves behind. Use an
    # ``is not None`` guard (not ``or``) so a section that strips down to an
    # empty string isn't silently replaced by the original ``memory_text``.
    cleaned = re.sub(
        r"\n{3,}", "\n\n", cleaned if cleaned is not None else memory_text
    ).strip()
    return cleaned + "\n" if cleaned else ""


def _format_recent_proposals(tickets: list[Ticket]) -> str:
    """Format a ``<recent_proposals>`` block for agent prompt injection.

    One line per ticket: ``[STATE] id | title``, most recent first.
    The full ``t.id`` is emitted so the agent can pass it straight to
    ``read_ticket`` (which rejects truncated IDs).
    """
    if not tickets:
        return "<recent_proposals>\n(no recent proposals)\n</recent_proposals>"
    lines = ["<recent_proposals>"]
    for t in tickets:
        state_val = t.state.value
        lines.append(f"[{state_val}] {t.id} | {t.title}")
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
                text = tail_keep(text, max_chars, label="memory")
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


def persist_memory(memory_file: Path, text: str, max_chars: int | None = None) -> None:
    """Write *text* to *memory_file*, creating parent dirs as needed.

    Strips the ephemeral ``## Prior proposals — verified state`` and
    ``## Proposed actions — pending`` tables an agent may have copied
    back into its memory output — those blocks are injected fresh each
    run from the DB and must never accrete in the cross-run ledger.

    When *max_chars* is set and ``len(text) > max_chars``, the text is
    tail-truncated via :func:`tail_keep` before writing — the same
    primitive and label already used by :func:`load_memory`.  Ephemeral
    sections are stripped BEFORE the cap check so the budget isn't
    wasted on content that would be stripped anyway.
    """
    text = strip_ephemeral_sections(text)
    if max_chars is not None and len(text) > max_chars:
        original_size = len(text)
        text = tail_keep(text, max_chars, label="memory")
        log.warning(
            "memory file %s truncated on write: %d → %d chars",
            memory_file,
            original_size,
            len(text),
        )
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

        # Primary check: the strict naming-convention mirror.
        if test_path.exists():
            return True

        # Narrowly-scoped fallback for route modules under
        # ``runtime/routes/``: those are exercised through the FastAPI app
        # via HTTP-endpoint tests, not via a 1:1 mirror file (e.g.
        # ``runtime/routes/_candidates.py`` is tested in
        # ``tests/runtime/test_candidates_routes.py``). Confined to this
        # prefix so non-route modules retain strict-mirror semantics.
        if module_path.startswith("runtime/routes/"):
            # basename without .py, leading underscores stripped:
            # _candidates.py -> candidates, _health.py -> health.
            token = basename[: -len(".py")].lstrip("_")
            if token:
                runtime_tests = repo_dir / "tests" / "runtime"
                for test_file in runtime_tests.rglob("test_*.py"):
                    if token in test_file.name:
                        return True
                    try:
                        contents = test_file.read_text(encoding="utf-8")
                    except OSError:
                        # Unreadable file — skip it, never raise out of the guard.
                        continue
                    if f"/{token}" in contents or f"test_{token}" in contents:
                        return True

        return False

    # HEALTH pattern: "Add tests/<dir>/ test subdirectory for ..."
    m = re.match(r"^Add tests/(.+?)/", title)
    if m:
        test_dir = repo_dir / "tests" / m.group(1).strip()
        if test_dir.is_dir():
            return any(test_dir.glob("test_*.py"))
        return False

    return False


def _source_module_exists_for_gap(repo_dir: Path, title: str) -> bool:
    """Return ``True`` when the **source** module a test-gap draft names
    actually exists in the audited repo's cloned tree.

    This is the inverse of ``_test_file_exists_for_gap``: that helper
    suppresses a draft when the expected *test* file already exists, while
    this one suppresses a draft when the *source* module is **absent** from
    the cloned tree — a cross-repo misrouting guard. The test-gap detector
    occasionally hallucinates module paths from its own knowledge of the mill
    codebase rather than strictly from the audited tree, filing
    ``test gap: add unit tests for <module>`` drafts for modules that exist
    only in another repository.

    Parses titles of the form ``test gap: add unit tests for <module_path>``
    with the same regex as ``_test_file_exists_for_gap``. Returns ``True``
    (conservative: do **not** suppress) on any parse failure — i.e. when the
    title does not match the test-gap pattern, or ``<module_path>`` does not
    end in ``.py``. The module is resolved flexibly against *repo_dir*,
    returning ``True`` if any candidate location is an existing file:

    * ``repo_dir / module_path`` (path already relative to the repo root,
      possibly already including a ``src/<pkg>/`` prefix).
    * ``repo_dir / "src" / <pkg> / module_path`` for each immediate
      subdirectory ``<pkg>`` of ``repo_dir / "src"`` (handles the common case
      where the agent emits the path relative to the package source root,
      e.g. ``stages/refine/orchestration.py`` →
      ``src/robotsix_mill/stages/refine/orchestration.py``).

    Returns ``False`` only when the module resolves to none of the candidate
    locations.
    """
    m = re.match(r"^test gap: add unit tests for (.+)", title)
    if not m:
        return True

    module_path = m.group(1).strip()
    # Strip a trailing :NN / :NN-NN line-range suffix if present.
    module_path = re.sub(r":\d+(?:-\d+)?$", "", module_path).strip()

    # Must end with .py to name a source module — otherwise pass through.
    if not module_path.endswith(".py"):
        return True

    # Candidate 1: path already relative to the repo root (possibly already
    # including a src/<pkg>/ prefix).
    if (repo_dir / module_path).is_file():
        return True

    # Candidate 2: path relative to a package source root under src/.
    src_dir = repo_dir / "src"
    if src_dir.is_dir():
        for pkg in src_dir.iterdir():
            if pkg.is_dir() and (pkg / module_path).is_file():
                return True

    return False


def _module_curator_premise_check(
    repo_dir: Path, title: str, body: str
) -> tuple[str, str] | None:
    """Verify a module_curator draft's factual premise against the
    cloned tree. Returns None when the premise holds (file the draft as-is),
    or a (disposition, note) tuple where disposition is 'suppress' or
    'advisory'. Conservative: returns None on any parse ambiguity so
    legitimate drafts are never blocked.

    ``(repo_dir / rel_path).exists()`` IS the HEAD check: ``repo_dir`` is a
    fresh checkout of ``settings.forge_target_branch``.
    """
    paths = _extract_paths(f"{title}\n{body}")
    title_lower = title.lower()
    body_lower = body.lower()

    # 1. suppress — a file-missing assertion that is in fact false.
    missing_signal = (
        re.match(r"^\s*create\s+\S", title_lower) is not None
        or "missing" in title_lower
        or "does not exist" in title_lower
        or "is absent" in title_lower
        or "missing" in body_lower
        or "does not exist" in body_lower
    )
    if missing_signal:
        for path in paths:
            if (repo_dir / path).exists():
                return ("suppress", f"{path} already exists on HEAD")

    # 2. advisory — a stale classify/relocate premise.
    classify_shape = (
        re.match(r"^\s*classify\s+", title_lower) is not None
        or re.match(r"^\s*reorganize module\s+", title_lower) is not None
        or re.match(r"^\s*consolidate modules?\s+", title_lower) is not None
        or re.match(r"^\s*cleanup module\s+", title_lower) is not None
    )
    if classify_shape:
        for path in paths:
            if not (repo_dir / path).exists():
                return ("advisory", f"path {path} no longer exists on HEAD")
        # Already classified under an existing module's paths glob?
        try:
            modules_file = repo_dir / "docs" / "modules.yaml"
            if modules_file.exists():
                data = yaml.safe_load(modules_file.read_text(encoding="utf-8"))
                modules = data.get("modules", []) if isinstance(data, dict) else []
                for path in paths:
                    for mod in modules:
                        mod_id = mod.get("id", "")
                        for glob in mod.get("paths", []) or []:
                            if PurePath(path).match(glob) or path in glob:
                                return (
                                    "advisory",
                                    f"{path} is already classified under module "
                                    f"{mod_id} in docs/modules.yaml",
                                )
        except Exception:
            return None

    return None


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

    # 2. Verify prior proposals; render ephemeral verified-state tables
    #    passed to the agent as a SEPARATE kwarg (not concatenated onto
    #    memory_text). The tables are recomputed from the DB every pass —
    #    persisting them into the memory ledger would cause a self-
    #    perpetuating leak (the agent echoes memory back verbatim, the
    #    runner persists it, the next tick re-prepends a fresh table on
    #    top, …).
    verified = _verify_prior_proposals(service, settings, source_label)
    verified_block = _render_verified_table(verified) if verified else ""

    decided_actions = _verify_proposed_actions(service, source_label)
    decided_block = _render_proposed_actions_table(decided_actions)

    # Concatenate both ephemeral sections
    combined_verified = "\n\n".join(filter(None, [verified_block, decided_block]))

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
        persist_memory(
            memory_file, res.updated_memory, max_chars=settings.max_memory_chars
        )

    # 5b. Persist proposed actions (proposed-action subsystem).
    proposed_actions = getattr(res, "proposed_actions", [])
    proposed_created: list[dict] = []
    for pa_item in proposed_actions:
        try:
            created_pa = service.create_proposed_action(
                source=str(source_label),
                target_ticket_id=pa_item.target_ticket_id,
                action_type=pa_item.action_type,
                rationale=pa_item.rationale,
                payload=pa_item.payload,
            )
            if created_pa is not None:
                proposed_created.append(
                    {
                        "id": created_pa.id,
                        "action_type": str(created_pa.action_type),
                        "target_ticket_id": created_pa.target_ticket_id,
                        "status": str(created_pa.status),
                    }
                )
                log.info(
                    "%s proposed %s on %s (proposal id %s)",
                    source_label,
                    pa_item.action_type,
                    pa_item.target_ticket_id,
                    created_pa.id,
                )
        except Exception:
            log.exception(
                "%s: failed to persist proposed action (%s on %s)",
                source_label,
                pa_item.action_type,
                pa_item.target_ticket_id,
            )

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
        # Source-module-existence guard: skip a TEST_GAP draft whose source
        # module is absent from the audited tree (inverse of the test-file
        # check above — a cross-repo misrouting guard). HEALTH drafts target a
        # tests/<dir>/ subdirectory, not a single source module, so they are
        # deliberately excluded.
        if source_label == SourceKind.TEST_GAP and repo_dir is not None:
            if not _source_module_exists_for_gap(repo_dir, title):
                log.warning(
                    "%s draft suppressed — source module absent from audited tree: %s",
                    source_label,
                    title,
                )
                continue
        # Module-curator premise guard: verify the draft's factual claim
        # against the cloned tree before filing. An unambiguous file-exists
        # falsification suppresses the draft; every other stale/overlap
        # signal annotates (never silently drops).
        if source_label == SourceKind.MODULE_CURATOR and repo_dir is not None:
            verdict = _module_curator_premise_check(repo_dir, title, body)
            if verdict is not None:
                disposition, note = verdict
                if disposition == "suppress":
                    log.warning(
                        "%s draft suppressed — false premise (%s): %s",
                        source_label,
                        note,
                        title,
                    )
                    continue
                # advisory: annotate, never drop
                body = annotate_child_body(
                    body,
                    note,
                    source_desc="module_curator pre-filing premise check",
                )
            # In-flight sibling cross-reference (advisory, best-effort).
            try:
                overlap = find_inflight_overlap(
                    service,
                    "",
                    title,
                    body,
                    settings,
                    datetime.now(timezone.utc),
                )
                if overlap is not None:
                    body = annotate_child_body(
                        body,
                        overlap,
                        source_desc="module_curator pre-filing dedup",
                    )
            except Exception:
                log.exception(
                    "%s: in-flight sibling cross-reference failed: %s",
                    source_label,
                    title,
                )
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
