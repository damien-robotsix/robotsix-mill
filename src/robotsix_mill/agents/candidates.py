"""Parse and mutate ``AGENT_CANDIDATES.md`` — the per-board append-only
queue retrospect writes to.

Each ``### Proposed addition to <section>`` block becomes a structured
``Candidate`` so the board UI can list pending entries and act on them.
``validate(...)`` rewrites the block in place with a
``**Status:** validated → <ticket_id>`` line so the entry is recorded as
acted-on; ``reject(...)`` does the same with ``rejected``.

The file is the single source of truth: no sidecar JSON, no DB row.
Keeping state inline preserves the human-readable format retrospect
writes and means an operator can still hand-edit if needed.

``candidate_id`` is a content hash (rule + proposed_at), not the file
offset. That keeps IDs stable if retrospect prepends a fresh entry
between a UI list-fetch and a UI button click.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_BLOCK_SEP = re.compile(r"\n---\n", re.MULTILINE)
_HEADING_RE = re.compile(r"^###\s+Proposed addition to\s+(.+?)\s*$", re.MULTILINE)
_RULE_RE = re.compile(
    r"^>\s*\*\*Rule:\*\*\s*(.+?)(?=\n\n|\Z)", re.MULTILINE | re.DOTALL
)
_RATIONALE_RE = re.compile(
    r"\*\*Rationale:\*\*\s*(.+?)(?=\n\n\*\*Proposed|\n\n\*\*Status|\Z)",
    re.DOTALL,
)
_PROPOSED_RE = re.compile(
    r"\*\*Proposed:\*\*\s*(.+?)\s*\(from\s+(.+?)\)",
)
_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s*(\S+)(?:\s*→\s*(.+?))?\s*$", re.MULTILINE)


@dataclass
class Candidate:
    """One ``### Proposed addition`` block from AGENT_CANDIDATES.md."""

    candidate_id: str
    section: str
    rule: str
    rationale: str
    proposed_at: str
    source_ticket: str
    status: str  # "pending" | "validated" | "rejected"
    filed_ticket: str | None  # the audited-repo draft id when status == validated


def candidates_path(data_dir: Path, board_id: str) -> Path:
    """Mirror retrospect.py's filename rule so a single source of truth
    backs both the writer and the UI reader."""
    return (
        data_dir / board_id / "AGENT_CANDIDATES.md"
        if board_id
        else data_dir / "AGENT_CANDIDATES.md"
    )


def _stable_id(rule: str, proposed_at: str) -> str:
    """8-char content hash. Stable across re-orderings; collision-safe
    enough for a per-board file with at most dozens of entries."""
    h = hashlib.sha256(f"{rule}\x00{proposed_at}".encode("utf-8")).hexdigest()
    return h[:8]


def _parse_block(raw: str) -> Candidate | None:
    """Parse one ``###`` block. Returns ``None`` on malformed input —
    retrospect's writer guarantees the format but a hand-edited file
    might not."""
    raw = raw.strip()
    if not raw:
        return None
    head = _HEADING_RE.search(raw)
    rule_m = _RULE_RE.search(raw)
    rat_m = _RATIONALE_RE.search(raw)
    prop_m = _PROPOSED_RE.search(raw)
    if not (head and rule_m and rat_m and prop_m):
        return None
    status_m = _STATUS_RE.search(raw)
    status = status_m.group(1).lower() if status_m else "pending"
    filed = (status_m.group(2) or "").strip() if status_m else None
    if filed == "":
        filed = None
    rule = rule_m.group(1).strip()
    proposed_at = prop_m.group(1).strip()
    return Candidate(
        candidate_id=_stable_id(rule, proposed_at),
        section=head.group(1).strip(),
        rule=rule,
        rationale=rat_m.group(1).strip(),
        proposed_at=proposed_at,
        source_ticket=prop_m.group(2).strip(),
        status=status,
        filed_ticket=filed,
    )


