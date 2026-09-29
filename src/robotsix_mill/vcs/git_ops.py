"""Thin git helpers over a per-ticket clone living in its workspace.

The implement stage clones the target repo fresh per ticket; the deliver
stage pushes the branch later. These wrappers shell out to ``git`` so
the container only needs the git binary (already in the image).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from enum import StrEnum
from pathlib import Path

log = logging.getLogger("robotsix_mill.vcs.git_ops")

_CREDENTIAL_IN_URL = re.compile(r"://[^@/\s']+@")


class ReconcileResult(StrEnum):
    """Outcome of :func:`reconcile_with_remote_pr`.

    ``SYNCED`` — the workspace already matches the remote PR branch, was
        fast-forwarded onto it, is strictly ahead of it, or the remote
        branch doesn't exist yet (first push). Safe to proceed.
    ``DIVERGED`` — the workspace and the remote PR branch have BOTH
        advanced independently AND at least one commit the remote carries
        (that a force-push would discard) is FOREIGN — i.e. authored by
        someone other than the mill (a human pushed to the PR after the
        clone). A force-push here would silently overwrite that foreign
        commit — ``push_with_lease`` does NOT protect this case, because
        reconcile's own fetch already advanced the lease ref to it, so the
        compare-and-swap would pass. Callers MUST block instead of pushing.
        NOTE: divergence where every discarded remote commit is
        mill-authored (the mill's OWN prior force-push from an earlier
        rebase cycle) returns ``SYNCED``, not ``DIVERGED`` — overwriting the
        mill's own commit is safe, and bailing there caused needless manual
        reconciliation after routine mill rebases.
    ``UNAVAILABLE`` — the remote couldn't be reached / inspected (fetch
        failed transiently, corrupt clone, etc.). Reconcile couldn't
        determine the relationship, but the lease ref was NOT advanced to
        any foreign commit, so ``push_with_lease`` still backstops a stale
        push. Callers may proceed (the lease catches a genuine race).
    """

    SYNCED = "synced"
    DIVERGED = "diverged"
    UNAVAILABLE = "unavailable"


def redact_credentials(text: str | bytes) -> str:
    """Strip ``user:token@`` userinfo from any URL embedded in *text*.

    Error paths that repr a failed git command (``CalledProcessError``
    includes the full argv) would otherwise echo the tokenized remote —
    ``https://oauth2:ghs_…@github.com/…`` — into ticket notes and
    Langfuse traces. Run every git-command error string through this
    before it leaves the process. Accepts bytes (CalledProcessError
    stderr is bytes when the command ran without ``text=True``).
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return _CREDENTIAL_IN_URL.sub("://***@", text)


def _authed_url(url: str, token: str | None) -> str:
    """Inject a token into an https remote for non-interactive clone/push.
    Other schemes (file://, ssh) are returned unchanged. Never log the
    result — it contains the credential.
    """
    if token and url.startswith("https://"):
        return url.replace("https://", f"https://oauth2:{token}@", 1)
    return url


# Wall-clock ceiling for git operations that talk to a remote (clone,
# fetch, push). Without one, a stalled connection hangs the calling
# thread forever — and because every stage offloads its blocking work to
# a shared thread pool, each hung git permanently removes one worker from
# that pool until the process is restarted. Observed live: the whole pool
# wedged in the periodic repo-refresh fetch while merge stages sat
# "active" for 10+ minutes doing nothing. Local-only git calls (log,
# diff, rev-parse) cannot hang on the network and stay unbounded.
NETWORK_GIT_TIMEOUT = 300


