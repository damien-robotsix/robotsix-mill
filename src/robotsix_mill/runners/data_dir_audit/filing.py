"""Filing logic for the data-dir audit (ticket 6).

Builds finding dicts and ticket bodies, orders findings, and files
draft tickets with cross-pass dedup via gap-id markers.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ...core.models import SourceKind
from ...core.service import TicketService
from ..data_dir_audit.orphans import OrphanWorkspace

log = logging.getLogger("robotsix_mill.data_dir_audit")

_WHITESPACE_RE = re.compile(r"\s+")


def _human_bytes(n: int) -> str:
    """Return a binary-unit string for *n* bytes (``"1.2 GiB"``).

    Uses powers of 1024 to match ``.data/`` accounting conventions.
    Negative values are formatted with a leading minus sign.
    """
    if n < 0:
        return "-" + _human_bytes(-n)
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"  # unreachable but appeases type checkers


def _trim_path(p: str, max_len: int = 80) -> str:
    """Middle-elide *p* with ``…`` so the result is at most *max_len* chars."""
    if len(p) <= max_len:
        return p
    # Keep room for the ellipsis (1 char).
    keep = max_len - 1
    head = keep // 2
    tail = keep - head
    return p[:head] + "…" + p[-tail:]


def _sanitize_gap_segment(s: str) -> str:
    """Replace any whitespace in *s* with ``_`` so the gap_id matches
    ``\\S+`` (no whitespace runs)."""
    return _WHITESPACE_RE.sub("_", s)


def _build_finding(
    path: Path,
    data_dir: Path,
    size: int,
    cap_bytes: int,
    cap_detail: str,
    pattern: str,
    record_count: int | None,
    record_max: int | None,
) -> dict:
    """Build a finding dict for ``path`` against its pattern's caps."""
    try:
        rel_path = str(path.relative_to(data_dir))
    except ValueError:
        rel_path = str(path)
    return {
        "check": "unbounded_candidates",
        "path": rel_path,
        "current_size": size,
        "cap_size": cap_bytes,
        "cap_detail": cap_detail,
        "pattern": pattern,
        "severity": "warning",
        "record_count": record_count,
        "record_max": record_max,
    }


def _build_oversized_finding(item: dict) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for an oversized-item finding."""
    path = item["path"]
    size = int(item["size_bytes"])
    is_dir = bool(item.get("is_directory"))
    gap_id = f"oversized:{_sanitize_gap_segment(path)}"
    kind = "directory" if is_dir else "file"
    title = f"oversized {path} ({_human_bytes(size)})"
    body = (
        "_Filed by the periodic data-dir audit pass._\n\n"
        "## Finding\n\n"
        f"- **Path:** `{path}` ({kind})\n"
        f"- **Current size:** {_human_bytes(size)} ({size} bytes)\n"
        "- **Threshold:** "
        f"{_human_bytes(100 * 1024 * 1024)} (default oversized threshold)\n\n"
        "Consider capping this file or scheduling a sweep.\n"
    )
    return gap_id, title, body


def _build_growth_finding(flag: dict) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for a growth-delta finding."""
    path = flag["path"]
    board_id = flag.get("board_id", "")
    delta_bytes = int(flag["delta_bytes"])
    delta_pct = flag["delta_pct"]
    current_size = int(flag["current_size_bytes"])
    prior_size = int(flag["prior_size_bytes"])
    threshold_exceeded = flag.get("threshold_exceeded", "")
    gap_id = f"growth:{_sanitize_gap_segment(board_id)}:{_sanitize_gap_segment(path)}"
    title = f"growth {path} (+{_human_bytes(delta_bytes)}, +{delta_pct}%)"
    body = (
        "_Filed by the periodic data-dir audit pass._\n\n"
        "## Finding\n\n"
        f"- **Board:** `{board_id}`\n"
        f"- **Path:** `{path}`\n"
        f"- **Prior size:** {_human_bytes(prior_size)} ({prior_size} bytes)\n"
        f"- **Current size:** {_human_bytes(current_size)} "
        f"({current_size} bytes)\n"
        f"- **Delta:** +{_human_bytes(delta_bytes)} ({delta_bytes} bytes), "
        f"+{delta_pct}%\n"
        f"- **Threshold exceeded:** {threshold_exceeded}\n"
    )

    from ..data_dir_audit.growth import _GROWTH_CLASS_OTHER

    breakdown = flag.get("breakdown") or []
    if breakdown:
        body += (
            "\n## Growth breakdown (top contributors)\n\n"
            "| Path | Growth | Classification |\n"
            "|---|---|---|\n"
        )
        for item in breakdown:
            body += (
                f"| `{_trim_path(item['path'])}` "
                f"| +{_human_bytes(int(item['delta_bytes']))} "
                f"| {item.get('classification', _GROWTH_CLASS_OTHER)} |\n"
            )
        explained_pct = flag.get("explained_pct")
        if explained_pct is not None:
            body += (
                f"\n~{explained_pct}% of this growth is attributable to "
                "self-healing categories (reclaimed by the audit GC or "
                "reported through their own findings).\n"
            )

    body += (
        "\nThe audit pass reclaims clone and workspace churn automatically; "
        "this finding was filed because the growth could NOT be fully "
        "attributed to self-healing categories. If a contributor above "
        "(focus on `other` rows) is an artifact that mill code writes "
        "without bound, spec a code fix capping or rotating that writer. "
        "If this is one-off operational data, close this ticket with a "
        "note — no agent has host data-dir access, so manual cleanup "
        "cannot be delegated to the pipeline.\n"
    )
    return gap_id, title, body


