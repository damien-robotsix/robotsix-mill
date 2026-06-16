"""Shared autoupdate CLI for robotsix-* Docker Compose services.

Stdlib-only.  Orchestrates: flock → checkout → guard uncommitted changes →
git fetch → SHA comparison → idle polling (pre-build) → .env backup →
fast-forward merge → .env restore → docker compose build →
idle polling (post-build) → docker compose up → record deployed SHA.

Used by both ``robotsix-mill`` (``dev/mill-autoupdate.sh``) and
``robotsix-auto-mail`` (``dev/auto-mail-autoupdate.sh``) via thin
bash wrappers that supply service-specific flags.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse *argv* and run the full autoupdate workflow.

    Returns 0 on success (including intentional skips), non-zero on error.
    Designed to be called as ``sys.exit(main())`` from a ``__main__`` guard,
    or wired into a ``console_scripts`` entry point.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run(args)


# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="robotsix-autoupdate",
        description="Pull latest commits and rebuild a Docker Compose service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Exit codes:
              0  success, or intentional skip (already deployed, lock held, WIP, idle timeout)
              1  operational failure (fetch / merge / build / up error)
            """),
    )
    p.add_argument(
        "--repo",
        default=os.getcwd(),
        help="Path to the git repository (default: current working directory).",
    )
    p.add_argument(
        "--state-dir",
        default=None,
        help="Directory for runtime state files (default: parent of --repo).",
    )
    p.add_argument(
        "--state-prefix",
        default="mill-autoupdate",
        help="Prefix for state filenames (default: %(default)s).",
    )
    p.add_argument(
        "--remote",
        default="origin/main",
        help="Remote ref to fetch and merge, as '<remote>/<branch>' (default: %(default)s).",
    )
    p.add_argument(
        "--service",
        default="mill",
        help="Docker Compose service name to build and restart (default: %(default)s).",
    )
    p.add_argument(
        "--idle-check-cmd",
        default=None,
        help="Shell command to run for idle check (default: skip idle polling).",
    )
    p.add_argument(
        "--ensure-branch",
        default=None,
        help="If set, 'git checkout <BRANCH>' before fetching.",
    )
    p.add_argument(
        "--pre-build-wait",
        type=int,
        default=1200,
        help="Max seconds to poll idle before build (default: %(default)s).",
    )
    p.add_argument(
        "--post-build-wait",
        type=int,
        default=300,
        help="Max seconds to poll idle after build, before 'docker compose up' (default: %(default)s).",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=90,
        help="Seconds between idle-check polls (default: %(default)s).",
    )
    p.add_argument(
        "--max-deferrals",
        type=int,
        default=4,
        help="Consecutive busy-deferral cap before force-deploy (default: %(default)s).",
    )
    p.add_argument(
        "--no-force-deploy",
        action="store_true",
        default=False,
        help="Never force-deploy when busy; always defer regardless of --max-deferrals.",
    )
    p.add_argument(
        "--no-idle-check",
        action="store_true",
        default=False,
        help="Skip all idle polling (overrides --idle-check-cmd and both wait flags).",
    )
    return p


