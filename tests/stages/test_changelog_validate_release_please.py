"""A release-please repo must be left alone by changelog validation.

`_modules_yaml_check` does not merely report — it *inserts* a
`changelog.d/*.md` glob into `docs/modules.yaml`. On a repo that has migrated
to release-please there is no such directory, so that insertion makes the
repo's own `check-registration` job fail. These tests pin the skip.
"""

from __future__ import annotations

import json
from pathlib import Path

from robotsix_mill.stages._changelog_validate import (
    uses_release_please,
    validate_changelog,
)

_MODULES_YAML = """\
modules:
  - id: core
    paths:
      - src/pkg/__init__.py
"""


def _repo(tmp_path: Path, *, release_please: bool) -> Path:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "modules.yaml").write_text(_MODULES_YAML, encoding="utf-8")
    if release_please:
        (tmp_path / "release-please-config.json").write_text(
            json.dumps({"release-type": "python"}), encoding="utf-8"
        )
    else:
        frag = tmp_path / "changelog.d"
        frag.mkdir(exist_ok=True)
        # No trailing newline — the validator would normally fix this.
        (frag / "1.misc.md").write_bytes(b"entry")
    return tmp_path


def test_detects_a_release_please_repo(tmp_path: Path) -> None:
    assert uses_release_please(_repo(tmp_path, release_please=True))


def test_plain_repo_is_not_release_please(tmp_path: Path) -> None:
    assert not uses_release_please(_repo(tmp_path, release_please=False))


def test_release_please_repo_is_skipped_entirely(tmp_path: Path) -> None:
    repo = _repo(tmp_path, release_please=True)
    before = (repo / "docs" / "modules.yaml").read_text(encoding="utf-8")

    assert validate_changelog(repo) == []

    # The critical assertion: modules.yaml is untouched. Inserting
    # `changelog.d/*.md` here would break check-registration on a repo whose
    # changelog.d/ no longer exists.
    after = (repo / "docs" / "modules.yaml").read_text(encoding="utf-8")
    assert after == before
    assert "changelog.d" not in after


def test_towncrier_repo_still_validated(tmp_path: Path) -> None:
    """The skip must not disarm validation for repos still on towncrier."""
    repo = _repo(tmp_path, release_please=False)

    msgs = validate_changelog(repo)

    # The missing trailing newline is fixed and reported.
    assert any("trailing newline" in m for m in msgs)
    assert (repo / "changelog.d" / "1.misc.md").read_bytes().endswith(b"\n")
