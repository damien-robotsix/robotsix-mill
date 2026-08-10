"""Git diff and file-analysis helpers.

Functions that inspect, compare, and enumerate files across branches
and refs — the "read" side of version control, as distinct from the
mutation primitives (clone, commit, push) in git_ops.py.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .git_ops import NETWORK_GIT_TIMEOUT, _authed_url, _git

log = logging.getLogger("robotsix_mill.vcs.git_diff")


def branch_has_net_diff(
    repo: Path, target_branch: str = "main", ref: str = "HEAD"
) -> bool:
    """Return True when *ref* has a non-empty content diff vs ``origin/main``.

    Uses the three-dot ``git diff --quiet origin/main...<ref>`` semantic
    (compare *ref* against the merge-base), which is exactly what the forge
    evaluates when opening a PR. This is distinct from
    :func:`branch_is_ahead_of_main`, which counts *commits*: a branch can carry
    a commit that is not on main by SHA (ahead by commit count) yet whose net
    content is identical to main — e.g. main independently landed the same
    change, or the commit was a no-op. The forge rejects such a PR with a 422
    "No commits between main and branch", so deliver must check the net diff,
    not just the commit count, before opening one.

    *ref* defaults to ``HEAD`` (the checked-out branch). Pass an explicit
    branch name when the caller cannot rely on HEAD being the feature branch
    (e.g. the merge stage's closed-PR no-op check).

    Fetches ``origin main`` first so the local ref is current. A fetch or diff
    failure returns True (assume there IS a diff) so delivery proceeds — we
    would rather hit the forge API than silently DONE a real change.
    """
    try:
        _git(repo, "fetch", "origin", target_branch, timeout=NETWORK_GIT_TIMEOUT)
    except subprocess.CalledProcessError:
        return True

    # `git diff --quiet` exits 0 when there is NO diff, 1 when there is one.
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", f"origin/{target_branch}...{ref}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    # Any other exit code is an error (bad ref, etc.) — assume a diff so we
    # don't wrongly route a real change to DONE.
    return True


def changed_source_files(
    repo: Path, target_branch: str = "main", ref: str = "HEAD"
) -> list[str]:
    """Return the list of added/modified source files between merge-base and *ref*.

    Uses ``git diff --name-only --diff-filter=AM <merge-base>...<ref>``.
    Returns an empty list on any error (fail-safe — we would rather skip
    the integrity check than block on a git plumbing failure).
    """
    try:
        merge_base = _git(repo, "merge-base", f"origin/{target_branch}", ref)
        out = _git(
            repo,
            "diff",
            "--name-only",
            "--diff-filter=AM",
            f"{merge_base}...{ref}",
        )
    except subprocess.CalledProcessError:
        return []
    return [line for line in out.split("\n") if line] if out else []


def tracked_paths_at(repo: Path, ref: str) -> set[str]:
    """Every path tracked at *ref*, as repo-relative strings.

    One ``ls-tree`` for the whole tree rather than a probe per path: callers
    ask about hundreds or thousands of files at once, and a subprocess each
    would cost more than the question is worth.

    Returns an empty set on any git failure. Callers must treat "empty" as
    "unknown", never as "nothing is tracked" — the latter would invert every
    membership test built on it.
    """
    try:
        out = _git(repo, "ls-tree", "-r", "--name-only", ref)
    except subprocess.CalledProcessError:
        return set()
    return {line for line in out.split("\n") if line} if out else set()


def file_blobs(repo: Path, paths: list[str], ref: str = "HEAD") -> dict[str, str]:
    """Map each of *paths* to its blob object id at *ref*.

    Paths missing at *ref* are omitted. Blob ids identify content
    exactly, so comparing them answers "is this the same file content?"
    without reading or transferring the content itself.
    """
    blobs: dict[str, str] = {}
    for path in paths:
        try:
            sha = _git(repo, "rev-parse", f"{ref}:{path}")
        except subprocess.CalledProcessError:
            continue
        if sha:
            blobs[path] = sha
    return blobs


#: Fallback for :func:`check_rebase_diff_integrity` when no caller supplies a
#: list. Production passes ``settings.rebase_drop_exempt_paths``; this keeps
#: direct callers and tests working without threading settings through.
DEFAULT_REBASE_DROP_EXEMPT_PATHS = (
    "CHANGELOG.md",
    "changelog.d/",
    "changelog/",
    "docs/modules.yaml",
    "site/modules.yaml",
    # detect-secrets' baseline: regenerated from a scan of the whole repo,
    # so like the registries above its content is a function of the tree
    # rather than of one branch. A rebase can legitimately land a version
    # matching neither side, which the blob-equality excuse cannot clear.
    ".secrets.baseline",
)


def check_rebase_diff_integrity(
    repo: Path,
    target_branch: str,
    pre_rebase_files: list[str],
    pre_rebase_blobs: dict[str, str] | None = None,
    exempt_paths: Sequence[str] | None = None,
    target_pre_blobs: dict[str, str] | None = None,
) -> tuple[bool, list[str], list[str]]:
    """Check that every source file from the pre-rebase diff survived the rebase.

    Returns ``(ok, dropped_files, sibling_likely)``. Paths in *exempt_paths*
    (exact match or directory prefix; defaults to
    :data:`DEFAULT_REBASE_DROP_EXEMPT_PATHS`) are ignored — registry and
    boilerplate files such as changelog fragments and ``docs/modules.yaml``
    are expected to change or be removed during rebase cycles, are re-derived
    by CI, and are not implement-stage content whose loss signals a silent
    drop. Their content is a function of the whole repo rather than of one
    branch, so a rebase can legitimately land a version matching neither side
    — which the blob-equality excuse below cannot clear.

    A file can also leave the post-rebase diff for an entirely healthy
    reason: another PR landed the *same* change first, so the rebase
    correctly collapses this branch's now-redundant delta to nothing.
    Reporting that as a silent drop dead-ends a working ticket — live,
    ten tickets blocked on the identical ``Dockerfile`` right after the
    canonical fix for it merged from a sibling PR.

    Telling the two apart needs the branch's *pre-rebase* content, since
    both cases leave HEAD agreeing with the target afterwards. Pass
    *pre_rebase_blobs* (from :func:`file_blobs` before the rebase): when
    the target now carries exactly the blob the branch had, the work is
    upstream and the file is excused. Without it the check keeps its
    original conservative behaviour and reports every missing file.

    A sibling PR that changed the same file on the target *during* the
    rebase window produces a third case: the file disappears from the
    post-rebase diff, but it was not dropped — it was superseded. Pass
    *target_pre_blobs* (the target branch's content for each file
    *before* the agent ran) to distinguish that scenario from a genuine
    drop. Files whose target content changed during the rebase window
    are reported in *sibling_likely* rather than *dropped*.
    """
    post_files = changed_source_files(repo, target_branch)
    if not pre_rebase_files:
        return (True, [], [])

    excluded_prefixes = (
        tuple(exempt_paths)
        if exempt_paths is not None
        else DEFAULT_REBASE_DROP_EXEMPT_PATHS
    )
    pre_set = {
        f
        for f in pre_rebase_files
        if not any(f == p or f.startswith(p) for p in excluded_prefixes)
    }
    post_set = set(post_files)
    candidates = sorted(pre_set - post_set)
    if not pre_rebase_blobs:
        return (len(candidates) == 0, candidates, [])

    target_blobs = file_blobs(repo, candidates, ref=f"origin/{target_branch}")
    dropped: list[str] = []
    sibling_likely: list[str] = []
    for f in candidates:
        # Excused only when the target's content for this path is byte-for-byte
        # what the branch was trying to deliver. A discarded change leaves the
        # target on its ORIGINAL content, which never matches.
        if f in pre_rebase_blobs and target_blobs.get(f) == pre_rebase_blobs[f]:
            continue

        # Sibling-modified: the target's content for this file changed during
        # the rebase window (a sibling PR landed between our pre-rebase
        # snapshot and now). The agent correctly took the sibling's version;
        # our specific delta is superseded — but we do NOT auto-excuse because
        # semantic equivalence can only be judged by a human.
        if (
            target_pre_blobs
            and f in target_pre_blobs
            and target_pre_blobs[f] != target_blobs.get(f)
        ):
            sibling_likely.append(f)
            continue

        dropped.append(f)

    return (len(dropped) == 0 and len(sibling_likely) == 0, dropped, sibling_likely)


def branch_is_behind_main(repo: Path, target_branch: str = "main") -> bool:
    """Return True when ``origin/main`` has commits not on HEAD.

    Counts commits on ``origin/main`` that are NOT on HEAD (the
    ``rev-list HEAD..origin/main`` semantic) — i.e. HEAD was cut from an older
    main and main has advanced since. The merge stage uses this to rebase a
    stale PR branch onto current main BEFORE handing a CI failure to ci_fix: a
    repo-wide gate (ruff/mypy) often fails on code that isn't the ticket's
    because main gained a fix the branch lacks; a rebase fixes it, ci_fix can't.

    Fetches ``origin main`` first so the local ref is current. A fetch or
    rev-list failure returns False — don't trigger a pointless rebase on a
    transient git error; the genuine-failure path (ci_fix) runs instead.
    """
    try:
        _git(repo, "fetch", "origin", target_branch, timeout=NETWORK_GIT_TIMEOUT)
    except subprocess.CalledProcessError:
        return False
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-list",
            "--count",
            f"HEAD..origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def changed_files(repo: Path, target_branch: str) -> list[str]:
    """Return every file that would land in the next commit vs
    ``origin/<target_branch>`` — including untracked new files.

    Union of:
      - ``git diff --name-only origin/<target>`` — tracked-file
        modifications (staged + unstaged).
      - ``git ls-files --others --exclude-standard`` — untracked
        files honouring ``.gitignore``.

    Untracked files matter for scope enforcement: the agent often
    writes new files into the working tree without staging them
    (or runs pytest itself, leaving ``__pycache__/*.pyc`` on disk).
    The next ``commit_all`` runs ``git add -A`` and sweeps them in,
    so scope check must see them BEFORE the commit or it lets the
    out-of-scope additions through.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    try:
        diff_out = _git(repo, "diff", "--name-only", f"origin/{target_branch}")
    except subprocess.CalledProcessError:
        log.warning(
            "changed_files: origin/%s ref not resolvable in %s — "
            "treating as no tracked diff available",
            target_branch,
            repo,
        )
        diff_out = ""
    if diff_out:
        for f in diff_out.split("\n"):
            if f and f not in seen_set:
                seen_set.add(f)
                seen.append(f)
    untracked_out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if untracked_out:
        for f in untracked_out.split("\n"):
            if f and f not in seen_set:
                seen_set.add(f)
                seen.append(f)
    return seen


def introduced_files(repo: Path, target_branch: str) -> list[str]:
    """Return every file the BRANCH introduces relative to its merge base
    with ``origin/<target_branch>`` — i.e. what the ticket itself changed,
    NOT files that ``origin/<target>`` modified after the branch was cut.

    Union of (order-preserving, deduplicated):
      - ``git diff --name-only origin/<target>...HEAD`` (THREE-dot) —
        committed branch changes vs the merge base. Three-dot diffs
        against the merge base, so files changed on <target> after the
        branch base do NOT appear.
      - ``git diff --name-only HEAD`` — uncommitted tracked working-tree
        changes (staged + unstaged) not yet in HEAD.
      - ``git ls-files --others --exclude-standard`` — untracked new
        files honouring .gitignore.
    """

    def _collect(git_output: str) -> None:
        for f in git_output.split("\n"):
            if f and f not in seen_set:
                seen_set.add(f)
                seen.append(f)

    seen: list[str] = []
    seen_set: set[str] = set()
    try:
        committed_out = _git(
            repo, "diff", "--name-only", f"origin/{target_branch}...HEAD"
        )
    except subprocess.CalledProcessError:
        log.warning(
            "introduced_files: origin/%s ref not resolvable in %s — "
            "treating as no branch-introduced diff available",
            target_branch,
            repo,
        )
        committed_out = ""
    if committed_out:
        _collect(committed_out)
    working_out = _git(repo, "diff", "--name-only", "HEAD")
    if working_out:
        _collect(working_out)
    untracked_out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if untracked_out:
        _collect(untracked_out)
    return seen


def added_files(repo: Path, target_branch: str) -> list[str]:
    """Return every file the BRANCH ADDS (git status ``A``) relative to
    its merge base with ``origin/<target_branch>``.

    Uses ``git diff --name-status --diff-filter=A
    origin/<target>...HEAD`` (THREE-dot, against the merge base) so files
    that ``origin/<target>`` independently added after the branch was cut
    do NOT appear — only brand-new files the branch itself introduces.
    Modified / deleted / renamed paths are excluded.
    """
    out = _git(
        repo,
        "diff",
        "--name-status",
        "--diff-filter=A",
        f"origin/{target_branch}...HEAD",
    )
    added: list[str] = []
    if out:
        for line in out.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].startswith("A") and parts[-1]:
                added.append(parts[-1])
    return added


def conflicted_files(repo: Path) -> list[str]:
    """Return the paths with unresolved merge conflicts (git status ``U``).

    Uses ``git diff --name-only --diff-filter=U``, which lists unmerged
    paths during an in-progress merge/rebase. Returns ``[]`` when there
    are none (clean tree, or the rebase was already aborted). Best-effort:
    any git error degrades to ``[]`` so failure reporting never crashes.
    """
    try:
        out = _git(repo, "diff", "--name-only", "--diff-filter=U")
    except Exception:
        return []
    return [line for line in out.split("\n") if line] if out else []


def restore_paths(repo: Path, target_branch: str, paths: list[str]) -> None:
    """Drop *paths* from the branch's effective diff vs ``origin/<target>``.

    Used to undo scope-triage-REJECTed out-of-scope changes before the
    next iteration. For each path:

    - If it exists in ``origin/<target_branch>``, restore that version
      (``git checkout origin/<target> -- path``) — reverting any tracked
      modification, whether unstaged or already WIP-committed.
    - Otherwise it is a new file: drop it from the index if tracked
      (``git rm``, covering WIP-committed additions) and delete it from
      disk if it still exists (covering untracked additions).

    After this, :func:`changed_files` no longer reports *paths*, and a
    subsequent :func:`commit_all` records the cleaned tree — so the
    rejected paths are absent from the diff vs origin in both the
    unstaged and the WIP-committed cases.
    """
    ref = f"origin/{target_branch}"
    for p in paths:
        rel = p.lstrip("/")
        if not rel:
            continue
        in_origin = (
            subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{rel}"],
                capture_output=True,
            ).returncode
            == 0
        )
        if in_origin:
            subprocess.run(
                ["git", "-C", str(repo), "checkout", ref, "--", rel],
                capture_output=True,
                text=True,
            )
            continue
        # Not in origin — a newly added file. Drop a tracked
        # (incl. WIP-committed) version from the index, then remove
        # any leftover untracked file from disk.
        subprocess.run(
            ["git", "-C", str(repo), "rm", "-f", "--ignore-unmatch", "--", rel],
            capture_output=True,
            text=True,
        )
        file_path = repo / rel
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError:
            pass  # best-effort: unlink failure is non-fatal


def ignored_existing_paths(repo: Path, paths: list[str]) -> list[str]:
    """Of *paths* (repo-relative), return those that exist on disk but are
    gitignored — i.e. invisible to ``status``/``diff``/``ls-files``.

    This is the "edits landed but git can't see them" detector: a manifest
    board (e.g. a ROS 2 workspace repo whose ``.gitignore`` carries
    ``/src/*`` for vcs-imported sub-repos) lets an agent write real files
    that never reach a diff, which otherwise surfaces only as an opaque
    "no changes produced" block.
    """
    hits: list[str] = []
    for p in paths:
        rel = p.lstrip("/")
        if not rel or not (repo / rel).exists():
            continue
        rc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--quiet", "--", rel],
            capture_output=True,
        ).returncode
        if rc == 0:
            hits.append(rel)
    return hits


def ignored_paths(repo: Path, paths: list[str]) -> list[str]:
    """Subset of *paths* that are gitignored in *repo*, whether or not
    they currently exist on disk (unlike :func:`ignored_existing_paths`).

    ``git check-ignore --quiet`` matches against ignore rules regardless
    of on-disk existence — including nested paths under an ignored
    directory (e.g. ``src/ros2/foo/Status.msg`` against a ``/src/*``
    rule). Used by the refine guard to reject specs whose ``file_map``
    targets paths the board cannot deliver (vcs-imported / vendored
    sub-trees managed via ``repos.yaml``, invisible to git).
    """
    hits: list[str] = []
    for p in paths:
        rel = p.lstrip("/")
        if not rel:
            continue
        rc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--quiet", "--", rel],
            capture_output=True,
        ).returncode
        if rc == 0:
            hits.append(rel)
    return hits


def diff_base(
    repo: Path,
    target_branch: str,
    *,
    remote_url: str | None = None,
    token: str | None = None,
) -> str:
    """Return the unified diff of all commits on the current branch
    vs origin/<target_branch>. Fetches first so the diff is current.

    When BOTH *remote_url* and *token* are provided, the fetch goes
    through a fresh token-authed URL — required for private repos
    because the GitHub App installation token baked into ``origin``'s
    URL at clone time expires ~1h later, so a stale clone's later
    fetch would fail with exit 128. Without a token, fall back to
    the clone's existing ``origin`` remote (correct for public
    repos and for tests that set up a local bare repo as origin).
    """
    if remote_url is not None and token is not None:
        _git(
            repo,
            "fetch",
            _authed_url(remote_url, token),
            f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}",
        )
    else:
        _git(repo, "fetch", "origin", target_branch, timeout=NETWORK_GIT_TIMEOUT)
    return subprocess.run(
        ["git", "-C", str(repo), "diff", f"origin/{target_branch}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
