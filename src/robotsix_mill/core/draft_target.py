"""Shared draft-routing helpers.

The mill pipeline runs against an audited repo, but some drafts that
periodic / retrospect agents propose are actually about the mill's
own source tree — those belong on the **mill maintenance board**, not
on the audited repo's board (whose refining agent has no clone of the
mill source and so cannot implement the fix).

This module owns:

- :data:`MILL_SIGNAL_TERMS`: the substring-based heuristic terms that
  indicate a draft is mill-internal.
- :func:`looks_like_mill_internal`: the ≥2-hit detector.
- :func:`resolve_mill_service`: resolve the mill maintenance board's
  :class:`TicketService` from settings, returning *None* on any
  failure so callers must explicitly handle the fallback.

The detection lives here (rather than in ``stages/retrospect.py``)
because it is shared by **both** the retrospect stage's
``draft_target``/``follow_up_target`` auto-correction AND the
periodic-pass runner (``pass_runner.run_agent_pass``) — the periodic
agents have no ``draft_target`` field, so the heuristic alone decides
routing for them.

Imports are deliberately minimal (``Settings`` + ``TicketService``
only) to avoid circular imports back to ``retrospect.py`` /
``pass_runner.py`` / any ``stages/`` module.
"""

from __future__ import annotations

import logging
import pathlib

from ..config import Settings
from ..vcs import git_ops
from .dedup import paths_excluding_out_of_scope
from .repo_layout import resolve_under_src
from .service import TicketService

log = logging.getLogger("robotsix_mill.core.draft_target")


# Token signals that strongly suggest the proposed draft is about
# mill-pipeline internals (and so belongs on the mill maintenance
# board, not the audited repo's board). Matched case-insensitively
# against ``<title>\n<body>``. Two or more hits override the routing
# to the mill board — the audited repo's refining agent has no clone
# of the mill source tree, so a mill-internal draft on the audited
# board can't be implemented.
MILL_SIGNAL_TERMS: frozenset[str] = frozenset(
    {
        "src/robotsix_mill/",
        "agent_definitions/",
        "scope-triage",
        "scope_triage",
        "refine agent",
        "refining.py",
        "implement stage",
        "implement.py",
        "deliver stage",
        "deliver.py",
        "merge stage",
        "merge.py",
        "retrospect stage",
        "retrospect.py",
        "review stage",
        "periodic_runner",
        "pass_runner",
        "the mill pipeline",
        "mill pipeline",
        "mill's pipeline",
        "stages/",
        "agents/",
        "runtime/",
        "agent_check",
        "config_sync",
        "trace-review",
        "trace_review",
        "trace-health",
        "trace_health",
        "TicketService",
        "RepoConfig",
        "TicketEvent",
        "config/config.example.yaml",
        "config/config.yaml",
        "config.example.yaml",
    }
)


# A subset of ``MILL_SIGNAL_TERMS`` that is, on its own, conclusive
# evidence the draft is about mill's own source tree. These paths are
# unique to the mill repo and never legitimately name an audited
# repo's own code, so a SINGLE hit suffices to reroute (no ≥2-hit
# guard). The weaker generic terms (``stages/``, ``agents/``,
# ``runtime/``, ...) can plausibly appear in audited-repo drafts and
# keep the ≥2-hit requirement.
MILL_STRONG_SIGNAL_TERMS: frozenset[str] = frozenset(
    {
        "src/robotsix_mill/",
        "agent_definitions/",
    }
)


# Path prefixes unique to the mill source tree.  When a draft
# references a token that starts with one of these (case-insensitive
# match on the normalised token) and the path does NOT exist in the
# audited repo, the token is likely a mill-internal path that the
# audited repo's refining agent cannot resolve.
MILL_PATH_PREFIXES: frozenset[str] = frozenset(
    {
        "src/robotsix_mill/",
        "agent_definitions/",
        "agent_definitions/language_instructions/",
        "config/config.example.yaml",
        "config/config.",
    }
)


