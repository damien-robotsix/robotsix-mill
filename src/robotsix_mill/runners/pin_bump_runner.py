"""Pin-bump runner — coordinated internal git-rev pin update pass.

Schedule-only periodic workflow (no LLM).  Runs weekly, aligned with
Renovate's schedule, to keep ``[tool.uv.sources]`` git pins across the
robotsix stack from drifting behind main.

Pipeline
--------
1. Clone every registered repo (best-effort; skips on failure).
2. Build the :class:`~.dependency_graph.DependencyGraph` from each
   repo's ``pyproject.toml``.
3. Run :func:`~.dependency_graph.resolve_coherent_pins` to compute a
   consistent set of target SHAs.
4. For each repo with outdated pins, create a branch, apply the bumps,
   run ``uv lock``, run ``pytest`` + ``ruff``, and open a PR.

PRs are ordered so a dependency's PR is opened before its dependents'
PRs (topological order).  Each PR's body links to the dependency PR
so the reviewer can follow the cascade.

Defaults
--------
- Interval: 604800 s (7 days), aligned with Renovate's weekly cadence.
- Enabled: requires a per-repo ``.robotsix-mill/periodic/pin_bump.yaml``
  presence file (or fleet-wide ``pin_bump_periodic`` Settings flag).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import RepoConfig, Settings, get_repos_config, target_branch_for
from ..dependency_graph import build_graph, resolve_coherent_pins
from ..dependency_graph.models import PinBump
from ..forge import get_forge
from ..forge.auth import _resolve_remote_url, github_token
from ..vcs import git_ops

log = logging.getLogger("robotsix_mill.pin_bump")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class PinBumpPassResult:
    """Summary of one pin-bump pass."""

    bumps_computed: list[PinBump] = field(default_factory=list)
    prs_opened: list[dict] = field(default_factory=list)
    repos_scanned: int = 0
    repos_with_bumps: int = 0
    summary: str = ""
    session_id: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clone_all_repos(
    settings: Settings,
) -> dict[str, Path]:
    """Clone every registered repo, returning ``{repo_id: clone_path}``.

    Best-effort: a clone failure skips that repo with a warning.
    """
    repos_config = get_repos_config()
    paths: dict[str, Path] = {}

    for repo_id, rc in repos_config.repos.items():
        remote = rc.forge_remote_url or settings.forge_remote_url
        if not remote:
            continue
        target = target_branch_for(settings, rc)
        clone_dest = settings.data_dir / repo_id / "pin_bump_workspace" / "repo"
        if clone_dest.exists():
            import shutil

            shutil.rmtree(clone_dest, ignore_errors=True)
        clone_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            token = None
            try:
                token = github_token(settings, repo_config=rc)
            except RuntimeError:
                pass  # no token available — proceed without authentication
            git_ops.clone(remote, clone_dest, target, token)
            paths[repo_id] = clone_dest
            log.info("pin_bump: cloned %s → %s", repo_id, clone_dest)
        except subprocess.CalledProcessError as exc:
            log.warning(
                "pin_bump: clone failed for %s: %s",
                repo_id,
                (exc.stderr or "")[:200],
            )
    return paths


def _run_tests(repo_path: Path, test_command: str | None = None) -> tuple[bool, str]:
    """Run the repo's test suite. Returns ``(ok, output)``."""
    if test_command is None:
        test_command = "pytest"
    try:
        result = subprocess.run(
            test_command.split(),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=600,
        )
        ok = result.returncode == 0
        output = (result.stdout + "\n" + result.stderr).strip()[:5000]
        return ok, output
    except subprocess.TimeoutExpired:
        return False, "tests timed out"
    except FileNotFoundError:
        return False, f"{test_command} not found"
    except Exception as exc:
        return False, str(exc)


