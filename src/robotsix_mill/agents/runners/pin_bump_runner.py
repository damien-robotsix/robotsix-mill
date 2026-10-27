"""Pin-bump scheduled runner — detection + PR actuator.

A ``schedule_only`` periodic runner that scans every registered repo's
``pyproject.toml`` for internal git-source pins, computes the coherent
dependency graph (topological order + current SHAs), and opens PRs to
bump stale pins.  No LLM call — pure graph computation.
"""

from __future__ import annotations

import contextlib
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from robotsix_mill.config.repos import (
    RepoConfig,
    ReposRegistry,
    get_repos_config,
    target_branch_for,
)
from robotsix_mill.config.settings import Settings
from robotsix_mill.deps.coherent_resolver import run_coherence_check
from robotsix_mill.deps.internal_graph import (
    CyclicDependencyError,
    GitPin,
    InternalDepGraph,
    build_internal_dep_graph,
)
from robotsix_mill.scm.forge import Forge, get_forge, rate_limit
from robotsix_mill.scm.forge.auth import (
    github_push_token,
    github_token,
    push_with_token_retry,
)
from robotsix_mill.scm.vcs import git_ops

log = logging.getLogger(__name__)

# Branch-name segment (after ``settings.branch_prefix``) that marks a
# pin-bump sweep PR.  These branches are created ticket-less by design;
# consumers (e.g. the orphaned-PR check) use this to recognize them.
PIN_BUMP_BRANCH_SEGMENT = "pin-bump/"

# Monotonic timestamp of the last cross-repo pass that did the work. The
# periodic supervisor runs one ``pin_bump`` loop PER registered repo, and every
# loop calls :func:`run_pin_bump_pass` with its own ``repo_config`` — but the
# pass itself is fleet-wide (full registry, every pyproject, every pin). Before
# this guard each of the ~20 per-repo firings re-ran the whole pass: 20 full
# passes a day instead of one, bunching into bursts of up to eight concurrent
# passes (2026-10-05 15:03Z) whose ``pulls?head=`` lookups and clones pushed
# the GitHub App installation over its hourly request quota every hour
# (``403 Forbidden`` at minutes :06–:13, merge-stage PR polls failing).
_last_pass_started_monotonic: float | None = None

# Fraction of ``pin_bump_interval_seconds`` that must elapse before another
# per-repo firing is allowed to run the cross-repo pass. Below 1.0 so the
# per-repo loops (which drift against each other) still produce one pass per
# interval instead of occasionally skipping a whole interval.
_PASS_GUARD_FRACTION = 0.9

# Latest ls-remote SHAs (``dep git_url → HEAD sha``) observed by the previous
# actuator pass.  When a new pass computes the identical mapping, nothing has
# moved upstream and there is no new bump to open, so the pass short-circuits
# instead of re-cloning every consumer and re-logging the same skips every
# interval (the "burns a pass every 5 min logging the same skips" symptom).
_last_latest_map: dict[str, str | None] | None = None

# A pin-bump PR title ends with ``pin to <rev>`` where ``<rev>`` is the
# 7–40 char hex prefix of the target dependency SHA.  Parsed to tell an
# already-current open PR (target == latest) from a stale one (target moved).
_PR_TARGET_REV_RE = re.compile(r"pin to ([0-9a-f]{7,40})\b")


def _pass_recently_ran(interval_seconds: float, now: float) -> bool:
    """Return True when a cross-repo pass started within the guard window."""
    last = _last_pass_started_monotonic
    return last is not None and (now - last) < interval_seconds * _PASS_GUARD_FRACTION


def _claim_pass_slot(interval_seconds: float, repo_id: str) -> bool:
    """Record this firing as THE cross-repo pass of the current interval.

    Returns False (after logging) when the periodic is disabled
    (``interval_seconds <= 0``) or another firing already ran the pass
    inside the guard window, so the caller should return without work.
    """
    global _last_pass_started_monotonic
    if interval_seconds <= 0:
        log.info("pin_bump: periodic pin bump is disabled (interval=0) — nothing to do")
        return False
    now = time.monotonic()
    if _pass_recently_ran(interval_seconds, now):
        log.info(
            "pin_bump: cross-repo pass already ran %.0fs ago (interval %ss) — "
            "skipping the firing for repo %s",
            now - (_last_pass_started_monotonic or now),
            interval_seconds,
            repo_id,
        )
        return False
    _last_pass_started_monotonic = now
    return True


