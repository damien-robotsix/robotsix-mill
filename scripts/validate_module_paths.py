#!/usr/bin/env python3
"""Validate that every path in docs/modules.yaml resolves to a real file.

Usage (from the repo root):
    python scripts/validate_module_paths.py

Path semantics: every pattern listed under ``modules[*].paths`` in
``docs/modules.yaml`` is resolved relative to the current working
directory.  This script is meant to be invoked from the repo root
(which is what CI and the ``validate-module-paths`` pre-commit hook
both guarantee).

Classification rules:
    * Literal pattern (no ``*`` and no ``?``): fails if the path does
      not exist on disk.
    * Glob pattern (contains ``*`` or ``?``): fails if ``glob.glob``
      with ``recursive=True`` returns an empty list.

Exit codes:
    0 — every listed path resolves to at least one real file.
    1 — at least one path is stale; details are printed to stderr.
"""

from __future__ import annotations

import glob
import os.path
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULES_YAML = _REPO_ROOT / "docs" / "modules.yaml"


def _is_glob(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern


def find_stale_paths(modules_yaml_path: Path) -> list[str]:
    """Return a list of ``"module_id: pattern"`` strings for every path
    that does not exist (literal) or matches no files (glob).

    An empty list means every listed path resolves to at least one
    real file on disk.
    """

    with open(modules_yaml_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    stale: list[str] = []
    for module in data.get("modules", []):
        module_id = module.get("id", "<unknown>")
        for pattern in module.get("paths", []):
            if _is_glob(pattern):
                matches = glob.glob(pattern, recursive=True)
                if not matches:
                    stale.append(f"{module_id}: {pattern}")
            else:
                if not os.path.exists(pattern):
                    stale.append(f"{module_id}: {pattern}")
    return stale


def main() -> int:
    with open(_MODULES_YAML, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    total_literal = 0
    total_glob = 0
    for module in data.get("modules", []):
        for pattern in module.get("paths", []):
            if _is_glob(pattern):
                total_glob += 1
            else:
                total_literal += 1

    stale = find_stale_paths(_MODULES_YAML)
    if stale:
        for entry in stale:
            print(f"STALE: {entry}", file=sys.stderr)
        print(
            f"FAIL: {len(stale)} stale path(s) in docs/modules.yaml",
            file=sys.stderr,
        )
        return 1

    print(
        f"module paths OK ({total_literal} literal, {total_glob} glob patterns validated)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
