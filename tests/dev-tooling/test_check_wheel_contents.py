"""Regression tests for scripts/check_wheel_contents.py.

Covers:
    * The real repo's force-include table is non-empty (a silent-pass guard
      on the lookup path itself).
    * A wheel holding every target passes.
    * A wheel with one target stripped out fails and names it.
    * Bare directory entries do not count as resources.
    * Missing / ambiguous dist artifacts are reported, not ignored.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_wheel_contents.py"

_checker = load_script(_SCRIPT_PATH)

force_include_targets = _checker.force_include_targets
missing_targets = _checker.missing_targets
sole_artifact = _checker.sole_artifact
main = _checker.main

_TARGETS = {
    "agent_definitions": "robotsix_mill/agent_definitions",
    "skills": "robotsix_mill/skills",
}


def _make_wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as whl:
        for name in names:
            whl.writestr(name, b"x")
    return path


# ---------------------------------------------------------------------------
#  Real repo state
# ---------------------------------------------------------------------------


def test_real_pyproject_declares_force_include_targets() -> None:
    """The lookup must find real entries, else the check passes vacuously."""
    targets = force_include_targets(_REPO_ROOT / "pyproject.toml")
    assert targets, "force-include table not found — the check would no-op"
    assert "agent_definitions" in targets


# ---------------------------------------------------------------------------
#  missing_targets
# ---------------------------------------------------------------------------


def test_all_targets_present(tmp_path: Path) -> None:
    wheel = _make_wheel(
        tmp_path / "pkg.whl",
        [
            "robotsix_mill/__init__.py",
            "robotsix_mill/agent_definitions/implement.yaml",
            "robotsix_mill/skills/review.md",
        ],
    )

    assert missing_targets(wheel, _TARGETS) == []


def test_stripped_target_is_reported(tmp_path: Path) -> None:
    wheel = _make_wheel(
        tmp_path / "pkg.whl",
        [
            "robotsix_mill/__init__.py",
            "robotsix_mill/agent_definitions/implement.yaml",
        ],
    )

    missing = missing_targets(wheel, _TARGETS)

    assert len(missing) == 1
    assert "'skills'" in missing[0]
    assert "robotsix_mill/skills/" in missing[0]


def test_bare_directory_entry_does_not_count(tmp_path: Path) -> None:
    """A directory entry with no files under it is still 'missing'."""
    wheel = _make_wheel(
        tmp_path / "pkg.whl",
        [
            "robotsix_mill/agent_definitions/implement.yaml",
            "robotsix_mill/skills/",
        ],
    )

    missing = missing_targets(wheel, _TARGETS)

    assert len(missing) == 1
    assert "'skills'" in missing[0]


def test_trailing_slash_in_target_is_tolerated(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path / "pkg.whl", ["robotsix_mill/skills/review.md"])

    assert missing_targets(wheel, {"skills": "robotsix_mill/skills/"}) == []


# ---------------------------------------------------------------------------
#  sole_artifact
# ---------------------------------------------------------------------------


def test_sole_artifact_finds_the_single_match(tmp_path: Path) -> None:
    (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"")

    assert sole_artifact(tmp_path, "*.whl").name == "pkg-1.0-py3-none-any.whl"


def test_sole_artifact_rejects_no_match(tmp_path: Path) -> None:
    try:
        sole_artifact(tmp_path, "*.whl")
    except FileNotFoundError as exc:
        assert "found 0" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected FileNotFoundError")


def test_sole_artifact_rejects_a_stale_second_artifact(tmp_path: Path) -> None:
    (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"")
    (tmp_path / "pkg-0.9-py3-none-any.whl").write_bytes(b"")

    try:
        sole_artifact(tmp_path, "*.whl")
    except FileNotFoundError as exc:
        assert "found 2" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected FileNotFoundError")


# ---------------------------------------------------------------------------
#  main
# ---------------------------------------------------------------------------


def test_main_passes_on_a_complete_wheel(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"skills" = "robotsix_mill/skills"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "pkg.whl", ["robotsix_mill/skills/review.md"])
    (dist / "pkg.tar.gz").write_bytes(b"")

    assert main(tmp_path) == 0


def test_main_fails_on_an_incomplete_wheel(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"skills" = "robotsix_mill/skills"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "pkg.whl", ["robotsix_mill/__init__.py"])
    (dist / "pkg.tar.gz").write_bytes(b"")

    assert main(tmp_path) == 1


def test_main_fails_when_the_sdist_is_absent(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"skills" = "robotsix_mill/skills"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "pkg.whl", ["robotsix_mill/skills/review.md"])

    assert main(tmp_path) == 1


def test_main_fails_on_an_empty_force_include_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )

    assert main(tmp_path) == 1