def looks_like_mill_internal(title: str | None, body: str | None) -> bool:
    """Heuristic: True when the draft's title+body name enough mill-
    internal symbols / files that routing to the audited repo would
    leave the fix on a codebase that can't implement it.

    Two ways to trigger the override:

    - **Strong short-circuit:** a single hit on any term in
      :data:`MILL_STRONG_SIGNAL_TERMS` (a path under
      ``src/robotsix_mill/`` or ``agent_definitions/``) suffices.
      Such a path is unique to the mill tree and never legitimately
      names an audited repo's own code. This catches the
      ``module_curator`` misroute where a draft titled
      ``Reorganize module notify: move notify.py to
      src/robotsix_mill/notify/`` (referencing
      ``src/robotsix_mill/notify.py``) carried only ONE signal term
      and so fell below the ≥2-hit threshold.
    - **Weak ≥2-hit rule:** otherwise, two or more distinct
      :data:`MILL_SIGNAL_TERMS` hits are required, suppressing false
      positives from weak generic terms (``stages/``, ``agents/``,
      ``runtime/``, ...) that can appear in audited-repo drafts.

    Catches the retrospect-agent misclassification observed on c57b
    (scope-triage loop bug filed on robotsix-auto-mail because the
    parent ticket was on auto-mail — but every proposed file change
    lived under ``src/robotsix_mill/``).
    """
    hay = f"{title or ''}\n{body or ''}".lower()
    if any(term.lower() in hay for term in MILL_STRONG_SIGNAL_TERMS):
        return True
    hits = sum(1 for term in MILL_SIGNAL_TERMS if term.lower() in hay)
    return hits >= 2


def resolve_mill_service(
    settings: Settings,
    default_service: TicketService,
    *,
    caller_label: str = "",
) -> TicketService | None:
    """Resolve the mill maintenance board's :class:`TicketService`.

    Reads ``settings.trace_review_target_repo_id`` and looks it up in
    the repos registry. On success, returns a fresh ``TicketService``
    bound to that repo's board. On any failure (unset setting, unknown
    repo_id, lookup error, missing ``board_id``), returns ``None`` —
    callers must explicitly handle the fallback to ``default_service``.

    *default_service* is accepted so the warning messages can identify
    the audited board the draft would otherwise fall back to; it is
    not returned by this function.

    *caller_label* is interpolated into log messages (e.g. ``"retrospect"``,
    ``"audit"``) so periodic-pass and retrospect-stage call sites are
    distinguishable in logs.
    """
    target_repo_id = settings.trace_review_target_repo_id
    if not target_repo_id:
        log.warning(
            "%s: mill routing requested but "
            "trace_review_target_repo_id is unset — caller should fall "
            "back to the current repo",
            caller_label or "draft_target",
        )
        return None
    try:
        from ..config import get_repos_config

        rc = get_repos_config().repos.get(target_repo_id)
    except Exception:  # noqa: BLE001 — fallback must always work
        log.exception(
            "%s: target-repo lookup failed; caller should fall back "
            "to the current repo",
            caller_label or "draft_target",
        )
        return None
    if rc is None:
        log.warning(
            "%s: configured mill target %r not in repos.yaml — caller "
            "should fall back to the current repo",
            caller_label or "draft_target",
            target_repo_id,
        )
        return None
    if not rc.board_id:
        log.warning(
            "%s: configured mill target %r has no board_id — caller "
            "should fall back to the current repo",
            caller_label or "draft_target",
            target_repo_id,
        )
        return None
    return TicketService(settings, board_id=rc.board_id)


def referenced_mill_paths_absent(
    title: str | None,
    body: str | None,
    repo_dir: pathlib.Path | None,
) -> list[str]:
    """Return mill-prefixed file-path tokens that are absent from *repo_dir*
    **and not gitignored**.

    Extracts candidate path tokens from ``f"{title}\\n{body}"`` (via
    :func:`paths_excluding_out_of_scope`, which skips tokens inside
    out-of-scope regions), keeps only those whose normalized form
    starts with one of :data:`MILL_PATH_PREFIXES` (case-insensitive),
    and returns the subset for which ``(repo_dir / token).exists()``
    is ``False``.

    Gitignored paths (detected via ``git check-ignore``) are excluded
    from the result — e.g. ``config/config.yaml``, created from the
    committed example at deploy time and gitignored by design, is a
    mill-prefixed path that will always be absent on a checkout but
    should NOT trigger a consumer-migration ticket.  If ``git
    check-ignore`` errors (not a git repo, git not on PATH, etc.) the
    filter fails open — all absent paths are returned unmodified.

    When *repo_dir* is ``None`` returns an empty list immediately
    (no filesystem is available to check).  The function is pure /
    read-only and must never raise — token iteration is wrapped
    defensively with a broad ``except``.
    """
    if repo_dir is None:
        return []
    haystack = f"{title or ''}\n{body or ''}"
    try:
        candidates = paths_excluding_out_of_scope(haystack)
    except Exception:
        return []
    absent: list[str] = []
    for token in candidates:
        try:
            token_lower = token.lower()
        except Exception:
            log.debug(
                "referenced_mill_paths_absent: token.lower() failed for %r", token
            )
            continue
        if any(token_lower.startswith(prefix.lower()) for prefix in MILL_PATH_PREFIXES):
            if resolve_under_src(repo_dir, token) is None:
                absent.append(token)

    if not absent:
        return absent

    # Exclude gitignored paths — an operator-local file like
    # config/config.yaml is always absent from a checkout because
    # it is never committed, but it is NOT a missing consumer path.
    try:
        ignored = git_ops.ignored_paths(repo_dir, absent)
    except Exception:
        log.debug(
            "referenced_mill_paths_absent: git check-ignore failed — "
            "returning all absent paths unfiltered",
            exc_info=True,
        )
        return absent

    if ignored:
        ignored_set = frozenset(ignored)
        absent = [p for p in absent if p not in ignored_set]

    return absent


