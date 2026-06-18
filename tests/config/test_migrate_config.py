"""Tests for the ``scripts/migrate-config`` migration script.

Loads the extensionless script via ``importlib`` (mirroring the
boilerplate in ``tests/test_modules_yaml_paths.py``) and exercises its
importable helpers plus the end-to-end ``main()`` path against a temp
directory so the real repo files are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "migrate-config"


_mc = load_script(_SCRIPT_PATH)


# ---------------------------------------------------------------------------
#  parse_env_file
# ---------------------------------------------------------------------------


def test_parse_env_file_skips_comments_and_blanks(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "   \n"
        "MILL_FORGE_KIND=gitlab\n"
        "  # indented comment\n"
        'FORGE_TOKEN="ght_y"\n'
        "export MILL_MODEL='deepseek/x'\n"
        "BAD_LINE_NO_EQUALS\n"
    )
    parsed = _mc.parse_env_file(env)
    assert parsed == {
        "MILL_FORGE_KIND": "gitlab",
        "FORGE_TOKEN": "ght_y",
        "MILL_MODEL": "deepseek/x",
    }


def test_parse_env_file_missing_is_empty(tmp_path: Path) -> None:
    assert _mc.parse_env_file(tmp_path / "nope.env") == {}


# ---------------------------------------------------------------------------
#  build_outputs — non-secret mapping & default comparison
# ---------------------------------------------------------------------------


def test_build_outputs_omits_default_equal_includes_differing() -> None:
    defaults = {"forge": {"kind": "none", "target_branch": "main"}}
    env_vars = {
        "FORGE_KIND": "gitlab",  # differs from default -> included
        "FORGE_TARGET_BRANCH": "main",  # equals default -> omitted
    }
    production, secrets, unmapped = _mc.build_outputs(env_vars, defaults)
    assert production == {"forge": {"kind": "gitlab"}}
    assert secrets == {}
    assert unmapped == []


def test_build_outputs_mill_prefixed_alias_resolution() -> None:
    # ``MILL_MAX_FIX_ITERATIONS`` -> strip ``MILL_`` -> ``max_fix_iterations``
    # alias -> dotted path ``core.limits.max_fix_iterations``.
    defaults = {"core": {"limits": {"max_fix_iterations": 8}}}
    production, _secrets, _unmapped = _mc.build_outputs(
        {"MILL_MAX_FIX_ITERATIONS": "12"}, defaults
    )
    assert production == {"core": {"limits": {"max_fix_iterations": 12}}}


def test_build_outputs_unmapped_var_is_reported_not_fatal() -> None:
    production, secrets, unmapped = _mc.build_outputs({"TOTALLY_UNKNOWN_VAR": "x"}, {})
    assert production == {}
    assert secrets == {}
    assert unmapped == ["TOTALLY_UNKNOWN_VAR"]


def test_build_outputs_missing_default_treated_as_override() -> None:
    production, _secrets, _unmapped = _mc.build_outputs({"FORGE_KIND": "gitlab"}, {})
    assert production == {"forge": {"kind": "gitlab"}}


# ---------------------------------------------------------------------------
#  build_outputs — secret routing
# ---------------------------------------------------------------------------


def test_build_outputs_routes_secrets_flat_and_lowercased() -> None:
    defaults = {"forge": {"kind": "none"}}
    env_vars = {
        "OPENROUTER_API_KEY": "sk-x",
        "FORGE_TOKEN": "ght_y",
        "FORGE_KIND": "gitlab",
    }
    production, secrets, _unmapped = _mc.build_outputs(env_vars, defaults)
    assert secrets == {"openrouter_api_key": "sk-x", "forge_token": "ght_y"}
    assert production == {"forge": {"kind": "gitlab"}}


# ---------------------------------------------------------------------------
#  main() end-to-end
# ---------------------------------------------------------------------------


def _setup_repo(tmp_path: Path) -> None:
    """Create a minimal repo layout (defaults + input env files)."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "mill.defaults.yaml").write_text(
        "forge:\n  kind: none\n  target_branch: main\n"
    )
    (tmp_path / ".env").write_text("FORGE_KIND=gitlab\nFORGE_TARGET_BRANCH=main\n")
    (tmp_path / "secrets.env").write_text(
        "OPENROUTER_API_KEY=sk-x\nFORGE_TOKEN=ght_y\n"
    )


def test_main_dry_run_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _mc.main(["migrate-config", "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "config" / "mill.production.yaml").exists()
    assert not (tmp_path / "config" / "secrets.yaml").exists()

    out = capsys.readouterr().out
    # Secret values must never be printed in cleartext.
    assert "sk-x" not in out
    assert "ght_y" not in out
    assert "forge_token: ***" in out


def test_main_writes_expected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = _mc.main(["migrate-config"])
    assert rc == 0

    production = yaml.safe_load(
        (tmp_path / "config" / "mill.production.yaml").read_text()
    )
    secrets = yaml.safe_load((tmp_path / "config" / "secrets.yaml").read_text())
    # default-equal target_branch omitted, differing kind included.
    assert production == {"forge": {"kind": "gitlab"}}
    assert secrets == {"openrouter_api_key": "sk-x", "forge_token": "ght_y"}


def test_main_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert _mc.main(["migrate-config"]) == 0
    prod_first = (tmp_path / "config" / "mill.production.yaml").read_bytes()
    secrets_first = (tmp_path / "config" / "secrets.yaml").read_bytes()

    assert _mc.main(["migrate-config"]) == 0
    prod_second = (tmp_path / "config" / "mill.production.yaml").read_bytes()
    secrets_second = (tmp_path / "config" / "secrets.yaml").read_bytes()

    assert prod_first == prod_second
    assert secrets_first == secrets_second


def test_main_missing_inputs_exits_zero_no_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "mill.defaults.yaml").write_text("forge:\n  kind: none\n")
    monkeypatch.chdir(tmp_path)

    rc = _mc.main(["migrate-config"])
    assert rc == 0
    assert not (tmp_path / "config" / "mill.production.yaml").exists()
    assert not (tmp_path / "config" / "secrets.yaml").exists()
    assert "Nothing to migrate" in capsys.readouterr().out