# ---------------------------------------------------------------------------
# Core workflow
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:  # noqa: C901
    """Execute the full autoupdate workflow given parsed *args*.

    Returns 0 on success/skip, non-zero on failure.
    """
    # -- resolve paths -------------------------------------------------------
    repo = Path(args.repo).resolve()
    state_dir = (
        Path(args.state_dir).resolve() if args.state_dir else repo.parent.resolve()
    )
    prefix = args.state_prefix

    log_path = state_dir / f"{prefix}.log"
    deployed_sha_path = state_dir / f".{prefix}-deployed-sha"
    deferral_path = state_dir / f".{prefix}-deferrals"
    lock_path = Path(f"/tmp/{prefix}.lock")  # nosec B108 — world-readable flock guard, same path the bash autoupdater used; contains no data

    # -- parse remote --------------------------------------------------------
    # "<remote>/<branch>" — split at the FIRST slash so branch names that
    # themselves contain slashes (origin/feature/x) keep the full branch
    # part.
    remote_name, _, remote_branch = args.remote.partition("/")

    # -- determine whether idle checking is active ---------------------------
    do_idle_check = bool(args.idle_check_cmd) and not args.no_idle_check

    forced = False  # becomes True when max-deferrals is hit

    # -- flock (non-blocking) ------------------------------------------------
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
    except OSError:
        _log(log_path, "ERROR: cannot open lock file — skipping", to_stderr=True)
        return 1
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log(log_path, "another run in progress — skipping")
        os.close(lock_fd)
        return 0

    try:
        # -- ensure branch ---------------------------------------------------
        if args.ensure_branch:
            _log(log_path, f"checking out branch: {args.ensure_branch}")
            cp = _run(
                ["git", "-C", str(repo), "checkout", args.ensure_branch],
                log_path,
                check=False,
            )
            if cp.returncode != 0:
                _log(
                    log_path,
                    f"ERROR: git checkout {args.ensure_branch} failed — skipping",
                    to_stderr=True,
                )
                return 1

        # -- guard uncommitted tracked changes -------------------------------
        cp = _run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            log_path,
            check=False,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            # Exclude .env lines — the local .env is always modified.
            lines = [
                line
                for line in cp.stdout.splitlines()
                if line.strip() and not line.strip().endswith(" .env")
            ]
            if lines:
                _log(
                    log_path,
                    "working tree has uncommitted changes — skipping pull/rebuild",
                )
                return 0

        # -- fetch -----------------------------------------------------------
        _log(log_path, f"fetching {remote_name} {remote_branch}")
        cp = _run(
            ["git", "-C", str(repo), "fetch", remote_name, remote_branch],
            log_path,
            check=False,
        )
        if cp.returncode != 0:
            _log(
                log_path,
                "ERROR: git fetch failed (SSH auth / network?) — skipping",
                to_stderr=True,
            )
            return 1

        # -- compare SHAs ----------------------------------------------------
        cp_remote = _run(
            ["git", "-C", str(repo), "rev-parse", f"{remote_name}/{remote_branch}"],
            log_path,
            check=False,
        )
        if cp_remote.returncode != 0:
            _log(
                log_path,
                f"ERROR: cannot resolve {remote_name}/{remote_branch} — skipping",
                to_stderr=True,
            )
            return 1
        remote_sha = cp_remote.stdout.strip()

        deployed_sha = _read_sha(deployed_sha_path)
        if deployed_sha and deployed_sha == remote_sha:
            _log(log_path, f"container already on {remote_sha[:7]} — nothing to do")
            return 0

        # -- log commit range ------------------------------------------------
        dep_short = deployed_sha[:7] if deployed_sha else "(first run)"
        _log(
            log_path,
            f"new commits on {remote_name}/{remote_branch} ({dep_short} -> {remote_sha[:7]}):",
        )
        range_spec = (
            f"{deployed_sha}..{remote_sha}" if deployed_sha else f"HEAD..{remote_sha}"
        )
        cp_log = _run(
            ["git", "-C", str(repo), "--no-pager", "log", "--oneline", range_spec],
            log_path,
            check=False,
        )
        if cp_log.returncode == 0 and cp_log.stdout.strip():
            for line in cp_log.stdout.strip().splitlines():
                _log(log_path, f"    {line}")

        # -- idle polling (pre-build) ----------------------------------------
        if do_idle_check:
            idle_reached = _poll_idle(
                args.idle_check_cmd,
                args.pre_build_wait,
                args.poll_interval,
                log_path,
            )
            if not idle_reached:
                deferrals = _read_deferral_count(deferral_path)
                deferrals += 1
                _write_deferral_count(deferral_path, deferrals)
                if deferrals >= args.max_deferrals:
                    if args.no_force_deploy:
                        _log(
                            log_path,
                            f"mill still busy after {args.pre_build_wait}s, "
                            f"{deferrals} consecutive deferrals >= {args.max_deferrals} "
                            f"— would force-deploy but --no-force-deploy is set; deferring",
                        )
                        return 0
                    forced = True
                    _log(
                        log_path,
                        f"mill still busy after {args.pre_build_wait}s, but "
                        f"{deferrals} consecutive deferrals >= {args.max_deferrals} "
                        f"— FORCING graceful deploy (stop_grace_period 30m drains "
                        f"in-flight stages; draft refines re-run)",
                    )
                else:
                    _log(
                        log_path,
                        f"mill still busy after {args.pre_build_wait}s — deferring "
                        f"update to next run (deferral {deferrals}/{args.max_deferrals})",
                    )
                    return 0

        # -- protect .env across merge ---------------------------------------
        env_backup: str | None = None
        if (repo / ".env").exists():
            cp_env_diff = _run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "diff",
                    "--quiet",
                    "HEAD",
                    f"{remote_name}/{remote_branch}",
                    "--",
                    ".env",
                ],
                log_path,
                check=False,
            )
            if cp_env_diff.returncode != 0:
                # origin changes .env — back it up
                ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
                env_backup = str(state_dir / f".env.autoupdate-bak-{ts}")
                shutil.copy2(str(repo / ".env"), env_backup)
                _log(
                    log_path,
                    f"{remote_name}/{remote_branch} changes .env — "
                    f"saved current .env to {env_backup}",
                )
                # Detach .env so the fast-forward merge applies cleanly
                _run(
                    ["git", "-C", str(repo), "checkout", "--quiet", "--", ".env"],
                    log_path,
                    check=False,
                )

        # -- fast-forward merge ----------------------------------------------
        cp_merge = _run(
            [
                "git",
                "-C",
                str(repo),
                "merge",
                "--ff-only",
                f"{remote_name}/{remote_branch}",
            ],
            log_path,
            check=False,
        )
        if cp_merge.returncode != 0:
            # ff-only failed → the local branch has diverged from the remote
            # (a squash-merged commit, a stray local commit, or — the usual
            # culprit — the checkout was left on a feature branch). Without a
            # deploy-branch contract this freezes the host indefinitely: every
            # run hits this line and skips the build.
            #
            # When --ensure-branch was given, the branch is a deploy MIRROR by
            # contract, so reconcile by hard-resetting to the fetched remote
            # tip rather than skipping. Uncommitted tracked changes were
            # already guarded above (the run returns early), and the discarded
            # commit stays recoverable via the reflog — so log the old SHA.
            if not args.ensure_branch:
                _log(
                    log_path,
                    "ERROR: ff-only merge failed (local diverged?) — skipping "
                    "(pass --ensure-branch to reconcile a deploy mirror)",
                    to_stderr=True,
                )
                if env_backup:
                    shutil.copy2(env_backup, str(repo / ".env"))
                return 1

            cp_head = _run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                log_path,
                check=False,
            )
            local_sha = cp_head.stdout.strip()[:9] if cp_head.returncode == 0 else "?"
            _log(
                log_path,
                f"ff-only merge failed; reconciling {args.ensure_branch} to "
                f"{remote_name}/{remote_branch} (discarding diverged local "
                f"{local_sha} — recoverable via reflog)",
            )
            cp_reset = _run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "reset",
                    "--hard",
                    f"{remote_name}/{remote_branch}",
                ],
                log_path,
                check=False,
            )
            if cp_reset.returncode != 0:
                _log(
                    log_path,
                    "ERROR: reset --hard to remote tip failed — skipping",
                    to_stderr=True,
                )
                if env_backup:
                    shutil.copy2(env_backup, str(repo / ".env"))
                return 1

        # -- restore .env ----------------------------------------------------
        if env_backup:
            shutil.copy2(env_backup, str(repo / ".env"))
            _log(
                log_path,
                f"restored your .env — review {env_backup} vs "
                f"the new committed .env for new keys",
            )

        # -- docker build ----------------------------------------------------
        docker_gid = os.environ.get("DOCKER_GID") or _detect_docker_gid()
        mill_build_sha = os.environ.get("MILL_BUILD_SHA") or _git_short_sha(repo)

        _log(
            log_path,
            f"building image for {remote_sha[:7]} (DOCKER_GID={docker_gid}, "
            f"MILL_BUILD_SHA={mill_build_sha})",
        )
        cp_build = _run(
            [
                "docker",
                "compose",
                "build",
                "--build-arg",
                f"DOCKER_GID={docker_gid}",
                "--build-arg",
                f"MILL_BUILD_SHA={mill_build_sha}",
                args.service,
            ],
            log_path,
            cwd=str(repo),
            check=False,
        )
        if cp_build.returncode != 0:
            _log(log_path, "ERROR: docker compose build failed", to_stderr=True)
            return 1

        # -- idle polling (post-build) ---------------------------------------
        if do_idle_check and not forced:
            idle_reached = _poll_idle(
                args.idle_check_cmd,
                args.post_build_wait,
                args.poll_interval,
                log_path,
            )
            if not idle_reached:
                _log(
                    log_path,
                    "mill became busy during build — deferring container "
                    "recreate to next run",
                )
                return 0

        # -- docker compose up -----------------------------------------------
        cp_up = _run(
            ["docker", "compose", "up", "-d", args.service],
            log_path,
            cwd=str(repo),
            check=False,
        )
        if cp_up.returncode != 0:
            _log(log_path, "ERROR: docker compose up failed", to_stderr=True)
            return 1

        # -- record deployed SHA ---------------------------------------------
        deployed_sha_path.write_text(remote_sha + "\n")
        _log(log_path, f"deployed SHA recorded: {remote_sha[:7]}")

        # -- reset deferral counter ------------------------------------------
        if deferral_path.exists():
            deferral_path.unlink()
            _log(log_path, "deferral counter reset")

        _log(log_path, f"rebuild + restart OK — container now on {remote_sha[:7]}")
        return 0

    finally:
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log(log_path: Path, message: str, *, to_stderr: bool = False) -> None:
    """Append a timestamped line to *log_path*."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)
    if to_stderr:
        sys.stderr.write(line)


def _run(
    cmd: list[str],
    log_path: Path,
    *,
    cwd: str | None = None,
    check: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* via ``subprocess.run``, logging the invocation and output.

    Returns the ``CompletedProcess`` — caller inspects ``returncode``.
    """
    _log(log_path, f"+ {' '.join(shlex.quote(a) for a in cmd)}")
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log(log_path, f"  ! timed out after {timeout}s")
        if exc.stdout:
            _log(log_path, f"  stdout: {exc.stdout.decode('utf-8', errors='replace')}")
        if exc.stderr:
            _log(log_path, f"  stderr: {exc.stderr.decode('utf-8', errors='replace')}")
        raise
    if cp.stdout:
        for line in cp.stdout.splitlines():
            _log(log_path, f"  stdout: {line}")
    if cp.stderr:
        for line in cp.stderr.splitlines():
            _log(log_path, f"  stderr: {line}")
    if check and cp.returncode != 0:
        raise subprocess.CalledProcessError(cp.returncode, cmd, cp.stdout, cp.stderr)
    return cp


