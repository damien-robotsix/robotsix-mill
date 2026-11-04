"""Deterministic refine gate: the draft's file-to-create already exists.

A remediation draft whose required work is "Create new test file:
``<path>``" is a no-op when ``<path>`` already exists at the target
branch HEAD — typically because the scan ticket that spawned it shipped
the file itself.  Running implement on it burns a pipeline pass that
correctly ends in "no change needed" (robotsix-ui ``collect.test.ts``
remediation, 2026-10-10: two wasted runs plus an investigation ticket).

:func:`check_deliverables_exist` closes such a draft as
``already satisfied`` (a non-implementation close prefix) citing the
base SHA and the commit that introduced each path.  It fires only when
EVERY named create-file deliverable exists; any missing one, an unsafe
path, or a git failure falls through to the normal refine flow.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from ...core.constants import ALREADY_SATISFIED_PREFIX
from ...scm.vcs import git_ops

# "Create new test file: `path`", "Create a new file `path`",
# "create file: path" — the deliverable named in a remediation draft.
_CREATE_FILE_RE = re.compile(
    r"\bcreate\s+(?:an?\s+)?(?:new\s+)?(?:test\s+|source\s+)?file\s*:?\s*"
    r"`?(?P<path>[\w./-]+\.[A-Za-z0-9]{1,10})`?",
    re.IGNORECASE,
)


def extract_create_file_paths(draft: str) -> list[str]:
    """Repo-relative paths a *draft* says to create, in order, deduplicated.

    Absolute paths and paths with ``..`` segments are dropped — they never
    name an in-repo deliverable and must not reach a git pathspec.
    """
    out: list[str] = []
    for m in _CREATE_FILE_RE.finditer(draft or ""):
        raw = m.group("path").strip().rstrip(".")
        raw = raw.removeprefix("./")
        pure = PurePosixPath(raw)
        if not raw or pure.is_absolute() or ".." in pure.parts:
            continue
        if raw not in out:
            out.append(raw)
    return out


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
    )


def check_deliverables_exist(
    draft: str, repo_dir: Path | None, target_branch: str
) -> str | None:
    """Return an ``already satisfied`` close note, or ``None`` to proceed.

    The note is returned only when the draft names at least one file to
    create and every such path exists at ``origin/<target_branch>``.
    """
    if repo_dir is None:
        return None
    paths = extract_create_file_paths(draft)
    if not paths:
        return None
    base_sha = git_ops.refresh_origin_target(repo_dir, target_branch)
    if not base_sha:
        return None
    ref = f"origin/{target_branch}"
    found: list[str] = []
    try:
        for path in paths:
            if _git(repo_dir, "cat-file", "-e", f"{ref}:{path}").returncode != 0:
                return None
            intro = _git(
                repo_dir,
                "log",
                "--diff-filter=A",
                "--format=%H",
                "-1",
                ref,
                "--",
                path,
            ).stdout.strip()
            found.append(f"`{path}` (added in {intro[:12]})" if intro else f"`{path}`")
    except Exception:
        return None
    return (
        f"{ALREADY_SATISFIED_PREFIX} — deliverable already exists at "
        f"{ref} (base {base_sha[:12]}): {', '.join(found)}; nothing to create"
    )
