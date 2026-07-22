#!/usr/bin/env python3
"""Config-standard file-footprint validator for pre-merge CI.

Usage (from the repo root):
    python scripts/check_config_standard_footprint.py [--base REF]

Detects config-standard compliance PRs by checking whether the diff
introduces any of the canonical four files.  When it does, **every**
newly-added file in the diff must fall within the approved footprint;
any out-of-footprint addition fails the check with a clear message.

The **canonical four-file footprint** for a config-standard-compliant
repo:

    1. ``config/config.json``
    2. ``config/config.schema.json``
    3. ``deploy/docker-compose.yml``
    4. ``CHANGELOG.md``

The canonical standard/doc sources are ``robotsix-config`` and
``robotsix-standards`` only — individual repos must **NOT** carry
local ``_standards/`` copies.

Exit codes:
    0 — no config-standard violation (or no config-standard PR detected).
    1 — at least one out-of-footprint file detected; details printed to stderr.
    2 — git error (unable to determine base ref or diff).
"""

from __future__ import annotations

import argparse
import subprocess
import sys

# ---------------------------------------------------------------------------
#  Canonical config-standard footprint
# ---------------------------------------------------------------------------

_CONFIG_STANDARD_FOOTPRINT: frozenset[str] = frozenset(
    {
        "config/config.json",
        "config/config.schema.json",
        "deploy/docker-compose.yml",
        "CHANGELOG.md",
    }
)


def _run_git(args: list[str]) -> str:
    """Run a git command and return its stdout, stripped.

    Raises :class:`subprocess.CalledProcessError` on failure.
    """
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _resolve_base_ref(base_ref: str | None) -> str:
    """Resolve the base ref to diff against.

    When *base_ref* is not provided, tries ``origin/main`` first (the
    common case for PRs), then falls back to the merge-base of HEAD and
    ``origin/main``.
    """
    if base_ref:
        return base_ref

    # In CI the base is typically origin/main.
    try:
        _run_git(["rev-parse", "--verify", "origin/main"])
        return "origin/main"
    except subprocess.CalledProcessError:
        pass

    # Fallback: try to find a merge base.
    try:
        return _run_git(["merge-base", "HEAD", "origin/main"]).strip()
    except subprocess.CalledProcessError:
        pass

    # Last resort: diff against the parent commit.
    return "HEAD~1"


def _added_files(base_ref: str) -> list[str]:
    """Return paths of files added (A) between *base_ref* and HEAD."""
    try:
        output = _run_git(
            ["diff", "--name-status", "--diff-filter=A", f"{base_ref}...HEAD"]
        )
    except subprocess.CalledProcessError:
        output = _run_git(
            ["diff", "--name-status", "--diff-filter=A", base_ref, "HEAD"]
        )

    added: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # --name-status output: "<status>\t<path>"
        if "\t" in line:
            status, path = line.split("\t", 1)
            if status == "A":
                added.append(path)
        elif line.startswith("A\t"):
            added.append(line[2:].lstrip("\t"))
        else:
            # If the line has no tab, it's a bare path (possible with
            # --name-only, though we use --name-status).
            added.append(line)
    return added


def _is_config_standard_pr(added: list[str]) -> bool:
    """True when the diff touches at least one config-standard footprint file."""
    return any(f in _CONFIG_STANDARD_FOOTPRINT for f in added)


def _check_footprint(added: list[str]) -> list[str]:
    """Return out-of-footprint added files, or an empty list when clean."""
    violations: list[str] = []
    for path in added:
        if path not in _CONFIG_STANDARD_FOOTPRINT:
            violations.append(path)
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate config-standard file footprint in the PR diff."
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Base ref to diff against (default: origin/main or merge-base).",
    )
    args = parser.parse_args()

    try:
        base_ref = _resolve_base_ref(args.base)
    except subprocess.CalledProcessError as e:
        print(
            f"git: unable to resolve base ref: {e.stderr.strip() or e}",
            file=sys.stderr,
        )
        return 2

    try:
        added = _added_files(base_ref)
    except subprocess.CalledProcessError as e:
        print(
            f"git: unable to diff against {base_ref}: {e.stderr.strip() or e}",
            file=sys.stderr,
        )
        return 2

    if not _is_config_standard_pr(added):
        print("config-standard footprint: not a config-standard PR — skipping check")
        return 0

    violations = _check_footprint(added)
    if not violations:
        print("config-standard footprint: OK (all added files within footprint)")
        return 0

    print(
        "FAIL: config-standard footprint violation — the following files",
        file=sys.stderr,
    )
    print("are outside the approved four-file footprint:", file=sys.stderr)
    print(file=sys.stderr)
    for v in sorted(violations):
        print(f"  - {v}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "A config-standard compliance PR may only add files from this list:",
        file=sys.stderr,
    )
    for f in sorted(_CONFIG_STANDARD_FOOTPRINT):
        print(f"  - {f}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "The canonical standard/doc sources are robotsix-config and "
        "robotsix-standards only — individual repos must NOT carry "
        "local _standards/ copies.",
        file=sys.stderr,
    )

    return 1


if __name__ == "__main__":
    sys.exit(main())