def _git_env() -> dict[str, str]:
    """Environment for git subprocesses: never prompt for credentials.

    A git process that decides it needs a username/password will block on
    the terminal indefinitely. There is no terminal here, but git can
    still wait on ``/dev/tty``, so a bad or expired token would hang the
    call rather than failing it. ``GIT_TERMINAL_PROMPT=0`` turns that
    into a clean non-zero exit.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str, timeout: float | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_git_env(),
    ).stdout.strip()


def _git_redacted(repo: Path, *args: str, timeout: float | None = None) -> str:
    """Like :func:`_git` but redacts credentials from any
    :class:`CalledProcessError` before propagation.

    Also redacts :class:`subprocess.TimeoutExpired`, whose ``cmd`` holds
    the same token-bearing URL.
    """
    try:
        return _git(repo, *args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise subprocess.TimeoutExpired(
            [redact_credentials(str(a)) for a in exc.cmd],
            exc.timeout,
            output=redact_credentials(exc.output or ""),
            stderr=redact_credentials(exc.stderr or ""),
        ) from None
    except subprocess.CalledProcessError as exc:
        raise subprocess.CalledProcessError(
            exc.returncode,
            [redact_credentials(str(a)) for a in exc.cmd],
            output=redact_credentials(exc.output or ""),
            stderr=redact_credentials(exc.stderr or ""),
        ) from None


def _remote_has_branches(remote_url: str, token: str | None) -> bool:
    """Return ``True`` if *remote_url* has at least one branch (``git
    ls-remote --heads`` returns non-empty output).

    A ``CalledProcessError`` (network error, permission denied, …) is
    treated as "unknown" and returns ``True`` — the caller should not
    attempt a bootstrap when it cannot verify emptiness.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "ls-remote",
                "--quiet",
                "--heads",
                _authed_url(remote_url, token),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=NETWORK_GIT_TIMEOUT,
            env=_git_env(),
        )
    except subprocess.CalledProcessError, subprocess.TimeoutExpired:
        # Can't determine — assume non-empty to avoid dangerous bootstrap.
        return True
    return bool(result.stdout.strip())


def _bootstrap_empty_repo(
    remote_url: str,
    dest: Path,
    branch: str,
    token: str | None,
    repo_id: str,
) -> None:
    """Bootstrap an empty remote repo by pushing an initial commit.

    Creates a temporary local repo with a minimal README, force-pushes
    it to *remote_url*, and then moves the repo to *dest* so it behaves
    like a fresh clone.  Raises :class:`subprocess.CalledProcessError`
    (or :class:`OSError`) on failure — callers must catch and log.
    """
    tmp = Path(tempfile.mkdtemp(prefix="bootstrap_"))
    try:
        init_repo(tmp, branch)
        (tmp / "README.md").write_text(
            f"# {repo_id}\n\nMill-managed repository — bootstrapped automatically.\n",
            encoding="utf-8",
        )
        commit_all(tmp, "Initial bootstrap commit")
        push(tmp, branch, remote_url, token)

        # Ensure destination is clean (clone may have left partial state).
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def clone(
    remote_url: str,
    dest: Path,
    branch: str,
    token: str | None = None,
    *,
    repo_id: str = "",
) -> None:
    """Single-branch clone of ``branch`` into ``dest`` (fresh per ticket).

    When the remote has no branches at all (truly empty repo — no commits
    yet), this function bootstraps the remote by pushing an initial commit
    and then behaves as if the clone succeeded.  *repo_id* is used for the
    bootstrap README content (defaults to *dest*'s directory name when
    empty).

    On failure raises :class:`subprocess.CalledProcessError` with the
    tokenized URL redacted from ``cmd`` and ``stderr`` — the repr of this
    error routinely ends up in ticket notes and traces.
    """
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--single-branch",
                "--branch",
                branch,
                _authed_url(remote_url, token),
                str(dest),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=NETWORK_GIT_TIMEOUT,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise subprocess.TimeoutExpired(
            [redact_credentials(str(a)) for a in exc.cmd],
            exc.timeout,
            output=redact_credentials(exc.output or ""),
            stderr=redact_credentials(exc.stderr or ""),
        ) from None
    except subprocess.CalledProcessError as exc:
        stderr_str = redact_credentials(exc.stderr or "")
        # When git reports the branch doesn't exist on the remote, the
        # remote might be truly empty (no branches at all — a freshly
        # created GitHub repo with auto_init=false).  Verify emptiness
        # via ``git ls-remote --heads`` (the actual signal, not a
        # brittle string match) and bootstrap only when confirmed empty.
        if "Remote branch" in stderr_str and "not found" in stderr_str:
            if not _remote_has_branches(remote_url, token):
                log.info("clone: remote has no branches — bootstrapping empty repo")
                try:
                    _bootstrap_empty_repo(
                        remote_url,
                        dest,
                        branch,
                        token,
                        repo_id or dest.name,
                    )
                    # Bootstrap succeeded — the repo is now at dest as if
                    # a normal clone happened.  Fall through to configure
                    # git identity (no early return — the bootstrap's
                    # init_repo already configured identity, but let's be
                    # safe and reconfigure on the moved directory).
                except Exception as bootstrap_err:
                    raise subprocess.CalledProcessError(
                        exc.returncode,
                        [redact_credentials(str(a)) for a in exc.cmd],
                        output=redact_credentials(exc.output or ""),
                        stderr=redact_credentials(
                            f"{stderr_str}\n"
                            f"(empty-repo bootstrap also failed: {bootstrap_err})"
                        ),
                    ) from None
            else:
                # Remote has branches but not the target one — a
                # configuration mismatch, not an empty repo.
                raise subprocess.CalledProcessError(
                    exc.returncode,
                    [redact_credentials(str(a)) for a in exc.cmd],
                    output=redact_credentials(exc.output or ""),
                    stderr=stderr_str,
                ) from None
        else:
            raise subprocess.CalledProcessError(
                exc.returncode,
                [redact_credentials(str(a)) for a in exc.cmd],
                output=redact_credentials(exc.output or ""),
                stderr=stderr_str,
            ) from None
    _git(dest, "config", "user.email", "mill@robotsix.local")
    _git(dest, "config", "user.name", "robotsix-mill")


