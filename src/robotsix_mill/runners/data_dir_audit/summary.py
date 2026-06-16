"""Rich summary generation for the data-dir audit (ticket 7)."""

from __future__ import annotations

from typing import Any

from .filing import _human_bytes, _trim_path
from .orphans import OrphanWorkspace


def _orphan_summary_line(
    orphans_by_board: dict[str, list[OrphanWorkspace]],
    total_orphans: int,
) -> str:
    """Render the orphan-workspaces summary line."""
    word = "workspace" if total_orphans == 1 else "workspaces"
    orphan_line = f"{total_orphans} orphan {word}"
    if total_orphans > 0 and orphans_by_board:
        flat = [o for items in orphans_by_board.values() for o in items]
        if flat:
            biggest = max(flat, key=lambda o: o.dir_size_bytes)
            path = _trim_path(
                f".data/{biggest.board_id}/workspaces/{biggest.ticket_id}"
            )
            orphan_line += f" (largest: {path}, {_human_bytes(biggest.dir_size_bytes)})"
    return orphan_line


def _has_no_issues(
    oversized: list[dict[str, Any]],
    all_growth_flags: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    total_orphans: int,
) -> bool:
    """Return True when every check produced zero results."""
    return (
        not oversized and not all_growth_flags and not findings and total_orphans == 0
    )


def _prune_lines(
    clones_pruned: int, closed_pruned: int, db_rows_purged: int
) -> list[str]:
    """Build prune/purge summary lines (empty when all counts are zero)."""
    lines: list[str] = []
    if clones_pruned > 0:
        lines.append(f"Terminal-ticket clones pruned: {clones_pruned}.")
    if closed_pruned > 0:
        lines.append(f"Closed workspaces pruned: {closed_pruned}.")
    if db_rows_purged > 0:
        lines.append(f"DB rows purged: {db_rows_purged}.")
    return lines


def _build_summary(
    total_bytes: int,
    total_files: int,
    oversized: list[dict[str, Any]],
    all_growth_flags: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    orphans_by_board: dict[str, list[OrphanWorkspace]],
    total_orphans: int,
    drafts_created: list[dict[str, Any]],
    closed_pruned: int = 0,
    clones_pruned: int = 0,
    db_rows_purged: int = 0,
) -> str:
    """Render a multi-line summary for the runs panel.

    Header is always ``"Scanned <bytes> in N files."``. When every
    check produced zero results, returns the single-line short-circuit
    ``"Scanned <bytes> in N files. No issues found."``. Otherwise each
    non-empty category contributes one line (oversized → growth →
    unbounded), the orphan line is always appended, and a final
    ``"Filed N draft(s)."`` line is added when drafts were created.
    """
    header = f"Scanned {_human_bytes(total_bytes)} in {total_files:,} files."

    if _has_no_issues(oversized, all_growth_flags, findings, total_orphans):
        base = header + " No issues found."
        prune = _prune_lines(clones_pruned, closed_pruned, db_rows_purged)
        if prune:
            base += "\n" + "\n".join(prune)
        return base

    lines: list[str] = [header]

    if oversized:
        n = len(oversized)
        largest = oversized[0]
        path = _trim_path(f".data/{largest['path']}")
        size = int(largest["size_bytes"])
        word = "item" if n == 1 else "items"
        lines.append(f"{n} oversized {word} (largest: {path} → {_human_bytes(size)})")

    if all_growth_flags:
        n = len(all_growth_flags)
        largest = max(all_growth_flags, key=lambda f: int(f.get("delta_bytes", 0)))
        path = _trim_path(f".data/{largest.get('board_id', '')}/{largest['path']}")
        delta = int(largest["delta_bytes"])
        word = "flag" if n == 1 else "flags"
        lines.append(f"{n} growth {word} ({path} grew by {_human_bytes(delta)})")

    if findings:
        n = len(findings)
        largest = max(findings, key=lambda f: int(f.get("current_size", 0)))
        path = _trim_path(f".data/{largest['path']}")
        current = int(largest["current_size"])
        cap = int(largest["cap_size"])
        word = "candidate" if n == 1 else "candidates"
        lines.append(
            f"{n} unbounded {word} "
            f"({path}: {_human_bytes(current)}, cap: {_human_bytes(cap)})"
        )

    lines.append(_orphan_summary_line(orphans_by_board, total_orphans))

    if drafts_created:
        n = len(drafts_created)
        word = "draft" if n == 1 else "drafts"
        lines.append(f"Filed {n} {word}.")

    lines.extend(_prune_lines(clones_pruned, closed_pruned, db_rows_purged))

    return "\n".join(lines)