def _read_sha(path: Path) -> str | None:
    """Read a SHA from *path*, stripping whitespace.  Returns ``None`` if absent."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _read_deferral_count(path: Path) -> int:
    """Read the consecutive deferral counter, defaulting to 0."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else 0
    except FileNotFoundError, ValueError:
        return 0


def _write_deferral_count(path: Path, count: int) -> None:
    path.write_text(str(count), encoding="utf-8")


def _idle_check(cmd: str, timeout: int = 15) -> bool:
    """Run *cmd* via ``shell=True`` (it is an opaque shell string by design).

    Returns ``True`` if idle (exit ≠ 0), ``False`` if busy (exit 0).
    """
    try:
        cp = subprocess.run(
            cmd,
            shell=True,  # nosec B602 — operator-supplied --idle-check-cmd from local config, an opaque shell string by design (no untrusted input)
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return cp.returncode != 0
    except subprocess.TimeoutExpired:
        # Command hung — treat as busy to be safe (don't kill in-flight work).
        return False


def _poll_idle(
    cmd: str,
    max_wait: int,
    poll_interval: int,
    log_path: Path,
) -> bool:
    """Poll *cmd* until it signals idle or *max_wait* seconds elapse.

    Returns ``True`` if idle was reached within the cap, ``False`` if
    the cap expired while the mill was still busy.
    """
    waited = 0
    while not _idle_check(cmd):
        if waited >= max_wait:
            return False
        time.sleep(poll_interval)
        waited += poll_interval
        _log(
            log_path,
            f"mill busy (audit/stage running) — waiting {poll_interval}s "
            f"(waited {waited}s)",
        )
    _log(log_path, f"mill idle after {waited}s")
    return True


def _detect_docker_gid() -> str:
    """Resolve the docker group id via ``getent group docker``, falling back to an empty string."""
    try:
        cp = subprocess.run(
            ["getent", "group", "docker"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip().split(":")[2]
    except subprocess.TimeoutExpired, FileNotFoundError, IndexError:
        pass
    return ""


def _git_short_sha(repo: Path) -> str:
    """Return the short (7-char) SHA of HEAD in *repo*."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except subprocess.TimeoutExpired, FileNotFoundError:
        pass
    return ""


# ---------------------------------------------------------------------------
# __main__ guard
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(main())