def init_repo(dest: Path, branch: str) -> None:
    """Initialise a fresh, empty git repo at ``dest`` with ``branch`` as the
    initial branch and the mill's commit identity configured.

    Use this to scaffold a brand-new (empty) remote: a freshly-created GitHub
    repo (``auto_init: false``) has no branches, so ``clone --branch <main>``
    fails. ``init`` + force-``push`` populates the default branch instead.
    """
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", "-b", branch, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(dest, "config", "user.email", "mill@robotsix.local")
    _git(dest, "config", "user.name", "robotsix-mill")


def has_changes(repo: Path) -> bool:
    """Return ``True`` if the repo has uncommitted changes."""
    return bool(_git(repo, "status", "--porcelain"))


def branch_exists(repo: Path, name: str) -> bool:
    """Return ``True`` if the local branch *name* exists."""
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{name}",
            ],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


# Age (seconds) past which a leftover ``.git/index.lock`` is treated as
# stale and safe to remove. No live git process in mill's per-ticket
# flow holds the index lock anywhere near this long; a lock older than
# this is the residue of a git process that crashed mid-operation.
# Observed 2026-09-02: a git crash mid-implement left ``index.lock``
# behind, and every subsequent ``git checkout`` failed with
# CalledProcessError, re-blocking the ticket in a retry loop on a
# terminal condition until a human removed the lock by hand.
STALE_INDEX_LOCK_SECONDS = 15 * 60


def clear_stale_index_lock(
    repo: Path, max_age_seconds: float = STALE_INDEX_LOCK_SECONDS
) -> bool:
    """Remove a stale ``.git/index.lock`` left by a crashed git process.

    Returns ``True`` when a stale lock was found and deleted (logging the
    repair), ``False`` otherwise. Best-effort: a missing lock, a lock
    younger than *max_age_seconds* (a genuinely live git process), or an
    ``OSError`` while inspecting/removing the file all return ``False``
    without raising, so callers can invoke this unconditionally before a
    workspace git command.
    """
    lock = repo / ".git" / "index.lock"
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    if age < max_age_seconds:
        return False
    try:
        lock.unlink()
    except OSError:
        return False
    log.warning(
        "cleared stale git index.lock in %s (age %.0fs > %.0fs) — "
        "repairing after a crashed git process",
        repo,
        age,
        max_age_seconds,
    )
    return True


def checkout(repo: Path, name: str) -> None:
    """Quiet checkout of branch *name*."""
    _git(repo, "checkout", "-q", name)


