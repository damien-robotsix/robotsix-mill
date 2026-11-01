"""Tests for the pre-implement module_curator ``unclassified:`` staleness gate."""

import subprocess
from pathlib import Path

from robotsix_mill.agents import coding
from robotsix_mill.core.constants import OBSOLESCENCE_GAP_PREFIX
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import ImplementStage
from robotsix_mill.stages.implement.module_gap_gate import (
    check_unclassified_gap,
    find_claiming_module,
    parse_unclassified_gap_path,
)
from tests.stages.implement.conftest import _git, _ticket

_PATH = ".github/PULL_REQUEST_TEMPLATE.md"
_MARKER = f"<!-- module_curator-gap-id: unclassified:{_PATH} -->"

_MODULES_CLAIMED = f"""modules:
  - id: core
    description: core
    paths:
      - src/**/*.py
  - id: ci
    description: ci
    paths:
      - .github/workflows/*.yml
      - {_PATH}
"""

_MODULES_UNCLAIMED = """modules:
  - id: ci
    description: ci
    paths:
      - .github/workflows/*.yml
"""


def _make_remote(tmp_path: Path, modules_yaml: str, with_file: bool = True) -> str:
    seed = tmp_path / "seed"
    (seed / "docs").mkdir(parents=True)
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "docs" / "modules.yaml").write_text(modules_yaml)
    if with_file:
        (seed / ".github").mkdir()
        (seed / _PATH).write_text("template\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", "main")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


def _clone(tmp_path: Path, remote: str) -> Path:
    repo = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", remote, str(repo)], check=True, capture_output=True
    )
    return repo


# --- pure helpers ----------------------------------------------------------


def test_parse_unclassified_gap_path():
    assert parse_unclassified_gap_path(f"spec\n{_MARKER}\n") == _PATH
    assert parse_unclassified_gap_path("no marker here") is None
    assert (
        parse_unclassified_gap_path("<!-- audit-gap-id: unclassified:x/y.py -->")
        is None
    )


def test_find_claiming_module_glob_exact_and_directory():
    assert find_claiming_module(_MODULES_CLAIMED, _PATH) == "ci"
    assert find_claiming_module(_MODULES_CLAIMED, "src/a/b.py") == "core"
    assert find_claiming_module(_MODULES_UNCLAIMED, _PATH) is None
    dir_yaml = "modules:\n  - id: gh\n    paths:\n      - .github\n"
    assert find_claiming_module(dir_yaml, _PATH) == "gh"
    assert find_claiming_module("not: [valid", _PATH) is None


# --- git-backed check -------------------------------------------------------


def test_check_closes_when_path_claimed_on_main(tmp_path):
    repo = _clone(tmp_path, _make_remote(tmp_path, _MODULES_CLAIMED))
    note = check_unclassified_gap(repo, "main", f"Classify it\n{_MARKER}")
    assert note is not None
    assert note.startswith(OBSOLESCENCE_GAP_PREFIX)
    assert "`ci`" in note
    assert _PATH in note


def test_check_ignores_local_working_tree_edits(tmp_path):
    """The match runs against origin/<target>, not the branch's own edits."""
    repo = _clone(tmp_path, _make_remote(tmp_path, _MODULES_UNCLAIMED))
    (repo / "docs" / "modules.yaml").write_text(_MODULES_CLAIMED)
    assert check_unclassified_gap(repo, "main", _MARKER) is None


def test_check_closes_when_path_gone_on_main(tmp_path):
    repo = _clone(tmp_path, _make_remote(tmp_path, _MODULES_UNCLAIMED, with_file=False))
    note = check_unclassified_gap(repo, "main", _MARKER)
    assert note is not None
    assert "no longer exists" in note


def test_check_passes_through_real_gap_and_unmarked_specs(tmp_path):
    repo = _clone(tmp_path, _make_remote(tmp_path, _MODULES_UNCLAIMED))
    assert check_unclassified_gap(repo, "main", _MARKER) is None
    assert check_unclassified_gap(repo, "main", "plain spec") is None
    # Unknown target branch → conservative pass-through.
    assert check_unclassified_gap(repo, "nope", _MARKER) is None


# --- stage wiring -----------------------------------------------------------


def test_implement_closes_claimed_gap_without_running_agent(
    ctx_factory, tmp_path, monkeypatch
):
    remote = _make_remote(tmp_path, _MODULES_CLAIMED)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")

    def _boom(**_kw):
        raise AssertionError("implement agent must not run")

    monkeypatch.setattr(coding, "run_implement_agent", _boom)
    monkeypatch.setattr(
        ImplementStage,
        "_run_baseline_check",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no baseline")),
    )
    t = _ticket(ctx, title=f"Classify {_PATH}", body=f"Classify it.\n{_MARKER}")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "`ci`" in out.note
