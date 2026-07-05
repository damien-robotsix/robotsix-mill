"""Auto-registration of detected vcs2l workspace members as RepoConfig
entries (Ticket 3 of the workspace member-sync epic).

:func:`detect_workspace_members` (see :mod:`workspace_members`) parses a
master repo's vcs2l manifest into a list of :class:`DetectedMember`. This
module turns those into registry entries: for each member it derives a
``repo_id`` from the manifest path, an ``forge_remote_url`` from the
manifest ``url``, a ``working_branch`` from the manifest ``version`` and a
``cross_repo_target`` from the master's per-member policy, inheriting the
master's Langfuse project (the inheritance mechanism is refined in Ticket
4). Entries are upserted into ``<data_dir>/registered_repos.yaml`` and the
registry singleton is hot-reloaded via :func:`_reset_repos_config`, exactly
like the repo-scaffold workflow does for brand-new repos.

Each *newly* registered member also gets a build-out ticket filed on its
own board (which the ticket service materialises on first write), so the
normal pipeline populates its ``.robotsix-mill/config.yaml`` on the pinned
working branch.

A member that has disappeared from the manifest is **flagged** for operator
removal (``pending_removal: true`` on its registry entry) rather than
auto-deleted — the board and its history are preserved for the operator to
retire deliberately.

Synced entries carry a ``member_of: <master_repo_id>`` provenance key so a
later pass can tell member-sync entries apart from manually configured
repos (and so disappearance detection only ever touches this master's own
members, never hand-written config).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..config import Settings

from ..config import _reset_repos_config
from . import _repos_yaml_path
from ..config.workspace_members import DetectedMember

log = logging.getLogger("robotsix_mill.workspace_member_sync")


@dataclass
class MemberSyncResult:
    """Outcome of a :func:`sync_workspace_members` pass.

    * ``added`` — repo_ids registered for the first time this pass.
    * ``updated`` — existing member entries whose fields were refreshed.
    * ``flagged_for_removal`` — member repo_ids no longer in the manifest,
      marked ``pending_removal: true`` (board left intact).
    * ``filed_tickets`` — ``repo_id -> ticket_id`` for build-out tickets.
    * ``skipped`` — repo_ids that collide with a non-member registry entry
      and were left untouched to avoid clobbering manual config.
    """

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    flagged_for_removal: list[str] = field(default_factory=list)
    filed_tickets: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def sync_workspace_members(
    settings: Settings,
    master_repo_id: str,
    members: Iterable[DetectedMember],
    *,
    repos_yaml_path: Path | None = None,
    file_tickets: bool = True,
) -> MemberSyncResult:
    """Register *members* of *master_repo_id* into the repos overlay.

    Parameters:
        settings: Mill :class:`~robotsix_mill.config.Settings`.
        master_repo_id: registry id of the workspace-skeleton repo the
            members were detected from; the inherited Langfuse project and
            the ``member_of`` provenance marker both come from it.
        members: detected workspace members (see
            :func:`~robotsix_mill.config.workspace_members.detect_workspace_members`).
        repos_yaml_path: override for the auto-registration overlay path;
            defaults to :func:`repo_scaffold._repos_yaml_path` (honours
            ``MILL_REPOS_FILE``).
        file_tickets: when True (default) file a build-out ticket on each
            newly registered member's board.

    Returns:
        :class:`MemberSyncResult` describing what changed. A no-op
        (``MILL_REPOS_FILE`` empty) returns an empty result.
    """
    members = list(members)
    path = (
        repos_yaml_path if repos_yaml_path is not None else _repos_yaml_path(settings)
    )
    result = MemberSyncResult()
    if path is None:
        log.info("MILL_REPOS_FILE is empty — skipping workspace member sync")
        return result

    data = _load_repos_document(path)
    repos = data["repos"]

    member_ids = _upsert_members(repos, members, master_repo_id, result)
    _flag_vanished(repos, master_repo_id, member_ids, result)

    _write_repos_document(path, data)
    _reset_repos_config()
    log.info(
        "workspace member sync for %s: +%d added, %d updated, %d flagged",
        master_repo_id,
        len(result.added),
        len(result.updated),
        len(result.flagged_for_removal),
    )

    if file_tickets:
        for repo_id in result.added:
            ticket_id = _file_member_buildout(settings, repo_id, repos[repo_id])
            if ticket_id:
                result.filed_tickets[repo_id] = ticket_id

    return result


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------


def _upsert_members(
    repos: dict[str, Any],
    members: list[DetectedMember],
    master_repo_id: str,
    result: MemberSyncResult,
) -> set[str]:
    """Upsert each detected member into *repos*, recording the outcome on
    *result*. Returns the set of repo_ids the manifest currently declares."""
    member_ids: set[str] = set()
    for member in members:
        repo_id = _member_repo_id(member.path)
        if not repo_id:
            log.warning(
                "workspace member %r yields an empty repo_id — skipping", member.path
            )
            continue
        member_ids.add(repo_id)
        existing = repos.get(repo_id)
        if existing is not None and not _is_member_of(existing, master_repo_id):
            log.warning(
                "workspace member %r maps to repo_id %r which is already a "
                "non-member registry entry — skipping to avoid clobbering it",
                member.path,
                repo_id,
            )
            result.skipped.append(repo_id)
            continue

        entry = _member_entry(member, repo_id, master_repo_id)
        if existing is None:
            repos[repo_id] = entry
            result.added.append(repo_id)
        else:
            # Refresh the member's derived fields; un-flag a member that has
            # reappeared in the manifest.
            existing.update(entry)
            existing.pop("pending_removal", None)
            result.updated.append(repo_id)
    return member_ids


def _flag_vanished(
    repos: dict[str, Any],
    master_repo_id: str,
    member_ids: set[str],
    result: MemberSyncResult,
) -> None:
    """Flag this master's member entries that vanished from the manifest for
    operator removal (``pending_removal: true``), never auto-deleting them —
    the board + history stay put."""
    for repo_id, entry in repos.items():
        if (
            isinstance(entry, dict)
            and _is_member_of(entry, master_repo_id)
            and repo_id not in member_ids
            and not entry.get("pending_removal")
        ):
            entry["pending_removal"] = True
            result.flagged_for_removal.append(repo_id)


def _member_repo_id(path: str) -> str:
    """Derive a safe ``repo_id`` from a manifest path key.

    Lowercases and collapses every run of non-alphanumeric characters
    (slashes, dots, spaces, ...) into a single hyphen, e.g.
    ``"src/zeta/pkg" -> "src-zeta-pkg"``.
    """
    out: list[str] = []
    prev_dash = False
    for ch in path.lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def _is_member_of(entry: dict[str, Any], master_repo_id: str) -> bool:
    """True when *entry* is a registry stanza synced from *master_repo_id*."""
    return isinstance(entry, dict) and entry.get("member_of") == master_repo_id


def _member_entry(
    member: DetectedMember, repo_id: str, master_repo_id: str
) -> dict[str, Any]:
    """Build the repos overlay stanza for a detected member."""
    entry: dict[str, Any] = {
        # Langfuse is configured globally (top-level ``langfuse`` block);
        # member repos inherit it automatically — no per-repo stanza.
        "forge_remote_url": member.url,
        # Provenance: lets a later pass distinguish member-sync entries from
        # manually configured repos (and scope disappearance detection).
        "member_of": master_repo_id,
    }
    if member.version:
        entry["working_branch"] = member.version
    if member.cross_repo_target is not None:
        entry["cross_repo_target"] = member.cross_repo_target.model_dump()
    return entry


def _load_repos_document(path: Path) -> dict[str, Any]:
    """Load the repos overlay file into a normalised ``{"repos": {...}}`` dict."""
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    repos = data.get("repos")
    if not isinstance(repos, dict):
        data["repos"] = {}
    return data


def _write_repos_document(path: Path, data: dict[str, Any]) -> None:
    """Write *data* back to the repos overlay file preserving key order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)


