"""Coherent-set resolver.

Given a :class:`DependencyGraph`, computes consistent target SHAs for
every repo.  The resolver works bottom-up (topological order): it
resolves the latest main-branch SHA for each leaf, then for each
dependent, rewrites its ``[tool.uv.sources]`` pin to point at that SHA
and runs ``uv lock`` to prove consistency.

The whole point is to avoid the ``Requirements contain conflicting
URLs`` failure mode that a naïve ``sed`` loop would hit: when repo A
depends on B at rev X, and B depends on C at rev Y, A's transitive
resolution of C must also resolve to Y.  ``uv lock`` empirically
verifies this for every dependent before we commit to a bump set.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .models import (
    DependencyGraph,
    PinBump,
    RepoNode,
    normalize_git_url,
    url_matches_repo,
)

log = logging.getLogger("robotsix_mill.dependency_graph.resolver")


def _run_uv_lock(repo_path: Path) -> tuple[bool, str]:
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


_CONFLICTING_URLS_MARKER = "Requirements contain conflicting URLs"


def _is_conflicting_urls_error(stderr: str) -> bool:
    """True when *stderr* contains the known conflicting-URLs message."""
    return _CONFLICTING_URLS_MARKER in stderr


# ---------------------------------------------------------------------------
# pyproject.toml rewriting (surgical edit, not a full re-serialization)
# ---------------------------------------------------------------------------

# Explanation: tomllib (stdlib, 3.11+) only reads — there is no tomllib.dump.
# A full write-out with a third-party TOML writer would strip comments and
# reformat the file, making the diff unreadable and breaking human-maintained
# groupings.  Instead we do a RE-based textual replacement of the single
# ``rev = "..."`` line inside the matching ``[tool.uv.sources.<package>]``
# block.  This preserves formatting, comments, and ordering exactly, and
# produces the smallest possible diff.

import re as _re  # noqa: E402


def _replace_pin_rev(
    content: str,
    package: str,
    new_rev: str,
    old_rev: str | None = None,
) -> str | None:
    """Replace the ``rev`` value for *package* in *content*.

    Returns the updated TOML text, or ``None`` when the package block
    isn't found.  Preserves all formatting and comments.

    Args:
        content: Full ``pyproject.toml`` text.
        package: The source key name (e.g. ``"robotsix-mill"``).
        new_rev: The full SHA to write.
        old_rev: The current rev; used only for the log message.
    """
    # Find the [tool.uv.sources.<package>] header.
    header_pat = _re.compile(
        r"^\[tool\.uv\.sources\." + _re.escape(package) + r"\]\s*$",
        _re.MULTILINE,
    )
    m = header_pat.search(content)
    if not m:
        log.warning("pin_bump: [tool.uv.sources.%s] header not found", package)
        return None

    block_start = m.end()
    # Find the next TOML section header or EOF.
    next_header = _re.compile(r"^\[", _re.MULTILINE)
    m2 = next_header.search(content, block_start)
    block_end = m2.start() if m2 else len(content)

    block = content[block_start:block_end]

    # Replace the rev line.
    rev_pat = _re.compile(r'^(\s*rev\s*=\s*")[^"]*(".*)$', _re.MULTILINE)
    new_block, count = rev_pat.subn(rf"\g<1>{new_rev}\g<2>", block)
    if count == 0:
        # No rev line — the pin is unpinned.  Insert a rev line after
        # the ``git = "..."`` line.
        git_pat = _re.compile(r'^(\s*git\s*=\s*".*")\s*$', _re.MULTILINE)
        new_block, count2 = git_pat.subn(
            rf"\1\nrev = \"{new_rev}\"",
            block,
        )
        if count2 == 0:
            log.warning("pin_bump: no git line in [tool.uv.sources.%s]", package)
            return None

    return content[:block_start] + new_block + content[block_end:]


def _apply_bump(repo_path: Path, bump: PinBump) -> bool:
    """Rewrite the pyproject.toml in *repo_path* to apply *bump*.

    Returns ``True`` on success.
    """
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        original = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    updated = _replace_pin_rev(original, bump.package, bump.new_rev, bump.old_rev)
    if updated is None:
        return False
    try:
        pyproject.write_text(updated, encoding="utf-8")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_coherent_pins(
    graph: DependencyGraph,
    *,
    dry_run: bool = False,
) -> list[PinBump]:
    """Compute a coherent set of pin bumps across *graph*.

    Walks repos in topological order (dependencies before dependents).
    For each repo whose pins are behind their dependency's latest SHA,
    rewrites the pin, runs ``uv lock``, and on success records the
    bump.  On ``Requirements contain conflicting URLs``, skips that
    repo (logs a warning) so the rest of the set stays coherent.

    Args:
        graph: The dependency graph from :func:`~.parser.build_graph`.
        dry_run: When ``True``, compute bumps but don't write files
            or run ``uv lock``.  The returned bumps have
            ``already_current`` set and ``new_rev`` set from the
            dependency's ``latest_sha``.

    Returns:
        A list of :class:`PinBump` objects.  Only bumps where
        ``already_current is False`` need to be applied.
    """
    # Map git URL → latest known SHA (populated as we resolve).
    resolved_shas: dict[str, str] = {}
    bumps: list[PinBump] = []

    for repo_id in graph.topo_order:
        node = graph.nodes.get(repo_id)
        if node is None or node.clone_path is None:
            continue

        # Record this repo's own latest SHA so dependents can pin to it.
        own_sha = node.latest_sha
        if own_sha:
            resolved_shas[normalize_git_url(node.forge_remote_url)] = own_sha

        for pin in node.pins:
            # Find which dependency repo this pin points at.
            dep_node: RepoNode | None = None
            for other_id, other_node in graph.nodes.items():
                if other_id == repo_id:
                    continue
                if url_matches_repo(pin.git_url, other_node):
                    dep_node = other_node
                    break

            if dep_node is None:
                # This pin points at a repo NOT in the registered set
                # (external dependency).  Skip — we only bump internal
                # pins.
                continue

            dep_sha = resolved_shas.get(normalize_git_url(dep_node.forge_remote_url))
            if dep_sha is None:
                # Use the dependency's own latest_sha as fallback.
                dep_sha = dep_node.latest_sha
            if dep_sha is None:
                continue

            already = pin.rev == dep_sha
            bump = PinBump(
                repo_id=repo_id,
                package=pin.package,
                old_rev=pin.rev,
                new_rev=dep_sha,
                git_url=pin.git_url,
                already_current=already,
            )

            if already:
                bumps.append(bump)
                continue

            if dry_run:
                bumps.append(bump)
                continue

            # Apply the bump and verify with uv lock.
            if not _apply_bump(node.clone_path, bump):
                log.warning(
                    "pin_bump: failed to apply bump %s → skipping",
                    bump.description,
                )
                bumps.append(
                    PinBump(
                        repo_id=repo_id,
                        package=pin.package,
                        old_rev=pin.rev,
                        new_rev=dep_sha,
                        git_url=pin.git_url,
                        already_current=False,
                    )
                )
                continue

            ok, err = _run_uv_lock(node.clone_path)
            if ok:
                log.info("pin_bump: uv lock OK for %s", bump.description)
                bumps.append(bump)
            elif _is_conflicting_urls_error(err):
                log.warning(
                    "pin_bump: conflicting URLs for %s — skipping "
                    "(will retry next week when transitive deps "
                    "may have settled): %s",
                    bump.description,
                    err[:200],
                )
                # Revert the pyproject.toml change.
                _apply_bump(
                    node.clone_path,
                    PinBump(
                        repo_id=repo_id,
                        package=pin.package,
                        old_rev=dep_sha,
                        new_rev=pin.rev or "",
                        git_url=pin.git_url,
                    ),
                )
                # Don't add this bump — skip it.
            else:
                log.warning(
                    "pin_bump: uv lock failed for %s (non-conflict): %s",
                    bump.description,
                    err[:200],
                )
                # Revert.
                _apply_bump(
                    node.clone_path,
                    PinBump(
                        repo_id=repo_id,
                        package=pin.package,
                        old_rev=dep_sha,
                        new_rev=pin.rev or "",
                        git_url=pin.git_url,
                    ),
                )

    return bumps