def _run_ruff(repo_path: Path) -> tuple[bool, str]:
    """Run ruff check + format. Returns ``(ok, output)``."""
    try:
        check = subprocess.run(
            ["ruff", "check", "."],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        fmt = subprocess.run(
            ["ruff", "format", "."],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        ok = check.returncode == 0 and fmt.returncode == 0
        output = (
            check.stdout + "\n" + check.stderr + "\n" + fmt.stdout + "\n" + fmt.stderr
        ).strip()[:5000]
        return ok, output
    except FileNotFoundError:
        return True, "ruff not installed (skipped)"
    except subprocess.TimeoutExpired:
        return False, "ruff timed out"
    except Exception as exc:
        return False, str(exc)


def _open_pr(
    repo_path: Path,
    repo_id: str,
    rc: RepoConfig,
    settings: Settings,
    bump: PinBump,
    dependency_pr_urls: dict[str, str],
) -> str | None:
    """Commit the bump, push a branch, and open a PR.  Returns PR URL or ``None``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    branch = f"{settings.branch_prefix}pin-bump-{bump.package}-{stamp}"

    # Commit message.
    lines = [
        f"chore(deps): bump {bump.package} pin to {bump.new_rev[:8]}",
        "",
        f"Update [tool.uv.sources.{bump.package}] rev from "
        f"`{bump.old_rev[:8] if bump.old_rev else 'unpinned'}` to "
        f"`{bump.new_rev[:8]}`.",
        "",
        "Automated by robotsix-mill · /pin-bump",
    ]
    commit_msg = "\n".join(lines)

    # PR body.
    body_lines = [
        f"## Pin bump: {bump.package}",
        "",
        f"- **Repo:** `{repo_id}`",
        f"- **Package:** `{bump.package}`",
        f"- **Old rev:** `{bump.old_rev[:8] if bump.old_rev else 'unpinned'}`",
        f"- **New rev:** `{bump.new_rev[:8]}`",
        f"- **Git URL:** {bump.git_url}",
        "",
    ]
    if dependency_pr_urls:
        body_lines.append("### Dependency PRs (merge these first)")
        for dep_id, dep_url in sorted(dependency_pr_urls.items()):
            body_lines.append(f"- [{dep_id}]({dep_url})")
        body_lines.append("")
    body_lines.append("---")
    body_lines.append("Automated by robotsix-mill · `/pin-bump`")
    pr_body = "\n".join(body_lines)

    remote_url = _resolve_remote_url(settings, rc)
    if not remote_url:
        return None
    try:
        token = github_token(settings, repo_config=rc)
    except RuntimeError:
        return None

    try:
        git_ops.create_branch(repo_path, branch)
        git_ops.commit_all(repo_path, commit_msg)
    except subprocess.CalledProcessError as exc:
        log.warning(
            "pin_bump: commit failed for %s: %s",
            repo_id,
            (exc.stderr or "").strip()[:200],
        )
        return None

    try:
        git_ops.push(repo_path, branch, remote_url, token)
    except subprocess.CalledProcessError as exc:
        log.warning(
            "pin_bump: push failed for %s: %s",
            repo_id,
            (exc.stderr or "").strip()[:200],
        )
        return None

    pr_title = f"chore(deps): bump {bump.package} pin to {bump.new_rev[:8]}"
    try:
        url = get_forge(settings, repo_config=rc).open_merge_request(
            source_branch=branch,
            title=pr_title,
            body=pr_body,
        )
        log.info("pin_bump: PR opened for %s: %s", repo_id, url)
        return url
    except Exception as exc:
        log.warning("pin_bump: open PR failed for %s: %s", repo_id, exc)
        return None


# ---------------------------------------------------------------------------
# Runner entry point
# ---------------------------------------------------------------------------


def run_pin_bump_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> PinBumpPassResult:
    """Execute one pin-bump pass across all registered repos.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Per-repo configuration (the repo whose presence
            file triggered this pass).  The pass still scans ALL repos;
            this is just the trigger.

    Returns:
        A :class:`PinBumpPassResult` with bumps computed and PRs opened.
    """
    settings = Settings()
    repos_config = get_repos_config()
    repo_ids = list(repos_config.repos.keys())

    if not repo_ids:
        return PinBumpPassResult(
            summary="no repos registered — nothing to bump",
            session_id=session_id,
        )

    log.info("pin_bump: pass starting for %d repo(s)", len(repo_ids))

    # 1. Clone all repos.
    clone_paths = _clone_all_repos(settings)
    if not clone_paths:
        return PinBumpPassResult(
            summary="no repos cloned — check forge credentials and remote URLs",
            repos_scanned=0,
            session_id=session_id,
        )

    # 2. Build the dependency graph.
    try:
        graph = build_graph(clone_paths, settings=settings)
    except Exception as exc:
        log.exception("pin_bump: graph build failed")
        return PinBumpPassResult(
            summary=f"graph build failed: {exc}",
            repos_scanned=len(clone_paths),
            session_id=session_id,
        )

    log.info(
        "pin_bump: graph built — %d nodes, topo order: %s",
        graph.node_count,
        " → ".join(graph.topo_order[:10]) + ("…" if len(graph.topo_order) > 10 else ""),
    )

    # 3. Resolve coherent pins.
    try:
        bumps = resolve_coherent_pins(graph, dry_run=False)
    except Exception as exc:
        log.exception("pin_bump: resolution failed")
        return PinBumpPassResult(
            summary=f"resolution failed: {exc}",
            repos_scanned=len(clone_paths),
            session_id=session_id,
        )

    # 4. Group bumps by repo (one PR per repo).
    bumps_by_repo: dict[str, list[PinBump]] = {}
    for bump in bumps:
        if bump.already_current:
            continue
        bumps_by_repo.setdefault(bump.repo_id, []).append(bump)

    if not bumps_by_repo:
        return PinBumpPassResult(
            bumps_computed=bumps,
            repos_scanned=len(clone_paths),
            repos_with_bumps=0,
            summary=f"all {len(bumps)} pin(s) already current in {len(clone_paths)} repo(s)",
            session_id=session_id,
        )

    # 5. For each repo with bumps, run tests + open a PR.
    # Process in topological order so dependency PRs land first.
    pr_urls: dict[str, str] = {}  # repo_id → PR URL
    prs_opened: list[dict] = []

    for repo_id in graph.topo_order:
        repo_bumps = bumps_by_repo.get(repo_id)
        if not repo_bumps:
            continue

        clone_path = clone_paths.get(repo_id)
        if clone_path is None:
            log.warning("pin_bump: no clone for %s — skipping bumps", repo_id)
            continue

        rc = repos_config.repos.get(repo_id)
        if rc is None:
            continue

        # Apply all bumps for this repo.
        for bump in repo_bumps:
            from ..dependency_graph.resolver import _apply_bump

            if not _apply_bump(clone_path, bump):
                log.warning("pin_bump: failed to apply %s", bump.description)
                continue
            ok, err = _run_uv_lock_simple(clone_path)
            if not ok:
                log.warning(
                    "pin_bump: uv lock failed for %s: %s", bump.description, err[:200]
                )
                continue

        # Run tests.
        test_cmd = None
        try:
            from ..config.repo_settings import load_repo_settings

            repo_settings = load_repo_settings(clone_path)
            test_cmd = repo_settings.test_command or test_cmd
        except Exception:
            log.debug("pin_bump: could not load repo settings for %s", repo_id)

        tests_ok, test_output = _run_tests(clone_path, test_cmd)
        if not tests_ok:
            log.warning(
                "pin_bump: tests failed for %s — opening PR anyway "
                "(CI will catch it): %s",
                repo_id,
                test_output[:200],
            )

        ruff_ok, ruff_output = _run_ruff(clone_path)

        # Collect the dependency PR URLs for the PR body.
        dep_pr_urls: dict[str, str] = {}
        node = graph.nodes.get(repo_id)
        if node:
            for dep_id in node.upstream:
                if dep_id in pr_urls:
                    dep_pr_urls[dep_id] = pr_urls[dep_id]

        # Open one PR per repo (all bumps in one commit).
        first_bump = repo_bumps[0]
        pr_url = _open_pr(
            clone_path,
            repo_id,
            rc,
            settings,
            first_bump,
            dep_pr_urls,
        )
        if pr_url:
            pr_urls[repo_id] = pr_url
            prs_opened.append(
                {
                    "repo_id": repo_id,
                    "pr_url": pr_url,
                    "packages": [b.package for b in repo_bumps],
                    "tests_ok": tests_ok,
                    "ruff_ok": ruff_ok,
                }
            )

    # Build summary.
    total_bumps = len(bumps)
    needed_bumps = sum(1 for b in bumps if not b.already_current)
    summary = (
        f"scanned={len(clone_paths)} repos, "
        f"total_pins={total_bumps}, "
        f"needed_bumps={needed_bumps}, "
        f"prs_opened={len(prs_opened)}"
    )
    log.info("pin_bump pass done: %s", summary)

    return PinBumpPassResult(
        bumps_computed=bumps,
        prs_opened=prs_opened,
        repos_scanned=len(clone_paths),
        repos_with_bumps=len(bumps_by_repo),
        summary=summary,
        session_id=session_id,
    )


def _run_uv_lock_simple(repo_path: Path) -> tuple[bool, str]:
    """Run ``uv lock`` in *repo_path*.  Returns ``(ok, stderr)``."""
    if not (repo_path / "pyproject.toml").exists():
        return False, "no pyproject.toml"
    try:
        result = subprocess.run(
            ["uv", "lock"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        ok = result.returncode == 0
        err = result.stderr.strip() or result.stdout.strip()
        return ok, err
    except FileNotFoundError:
        return False, "uv not found"
    except subprocess.TimeoutExpired:
        return False, "uv lock timed out"