def _package_root(token: str) -> str | None:
    """The package-root directory for a path token.

    Only ``src/<pkg>/...`` paths have a meaningful package root —
    generic directories (``tests/``, ``docs/``, etc.) are not
    repo-specific and return ``None``.

    ``src/robotsix_chat/chat/server/app.py`` → ``src/robotsix_chat``
    ``tests/test_foo.py`` → ``None``
    ``docs/guide.md`` → ``None``
    ``foo.py`` → ``None`` (no directory component)
    """
    parts = token.split("/")
    if len(parts) < 2:
        return None
    if parts[0] == "src" and len(parts) >= 2:
        return f"src/{parts[1]}"
    return None


def has_unverifiable_cross_repo_refs(
    title: str | None,
    body: str | None,
    repo_dir: pathlib.Path | None,
) -> bool:
    """Return True when *title*/*body* reference source paths whose
    package root does not exist in *repo_dir* — i.e. the follow-up
    describes work in a different repo that the current workspace
    cannot verify.

    Mill-internal paths (matching :data:`MILL_PATH_PREFIXES`) are
    excluded — those are routed to the mill board separately by
    :func:`looks_like_mill_internal`.

    When *repo_dir* is ``None``, returns ``False`` (no filesystem
    available to check).  Pure / read-only, must never raise.
    """
    if repo_dir is None:
        return False
    haystack = f"{title or ''}\n{body or ''}"
    try:
        candidates = paths_excluding_out_of_scope(haystack)
    except Exception:
        return False
    for token in candidates:
        try:
            token_lower = token.lower()
        except Exception:
            log.debug(
                "has_unverifiable_cross_repo_refs: token.lower() failed for %r", token
            )
            continue
        if any(token_lower.startswith(prefix.lower()) for prefix in MILL_PATH_PREFIXES):
            continue
        pkg = _package_root(token)
        if pkg is None:
            continue
        try:
            if not (repo_dir / pkg).exists():
                return True
        except Exception:
            log.debug(
                "has_unverifiable_cross_repo_refs: exists check failed for %r", pkg
            )
            continue
    return False


def referenced_local_deliverable_paths(
    title: str | None,
    body: str | None,
    repo_dir: pathlib.Path | None,
) -> list[str]:
    """In-scope path tokens that are NOT mill-prefixed AND whose package
    root directory exists under *repo_dir* (existing or to-be-created
    files in a package that lives on the current checkout).

    Extracts candidates via :func:`paths_excluding_out_of_scope` (so
    out-of-scope and excluded-region paths never count), excludes any
    token matching a :data:`MILL_PATH_PREFIXES` prefix (case-insensitive),
    and keeps only tokens whose **package root** directory exists on
    disk.  The package root is computed as:

    - ``src/<segment-1>/`` when the token starts with ``src/`` and has
      at least two segments (e.g. ``src/robotsix_llmio/core/foo.py`` →
      ``src/robotsix_llmio``).
    - Otherwise the first path segment (before the first ``/``).

    A stray ``src/foo.py``-style token (only one segment under ``src/``)
    is never treated as a local deliverable — it requires ≥2 segments.

    *repo_dir* is ``None`` → returns ``[]`` immediately.
    Pure / read-only, must never raise.
    """
    if repo_dir is None:
        return []
    haystack = f"{title or ''}\n{body or ''}"
    try:
        candidates = paths_excluding_out_of_scope(haystack)
    except Exception:
        return []
    result: list[str] = []
    for token in candidates:
        try:
            token_lower = token.lower()
        except Exception:
            log.debug(
                "referenced_local_deliverable_paths: token.lower() failed for %r",
                token,
            )
            continue
        # Exclude mill-prefixed consumer paths.
        if any(token_lower.startswith(prefix.lower()) for prefix in MILL_PATH_PREFIXES):
            continue
        # Compute package root.
        parts = token.split("/")
        if parts[0] == "src" and len(parts) >= 2:
            package_root = f"src/{parts[1]}"
        else:
            package_root = parts[0]
        try:
            if (repo_dir / package_root).is_dir():
                if token not in result:
                    result.append(token)
        except Exception:
            log.debug(
                "referenced_local_deliverable_paths: is_dir check failed for %r",
                package_root,
                exc_info=True,
            )
            continue
    return result
