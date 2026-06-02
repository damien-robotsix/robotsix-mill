"""Regression tests for scripts/validate_module_paths.py.

Covers:
    * Happy path against the real docs/modules.yaml — every literal
      path must exist and every glob must match at least one file.
    * Stale literal path is detected.
    * Empty glob pattern is detected.
    * A synthetic clean modules.yaml passes.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "validate_module_paths.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_module_paths", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("validate_module_paths", module)
    spec.loader.exec_module(module)
    return module


_validator = _load_validator()
find_stale_paths = _validator.find_stale_paths


@pytest.fixture
def at_repo_root(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test with the repo root as the current working directory.

    The validator resolves patterns relative to ``os.getcwd()``, so all
    cases that exercise the real on-disk state need this fixture.
    """

    monkeypatch.chdir(_REPO_ROOT)
    return _REPO_ROOT


def test_real_modules_yaml_has_no_stale_paths(at_repo_root: Path) -> None:
    modules_yaml = at_repo_root / "docs" / "modules.yaml"
    stale = find_stale_paths(modules_yaml)
    assert stale == [], f"docs/modules.yaml has stale paths: {stale}"


def test_stale_literal_path_is_detected(tmp_path: Path, at_repo_root: Path) -> None:
    yaml_path = tmp_path / "modules.yaml"
    yaml_path.write_text(
        "modules:\n"
        "  - id: synthetic\n"
        "    description: synthetic test module\n"
        "    paths:\n"
        "      - nonexistent/file.py\n"
    )
    stale = find_stale_paths(yaml_path)
    assert stale, "expected at least one stale path entry"
    assert any("nonexistent/file.py" in entry for entry in stale)
    assert any("synthetic" in entry for entry in stale)


def test_empty_glob_is_detected(tmp_path: Path, at_repo_root: Path) -> None:
    yaml_path = tmp_path / "modules.yaml"
    yaml_path.write_text(
        "modules:\n"
        "  - id: synthetic\n"
        "    description: synthetic test module\n"
        "    paths:\n"
        "      - nonexistent/**/*.py\n"
    )
    stale = find_stale_paths(yaml_path)
    assert stale, "expected at least one stale glob entry"
    assert any("nonexistent/**/*.py" in entry for entry in stale)


def test_clean_modules_yaml_passes(tmp_path: Path, at_repo_root: Path) -> None:
    yaml_path = tmp_path / "modules.yaml"
    yaml_path.write_text(
        "modules:\n"
        "  - id: synthetic\n"
        "    description: synthetic test module\n"
        "    paths:\n"
        "      - pyproject.toml\n"
        "      - docs/*.md\n"
    )
    stale = find_stale_paths(yaml_path)
    assert stale == [], f"expected no stale paths, got {stale}"


def test_at_repo_root_is_actually_repo_root(at_repo_root: Path) -> None:
    # Sanity check the fixture: pyproject.toml exists at the chdir target.
    assert os.path.exists("pyproject.toml")
    assert at_repo_root == _REPO_ROOT