def _build_unbounded_finding(finding: dict) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for an unbounded-collection finding."""
    path = finding["path"]
    current_size = int(finding["current_size"])
    cap_bytes = int(finding["cap_size"])
    cap_detail = finding.get("cap_detail", "")
    pattern = finding.get("pattern", "")
    record_count = finding.get("record_count")
    record_max = finding.get("record_max")
    gap_id = f"unbounded:{_sanitize_gap_segment(path)}"
    title = f"unbounded {path} (>{_human_bytes(cap_bytes)})"
    body_lines = [
        "_Filed by the periodic data-dir audit pass._",
        "",
        "## Finding",
        "",
        f"- **Path:** `{path}`",
        f"- **Current size:** {_human_bytes(current_size)} ({current_size} bytes)",
        f"- **Cap:** {cap_detail} ({_human_bytes(cap_bytes)})",
        f"- **Pattern:** `{pattern}`",
    ]
    if record_count is not None and record_max is not None:
        body_lines.append(f"- **Record count:** {record_count} (max {record_max})")
    body_lines.append("")
    body_lines.append(
        f"This file exceeds its documented cap ({cap_detail}); "
        "consider pruning or capping.\n"
    )
    body = "\n".join(body_lines)
    return gap_id, title, body


def _build_orphan_finding(orphan: OrphanWorkspace) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for an orphan-workspace finding."""
    board_id = orphan.board_id
    ticket_id = orphan.ticket_id
    size = orphan.dir_size_bytes
    gap_id = (
        f"orphan:{_sanitize_gap_segment(board_id)}:{_sanitize_gap_segment(ticket_id)}"
    )
    title = (
        f"data-dir audit: orphan workspace {board_id}/{ticket_id} "
        f"({_human_bytes(size)})"
    )
    body = (
        "_Filed by the periodic data-dir audit pass._\n\n"
        "## Finding\n\n"
        f"- **Board:** `{board_id}`\n"
        f"- **Ticket id:** `{ticket_id}`\n"
        f"- **Path:** `{orphan.path}`\n"
        f"- **Dir size:** {_human_bytes(size)} ({size} bytes)\n\n"
        "This workspace dir belongs to a ticket no longer in the DB; "
        "consider removing it.\n"
    )
    return gap_id, title, body


