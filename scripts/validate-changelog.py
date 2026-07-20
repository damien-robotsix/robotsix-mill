#!/usr/bin/env python3
"""Validate changelog fragment files before commit — CLI wrapper.

Thin wrapper that imports ``validate_changelog`` from
``robotsix_mill.stages._changelog_validate``.
See that module for the full implementation.

Can be invoked for ad-hoc validation::

    python scripts/validate-changelog.py [repo-dir]

Exit code 0 means clean (or auto-fixed).  Exit code 1 means a
non-recoverable error was encountered.
"""

from __future__ import annotations

import sys
from pathlib import Path

from robotsix_mill.stages._changelog_validate import validate_changelog


def main() -> int:
    repo_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    msgs = validate_changelog(repo_dir)
    for m in msgs:
        print(f"validate-changelog: {m}", file=sys.stderr)
    if msgs:
        print(f"validate-changelog: {len(msgs)} issue(s) auto-fixed", file=sys.stderr)
    return 0  # Always succeed — issues are auto-fixed.


if __name__ == "__main__":
    raise SystemExit(main())
