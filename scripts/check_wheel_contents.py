#!/usr/bin/env python3
"""Assert the built wheel actually ships its force-included resources.

``uv sync`` installs from the live source tree, so every CI gate passes
even when the *packaged* wheel is missing bundled data.  The
``[tool.hatch.build.targets.wheel.force-include]`` table pulls four
non-package directories (``agent_definitions``, ``expert_definitions``,
``skills``, ``contrib/completions``) into ``robotsix_mill/``.  Rename or
delete one of those source paths and the entry goes stale; delete the
resources some other way and the wheel ships without them, failing only
at runtime — after the release image is published.

The expected targets are read from the force-include table rather than
hardcoded, so a newly bundled directory is covered the moment its entry
is added.

Run after ``uv build``.
"""

from __future__ import annotations

import sys
import tomllib
import zipfile
from pathlib import Path


def force_include_targets(pyproject: Path) -> dict[str, str]:
    """Return the ``{source path: wheel target}`` force-include mapping."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    targets = (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )
    return dict(targets)


def sole_artifact(dist: Path, pattern: str) -> Path:
    """Return the single *dist* artifact matching *pattern*.

    Raises:
        FileNotFoundError: No match, or more than one (a stale artifact
            from an earlier build would make the check ambiguous).
    """
    matches = sorted(dist.glob(pattern))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"expected exactly one {pattern} in {dist}/, found {len(matches)}: "
            f"{[m.name for m in matches]} — run 'uv build' first"
        )
    return matches[0]


def missing_targets(wheel: Path, targets: dict[str, str]) -> list[str]:
    """Return a description per force-include target absent from *wheel*.

    A target counts as present only when the wheel holds at least one
    real file (not a bare directory entry) beneath it.
    """
    names = zipfile.ZipFile(wheel).namelist()
    missing: list[str] = []
    for source, target in sorted(targets.items()):
        prefix = target.rstrip("/") + "/"
        if not any(n.startswith(prefix) and not n.endswith("/") for n in names):
            missing.append(f"{source!r} -> {target!r} (no files under {prefix})")
    return missing


def main(root: Path | None = None) -> int:
    """Check the wheel in ``<root>/dist`` against ``<root>/pyproject.toml``."""
    root = root or Path()
    targets = force_include_targets(root / "pyproject.toml")
    if not targets:
        print(
            "no [tool.hatch.build.targets.wheel.force-include] entries found — "
            "this check would silently pass; fix the lookup or drop the check",
            file=sys.stderr,
        )
        return 1

    dist = root / "dist"
    try:
        wheel = sole_artifact(dist, "*.whl")
        sole_artifact(dist, "*.tar.gz")  # the sdist must build too
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    missing = missing_targets(wheel, targets)
    if missing:
        print(f"{wheel.name} is missing force-included resources:", file=sys.stderr)
        for line in missing:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nEither the source path was renamed/deleted without updating "
            "[tool.hatch.build.targets.wheel.force-include], or the build "
            "backend dropped it.",
            file=sys.stderr,
        )
        return 1

    plural = "y" if len(targets) == 1 else "ies"
    print(
        f"{wheel.name}: all {len(targets)} force-included resource "
        f"director{plural} present."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