def _fetch_one_pyproject(
    repo_id: str,
    rc: RepoConfig,
    settings: Settings,
) -> str | None:
    """Clone *rc* and return its ``pyproject.toml`` text, or ``None``.

    Returns ``None`` for any expected failure (no remote URL, no
    token, clone failure, missing file, read error) — each case is
    logged at warning level so the scheduler loop can see why a repo
    was skipped without crashing the pass.
    """
    repo_url = rc.forge_remote_url
    if not repo_url:
        log.warning("pin_bump: no forge_remote_url for %s — skipping", repo_id)
        return None

    try:
        token = github_token(settings, repo_config=rc)
    except RuntimeError as exc:
        log.warning("pin_bump: no forge token for %s — skipping (%s)", repo_id, exc)
        return None

    branch = target_branch_for(settings, rc)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        try:
            git_ops.clone(repo_url, tmp_path, branch, token)
        except Exception as exc:
            log.warning(
                "pin_bump: clone failed for %s (branch=%s) — skipping (%s)",
                repo_id,
                branch,
                exc,
            )
            return None

        toml_path = tmp_path / "pyproject.toml"
        if not toml_path.is_file():
            log.warning("pin_bump: no pyproject.toml in %s — skipping", repo_id)
            return None

        try:
            return toml_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                "pin_bump: cannot read pyproject.toml for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return None


def run_pin_bump_pass(  # noqa: C901 — rate-limit guard + detection/actuator pipeline, each step with its own handling
    *,
    session_id: str,
    repo_config: RepoConfig | None = None,
) -> None:
    """Execute one pin-bump detection pass across all registered repos.

    Returns immediately when *repo_config* is ``None`` (the periodic
    supervisor fires per-repo, but the pin_bump pass is cross-repo —
    it needs the full registry).  Because every per-repo loop fires this
    same fleet-wide pass, only the first firing per
    ``pin_bump_interval_seconds`` does the work; the others log and return
    (see ``_last_pass_started_monotonic``).

    Detection, then the PR actuator — computes and logs the topological
    order and current pin SHAs, then bumps stale pins.
    """
    if repo_config is None:
        return

    settings = Settings()
    if not _claim_pass_slot(settings.pin_bump_interval_seconds, repo_config.repo_id):
        return

    skip = rate_limit.should_skip(settings)
    if skip is not None:
        log.info(
            "pin_bump: skipping pass — GitHub rate-limit reserve reached "
            "(%d/%d remaining), reset %s",
            skip.remaining,
            skip.limit,
            skip.reset_hhmm(),
        )
        return

    registry = get_repos_config()

    # Gather pyproject.toml text from every reachable registered repo.
    pyproject_map: dict[str, str] = {}
    for repo_id, rc in registry.repos.items():
        content = _fetch_one_pyproject(repo_id, rc, settings)
        if content is not None:
            pyproject_map[repo_id] = content

    if not pyproject_map:
        log.info("pin_bump: no reachable repos with pyproject.toml — nothing to do")
        return

    # Build the dependency graph and report.
    try:
        graph = build_internal_dep_graph(pyproject_map, registry)
    except CyclicDependencyError as exc:
        log.warning(
            "pin_bump: cyclic dependency detected — cannot compute "
            "topological order (%s)",
            exc,
        )
        return

    log.info("pin_bump: topological order: %s", graph.topo_order)
    for repo_id in graph.topo_order:
        pins = graph.pins.get(repo_id, {})
        if pins:
            for dep_id, pin in sorted(pins.items()):
                log.info(
                    "pin_bump: pin  %s → %s  @ %s",
                    repo_id,
                    dep_id,
                    pin.rev,
                )
        else:
            log.info("pin_bump: pin  %s  (no internal deps)", repo_id)

    # After detection, run the PR actuator to bump stale pins.
    run_pin_bump_pr_actuator(
        session_id=session_id,
        repo_config=repo_config,
        graph=graph,
    )


