"""Deterministic ground-truth helpers injected into the meta prompt.

These replace LLM-driven discovery (which the meta-agent reliably skips —
it has produced whole passes with zero tool calls). The helpers must
surface markers/adoption gaps the agent would otherwise miss, e.g. a TODO
outside ``src/``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from robotsix_mill.meta.agent import (
    _cross_repo_adoption,
    _has_buildout_placeholder,
    _outstanding_todos,
    _robotsix_deps_of,
)


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    """Init a git repo at *path* with *files* (rel → content), tracked so
    ``git grep`` sees them. No commit needed — ``git add`` is enough."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    return path


def test_outstanding_todos_finds_markers_outside_src(tmp_path):
    """The regression that motivated this: a TODO in a periodic yaml
    (outside ``src/``) must be surfaced — the old src-only sweep missed it."""
    repo = _git_repo(
        tmp_path / "llmio",
        {
            ".robotsix-mill/periodic/langfuse_cleanup.yaml": "# TODO: clean up langfuse data\n",
            "src/pkg/mod.py": "x = 1  # FIXME: handle None\n",
            "README.md": "Just docs, nothing actionable.\n",
        },
    )
    out = _outstanding_todos({"robotsix-llmio": repo})
    assert ".robotsix-mill/periodic/langfuse_cleanup.yaml:1" in out
    assert "src/pkg/mod.py:1" in out
    # repo id prefixes every line so target_repo_id is unambiguous
    assert out.startswith("robotsix-llmio ")


def test_outstanding_todos_empty_when_no_markers(tmp_path):
    repo = _git_repo(tmp_path / "clean", {"src/a.py": "x = 1\n"})
    out = _outstanding_todos({"clean": repo})
    assert "no todo" in out.lower()


def test_outstanding_todos_caps_and_notes_truncation(tmp_path):
    body = "".join(f"# TODO item {i}\n" for i in range(10))
    repo = _git_repo(tmp_path / "many", {"notes.txt": body})
    out = _outstanding_todos({"many": repo}, cap=3)
    assert len([ln for ln in out.splitlines() if ln.startswith("many ")]) == 3
    assert "+7 more" in out


def test_has_buildout_placeholder_exact_marker_only(tmp_path):
    skeleton = _git_repo(
        tmp_path / "board",
        {"src/board/static/board.js": "// TODO(build-out): port the refresh loop\n"},
    )
    prose = _git_repo(
        tmp_path / "mill",
        {"agent_definitions/x.yaml": "We describe the build-out plan in prose.\n"},
    )
    assert _has_buildout_placeholder(skeleton) is True
    # loose 'build-out' prose must NOT misflag a non-skeleton repo
    assert _has_buildout_placeholder(prose) is False


def test_robotsix_deps_of_parses_pyproject(tmp_path):
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "robotsix-mill"\n'
        'dependencies = ["pyyaml>=6", '
        '"robotsix-yaml-config @ git+https://example/x@main"]\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest>=8", "robotsix-modules>=0.1"]\n',
        encoding="utf-8",
    )
    pkg, deps = _robotsix_deps_of(repo)
    assert pkg == "robotsix-mill"
    assert deps == {"robotsix-yaml-config", "robotsix-modules"}


def test_robotsix_deps_of_missing_pyproject(tmp_path):
    repo = tmp_path / "norepo"
    repo.mkdir()
    pkg, deps = _robotsix_deps_of(repo)
    assert pkg == "norepo"
    assert deps == set()


def test_cross_repo_adoption_matrix(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "pyproject.toml").write_text(
        '[project]\nname = "robotsix-yaml-config"\ndependencies = []\n',
        encoding="utf-8",
    )
    consumer = tmp_path / "app"
    consumer.mkdir()
    (consumer / "pyproject.toml").write_text(
        '[project]\nname = "robotsix-mill"\n'
        'dependencies = ["robotsix-yaml-config @ git+https://x@main"]\n',
        encoding="utf-8",
    )
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text(
        '[project]\nname = "robotsix-auto-mail"\ndependencies = []\n',
        encoding="utf-8",
    )
    out = _cross_repo_adoption(
        {"yaml-config": lib, "mill": consumer, "auto-mail": other}
    )
    assert "`yaml-config`" in out
    assert "consumed by [mill]" in out
    assert "auto-mail" in out  # listed as a NOT-consumed repo


def test_cross_repo_adoption_none_when_no_libs(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "pyproject.toml").write_text(
        '[project]\nname = "robotsix-a"\ndependencies = ["pyyaml>=6"]\n',
        encoding="utf-8",
    )
    out = _cross_repo_adoption({"a": a})
    assert "no shared" in out.lower()
