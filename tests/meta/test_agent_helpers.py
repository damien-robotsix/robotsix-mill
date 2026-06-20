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

    # TODO(build-out) markers inside agent_definitions/, tests/, or
    # meta/ tooling are false positives — exclude them.
    false_positive = _git_repo(
        tmp_path / "mill-real",
        {
            "agent_definitions/p.yaml": "skip TODO(build-out) markers\n",
            "tests/fixture.py": "# TODO(build-out): synthetic fixture\n",
            "src/robotsix_mill/meta/agent.py": '"""TODO(build-out) docstring."""\n',
        },
    )
    assert _has_buildout_placeholder(false_positive) is False


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


def test_robotsix_deps_of_parses_dependency_groups(tmp_path):
    # PEP 735 [dependency-groups] tooling must be discovered too, not just
    # [project.optional-dependencies] extras.
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "robotsix-mill"\n'
        'dependencies = ["robotsix-yaml-config @ git+https://example/x@main"]\n'
        "[dependency-groups]\n"
        'dev = ["pytest>=8", "robotsix-modules @ git+https://example/m@main"]\n',
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