def _update_pin_rev(pyproject_text: str, dep_name: str, new_rev: str) -> str:
    """Update the ``rev`` value for *dep_name* in ``[tool.uv.sources]``.

    Returns *pyproject_text* with the ``rev`` string replaced in-place.
    Raises ``ValueError`` when the source entry or its ``rev`` key
    cannot be found.
    """
    pattern = re.compile(
        rf'({re.escape(dep_name)}\s*=\s*\{{[^}}]*?rev\s*=\s*")[^"]*(")',
    )
    if not pattern.search(pyproject_text):
        raise ValueError(f"Could not find rev for {dep_name} in [tool.uv.sources]")
    # Substitute via a function, NOT a template string. A template interpolating
    # *new_rev* after ``\1`` re-parses the digits of the SHA as part of the
    # backreference: ``\1`` + ``9a3f…`` becomes ``\19`` (group 19 — "invalid
    # group reference"), and ``\1`` + ``21f2…`` becomes the octal escape
    # ``\121`` ("Q"), silently swallowing the group-1 prefix and two SHA
    # characters. 160 of the 256 possible two-hex-char SHA prefixes are
    # affected — 62.5% of real bumps. A function replacement is not parsed at
    # all, so no SHA can be mistaken for a reference.
    return pattern.sub(lambda m: f"{m.group(1)}{new_rev}{m.group(2)}", pyproject_text)


def _compute_actuator_graph(
    settings: Settings, registry: ReposRegistry
) -> InternalDepGraph | None:
    """Fetch pyproject.toml from every registered repo and build the graph.

    Returns ``None`` when no repos are reachable or a cycle is detected
    (logged and swallowed).
    """
    pyproject_map: dict[str, str] = {}
    for repo_id, rc in registry.repos.items():
        content = _fetch_one_pyproject(repo_id, rc, settings)
        if content is not None:
            pyproject_map[repo_id] = content

    if not pyproject_map:
        log.info("pin_bump: no reachable repos with pyproject.toml — nothing to do")
        return None

    try:
        return build_internal_dep_graph(pyproject_map, registry)
    except CyclicDependencyError as exc:
        log.warning(
            "pin_bump: cyclic dependency detected — cannot compute "
            "topological order (%s)",
            exc,
        )
        return None


def run_pin_bump_pr_actuator(
    *,
    session_id: str,
    repo_config: RepoConfig | None = None,
    graph: InternalDepGraph | None = None,
) -> None:
    """Create PRs to bump stale internal-dependency pins.

    For every :class:`GitPin` in *graph* whose ``rev`` does not match
    the latest commit SHA on the dependency's default branch, clones
    the consuming repo, edits its ``pyproject.toml``, regenerates
    ``uv.lock``, and opens a PR via the forge API.

    When *graph* is ``None`` the dependency graph is computed from
    scratch (re-fetches every ``pyproject.toml``).  Callers that
    already have the graph (e.g. :func:`run_pin_bump_pass`) should
    pass it to avoid duplicate I/O.

    Logs a summary of created PRs and skipped (already-latest) pins.
    Never raises — all expected failures are caught and logged.
    """
    if repo_config is None:
        return

    settings = Settings()
    if settings.pin_bump_interval_seconds <= 0:
        log.info("pin_bump: periodic pin bump is disabled (interval=0) — nothing to do")
        return

    registry = get_repos_config()

    if graph is None:
        graph = _compute_actuator_graph(settings, registry)
        if graph is None:
            return

    try:
        ls_token = github_token(settings, repo_config=repo_config)
    except RuntimeError as exc:
        log.warning("pin_bump: no forge token for ls-remote — skipping (%s)", exc)
        return

    # Resolve every dependency's latest HEAD SHA once (deduped across the
    # consumers that share a pin). When the mapping is identical to the
    # previous pass, nothing moved upstream — skip the whole actuator rather
    # than re-clone every repo and re-log the same skips every interval.
    global _last_latest_map
    latest_map = _resolve_latest_shas(graph, ls_token)
    if _last_latest_map is not None and latest_map == _last_latest_map:
        log.info("pin_bump: upstream SHAs unchanged since last pass — nothing to bump")
        return
    _last_latest_map = dict(latest_map)

    prs_created, superseded, skipped = _bump_pins_for_repos(
        settings=settings,
        registry=registry,
        graph=graph,
        latest_map=latest_map,
    )
    _log_pass_summary(prs_created, superseded, skipped)


def _log_pass_summary(
    prs_created: list[str], superseded: list[str], skipped: list[str]
) -> None:
    """Emit the per-pass detail lines plus one terse summary line."""
    if prs_created:
        log.info("pin_bump: PRs created: %s", prs_created)
    if superseded:
        log.info("pin_bump: superseded stale PRs: %s", superseded)
    if skipped:
        log.info("pin_bump: skipped (already at latest): %s", skipped)
    if not prs_created and not skipped and not superseded:
        log.info("pin_bump: no internal pins to bump")
    log.info(
        "pin_bump: pass summary — created=%d superseded=%d already-latest=%d",
        len(prs_created),
        len(superseded),
        len(skipped),
    )