def _order_findings(
    oversized: list[dict],
    growth_flags: list[dict],
    unbounded: list[dict],
    orphans_by_board: dict[str, list[OrphanWorkspace]],
) -> list[tuple[str, str, str]]:
    """Order findings deterministically and return ``[(gap_id, title, body)]``.

    Filing priority: orphans → growth (delta_bytes desc) → oversized
    (size_bytes desc) → unbounded (current_size desc).
    """
    ordered: list[tuple[str, str, str]] = []

    # 1. Orphans: sort by board_id, then ticket_id.
    for board_id in sorted(orphans_by_board):
        for orphan in sorted(orphans_by_board[board_id], key=lambda o: o.ticket_id):
            ordered.append(_build_orphan_finding(orphan))

    # 2. Growth flags: delta_bytes desc, then path for ties.
    for flag in sorted(
        growth_flags,
        key=lambda f: (-int(f.get("delta_bytes", 0)), f.get("path", "")),
    ):
        ordered.append(_build_growth_finding(flag))

    # 3. Oversized: size_bytes desc, then path for ties.
    for item in sorted(
        oversized,
        key=lambda i: (-int(i.get("size_bytes", 0)), i.get("path", "")),
    ):
        ordered.append(_build_oversized_finding(item))

    # 4. Unbounded: current_size desc, then path for ties.
    for finding in sorted(
        unbounded,
        key=lambda f: (-int(f.get("current_size", 0)), f.get("path", "")),
    ):
        ordered.append(_build_unbounded_finding(finding))

    return ordered


def _file_findings_as_tickets(
    settings,
    service: TicketService,
    oversized: list[dict],
    growth_flags: list[dict],
    unbounded: list[dict],
    orphans_by_board: dict[str, list[OrphanWorkspace]],
    session_id: str = "",
) -> list[dict]:
    """File draft tickets for findings, dedup'd via gap-id markers.

    Returns ``[{"id": ticket.id, "title": ticket.title}, ...]`` for
    every draft actually created. Findings whose gap-id matches an
    *in-flight* prior draft are silently skipped; findings beyond
    ``settings.data_dir_audit_max_drafts_per_pass`` are dropped.

    Filing target: every draft is created on the service the caller
    passes in — typically the scheduling board's ``TicketService``.
    When the periodic audit is enabled on multiple boards, each board
    scans the entire ``.data/`` and would race to file overlapping
    findings. Cross-pass dedup catches re-runs within one board's
    history but does NOT prevent cross-board duplication on the first
    pass. Acceptable trade-off — operators are expected to enable
    ``data_dir_audit_periodic`` on a single (maintenance) board.
    """
    from ..pass_runner import _verify_prior_proposals

    prior = _verify_prior_proposals(service, settings, SourceKind.DATA_DIR_AUDIT)
    in_flight: set[str] = {
        gid for gid, info in prior.items() if info["resolution"] == "in-flight"
    }

    ordered = _order_findings(oversized, growth_flags, unbounded, orphans_by_board)

    # Single per-sweep cap spanning ALL growth classes: the cap counts
    # only drafts actually ``created`` across the unified ``_order_findings``
    # list (orphan → growth → oversized → unbounded). There is no
    # per-class cap; dedup-skipped in-flight findings do not consume slots.
    cap = settings.data_dir_audit_max_drafts_per_pass
    created: list[dict] = []
    for gap_id, title, body in ordered:
        if cap > 0 and len(created) >= cap:
            log.info(
                "data_dir_audit: hit per-pass cap of %d drafts — "
                "remaining findings dropped (will be reconsidered next pass)",
                cap,
            )
            break
        if gap_id in in_flight:
            log.debug(
                "data_dir_audit: skipping finding — in-flight ticket "
                "already filed for gap_id=%s",
                gap_id,
            )
            continue
        marker = f"<!-- data_dir_audit-gap-id: {gap_id} -->"
        body_with_marker = body.rstrip() + "\n\n" + marker
        try:
            ticket = service.create(
                title=title,
                description=body_with_marker,
                source=SourceKind.DATA_DIR_AUDIT,
                origin_session=session_id or None,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            in_flight.add(gap_id)
            log.info(
                "data_dir_audit: created draft %s — %s",
                ticket.id,
                title,
            )
        except Exception:
            log.exception(
                "data_dir_audit: failed to create draft ticket: %s",
                title,
            )
    return created
