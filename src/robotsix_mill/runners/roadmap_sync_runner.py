"""Roadmap-sync runner — create/update epics from a repo's ROADMAP.md.

The runner reads ``ROADMAP.md`` from the configured repo's clone,
parses its H2 sections as epics, and reconciles them against the
board's existing epics by an HTML-comment marker:

    ## My Epic Title
    <!-- epic-id: 20260527T123456Z-my-epic-title-abcd -->

    Body content...

- Sections that carry a marker → update the matching epic's title and
  description if they differ.
- Sections without a marker → create a new epic and append the marker
  back into the section so the next sync is idempotent.
- Sections whose marker points to a now-missing epic → log a warning
  and skip (the epic was probably deliberately closed).

When new epics were created, the runner commits the updated
``ROADMAP.md`` to a branch and opens a PR so the marker insertions
land alongside the user's roadmap edits. No remote / no credentials →
the epics still land in the board, but the marker write-back is left
in the local clone and reported in the summary.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import RepoConfig, Settings, target_branch_for
from ..core.models import SourceKind, Ticket, TicketKind
from ..core.service import TicketService
from ..forge import get_forge
from ..forge.auth import _resolve_remote_url, github_push_token, github_token
from ..vcs import git_ops

log = logging.getLogger("robotsix_mill.roadmap_sync")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_MARKER_RE = re.compile(
    r"<!--\s*epic-id:\s*([A-Za-z0-9._\-]+)\s*-->",
)
_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


@dataclass
class EpicSection:
    """One H2-delimited epic section parsed from ROADMAP.md.

    ``raw_span`` is the (start, end) byte offsets into the original
    markdown — used to splice the marker back in without disturbing
    the rest of the file.
    """

    title: str
    body: str  # section content, marker stripped
    marker_id: str | None  # epic id from <!-- epic-id: ... --> or None
    raw_span: tuple[int, int]


def parse_roadmap(markdown: str) -> list[EpicSection]:
    """Parse *markdown* into an ordered list of epic sections.

    H2 headings (``## ...``) delimit sections; the title is the
    heading text, the body is everything up to the next H2 (or EOF),
    with any ``<!-- epic-id: ... -->`` comment removed. Content before
    the first H2 (preamble) is ignored.
    """
    matches = list(_H2_RE.finditer(markdown))
    sections: list[EpicSection] = []
    for i, m in enumerate(matches):
        title = m.group("title").strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        raw = markdown[body_start:body_end]
        marker_match = _MARKER_RE.search(raw)
        marker_id = marker_match.group(1) if marker_match else None
        if marker_match:
            # Drop the marker line cleanly (with its trailing newline
            # if present) so the body the user sees on the epic is
            # free of the synthetic comment.
            start, end = marker_match.span()
            # Pull in the trailing newline if it follows immediately.
            if end < len(raw) and raw[end] == "\n":
                end += 1
            cleaned = raw[:start] + raw[end:]
        else:
            cleaned = raw
        sections.append(
            EpicSection(
                title=title,
                body=cleaned.strip(),
                marker_id=marker_id,
                raw_span=(m.start(), body_end),
            )
        )
    return sections


def insert_markers(markdown: str, new_ids: dict[int, str]) -> str:
    """Return *markdown* with ``<!-- epic-id: ... -->`` inserted into
    every section whose 0-based index is in *new_ids*.

    The marker is placed on the line directly after the H2 heading,
    preserving everything else byte-for-byte. Sections must be
    processed bottom-up so earlier-section spans aren't shifted.
    """
    matches = list(_H2_RE.finditer(markdown))
    out = markdown
    for idx in sorted(new_ids.keys(), reverse=True):
        if idx >= len(matches):
            continue
        m = matches[idx]
        insert_at = m.end()
        # The H2 line ends with the newline that follows the title;
        # the regex end-point is BEFORE that newline. Insert the
        # marker block one line below for readability.
        marker_block = f"\n<!-- epic-id: {new_ids[idx]} -->\n"
        out = out[:insert_at] + marker_block + out[insert_at:]
    return out


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass
class RoadmapSyncPassResult:
    """Summary of one roadmap-sync pass."""

    created: list[dict]  # [{id, title}]
    updated: list[dict]  # [{id, title, fields: ["title", "body"]}]
    skipped: list[dict]  # [{title, reason}]
    pr_url: str | None = None
    summary: str = ""
    session_id: str = ""


def _list_existing_epics(service: TicketService) -> dict[str, Ticket]:
    """Return ``{epic_id: Ticket}`` for every epic on the service's board."""
    return {t.id: t for t in service.list() if t.kind == TicketKind.EPIC}


def _read_body(service: TicketService, ticket: Ticket) -> str:
    try:
        return service.workspace(ticket).read_description().strip()
    except Exception:  # noqa: BLE001 — be forgiving on read errors
        return ""


def _normalize_body(text: str) -> str:
    """Body comparator — strip trailing whitespace per-line so a stray
    space at end-of-line doesn't trigger a spurious update."""
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _create_or_update_epics(
    service: TicketService,
    sections: list[EpicSection],
) -> tuple[list[dict], list[dict], list[dict], dict[int, str]]:
    """Apply each parsed section to the board.

    Returns ``(created, updated, skipped, new_ids_by_section_index)``
    where ``new_ids_by_section_index`` maps each freshly-created
    epic's section index to its new ticket id — used by
    :func:`insert_markers` to splice the marker back into ROADMAP.md.
    """
    existing = _list_existing_epics(service)
    created: list[dict] = []
    updated: list[dict] = []
    skipped: list[dict] = []
    new_ids: dict[int, str] = {}

    for idx, section in enumerate(sections):
        if section.marker_id is not None:
            epic = existing.get(section.marker_id)
            if epic is None:
                log.warning(
                    "roadmap-sync: marker points to missing epic %s "
                    "(section %r) — skipping",
                    section.marker_id,
                    section.title,
                )
                skipped.append(
                    {
                        "title": section.title,
                        "reason": f"marker {section.marker_id} not found on board",
                    }
                )
                continue
            fields_changed: list[str] = []
            if epic.title.strip() != section.title:
                service.set_title(epic.id, section.title)
                fields_changed.append("title")
            current_body = _normalize_body(_read_body(service, epic))
            new_body = _normalize_body(section.body)
            if current_body != new_body:
                ws = service.workspace(epic)
                content_hash = ws.write_description(section.body)
                service.set_content_hash(epic.id, content_hash)
                fields_changed.append("body")
            if fields_changed:
                updated.append(
                    {
                        "id": epic.id,
                        "title": section.title,
                        "fields": fields_changed,
                    }
                )
        else:
            t = service.create(
                title=section.title,
                description=section.body,
                source=SourceKind.ROADMAP_SYNC,
                kind=TicketKind.EPIC,
            )
            created.append({"id": t.id, "title": t.title})
            new_ids[idx] = t.id

    return created, updated, skipped, new_ids


# ---------------------------------------------------------------------------
# Marker write-back via PR
# ---------------------------------------------------------------------------


def _commit_and_open_pr(
    settings: Settings,
    repo_config: RepoConfig | None,
    repo_dir: Path,
    created: list[dict],
) -> str | None:
    """Commit ROADMAP.md, push a fresh branch, open a PR. Returns the
    PR URL on success or ``None`` when remote/credentials aren't
    configured (the marker patch stays local — operator can commit
    by hand)."""
    remote_url = _resolve_remote_url(settings, repo_config)
    if not remote_url:
        log.info("roadmap-sync: no remote configured — markers left local")
        return None
    try:
        token = github_push_token(settings, repo_config=repo_config)
    except RuntimeError as e:
        log.warning("roadmap-sync: forge auth missing (%s) — markers left local", e)
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    branch = f"{settings.branch_prefix}roadmap-sync-{stamp}"

    titles = ", ".join(f"`{e['title']}`" for e in created[:5])
    if len(created) > 5:
        titles += f", … (+{len(created) - 5} more)"
    commit_msg = (
        f"docs(roadmap): add epic markers for {len(created)} new epic(s)\n\n"
        f"Created: {titles}\n\n"
        "Automated by robotsix-mill · /roadmap-sync"
    )

    try:
        git_ops.create_branch(repo_dir, branch)
        git_ops.commit_all(repo_dir, commit_msg)
    except subprocess.CalledProcessError as e:
        log.warning(
            "roadmap-sync: git commit failed (%s) — markers left local",
            (e.stderr or "").strip()[:200],
        )
        return None

    try:
        git_ops.push(repo_dir, branch, remote_url, token)
    except subprocess.CalledProcessError as e:
        log.warning(
            "roadmap-sync: git push failed (%s) — markers left local",
            (e.stderr or "").strip()[:200],
        )
        return None

    pr_title = f"docs(roadmap): add epic markers ({len(created)})"
    pr_body = (
        f"This PR adds `<!-- epic-id: ... -->` markers to ROADMAP.md for "
        f"{len(created)} epic(s) that the roadmap-sync agent just created "
        f"on the board.\n\n"
        f"New epics:\n"
        + "\n".join(f"- `{e['id']}` — {e['title']}" for e in created)
        + "\n\n---\nAutomated by robotsix-mill · `/roadmap-sync`\n"
    )
    try:
        return get_forge(settings, repo_config=repo_config).open_merge_request(
            source_branch=branch,
            title=pr_title,
            body=pr_body,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("roadmap-sync: open PR failed (%s) — branch pushed but no PR", e)
        return None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _clone_or_reuse(
    settings: Settings,
    repo_config: RepoConfig | None,
) -> Path | None:
    """Clone the repo into a roadmap-sync workspace, or refresh an
    existing clone to the repo's resolved target branch (per-repo when
    configured, else ``settings.forge_target_branch``) so this run
    sees the latest ROADMAP.md on that branch.

    Returns the clone path, or ``None`` when no remote is configured
    / clone fails. Best-effort throughout — every failure path logs
    a warning rather than raising.
    """
    remote_url = _resolve_remote_url(settings, repo_config)
    if not remote_url:
        return None
    repo_id = repo_config.repo_id if repo_config else "default"
    workspace = settings.data_dir / repo_id / "roadmap_sync_workspace"
    cand = workspace / "repo"
    try:
        token = github_token(settings, repo_config=repo_config)
    except RuntimeError:
        token = None
    target = target_branch_for(settings, repo_config)

    if (cand / ".git").exists():
        # Refresh the clone so we read the latest ROADMAP.md on main.
        try:
            git_ops.fetch(cand, remote_url=remote_url, token=token, branch=target)
            git_ops.checkout(cand, target)
            git_ops._git(cand, "reset", "--hard", f"origin/{target}")
            git_ops._git(cand, "clean", "-fd")
            return cand
        except subprocess.CalledProcessError as e:
            log.warning(
                "roadmap-sync: clone refresh failed (%s) — reusing stale clone",
                (e.stderr or "").strip()[:200],
            )
            return cand

    try:
        workspace.mkdir(parents=True, exist_ok=True)
        git_ops.clone(remote_url, cand, target, token)
        return cand
    except subprocess.CalledProcessError as e:
        log.warning(
            "roadmap-sync: clone failed (%s)",
            (e.stderr or "").strip()[:200],
        )
        return None


def run_roadmap_sync_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> RoadmapSyncPassResult:
    """Execute one roadmap-sync pass for *repo_config*'s board.

    Returns a :class:`RoadmapSyncPassResult` even on best-effort
    failure paths (no remote, no ROADMAP.md, clone failure) — the
    operator can read ``summary`` for the cause.
    """
    settings = Settings()
    board_id = repo_config.board_id if repo_config else ""
    service = TicketService(settings, board_id=board_id)

    repo_dir = _clone_or_reuse(settings, repo_config)
    if repo_dir is None:
        return RoadmapSyncPassResult(
            created=[],
            updated=[],
            skipped=[],
            summary="no remote configured / clone failed — see logs",
            session_id=session_id,
        )

    roadmap = repo_dir / "ROADMAP.md"
    if not roadmap.exists():
        return RoadmapSyncPassResult(
            created=[],
            updated=[],
            skipped=[],
            summary="ROADMAP.md not found in repo",
            session_id=session_id,
        )

    original = roadmap.read_text(encoding="utf-8")
    sections = parse_roadmap(original)
    if not sections:
        return RoadmapSyncPassResult(
            created=[],
            updated=[],
            skipped=[],
            summary="ROADMAP.md has no `## ...` epic sections",
            session_id=session_id,
        )

    created, updated, skipped, new_ids = _create_or_update_epics(
        service,
        sections,
    )

    pr_url: str | None = None
    if new_ids:
        new_markdown = insert_markers(original, new_ids)
        roadmap.write_text(new_markdown, encoding="utf-8")
        pr_url = _commit_and_open_pr(settings, repo_config, repo_dir, created)

    summary_parts = [
        f"created={len(created)}",
        f"updated={len(updated)}",
        f"skipped={len(skipped)}",
    ]
    if pr_url:
        summary_parts.append(f"marker PR: {pr_url}")
    elif new_ids:
        summary_parts.append("markers left in local clone (no PR)")

    return RoadmapSyncPassResult(
        created=created,
        updated=updated,
        skipped=skipped,
        pr_url=pr_url,
        summary="; ".join(summary_parts),
        session_id=session_id,
    )
