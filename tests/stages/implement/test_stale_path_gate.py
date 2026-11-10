"""Tests for the post-move stale-reference sweep (``stale_path_gate``)."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

from robotsix_mill.core.states import State
from robotsix_mill.stages.implement._shared import _ImplementContext
from robotsix_mill.stages.implement.stale_path_gate import (
    STALE_PATHS_MARKER,
    _dotted_name,
    find_stale_references,
    moved_files,
    run_stale_path_gate,
)
from tests.stages.implement.conftest import _git

_FILES = {
    "src/pkg/__init__.py": "",
    "src/pkg/routes.py": "def route():\n    return 1\n",
    "src/pkg/config.py": "VALUE = 1\n",
    "tests/test_routes.py": "from pkg.routes import route\n",
    "pyproject.toml": '[tool.ruff.lint.per-file-ignores]\n"tests/test_routes.py" = ["S101"]\n',
    "ARCHITECTURE.md": "- `src/pkg/routes.py` — routes\n- pkg/config.py config\n",
    "CHANGELOG.md": "- moved src/pkg/routes.py\n",
}


def _repo(tmp_path: Path) -> Path:
    """Clone with an ``origin/main`` holding the pre-move tree."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    for rel, text in _FILES.items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(seed), str(clone)], check=True, capture_output=True
    )
    return clone


def _move(repo: Path, old: str, new: str) -> None:
    """Move via delete + untracked create (not ``git mv``)."""
    (repo / new).parent.mkdir(parents=True, exist_ok=True)
    (repo / new).write_text((repo / old).read_text())
    (repo / old).unlink()


def _ic(feedback: str | None = None) -> _ImplementContext:
    return _ImplementContext(
        spec="spec",
        memory_text="",
        reference_files=None,
        file_map=None,
        feedback=feedback,
        previous_attempt_summary=None,
    )


def test_dotted_name() -> None:
    assert _dotted_name("src/pkg/a/b.py") == "pkg.a.b"
    assert _dotted_name("tests/test_x.py") == "tests.test_x"
    assert _dotted_name("src/pkg/sub/__init__.py") == "pkg.sub"
    assert _dotted_name("src/foo.py") is None
    assert _dotted_name("docs/a.md") is None
    assert _dotted_name("src/pkg/my-mod.py") is None


def test_moved_files_detects_untracked_move(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _move(repo, "tests/test_routes.py", "tests/pkg/test_routes.py")
    assert moved_files(repo, "main") == [
        ("tests/test_routes.py", "tests/pkg/test_routes.py")
    ]
    # The real index is untouched.
    status = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


def test_no_moves_no_hits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("hi\n")
    assert find_stale_references(repo, "main") == []


def test_finds_stale_path_and_module_refs_excluding_changelog(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _move(repo, "src/pkg/routes.py", "src/pkg/web/routes.py")
    _move(repo, "tests/test_routes.py", "tests/pkg/test_routes.py")
    hits = find_stale_references(repo, "main")
    assert hits is not None
    locs = {loc.split(":", 2)[0] + ":" + term for term, loc in hits}
    assert "ARCHITECTURE.md:src/pkg/routes.py" in locs
    assert "pyproject.toml:tests/test_routes.py" in locs
    # Old dotted module name referenced from the moved test file itself.
    assert "tests/pkg/test_routes.py:pkg.routes" in locs
    assert not any(loc.startswith("CHANGELOG.md") for _t, loc in hits)
    # ``src/pkg/routes.py`` must not double-report as ``pkg/routes.py``.
    assert not any(
        term == "pkg/routes.py" and loc.startswith("ARCHITECTURE.md")
        for term, loc in hits
    )


def test_flat_to_package_keeps_dotted_name(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _move(repo, "src/pkg/routes.py", "src/pkg/routes/__init__.py")
    (repo / "ARCHITECTURE.md").write_text("- `src/pkg/routes/` — routes\n")
    hits = find_stale_references(repo, "main")
    # ``pkg.routes`` is still importable — only path refs would count.
    assert hits == []


def test_fixed_references_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _move(repo, "src/pkg/config.py", "src/pkg/settings/config.py")
    (repo / "ARCHITECTURE.md").write_text(
        "- `src/pkg/routes.py` — routes\n- pkg/settings/config.py config\n"
    )
    assert find_stale_references(repo, "main") == []


def test_gate_retries_then_blocks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _move(repo, "tests/test_routes.py", "tests/pkg/test_routes.py")
    ticket = SimpleNamespace(id="T-1")

    first = run_stale_path_gate(ticket, repo, "main", _ic(), b"msgs")
    assert first is not None
    assert first.next_action == "retry"
    assert first.feedback is not None
    assert first.feedback.startswith(STALE_PATHS_MARKER)
    assert "pyproject.toml:2:" in first.feedback
    assert first.ic is not None and first.ic.feedback == first.feedback
    assert first.new_msgs == b"msgs"

    second = run_stale_path_gate(ticket, repo, "main", first.ic, None)
    assert second is not None
    assert second.next_action == "return"
    assert second.outcome is not None
    assert second.outcome.next_state == State.BLOCKED
    assert "pyproject.toml" in second.outcome.note


def test_gate_fails_open_without_git(tmp_path: Path) -> None:
    ticket = SimpleNamespace(id="T-1")
    assert run_stale_path_gate(ticket, tmp_path, "main", _ic(), None) is None
