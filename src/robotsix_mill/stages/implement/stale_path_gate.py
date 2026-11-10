"""Post-move stale-reference sweep for implement passes that move files.

A module reorg (flat ``foo.py`` → ``foo/`` package, tests pooled into
``tests/<pkg>/<module>/``) is specced with a hand-maintained list of
files to fix up, and references outside that list — ``.secrets.baseline``,
``pyproject.toml`` per-file-ignores, ``ARCHITECTURE.md``,
``vulture_whitelist.py``, docstrings — used to survive the merge and
spawn follow-up "fix stale paths" tickets.

This gate makes the sweep deterministic: every file the pass RENAMED
contributes its old repo-relative path (plus the ``src/``-stripped form)
and, for Python files, its old dotted module name.  Each is grepped
across the whole working tree (tracked + untracked, ``CHANGELOG.md``
excluded); any hit sends the pass back with the ``file:line`` list, and
a second failure blocks the ticket so a stale reference never merges.
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
from pathlib import Path

from ...core.models import Ticket
from ...core.states import State
from ..base import Outcome
from ._shared import _ImplementContext, _SinglePassResult, log

# Feedback prefix for the stale-path re-prompt; seeing it on the incoming
# feedback means the agent already had its one retry.
STALE_PATHS_MARKER = "[STALE-PATHS]"

# Max hits listed in the feedback / block note.
_MAX_REPORTED_HITS = 40

# Pathspecs never swept: the changelog legitimately records old paths.
_EXCLUDE_PATHSPECS = (
    ":(exclude)CHANGELOG.md",
    ":(exclude,glob)**/CHANGELOG.md",
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _git(
    repo_dir: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except OSError, subprocess.SubprocessError:
        return None


def moved_files(repo_dir: Path, target_branch: str) -> list[tuple[str, str]] | None:
    """Return ``(old, new)`` pairs for every file the branch + working tree
    renames relative to its merge base with ``origin/<target_branch>``.

    Uses a throw-away index (``git add -A`` into a temp ``GIT_INDEX_FILE``)
    so moves done as delete + untracked-create are detected exactly like
    ``git mv``, without touching the real index.  Returns ``None`` on any
    git failure (the gate then fails open).
    """
    base = _git(repo_dir, "merge-base", f"origin/{target_branch}", "HEAD")
    if base is None or base.returncode != 0 or not base.stdout.strip():
        return None
    index_path = _git(repo_dir, "rev-parse", "--git-path", "mill-stale-sweep.index")
    if index_path is None or index_path.returncode != 0:
        return None
    tmp_index = Path(index_path.stdout.strip())
    if not tmp_index.is_absolute():
        tmp_index = repo_dir / tmp_index
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
    try:
        for args in (("read-tree", "HEAD"), ("add", "-A")):
            res = _git(repo_dir, *args, env=env)
            if res is None or res.returncode != 0:
                return None
        diff = _git(
            repo_dir,
            "diff",
            "--cached",
            "-M",
            "--name-status",
            "--diff-filter=R",
            base.stdout.strip(),
            env=env,
        )
    finally:
        tmp_index.unlink(missing_ok=True)
    if diff is None or diff.returncode != 0:
        return None
    pairs: list[tuple[str, str]] = []
    for line in diff.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            pairs.append((parts[1], parts[2]))
    return pairs


def _dotted_name(path: str) -> str | None:
    """Old dotted module name for a ``.py`` path, or ``None``.

    ``src/pkg/a/b.py`` → ``pkg.a.b``; ``tests/test_x.py`` →
    ``tests.test_x``; ``pkg/sub/__init__.py`` → ``pkg.sub``.  Single-
    component names (``foo``, a bare package) are too broad to grep and
    yield ``None``.
    """
    if not path.endswith(".py"):
        return None
    stem = path.removeprefix("src/").removesuffix(".py")
    parts = stem.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if len(parts) < 2 or not all(_IDENT_RE.match(p) for p in parts):
        return None
    return ".".join(parts)


def _module_still_exists(repo_dir: Path, old_path: str) -> bool:
    """True when the old path's dotted module is still importable — e.g.
    a flat ``foo.py`` became ``foo/__init__.py`` (same dotted name).
    """
    stem = old_path.removesuffix(".py")
    if stem.endswith("/__init__"):
        stem = stem.removesuffix("/__init__")
    return (repo_dir / f"{stem}.py").is_file() or (
        repo_dir / stem / "__init__.py"
    ).is_file()


def stale_terms(repo_dir: Path, moves: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Build ``(term, ERE pattern)`` pairs to sweep for the given moves."""
    terms: dict[str, str] = {}
    for old, _new in moves:
        if (repo_dir / old).exists():
            continue
        path_forms = [old]
        if old.startswith("src/") and not (repo_dir / old[4:]).exists():
            path_forms.append(old[4:])
        for form in path_forms:
            terms[form] = (
                r"(^|[^A-Za-z0-9_./-])" + re.escape(form) + r"([^A-Za-z0-9_/-]|$)"
            )
        dotted = _dotted_name(old)
        if dotted and not _module_still_exists(repo_dir, old):
            terms[dotted] = (
                r"(^|[^A-Za-z0-9_.])" + re.escape(dotted) + r"([^A-Za-z0-9_]|$)"
            )
    return list(terms.items())