def try_rebase_onto(
    repo: Path,
    target: str,
    *,
    remote_url: str | None = None,
    token: str | None = None,
) -> bool:
    """Fetch ``<target>`` and rebase the current branch onto it.

    Deterministic (no agent). Returns ``True`` on a clean rebase;
    on any fetch/rebase failure it aborts a half-applied rebase and
    returns ``False`` so the caller can fall back to a fresh clone.
    Used by the implement resume path so a WIP branch pinned to an old
    base picks up current ``main`` (e.g. a fixed test-gate conftest)
    instead of failing the gate forever.

    ``remote_url`` + ``token`` are the GitHub App installation token
    flow used by ``push``/``fetch``: they are token-authed at call time
    via :func:`_authed_url`. Without them the function falls back to
    the clone's stored ``origin`` URL — fine for unauthenticated remotes
    but a footgun for GitHub App tokens (1-hour TTL): a clone made
    hours ago carries an expired token in ``origin``, so ``git fetch
    origin`` returns 401, this function returns False, and the
    implement→rebase loop in implement.py:818 fires every poll because
    the rebase stage's own push/fetch use a freshly minted token and
    never rewrite ``origin``. Passing the fresh token here breaks the
    loop. ff45 hit exactly this on 2026-05-29.

    Any uncommitted edits in the working tree are discarded before the
    rebase. These come exclusively from a server interrupt mid-stage —
    the agent had committed its real progress and started another edit
    when the process was killed. The leftover diff is throwaway state,
    not work-to-preserve; trying to autostash it just carried the
    interrupted edits forward into the next cycle and re-broke things.
    Start from a clean checkout instead.
    """
    # Always-fresh authed URL when the caller has a remote_url;
    # otherwise fall back to the clone's stored origin (no remote_url
    # at all → legacy callers / tests that don't thread auth). When
    # remote_url is set but token is None (e.g. file:// remote in
    # tests), _authed_url passes the URL through unchanged.
    fetch_remote = _authed_url(remote_url, token) if remote_url else "origin"
    try:
        _git(repo, "fetch", fetch_remote, target, timeout=NETWORK_GIT_TIMEOUT)
    except subprocess.CalledProcessError:
        return False
    # `git fetch <explicit-url> <target>` writes to FETCH_HEAD but does
    # NOT update `refs/remotes/origin/<target>`, so the subsequent
    # `git rebase origin/<target>` would rebase onto a STALE
    # remote-tracking ref. Update the ref explicitly so the rebase
    # picks up what we just fetched.
    if fetch_remote != "origin":
        try:
            _git(repo, "update-ref", f"refs/remotes/origin/{target}", "FETCH_HEAD")
        except subprocess.CalledProcessError:
            # If the update fails the rebase target will be stale —
            # bail rather than rebase onto an old SHA.
            return False
    # Discard any leftover uncommitted state from a prior interrupted
    # stage. Best-effort — a failure here just falls through to the
    # rebase, where the original error will surface.
    try:
        _git(repo, "reset", "--hard", "HEAD")
        _git(repo, "clean", "-fd")
    except subprocess.CalledProcessError:
        pass
    try:
        _git(repo, "rebase", f"origin/{target}")
        return True
    except subprocess.CalledProcessError:
        with contextlib.suppress(subprocess.CalledProcessError):
            _git(repo, "rebase", "--abort")
        return False


def try_mechanical_rebase(repo: Path, target_branch: str) -> bool:
    """Attempt a clean git rebase onto origin/<target_branch>.

    Fetches the target branch, then runs `git rebase origin/<target_branch>`.
    Returns True on clean success (exit 0, no conflicts).
    On any failure (including conflicts), runs `git rebase --abort` and returns False,
    leaving the working tree in a clean state for the caller (LLM agent) to take over.
    """
    try:
        _git(repo, "fetch", "origin", target_branch, timeout=NETWORK_GIT_TIMEOUT)
    except subprocess.CalledProcessError:
        return False
    try:
        _git(repo, "rebase", f"origin/{target_branch}")
        return True
    except subprocess.CalledProcessError:
        with contextlib.suppress(subprocess.CalledProcessError):
            _git(repo, "rebase", "--abort")
        return False