def _resolve_latest_shas(
    graph: InternalDepGraph, ls_token: str
) -> dict[str, str | None]:
    """Resolve the latest HEAD SHA for every distinct pinned dependency.

    Deduped by ``git_url`` so a dependency pinned by several consumers is
    looked up once.  A value of ``None`` means the ls-remote lookup failed.
    """
    latest: dict[str, str | None] = {}
    for repo_id in graph.topo_order:
        for pin in graph.pins.get(repo_id, {}).values():
            if pin.git_url not in latest:
                latest[pin.git_url] = git_ops.ls_remote_sha(
                    pin.git_url, ref="HEAD", token=ls_token
                )
    return latest


def _consumer_counts(graph: InternalDepGraph) -> dict[str, int]:
    """Count how many repos pin each dependency (its waiting-consumer count)."""
    counts: dict[str, int] = {}
    for repo_pins in graph.pins.values():
        for dep_name in repo_pins:
            counts[dep_name] = counts.get(dep_name, 0) + 1
    return counts


def _bump_pins_for_repos(
    *,
    settings: Settings,
    registry: ReposRegistry,
    graph: InternalDepGraph,
    latest_map: dict[str, str | None],
) -> tuple[list[str], list[str], list[str]]:
    """Iterate *graph* topo-order and bump every stale pin.

    Returns ``(prs_created, superseded, skipped)`` summary lists.
    """
    prs_created: list[str] = []
    skipped: list[str] = []
    superseded: list[str] = []

    # When the in-flight cap binds, spend the free slots on the dependencies
    # with the most waiting consumers first (e.g. a shared library blocking
    # several repos outranks a single-consumer bump).
    consumer_count = _consumer_counts(graph)

    for repo_id in graph.topo_order:
        pins = graph.pins.get(repo_id, {})
        if not pins:
            continue

        rc = registry.repos.get(repo_id)
        if rc is None:
            continue

        try:
            push_token = github_push_token(settings, repo_config=rc)
        except RuntimeError as exc:
            log.warning("pin_bump: no forge token for %s — skipping (%s)", repo_id, exc)
            continue

        target_branch = target_branch_for(settings, rc)
        forge = get_forge(settings, repo_config=rc)

        ordered = sorted(
            pins.items(),
            key=lambda kv: (-consumer_count.get(kv[0], 0), kv[0]),
        )
        for dep_name, pin in ordered:
            _bump_one_pin(
                settings=settings,
                repo_id=repo_id,
                rc=rc,
                dep_name=dep_name,
                pin=pin,
                latest_sha=latest_map.get(pin.git_url),
                push_token=push_token,
                target_branch=target_branch,
                forge=forge,
                prs_created=prs_created,
                skipped=skipped,
                superseded=superseded,
            )

    return prs_created, superseded, skipped


def _bump_one_pin(
    *,
    settings: Settings,
    repo_id: str,
    rc: RepoConfig,
    dep_name: str,
    pin: GitPin,
    latest_sha: str | None,
    push_token: str,
    target_branch: str,
    forge: Forge,
    prs_created: list[str],
    skipped: list[str],
    superseded: list[str],
) -> None:
    """Apply the bump for *pin* when its target is stale (latest resolved upfront)."""
    if latest_sha is None:
        log.warning(
            "pin_bump: cannot resolve latest SHA for %s → %s (%s)",
            repo_id,
            dep_name,
            pin.git_url,
        )
        return

    if latest_sha == pin.rev:
        skipped.append(f"{repo_id} → {dep_name} (already at {pin.rev[:7]})")
        return

    log.info(
        "pin_bump: bump  %s → %s  %s → %s",
        repo_id,
        dep_name,
        pin.rev[:7],
        latest_sha[:7],
    )

    _apply_one_bump(
        settings=settings,
        repo_id=repo_id,
        rc=rc,
        dep_name=dep_name,
        old_rev=pin.rev,
        new_rev=latest_sha,
        push_token=push_token,
        target_branch=target_branch,
        forge=forge,
        prs_created=prs_created,
        superseded=superseded,
    )


def _parse_pr_target_rev(title: str | None) -> str | None:
    """Return the target-SHA prefix embedded in a pin-bump PR *title*, or ``None``."""
    m = _PR_TARGET_REV_RE.search(title or "")
    return m.group(1) if m else None


