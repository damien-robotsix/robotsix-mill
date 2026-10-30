"""Every Python source and test file must be claimed by docs/modules.yaml.

AGENT.md ("Adding a tracked file") requires each new ``src/`` and
``tests/`` file to be registered under its module in
``docs/modules.yaml``.  This test walks both trees on disk and fails
listing every ``.py`` file no module's ``paths`` globs claim.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from robotsix_modules.validation.registration import check_registration

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MODULES_YAML = _REPO_ROOT / "docs" / "modules.yaml"
_COVERED_DIRS = ("src", "tests")


def _python_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for d in _COVERED_DIRS
        for p in (root / d).rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _unclassified(taxonomy: dict, root: Path) -> list[str]:
    findings = check_registration(taxonomy, root, tracked_files=_python_files(root))
    return [f.file for f in findings if f.kind == "unclassified_file" and f.file]


def test_every_src_and_test_file_is_registered() -> None:
    taxonomy = yaml.safe_load(_MODULES_YAML.read_text())
    missing = _unclassified(taxonomy, _REPO_ROOT)
    assert missing == [], (
        "Files missing from docs/modules.yaml — add each to its module's "
        "`paths`:\n  " + "\n  ".join(missing)
    )


def test_unlisted_file_is_detected(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "pkg" / "listed.py").write_text("")
    (tmp_path / "src" / "pkg" / "unlisted.py").write_text("")
    (tmp_path / "tests" / "test_listed.py").write_text("")
    taxonomy = {
        "modules": [
            {
                "id": "pkg",
                "description": "synthetic",
                "paths": ["src/pkg/listed.py", "tests/test_listed.py"],
                "dependencies": [],
            }
        ]
    }
    assert _unclassified(taxonomy, tmp_path) == ["src/pkg/unlisted.py"]
