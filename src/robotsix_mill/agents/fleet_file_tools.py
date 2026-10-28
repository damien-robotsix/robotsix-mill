"""Read-only cross-repo file access for registered fleet repos.

``read_fleet_file`` and ``grep_fleet_repo`` let the review and implement
agents verify a claim about another fleet repo's file (e.g. a shared
workflow in ``robotsix-github-workflows``) instead of parking the ticket
at ``awaiting_user_reply`` (standards ticket 7820: "a human should confirm
docker-release.yml runs ``--provenance mode=max``").

Constraints (operator-approved):

- READ-ONLY: only forge GETs (contents / git tree) with the existing App
  token — no write scopes, no new secrets.
- Only repos registered in ``repos_config``; anything else is refused.
- ``ref`` defaults to the repo's default branch.
- Hard caps: 256 KB per read, 50 results per grep.
- Every call is logged with ticket id + repo + path.

The tools run HOST-SIDE in the mill process (like ``fetch_ci_logs``); the
agent sandbox's network policy is unchanged.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from ..config import Settings
from ..runtime.tracing import trace_stage

log = logging.getLogger(__name__)

# Hard cap on the bytes a single read_fleet_file call returns.
READ_MAX_BYTES = 256 * 1024
# Hard cap on the matches a single grep_fleet_repo call returns.
GREP_MAX_RESULTS = 50
# Files a single grep fetches at most (each read is itself capped).
GREP_MAX_FILES = 100
# Long matched lines are clipped so 50 results stay small.
_GREP_LINE_CHARS = 300


def _registered_repos() -> dict[str, Any]:
    from ..config import get_repos_config

    return dict(get_repos_config().repos)


def _clean_path(path: str) -> str | None:
    """Repo-relative POSIX path, or ``None`` when it escapes the repo root."""
    p = (path or "").strip().lstrip("/")
    if not p or "\\" in p or any(part == ".." for part in PurePosixPath(p).parts):
        return None
    return p


def _cap_text(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= READ_MAX_BYTES:
        return text
    return (
        raw[:READ_MAX_BYTES].decode("utf-8", "ignore")
        + f"\n\n[read_fleet_file: truncated — the file is {len(raw):,} bytes, "
        f"capped at {READ_MAX_BYTES:,}. Use grep_fleet_repo to locate the "
        "relevant lines.]"
    )


def _forge_for(settings: Settings, repo_id: str) -> tuple[Any, str]:
    """``(forge, "")`` for a registered repo, else ``(None, error)``."""
    repos = _registered_repos()
    rc = repos.get(repo_id)
    if rc is None:
        return None, (
            f"error: {repo_id!r} is not a registered fleet repo. "
            f"Registered repo_ids: {', '.join(sorted(repos)) or '(none)'}"
        )
    if not rc.forge_remote_url:
        return None, f"error: fleet repo {repo_id!r} has no forge_remote_url"
    from ..scm.forge.base import get_forge

    return get_forge(settings, repo_config=rc), ""


def _grep_files(forge: Any, rx: re.Pattern[str], files: list[str]) -> list[str]:
    """Up to ``GREP_MAX_RESULTS`` ``path:line: text`` matches of *rx*."""
    results: list[str] = []
    for f in files:
        text = forge.read_repo_file(path=f)
        if not text:
            continue
        for n, line in enumerate(_cap_text(text).splitlines(), 1):
            if rx.search(line):
                results.append(f"{f}:{n}: {line[:_GREP_LINE_CHARS]}")
                if len(results) >= GREP_MAX_RESULTS:
                    return results
    return results


def _read(settings: Settings, repo_id: str, path: str, ref: str) -> str:
    clean = _clean_path(path)
    if clean is None:
        return f"error: invalid repo-relative path {path!r}"
    forge, err = _forge_for(settings, repo_id)
    if forge is None:
        return err
    text = forge.read_repo_file(path=clean, ref=ref)
    if text is None:
        return (
            f"error: could not read {clean!r} from {repo_id!r} at "
            f"{ref or 'the default branch'} (missing file, directory "
            "or forge error)"
        )
    return _cap_text(text)


def _grep(settings: Settings, repo_id: str, pattern: str, path_glob: str) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"error: invalid regex {pattern!r}: {exc}"
    forge, err = _forge_for(settings, repo_id)
    if forge is None:
        return err
    files = forge.list_repo_files()
    if files is None:
        return f"error: could not list files of {repo_id!r}"
    candidates = [f for f in files if fnmatch.fnmatch(f, path_glob or "*")]
    notes: list[str] = []
    if len(candidates) > GREP_MAX_FILES:
        notes.append(
            f"[grep_fleet_repo: {len(candidates)} files match "
            f"{path_glob!r}; only the first {GREP_MAX_FILES} were "
            "searched — narrow path_glob.]"
        )
        candidates = candidates[:GREP_MAX_FILES]
    results = _grep_files(forge, rx, candidates)
    if not results:
        return (
            f"(no match for {pattern!r} in {len(candidates)} file(s) "
            f"of {repo_id!r} matching {path_glob!r})"
        )
    if len(results) >= GREP_MAX_RESULTS:
        notes.append(
            f"[grep_fleet_repo: stopped at {GREP_MAX_RESULTS} "
            "results — narrow pattern or path_glob.]"
        )
    return "\n".join(results + notes)


def build_fleet_file_tools(
    settings: Settings,
    *,
    ticket_id: str = "",
    stage: str = "",
) -> list[Callable[..., Any]]:
    """Build the ``read_fleet_file`` / ``grep_fleet_repo`` tool closures.

    *ticket_id* and *stage* are recorded in the audit log line of every
    call.
    """

    def _audit(tool: str, repo_id: str, path: str, ref: str) -> None:
        # %r neutralises newlines in the model-supplied values.
        log.info(
            "%s: ticket=%r stage=%r repo=%r path=%r ref=%r",
            tool,
            ticket_id,
            stage,
            repo_id,
            path,
            ref or "<default>",
        )

    def read_fleet_file(repo_id: str, path: str, ref: str = "") -> str:
        """Read a file from another registered fleet repo (read-only).

        Use it to verify what a fleet repo's file actually says (e.g.
        ``read_fleet_file("robotsix-github-workflows",
        ".github/workflows/docker-release.yml")``) instead of asking a
        human. *repo_id* must be a repo registered with the mill; *path*
        is repo-relative; *ref* (branch, tag or SHA) defaults to the repo's
        default branch. Output is capped at 256 KB.
        """
        with trace_stage("read_fleet_file"):
            _audit("read_fleet_file", repo_id, path, ref)
            return _read(settings, repo_id, path, ref)

    def grep_fleet_repo(repo_id: str, pattern: str, path_glob: str = "*") -> str:
        """Search files of another registered fleet repo with a regex
        (read-only, default branch).

        *pattern* is a Python regular expression matched per line;
        *path_glob* (fnmatch, e.g. ``.github/workflows/*.yml``) narrows
        the files searched. Returns up to 50 ``path:line: text`` matches.
        """
        with trace_stage("grep_fleet_repo"):
            _audit("grep_fleet_repo", repo_id, path_glob, "")
            return _grep(settings, repo_id, pattern, path_glob)

    return [read_fleet_file, grep_fleet_repo]


# Register in the system-wide capability catalog so the prompt-tool-consistency
# guard and smoke tests recognise the tool names.
from .tool_registry import ToolInfo, ToolRegistry

ToolRegistry.register(
    ToolInfo(
        name="read_fleet_file",
        description=(
            "Read-only: read a file from another fleet repo registered with "
            "the mill (default branch unless ref is given; capped at 256 KB)."
        ),
        category="git",
        parameters={"repo_id": "str", "path": "str", "ref": "str"},
    )
)
ToolRegistry.register(
    ToolInfo(
        name="grep_fleet_repo",
        description=(
            "Read-only: regex-search files of another registered fleet repo "
            "(default branch; up to 50 path:line matches)."
        ),
        category="git",
        parameters={"repo_id": "str", "pattern": "str", "path_glob": "str"},
    )
)