def _open_pr_title(forge: Forge, branch_name: str) -> str | None:
    """Best-effort title of the open PR whose head is *branch_name*.

    Returns ``None`` when the forge cannot enumerate open PRs (the title is
    then simply unknown — callers treat an unparseable target as "unknown").
    """
    try:
        for pr in forge.list_open_prs() or []:
            if pr.get("branch") == branch_name:
                return pr.get("title") or ""
    except Exception:
        return None
    return None


def _existing_pr_is_current(
    *, existing: dict[str, Any], title: str | None, latest_sha: str
) -> bool:
    """Return ``True`` when the open PR already delivers the latest bump.

    A PR is *current* when it targets ``latest_sha`` AND is not conflicting.
    A PR targeting an older SHA, or one that conflicts with the consumer's
    default branch (dirty ``uv.lock``), is stale and must be superseded.
    """
    target = _parse_pr_target_rev(title)
    if target is not None and not latest_sha.startswith(target):
        return False  # targets an older/different rev than the latest
    # Otherwise current iff not conflicting (a dirty uv.lock needs a fresh PR).
    return existing.get("mergeable") is not False


def _supersede_pr(
    *,
    repo_id: str,
    dep_name: str,
    branch_name: str,
    existing: dict[str, Any],
    new_rev: str,
    forge: Forge,
    superseded: list[str],
) -> None:
    """Close + delete a stale own pin-bump PR so its cap slot frees immediately."""
    url = existing.get("url", "no url")
    note = (
        f"Superseded by a fresh pin-bump of `{dep_name}` to `{new_rev[:7]}`: "
        "closing this stale PR and re-opening from the current default branch "
        "with a regenerated `uv.lock`."
    )
    # Best-effort remote mutations — never block the supersede on any of them.
    with contextlib.suppress(Exception):
        forge.post_pr_comment(source_branch=branch_name, body=note)
    with contextlib.suppress(Exception):
        forge.close_pr(source_branch=branch_name)
    with contextlib.suppress(Exception):
        forge.delete_branch(branch=branch_name)
    log.info(
        "pin_bump: superseded stale PR for %s → %s (%s)",
        repo_id,
        dep_name,
        url,
    )
    superseded.append(f"{repo_id} → {dep_name} ({url})")


def _inflight_current_count(forge: Forge) -> int:
    """Count open pin-bump PRs that still count toward the in-flight cap.

    Only *current* PRs (open and not conflicting) are counted — a stale,
    conflicting PR no longer holds a slot, so it can never saturate the cap
    and block a newer bump.
    """
    count = 0
    for branch in forge.list_open_pr_branches():
        if not branch.startswith(f"mill/{PIN_BUMP_BRANCH_SEGMENT}"):
            continue
        try:
            st = forge.pr_status(source_branch=branch)
        except Exception:
            # Lookup failed — count it conservatively rather than risk
            # over-opening past the cap.
            count += 1
            continue
        if st and st.get("state") == "open" and st.get("mergeable") is not False:
            count += 1
    return count


def _reconcile_and_check_cap(
    *,
    repo_id: str,
    rc: RepoConfig,
    dep_name: str,
    branch_name: str,
    new_rev: str,
    forge: Forge,
    superseded: list[str],
) -> bool:
    """Reconcile this dep's own PR and check the in-flight cap.

    Returns ``True`` when it is safe to open a fresh PR, ``False`` to skip
    (a current PR already exists, the lookup failed, or the cap binds).  A
    *stale* own PR is superseded here so its slot frees before the cap check.
    """
    try:
        existing = forge.pr_status(source_branch=branch_name)
    except Exception as exc:
        # A persistent GitHub 5xx (e.g. a PR stuck in a bad state) on the
        # lookup must not crash the whole pin-bump pass — the transport
        # already retries transient network errors and 401s, so reaching
        # here means a durable failure. Skip this pin (conservatively, so
        # we never open a duplicate PR) and let the pass continue.
        log.warning(
            "pin_bump: PR lookup failed for %s → %s (branch %s) — "
            "skipping to avoid a duplicate PR (%s)",
            repo_id,
            dep_name,
            branch_name,
            exc,
        )
        return False

    if existing is not None and existing.get("state") == "open":
        title = _open_pr_title(forge, branch_name)
        if _existing_pr_is_current(existing=existing, title=title, latest_sha=new_rev):
            log.info(
                "pin_bump: PR for %s → %s already open (%s) — skipping",
                repo_id,
                dep_name,
                existing.get("url", "no url"),
            )
            return False
        _supersede_pr(
            repo_id=repo_id,
            dep_name=dep_name,
            branch_name=branch_name,
            existing=existing,
            new_rev=new_rev,
            forge=forge,
            superseded=superseded,
        )

    if rc.max_inflight_prs > 0:
        inflight = _inflight_current_count(forge)
        if inflight >= rc.max_inflight_prs:
            log.info(
                "pin_bump: %s at pin-bump in-flight cap (%d/%d) — skipping %s",
                repo_id,
                inflight,
                rc.max_inflight_prs,
                dep_name,
            )
            return False

    return True


