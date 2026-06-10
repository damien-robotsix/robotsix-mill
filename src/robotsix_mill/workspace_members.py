"""Workspace-member detection from a managed repo's vcs2l manifest.

A workspace-skeleton repo (e.g. ``robotsix-mill-ros2``) declares its
member repos in a **vcs2l manifest** committed at the repo root:

    <repo_root>/repos.yaml

The manifest is a YAML file with a top-level ``repositories:`` mapping
of ``path -> {type, url, version}``. The master repo can additionally
declare a per-member upstream-contribution policy in its own source
tree at ``<repo_root>/.robotsix-mill/config.yaml`` under a top-level
``members:`` mapping keyed by manifest path, each value an optional
mapping with an optional ``cross_repo_target``.

:func:`detect_workspace_members` reads both, merges them by manifest
path, and returns a structured list of :class:`DetectedMember`. The
``path -> repo_id``, ``url -> forge_remote_url`` and
``version -> working_branch`` derivations happen later (Ticket 3);
this module only reads and returns the raw extracted fields plus the
policy.

Like ``repo_settings.py``, every reader here follows a warn-and-skip
hardening contract: a managed repo MUST NOT be able to crash mill by
committing a broken file, so a malformed/missing input is a silent
no-op (or a logged warning) that returns ``[]`` / ``None`` rather than
raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from robotsix_mill.config import CrossRepoTarget
from robotsix_mill.repo_settings import _load_repo_config_dict

log = logging.getLogger("robotsix_mill.workspace_members")


@dataclass(frozen=True)
class DetectedMember:
    """A workspace member detected from a managed repo's vcs2l manifest.

    Holds the raw extracted manifest fields (``path``, ``url``,
    ``version``) plus the master's per-member ``cross_repo_target``
    policy (or ``None``). The downstream derivations
    (``path -> repo_id``, ``url -> forge_remote_url``,
    ``version -> working_branch``) happen in the registration ticket,
    not here."""

    path: str
    url: str
    version: str | None
    cross_repo_target: CrossRepoTarget | None


def detect_workspace_members(repo_dir: Path | None) -> list[DetectedMember]:
    """Detect workspace members from ``<repo_dir>/repos.yaml``.

    Parses the master repo's vcs2l manifest plus its per-member upstream
    policy and returns the merged list of :class:`DetectedMember`, sorted
    by ``path`` for deterministic output. Never raises — a managed repo
    must not be able to crash mill by committing a broken file:

    * ``repo_dir is None`` → ``[]``.
    * ``<repo_dir>/repos.yaml`` absent → ``[]`` (silent no-op).
    * unreadable / invalid YAML → ``log.warning`` + ``[]``.
    * top-level not a mapping, no ``repositories:`` key, or
      ``repositories`` value not a mapping → ``log.warning`` + ``[]``.
    * a member entry not a dict, or missing/empty ``url`` →
      ``log.warning`` + skip that member.
    * ``version`` absent or not a non-empty string → ``None`` (allowed,
      no warning).
    """
    if repo_dir is None:
        return []
    path = Path(repo_dir) / "repos.yaml"
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning(
            "workspace manifest %s: read/parse error — ignoring (%s)", path, exc
        )
        return []
    if not isinstance(raw, dict):
        log.warning(
            "workspace manifest %s: top-level must be a mapping — ignoring", path
        )
        return []
    repositories = raw.get("repositories")
    if not isinstance(repositories, dict):
        log.warning(
            "workspace manifest %s: 'repositories' must be a mapping — ignoring", path
        )
        return []

    policy = _load_members_policy(repo_dir)

    members: list[DetectedMember] = []
    for member_path, entry in repositories.items():
        if not isinstance(entry, dict):
            log.warning(
                "workspace manifest %s: member %r is not a mapping — skipping",
                path,
                member_path,
            )
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            log.warning(
                "workspace manifest %s: member %r has no valid 'url' — skipping",
                path,
                member_path,
            )
            continue
        version_raw = entry.get("version")
        version = (
            version_raw.strip()
            if isinstance(version_raw, str) and version_raw.strip()
            else None
        )
        members.append(
            DetectedMember(
                path=str(member_path),
                url=url.strip(),
                version=version,
                cross_repo_target=policy.get(str(member_path)),
            )
        )

    members.sort(key=lambda m: m.path)
    return members


def _load_members_policy(repo_dir: Path | None) -> dict[str, CrossRepoTarget | None]:
    """Return the master's per-member upstream policy keyed by manifest
    path, from ``<repo_dir>/.robotsix-mill/config.yaml`` ``members:`` map.

    Never raises. Returns ``{}`` when there is no policy:

    * config dict absent (``_load_repo_config_dict`` returns ``None``) → ``{}``.
    * ``members`` key absent → ``{}`` (no warning).
    * ``members`` present but not a mapping → ``log.warning`` + ``{}``.

    For each ``path -> entry`` under ``members:``: if ``entry`` is a dict
    carrying a ``cross_repo_target`` sub-dict, parse it via
    :class:`CrossRepoTarget`; on a partial/invalid dict
    (``TypeError``/``ValidationError``) ``log.warning`` and map the path to
    ``None`` (a member whose policy is malformed is still a valid member —
    we just drop its policy).
    """
    raw = _load_repo_config_dict(repo_dir)
    if raw is None:
        return {}
    members = raw.get("members")
    if members is None:
        return {}
    if not isinstance(members, dict):
        log.warning("repo settings: 'members' must be a mapping — ignoring")
        return {}

    policy: dict[str, CrossRepoTarget | None] = {}
    for member_path, entry in members.items():
        cross_repo_raw = (
            entry.get("cross_repo_target") if isinstance(entry, dict) else None
        )
        if not isinstance(cross_repo_raw, dict):
            policy[str(member_path)] = None
            continue
        try:
            policy[str(member_path)] = CrossRepoTarget(**cross_repo_raw)
        except (TypeError, ValidationError) as exc:
            log.warning(
                "repo settings: member %r has an invalid 'cross_repo_target' — "
                "ignoring policy (%s)",
                member_path,
                exc,
            )
            policy[str(member_path)] = None
    return policy