def try_rebase_onto_branch(
    repo: Path,
    branch: str,
    *,
    remote_url: str,
    token: str | None,
) -> bool:
    """Fetch ``<branch>`` and rebase the current branch onto its current
    remote tip (``origin/<branch>``).

    Used by the ci-fix post-push recovery to re-land an agent's DONE push
    after a lease rejection: the mill may have rebased the PR branch
    concurrently, so the agent's committed fix (still only local) is
    replayed on top of the fresh remote tip and the caller re-pushes with
    lease.

    Returns ``True`` on a clean rebase (a no-op when the remote is already
    an ancestor of the local branch). On any fetch/rebase failure a
    half-applied rebase is aborted and ``False`` is returned so the caller
    can stop retrying and surface the problem to a human. Any uncommitted
    working-tree leftovers are discarded first — they come only from an
    interrupted agent mid-edit; the real fix is committed.
    """
    try:
        fetch(repo, remote_url=remote_url, token=token, branch=branch)
    except subprocess.SubprocessError:
        return False
    # Discard leftover uncommitted state from an interrupted agent; the
    # real fix is committed. Best-effort — a failure falls through to the
    # rebase, where the original error surfaces.
    try:
        _git(repo, "reset", "--hard", "HEAD")
        _git(repo, "clean", "-fd")
    except subprocess.CalledProcessError:
        pass
    try:
        _git(repo, "rebase", f"origin/{branch}")
        return True
    except subprocess.SubprocessError:
        with contextlib.suppress(subprocess.CalledProcessError):
            _git(repo, "rebase", "--abort")
        return False


def head_sha(repo: Path) -> str:
    """Current HEAD commit SHA. Used to detect a no-op rebase so the
    merge stage can skip a pointless force-push (an unchanged push still
    re-triggers CI and a GitHub mergeable recompute → state churn).
    """
    return _git(repo, "rev-parse", "HEAD")


def remote_branch_sha(repo: Path, branch: str) -> str | None:
    """SHA the remote currently has for *branch* (the rebase agent runs
    ``git fetch origin`` first, so ``origin/<branch>`` is fresh). Returns
    None if the remote has no such branch yet. The merge stage skips the
    force-push only when this equals local HEAD — i.e. the remote truly
    already has this exact commit (not merely a local-rebase no-op).
    """
    try:
        return _git(repo, "rev-parse", f"refs/remotes/origin/{branch}")
    except subprocess.CalledProcessError:
        return None