def load_candidates(path: Path) -> list[Candidate]:
    """Parse every block in ``path``. Returns ``[]`` when the file
    doesn't exist or is empty — the UI just shows an empty list."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("AGENT_CANDIDATES.md read failed: %s", e)
        return []
    out: list[Candidate] = []
    # Split on ``---`` separators; the writer puts one after every block.
    for raw in _BLOCK_SEP.split(text):
        c = _parse_block(raw)
        if c is not None:
            out.append(c)
    return out


def _format_status_line(status: str, filed: str | None) -> str:
    if filed:
        return f"**Status:** {status} → {filed}"
    return f"**Status:** {status}"


def _rewrite_block_status(block: str, new_status: str, filed_ticket: str | None) -> str:
    """Return *block* with its Status line replaced (or appended). The
    new line sits between **Proposed:** and the trailing block, so the
    block stays human-readable."""
    new_line = _format_status_line(new_status, filed_ticket)
    if _STATUS_RE.search(block):
        return _STATUS_RE.sub(new_line, block, count=1)
    # No existing Status — append after the **Proposed:** line. The
    # writer guarantees Proposed exists.
    prop_m = _PROPOSED_RE.search(block)
    if not prop_m:
        return block.rstrip() + f"\n\n{new_line}\n"
    insert_at = block.find("\n", prop_m.end())
    if insert_at == -1:
        return block.rstrip() + f"\n\n{new_line}\n"
    return block[: insert_at + 1] + f"\n{new_line}\n" + block[insert_at + 1 :]


def update_status(
    path: Path,
    candidate_id: str,
    new_status: str,
    filed_ticket: str | None = None,
) -> Candidate | None:
    """Rewrite the matching block's Status line in place.

    Atomic: writes to a sibling ``.tmp`` then renames so a crash during
    write can't truncate the file. Returns the updated ``Candidate`` on
    success, ``None`` when no block with ``candidate_id`` exists.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("AGENT_CANDIDATES.md read failed: %s", e)
        return None

    # Walk blocks and rebuild. We preserve exact original separators by
    # re-joining with ``\n---\n`` only between non-empty blocks.
    blocks = _BLOCK_SEP.split(text)
    target_idx: int | None = None
    updated: Candidate | None = None
    for i, block in enumerate(blocks):
        c = _parse_block(block)
        if c is None:
            continue
        if c.candidate_id == candidate_id:
            target_idx = i
            blocks[i] = _rewrite_block_status(block, new_status, filed_ticket)
            updated = Candidate(
                candidate_id=c.candidate_id,
                section=c.section,
                rule=c.rule,
                rationale=c.rationale,
                proposed_at=c.proposed_at,
                source_ticket=c.source_ticket,
                status=new_status,
                filed_ticket=filed_ticket,
            )
            break
    if target_idx is None:
        return None

    new_text = "\n---\n".join(blocks)
    # Preserve a trailing newline if the original had one.
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    tmp = path.with_suffix(".md.tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("AGENT_CANDIDATES.md write failed: %s", e)
        return None
    return updated


def to_ticket_payload(c: Candidate) -> tuple[str, str]:
    """Render the audited-repo ticket title + body for a validated
    candidate. Title stays short (~80 chars); body restates the rule,
    rationale, target section, and provenance so refine has full
    context when it runs."""
    rule_short = c.rule.splitlines()[0].strip()
    if len(rule_short) > 80:
        rule_short = rule_short[:77] + "..."
    title = f"AGENT.md: {rule_short}"
    body = (
        f"## Proposed AGENT.md edit\n\n"
        f"**Target section:** `{c.section}`\n\n"
        f"**Rule to add:**\n\n> {c.rule}\n\n"
        f"**Rationale:** {c.rationale}\n\n"
        f"## Implementation\n\n"
        f"Edit `AGENT.md` in this repository. Add the rule above under "
        f"the section header `{c.section}`. If the section does not "
        f"exist yet, add it (the proposed section name is a suggestion "
        f"— rename or fold into an existing section if a better fit "
        f"exists). Match the tone and formatting of surrounding rules.\n\n"
        f"## Provenance\n\n"
        f"Validated by the operator on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        f"from AGENT_CANDIDATES.md (candidate `{c.candidate_id}`). "
        f"Originally proposed by retrospect on {c.proposed_at} while "
        f"reviewing ticket `{c.source_ticket}`."
    )
    return title, body