def _file_member_buildout(
    settings: Settings, repo_id: str, entry: dict[str, Any]
) -> str | None:
    """File a build-out ticket on a newly registered member's own board.

    The build-out covers adding the member's ``.robotsix-mill/config.yaml``
    (``test_command`` + ``languages``) on the pinned working branch so the
    normal pipeline onboards it. Best-effort — returns the ticket id, or
    ``None`` on failure (registration is not failed over this).
    """
    from ..core.models import SourceKind
    from ..core.service import TicketService

    branch = entry.get("working_branch")
    branch_note = (
        f"the pinned working branch `{branch}`"
        if branch
        else "the repository's default branch"
    )
    body = (
        f"The workspace member **{repo_id}** was auto-detected from the "
        f"master repo's vcs2l manifest and registered in "
        f"`<data_dir>/registered_repos.yaml` "
        f"(`forge_remote_url`: `{entry.get('forge_remote_url')}`).\n\n"
        f"## Scope\n\n"
        f"Onboard {repo_id} so the mill pipeline can run against it: add its "
        f"`.robotsix-mill/config.yaml` (`test_command` + `languages`) on "
        f"{branch_note}. If it contributes upstream via a fork, confirm the "
        f"`cross_repo_target` matches the manifest's upstream policy."
    )
    try:
        svc = TicketService(settings, board_id=repo_id)
        ticket = svc.create(
            title=f"Onboard workspace member {repo_id}",
            description=body,
            source=SourceKind.AGENT,
        )
        log.info(
            "workspace member sync: filed build-out ticket %s on board %s",
            ticket.id,
            repo_id,
        )
        return ticket.id
    except Exception:
        log.exception(
            "workspace member sync: failed to file build-out ticket for %s", repo_id
        )
        return None
