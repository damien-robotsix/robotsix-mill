"""Module-level helpers and the ``TransitionError`` exception.

Hash-chain event helpers, slug / JSON-list parsing utilities, and the
state-machine :class:`TransitionError`, factored out of ``service.py`` so
the mixin modules (and the package ``__init__``) can share them without a
circular import.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from ..models import Ticket, TicketEvent
from ..states import State

log = logging.getLogger("robotsix_mill.service")


def _get_ticket(db_session: Session, ticket_id: str) -> Ticket:
    """Return the Ticket for *ticket_id*, or raise ``KeyError``."""
    ticket = db_session.get(Ticket, ticket_id)
    if ticket is None:
        raise KeyError(ticket_id)
    return ticket


def _event_hash(
    ticket_id: str,
    state: str,
    note: str | None,
    at: str,
    prev_hash: str | None,
) -> str:
    """Compute BLAKE2b hash over the canonical JSON payload of an event."""
    payload = {
        "ticket_id": ticket_id,
        "state": state,
        "note": note,
        "at": at,
        "prev_hash": prev_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()


def _prev_hash_for(db_session, ticket_id: str) -> str | None:
    """Return the hash of the most recent event for *ticket_id*, or None."""
    prev = db_session.exec(
        select(TicketEvent.hash)
        .where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.id.desc())
    ).first()
    return prev if prev else None


def _make_event(
    db_session: Session,
    ticket_id: str,
    state: State,
    note: str | None = None,
) -> TicketEvent:
    """Build a TicketEvent with hash-chain fields populated."""
    at = datetime.now(timezone.utc)
    prev_hash = _prev_hash_for(db_session, ticket_id)
    h = _event_hash(
        ticket_id=ticket_id,
        state=state.value,
        note=note,
        at=at.isoformat(),
        prev_hash=prev_hash,
    )
    return TicketEvent(
        ticket_id=ticket_id,
        state=state,
        note=note,
        at=at,
        prev_hash=prev_hash,
        hash=h,
    )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:40].strip("-") or "ticket"


def _parse_depends_on_str(raw: str | None) -> list[str]:
    """Parse a JSON-encoded list of ticket IDs from the depends_on
    column. Returns an empty list for ``None`` or malformed input."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return parsed
    except json.JSONDecodeError, TypeError:
        pass
    return []


def _parse_labels(raw: str | None) -> list[str]:
    """Parse a JSON-encoded list of label strings from the labels
    column. Returns an empty list for ``None`` or malformed input."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return parsed
    except json.JSONDecodeError, TypeError:
        pass
    return []


def verify_merge_before_done(
    ticket_id: str,
    repo_dir: Path | None,
    branch_prefix: str,
    forge_target_branch: str,
    *,
    branch_name: str | None = None,
) -> None:
    """Verify that the ticket's branch has been merged to origin/main.

    Fast path: ``git merge-base --is-ancestor``.  Fallback 1: log
    grep for the ticket ID on origin/main (squash-merge detection).
    Fallback 2: content-level — diff the branch tip against
    origin/main, and for each changed file grep for the ticket ID
    in the file on origin/main.

    Raises ``TransitionError`` with a descriptive message when
    the merge cannot be confirmed.  Best-effort: returns silently
    when the repo is unavailable or git commands fail (do not
    block on transient tooling issues).
    """
    if repo_dir is None or not repo_dir.exists():
        return  # no workspace clone — can't verify, allow

    branch = branch_name or f"{branch_prefix}{ticket_id}"
    target = f"origin/{forge_target_branch}"

    # Resolve the branch tip.
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", branch],
            capture_output=True,
            text=True,
        )
    except Exception:
        return  # best-effort: git unavailable
    if result.returncode != 0:
        return  # branch doesn't exist locally — can't verify, allow
    branch_tip = result.stdout.strip()

    # Fetch the latest merge target (best-effort).
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "fetch",
                "origin",
                forge_target_branch,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, Exception):  # fmt: skip
        pass  # best-effort: fetch failed, try with existing ref

    # Verify the target ref exists (local or remote-tracking).
    try:
        ref_check = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", target],
            capture_output=True,
            text=True,
        )
    except Exception:
        return  # best-effort: git unavailable
    if ref_check.returncode != 0:
        return  # target ref not available — can't verify, allow

    # Fast path: ancestor check.
    try:
        anc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "merge-base",
                "--is-ancestor",
                branch_tip,
                target,
            ],
            capture_output=True,
            text=True,
        )
    except Exception:
        return
    if anc.returncode == 0:
        return  # branch tip is an ancestor

    # Fallback 1: squash-merge detection via log grep.
    try:
        greplog = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "log",
                target,
                "--oneline",
                "--fixed-strings",
                f"--grep={ticket_id}",
            ],
            capture_output=True,
            text=True,
        )
    except Exception:
        return
    if greplog.returncode == 0 and greplog.stdout.strip():
        return  # squash-merge commit found

    # Fallback 2: content-level verification.
    try:
        diff_files = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--name-only",
                f"{target}..{branch_tip}",
            ],
            capture_output=True,
            text=True,
        )
    except Exception:
        return
    if diff_files.returncode != 0:
        return
    changed = [f for f in diff_files.stdout.strip().split("\n") if f]
    if not changed:
        return  # no diff — content already on target

    for path in changed:
        try:
            show = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "show",
                    f"{target}:{path}",
                ],
                capture_output=True,
                text=True,
            )
        except Exception:
            log.debug(
                "%s: git show %s:%s failed — skipping content check",
                ticket_id,
                target,
                path,
            )
            continue
        if show.returncode == 0 and ticket_id in show.stdout:
            return  # content evidence found

    raise TransitionError(
        f"{ticket_id}: cannot mark done — "
        f"branch {branch} has not been merged to {target}. "
        f"Merge the PR first, then retry."
    )


class TransitionError(RuntimeError):
    """Requested state transition is not allowed by the state machine."""