def find_stale_references(
    repo_dir: Path, target_branch: str
) -> list[tuple[str, str]] | None:
    """Return ``(term, "file:line: text")`` hits for references to moved
    files' old paths / module names anywhere in the working tree.

    ``None`` when the sweep could not run (git failure); ``[]`` when there
    is nothing stale (including when the pass moved no file).
    """
    moves = moved_files(repo_dir, target_branch)
    if moves is None:
        return None
    hits: list[tuple[str, str]] = []
    for term, pattern in stale_terms(repo_dir, moves):
        res = _git(
            repo_dir,
            "grep",
            "-n",
            "-I",
            "--untracked",
            "-E",
            "-e",
            pattern,
            "--",
            ".",
            *_EXCLUDE_PATHSPECS,
        )
        if res is None or res.returncode not in (0, 1):
            return None
        for line in res.stdout.splitlines():
            if line:
                hits.append((term, line.strip()[:200]))
    return hits


def _render_hits(hits: list[tuple[str, str]]) -> str:
    shown = [f"- `{term}` → {loc}" for term, loc in hits[:_MAX_REPORTED_HITS]]
    if len(hits) > _MAX_REPORTED_HITS:
        shown.append(f"- … and {len(hits) - _MAX_REPORTED_HITS} more")
    return "\n".join(shown)


def run_stale_path_gate(
    ticket: Ticket,
    repo_dir: Path,
    target_branch: str,
    ic: _ImplementContext,
    new_msgs: bytes | None,
) -> _SinglePassResult | None:
    """Keep a move that leaves stale old-path references out of review.

    Returns ``None`` when nothing stale remains (or the sweep could not
    run — fail open).  Otherwise re-prompts once with a
    ``[STALE-PATHS]`` feedback block listing every ``file:line`` hit;
    if hits remain on that re-prompt, the ticket is blocked.
    """
    hits = find_stale_references(repo_dir, target_branch)
    if not hits:
        return None
    listing = _render_hits(hits)
    if (ic.feedback or "").startswith(STALE_PATHS_MARKER):
        log.warning(
            "%s: %d stale moved-path reference(s) remain after re-prompt; blocking",
            ticket.id,
            len(hits),
        )
        return _SinglePassResult(
            next_action="return",
            outcome=Outcome(
                State.BLOCKED,
                "stale-path sweep failed after retry — references to moved "
                f"files' old paths remain (needs human review):\n{listing}",
            ),
        )
    log.warning(
        "%s: %d stale moved-path reference(s) found; re-prompting",
        ticket.id,
        len(hits),
    )
    feedback = (
        f"{STALE_PATHS_MARKER} The post-move stale-reference sweep is "
        "authoritative over any hand-maintained path list in the spec. "
        "These lines still reference the OLD path or dotted module name "
        "of a file this change moved — update every one to the new "
        "location (or drop it) in this same change. For "
        "`.secrets.baseline`, re-run the baseline update "
        "(`detect-secrets scan --baseline .secrets.baseline`) instead of "
        f"hand-editing it.\n{listing}"
    )
    return _SinglePassResult(
        next_action="retry",
        feedback=feedback,
        ic=dataclasses.replace(ic, feedback=feedback),
        new_msgs=new_msgs,
    )