def ls_remote_sha(
    remote_url: str, ref: str = "HEAD", token: str | None = None
) -> str | None:
    """Resolve *ref* on *remote_url* to a commit SHA without cloning.

    Runs ``git ls-remote`` against the remote and returns the SHA,
    or ``None`` on any failure (timeout, non-zero exit, unparseable
    output).  For private repos pass the forge *token* — it is
    injected into ``https://`` URLs via :func:`_authed_url`.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", _authed_url(remote_url, token), ref],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, _ = line.partition("\t")
        if sha:
            return sha

    return None


# ---------------------------------------------------------------------------
# Push-access probe (health endpoint)
# ---------------------------------------------------------------------------

# Auth-failure substrings that ``git ls-remote`` / ``git push`` emit when
# the token is expired, revoked, or otherwise invalid.  Case-insensitive
# match so we catch both lowercase and uppercase variants.
PUSH_AUTH_FAILURE_SUBSTRINGS: tuple[str, ...] = (
    "invalid username or token",
    "password authentication is not supported",
    "authentication failed",
    "remote: invalid",
    "401",
    "403",
    "fatal: authentication failed",
    "could not read from remote repository",
    "repository not found",
)


def classify_push_error(stderr: str) -> str:
    """Distinguish an auth failure from a generic push failure.

    Returns ``"auth"`` when *stderr* contains a known auth-failure
    substring, ``"generic"`` otherwise.
    """
    lowered = stderr.lower()
    for marker in PUSH_AUTH_FAILURE_SUBSTRINGS:
        if marker in lowered:
            return "auth"
    return "generic"


def check_push_access(
    remote_url: str, token: str | None = None, ref: str = "HEAD"
) -> dict[str, object]:
    """Verify push-scope access to *remote_url* without side effects.

    Runs ``git ls-remote`` against the remote (a read-only operation
    that still exercises token auth) and classifies the result:

    Returns a dict with keys:
    - ``ok`` (bool): ``True`` when the remote is reachable and the
      token authenticates successfully.
    - ``status`` (str): ``"ok"``, ``"auth_error"``, or ``"error"``.
    - ``latency_ms`` (int): wall-clock latency of the git call.
    - ``detail`` (str | None): human-readable detail (SHA on success,
      error summary on failure).

    Unlike ``ls_remote_sha`` this function captures and classifies the
    stderr to distinguish auth failures from transient network errors.
    """
    import time as _time

    start = _time.perf_counter()
    try:
        result = subprocess.run(
            ["git", "ls-remote", _authed_url(remote_url, token), ref],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        elapsed = int((_time.perf_counter() - start) * 1000)
        return {
            "ok": False,
            "status": "error",
            "latency_ms": elapsed,
            "detail": f"git ls-remote raised: {exc}",
        }

    elapsed = int((_time.perf_counter() - start) * 1000)

    if result.returncode == 0:
        sha: str | None = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            sha_part, _, _ = line.partition("\t")
            if sha_part:
                sha = sha_part
                break
        return {
            "ok": True,
            "status": "ok",
            "latency_ms": elapsed,
            "detail": sha,
        }

    combined_stderr = result.stderr or ""
    classification = classify_push_error(combined_stderr)

    return {
        "ok": False,
        "status": "auth_error" if classification == "auth" else "error",
        "latency_ms": elapsed,
        "detail": redact_credentials(
            combined_stderr[:200] or f"git ls-remote exit {result.returncode}"
        ),
    }


def branch_ancestry(repo: Path, branch: str, target: str) -> list[dict[str, str]]:
    """Return commits on ``origin/<branch>`` not on ``origin/<target>``.

    Each commit dict carries ``sha``, ``author_name``, ``author_email``,
    ``committer_name``, ``committer_email``, and ``subject``.  The agent
    calls this after a lease rejection to decide foreign-vs-self divergence:
    if every commit's author/committer is the mill itself it is a prior
    self-rebase and safe to retry; a foreign author means a human pushed
    and the mill must NOT clobber it.

    The caller must have already fetched both refs so ``origin/<branch>``
    and ``origin/<target>`` are current.  Returns an empty list when the
    two refs are identical or the remote branch doesn't exist.
    """
    try:
        out = _git(
            repo,
            "log",
            f"origin/{target}..origin/{branch}",
            "--format=%H|%an|%ae|%cn|%ce|%s",
        )
    except subprocess.CalledProcessError:
        return []
    if not out:
        return []
    commits: list[dict[str, str]] = []
    for line in out.split("\n"):
        parts = line.split("|", 5)
        if len(parts) >= 6:
            commits.append(
                {
                    "sha": parts[0],
                    "author_name": parts[1],
                    "author_email": parts[2],
                    "committer_name": parts[3],
                    "committer_email": parts[4],
                    "subject": parts[5],
                }
            )
    return commits


def create_branch(repo: Path, name: str) -> None:
    """Create or reset a branch (``git checkout -B``)."""
    _git(repo, "checkout", "-q", "-B", name)


def commit_all(repo: Path, message: str) -> None:
    """Stage all changes and commit (``git add -A`` + ``git commit -q -m``).

    If the staging area is empty after ``git add -A``, no commit is
    created and the function returns silently (no-op).  This avoids
    ``CalledProcessError`` when the working tree has no changes
    (e.g. a review-fix pass whose net diff is a pure deletion that
    was already applied in an earlier attempt).
    """
    _git(repo, "add", "-A")
    if not _git(repo, "status", "--porcelain"):
        return
    _git(repo, "commit", "-q", "-m", message)


def commit_file(repo: Path, filename: str, message: str) -> bool:
    """Stage ``filename`` and commit if it differs from HEAD.

    Returns True when a commit was created, False when the file was
    unchanged (nothing staged) — allowing callers to skip logging.

    Raises ``subprocess.CalledProcessError`` on git failure (caller is
    responsible for catching and warning).
    """
    _git(repo, "add", filename)
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", filename],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return False
    _git(repo, "commit", "-q", "-m", message)
    return True


def empty_commit(repo: Path, message: str) -> None:
    """Create an empty commit (``git commit --allow-empty -q -m``).

    Used to force a new CI run on a branch whose HEAD is already
    current — the new (empty) commit produces a fresh head SHA,
    which triggers a new pull_request workflow run on the forge.
    """
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)


def push(repo: Path, branch: str, remote_url: str, token: str | None) -> None:
    """Push ``branch`` to ``remote_url`` (token-auth for https). Uses
    ``--force`` so a re-delivery updates the bot-owned branch; pushes to
    the explicit authed URL rather than the clone's origin (the clone
    may have been made without a write token, and there is no
    remote-tracking ref to lease against on an explicit-URL push).
    """
    _git_redacted(
        repo,
        "push",
        "--force",
        _authed_url(remote_url, token),
        f"{branch}:{branch}",
        timeout=NETWORK_GIT_TIMEOUT,
    )


def fetch(repo: Path, *, remote_url: str, token: str | None, branch: str) -> None:
    """Fetch ``branch`` from ``remote_url`` (token-auth for https) and
    update ``refs/remotes/origin/<branch>``.  Uses an explicit refspec
    so the remote-tracking ref is refreshed even when fetching from
    an explicit URL rather than the clone's origin remote.
    """
    _git_redacted(
        repo,
        "fetch",
        _authed_url(remote_url, token),
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        timeout=NETWORK_GIT_TIMEOUT,
    )


def refresh_origin_target(repo: Path, target_branch: str = "main") -> str | None:
    """Best-effort ``git fetch origin --prune <target_branch>`` and resolve
    ``origin/<target_branch>`` to its commit SHA.

    Returns the SHA, or ``None`` when the fetch or resolution fails (no
    ``origin`` remote, no network, or the ref does not exist).  Callers
    that assert a commit/branch is ABSENT from the remote must call this
    first so a stale local clone can never fabricate a "missing" verdict:
    if a fresh fetch is impossible, the caller must fall back to the
    pre-existing local ref (returned here when it already exists) rather
    than concluding anything is absent.
    """
    with contextlib.suppress(Exception):
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin", "--prune", target_branch],
            capture_output=True,
            text=True,
            timeout=NETWORK_GIT_TIMEOUT,
        )
    with contextlib.suppress(Exception):
        out = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--verify",
                "--quiet",
                f"origin/{target_branch}",
            ],
            capture_output=True,
            text=True,
        )
        sha = out.stdout.strip()
        if sha:
            return sha
    return None


# ---------------------------------------------------------------------------
# Re-exports from git_diff — keep git_ops as the single import target for
# callers so no call-site changes are needed.
# ---------------------------------------------------------------------------
from .git_diff import (
    DEFAULT_REBASE_DROP_EXEMPT_PATHS,
    _paths_from_diff,
    added_files,
    branch_has_net_diff,
    branch_is_ahead_of_main,
    branch_is_behind_main,
    changed_files,
    changed_source_files,
    check_rebase_diff_integrity,
    conflicted_files,
    diff_base,
    file_blobs,
    ignored_existing_paths,
    ignored_paths,
    introduced_files,
    restore_paths,
    tracked_paths_at,
)

__all__ = [
    "DEFAULT_REBASE_DROP_EXEMPT_PATHS",
    "PostPushResult",
    "_paths_from_diff",
    "added_files",
    "branch_has_net_diff",
    "branch_is_ahead_of_main",
    "branch_is_behind_main",
    "changed_files",
    "changed_source_files",
    "check_rebase_diff_integrity",
    "conflicted_files",
    "diff_base",
    "file_blobs",
    "ignored_existing_paths",
    "ignored_paths",
    "introduced_files",
    "post_push_check",
    "push_with_lease",
    "reconcile_with_remote_pr",
    "restore_paths",
    "tracked_paths_at",
]

# Backward-compat re-exports: PR-reconciliation logic now lives in
# pr_reconciliation.py (extracted for module size). Imported at module
# bottom so git_ops's own helpers are defined before pr_reconciliation
# imports them back (avoids a circular-import failure at load time).
from .pr_reconciliation import (
    PostPushResult,
    post_push_check,
    push_with_lease,
    reconcile_with_remote_pr,
)