def _apply_one_bump(
    *,
    settings: Settings,
    repo_id: str,
    rc: RepoConfig,
    dep_name: str,
    old_rev: str,
    new_rev: str,
    push_token: str,
    target_branch: str,
    forge: Forge,
    prs_created: list[str],
    superseded: list[str],
) -> None:
    """Clone *rc*, update the pin rev, regenerate uv.lock, push, and open a PR.

    Before touching the clone, reconcile this dep's own open PR: a *current*
    one (targets ``new_rev``, mergeable) means there is nothing to do; a
    *stale* one (targets an older rev, or conflicts) is superseded so a fresh
    bump can replace it rather than letting the dead PR hold a cap slot.
    """
    branch_name = f"mill/{PIN_BUMP_BRANCH_SEGMENT}{dep_name}"

    if not _reconcile_and_check_cap(
        repo_id=repo_id,
        rc=rc,
        dep_name=dep_name,
        branch_name=branch_name,
        new_rev=new_rev,
        forge=forge,
        superseded=superseded,
    ):
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        try:
            git_ops.clone(
                rc.forge_remote_url or "",
                tmp_path,
                target_branch,
                push_token,
            )
        except Exception as exc:
            log.warning(
                "pin_bump: clone failed for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return

        toml_path = tmp_path / "pyproject.toml"
        if not toml_path.is_file():
            log.warning(
                "pin_bump: no pyproject.toml in %s clone — skipping",
                repo_id,
            )
            return

        original_text = toml_path.read_text(encoding="utf-8")
        try:
            updated_text = _update_pin_rev(original_text, dep_name, new_rev)
        except ValueError as exc:
            log.warning(
                "pin_bump: cannot update rev for %s → %s — skipping (%s)",
                repo_id,
                dep_name,
                exc,
            )
            return

        toml_path.write_text(updated_text, encoding="utf-8")

        # Run coherence check via uv lock; skip PR on conflicts.
        conflicts = run_coherence_check(tmp_path)
        if conflicts:
            log.warning(
                "pin_bump: coherence check failed for %s → %s "
                "(%d conflict(s)) — skipping PR: %s",
                repo_id,
                dep_name,
                len(conflicts),
                conflicts,
            )
            return

        # Create branch, commit, push.
        try:
            git_ops.create_branch(tmp_path, branch_name)
            git_ops.commit_all(
                tmp_path,
                f"chore(deps): bump {dep_name} pin to {new_rev[:7]}",
            )
        except Exception as exc:
            log.warning(
                "pin_bump: commit failed for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return

        try:
            push_with_token_retry(
                lambda t: git_ops.push(
                    tmp_path, branch_name, rc.forge_remote_url or "", t
                ),
                push_token,
                settings,
                rc,
                label=repo_id,
            )
        except Exception as exc:
            log.warning(
                "pin_bump: push failed for %s — skipping (%s)",
                repo_id,
                exc,
            )
            return

        try:
            pr_url = forge.open_merge_request(
                source_branch=branch_name,
                title=f"chore(deps): bump {dep_name} pin to {new_rev[:7]}",
                body=(
                    f"Bump `{dep_name}` pin from `{old_rev[:7]}` "
                    f"to `{new_rev[:7]}`.\n\n"
                    f"*Detected by the pin-bump periodic pass.*"
                ),
            )
        except Exception as exc:
            log.warning(
                "pin_bump: PR creation failed for %s → %s — "
                "branch %s was pushed but no PR opened (%s)",
                repo_id,
                dep_name,
                branch_name,
                exc,
            )
            return

        prs_created.append(
            f"{repo_id} → {dep_name} ({old_rev[:7]} → {new_rev[:7]}) {pr_url}"
        )
