"""Tests for the test sub-agent (src/robotsix_mill/agents/testing.py).

Covers the pure detection/heuristic functions and the orchestration
functions with mocked sandbox/dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from robotsix_mill.agents.testing import (
    ENV_ERROR_PREFIX,
    _check_pyproject_toml,
    _detect_missing_binary,
    _detect_noexec_script,
    _env_error_diag,
    _evaluate_gate_result,
    _load_file_map,
    is_network_dependent_failure,
    run_smoke_agent,
    run_test_agent,
    smoke_paths_match,
)


# ---------------------------------------------------------------------------
# _detect_missing_binary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "out,expected",
    [
        # sh / dash style
        ("sh: 1: yamllint: not found", "yamllint"),
        ("sh: 2: mypy: not found", "mypy"),
        # bash style
        ("yamllint: command not found", "yamllint"),
        ("\n pytest: command not found", "pytest"),
        # no match
        ("some random output", None),
        ("", None),
        ("Permission denied", None),
    ],
)
def test_detect_missing_binary(out, expected):
    assert _detect_missing_binary(out) == expected


def test_detect_missing_binary_sh_wins_over_bash():
    """When both shell signatures appear, the sh pattern (checked first) wins."""
    out = "sh: 1: foo: not found\nbar: command not found"
    assert _detect_missing_binary(out) == "foo"


# ---------------------------------------------------------------------------
# _detect_noexec_script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "out,expected",
    [
        ("/tmp/script: Permission denied", "/tmp/script"),
        (
            "/home/user/.local/bin/pytest: Permission denied",
            "/home/user/.local/bin/pytest",
        ),
        ("/tmp/.local/bin/ruff: Permission denied", "/tmp/.local/bin/ruff"),
        # no match — not under /tmp or .local/bin
        ("/usr/bin/foo: Permission denied", None),
        ("some random output", None),
        ("", None),
    ],
)
def test_detect_noexec_script(out, expected):
    assert _detect_noexec_script(out) == expected


# ---------------------------------------------------------------------------
# _env_error_diag
# ---------------------------------------------------------------------------


def test_env_error_diag_rc_127_generic():
    diag = _env_error_diag(127, "some output without command-not-found")
    assert diag is not None
    assert diag.startswith(ENV_ERROR_PREFIX)
    assert "rc=127" in diag


def test_env_error_diag_rc_127_with_missing_binary():
    diag = _env_error_diag(127, "sh: 1: ruff: not found")
    assert diag is not None
    assert diag.startswith(ENV_ERROR_PREFIX)
    assert "ruff" in diag
    assert "rc=127" in diag


def test_env_error_diag_rc_0_with_missing_binary_signature():
    """rc=0 but output has command-not-found — still treated as env error."""
    diag = _env_error_diag(0, "sh: 1: mypy: not found")
    assert diag is not None
    assert diag.startswith(ENV_ERROR_PREFIX)
    assert "mypy" in diag


def test_env_error_diag_rc_126_noexec():
    diag = _env_error_diag(126, "/tmp/.local/bin/pytest: Permission denied")
    assert diag is not None
    assert diag.startswith(ENV_ERROR_PREFIX)
    assert "not executable" in diag
    assert "noexec" in diag


def test_env_error_diag_rc_126_no_noexec_and_no_missing_binary():
    """rc=126 without noexec signature and without missing binary → not env error."""
    diag = _env_error_diag(126, "some assertion failed")
    assert diag is None


def test_env_error_diag_rc_1_not_env():
    """Normal test failure (rc=1) is not an environmental error."""
    diag = _env_error_diag(1, "tests failed: assert 1 == 2")
    assert diag is None


def test_env_error_diag_rc_124_timeout():
    """rc=124 (sandbox timeout) is an environmental error — no LLM distiller."""
    diag = _env_error_diag(124, "command timed out after 3600s")
    assert diag is not None
    assert diag.startswith(ENV_ERROR_PREFIX)
    assert "rc=124" in diag
    assert "sandbox timeout" in diag


def test_env_error_diag_is_stable():
    """Same input produces byte-identical output (circuit-breaker requirement)."""
    diag1 = _env_error_diag(127, "sh: 1: yamllint: not found")
    diag2 = _env_error_diag(127, "sh: 1: yamllint: not found")
    assert diag1 == diag2
    assert isinstance(diag1, str)


# ---------------------------------------------------------------------------
# _load_file_map
# ---------------------------------------------------------------------------


def test_load_file_map_no_file(tmp_path):
    # _load_file_map reads artifacts from repo_dir.parent, so use a
    # subdirectory as the fake repo_dir.
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    assert _load_file_map(repo_dir) is None


def test_load_file_map_valid(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fm = artifacts / "file_map.json"
    fm.write_text(json.dumps([{"file": "src/foo.py"}, {"file": "tests/test_foo.py"}]))
    result = _load_file_map(repo_dir)
    assert result == ["src/foo.py", "tests/test_foo.py"]


def test_load_file_map_empty_array(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fm = artifacts / "file_map.json"
    fm.write_text("[]")
    assert _load_file_map(repo_dir) is None


def test_load_file_map_invalid_json(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fm = artifacts / "file_map.json"
    fm.write_text("not json")
    assert _load_file_map(repo_dir) is None


def test_load_file_map_missing_file_key(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fm = artifacts / "file_map.json"
    fm.write_text(json.dumps([{"not_file": "src/foo.py"}]))
    assert _load_file_map(repo_dir) is None


# ---------------------------------------------------------------------------
# smoke_paths_match
# ---------------------------------------------------------------------------


def test_smoke_paths_match_empty_globs_runs_unconditionally():
    assert smoke_paths_match(["any/file.py"], []) is True
    assert smoke_paths_match([], []) is True


def test_smoke_paths_match_exact():
    assert smoke_paths_match(["src/main.py"], ["src/main.py"]) is True


def test_smoke_paths_match_fnmatch_glob():
    assert smoke_paths_match(["src/main.py"], ["src/*.py"]) is True


def test_smoke_paths_match_purepath_recursive():
    """PurePath.match handles ** patterns for directory recursion."""
    assert (
        smoke_paths_match(
            ["src/robotsix_mill/runtime/static/board.js"],
            ["src/robotsix_mill/runtime/**"],
        )
        is True
    )


def test_smoke_paths_match_no_match():
    assert smoke_paths_match(["docs/readme.md"], ["src/*.py"]) is False


def test_smoke_paths_match_multiple_files_one_matches():
    assert (
        smoke_paths_match(
            ["docs/readme.md", "src/main.py"],
            ["src/*.py"],
        )
        is True
    )


def test_smoke_paths_match_multiple_globs_second_matches():
    assert (
        smoke_paths_match(
            ["tests/test_foo.py"],
            ["src/*.py", "tests/*.py"],
        )
        is True
    )


def test_smoke_paths_match_invalid_purepath_pattern_graceful():
    """An invalid PurePath pattern is skipped (fnmatch already had its chance).

    ``file[.txt`` is an invalid PurePath pattern (unmatched ``[``) that
    raises ValueError.  fnmatch.fnmatch returns False for it because the
    literal ``[`` does not appear in ``file.txt``, so we fall through
    to PurePath.match → ValueError → skip.
    """
    assert smoke_paths_match(["file.txt"], ["file[.txt"]) is False


# ---------------------------------------------------------------------------
# _evaluate_gate_result
# ---------------------------------------------------------------------------

_FAKE_SETTINGS = None  # placeholder — patched out in tests


def _make_settings_for_gate(tmp_path):
    from robotsix_mill.config import Settings
    from robotsix_mill.core import db

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")
    return s


def test_evaluate_gate_result_pass():
    """rc=0 → pass with success message."""
    passed, msg = _evaluate_gate_result(
        settings=None,  # not used when rc=0
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=0,
        out="",
        retry_on_failure=False,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry good",
        is_test_gate=True,
    )
    assert passed is True
    assert msg == "all good"


def test_evaluate_gate_result_no_tests_pytest_rc5():
    """pytest rc=5 + 'no tests ran' → pass (not a regression)."""
    passed, msg = _evaluate_gate_result(
        settings=None,
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=5,
        out="collected 0 items\nno tests ran in 0.01s",
        retry_on_failure=False,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry good",
        is_test_gate=True,
    )
    assert passed is True
    assert "no tests collected" in msg


def test_evaluate_gate_result_rc5_not_test_gate_still_fails():
    """rc=5 on the smoke gate (is_test_gate=False) is a real failure."""
    with patch(
        "robotsix_mill.agents.testing._distill_failure", return_value="distilled"
    ):
        passed, msg = _evaluate_gate_result(
            settings=None,
            repo_dir=Path("/fake"),
            cmd="smoke",
            rc=5,
            out="something broke",
            retry_on_failure=False,
            sandbox_image=None,
            file_map=None,
            success_msg="smoke good",
            retry_success_msg="retry good",
            is_test_gate=False,
        )
    assert passed is False
    assert msg == "distilled"


def test_evaluate_gate_result_env_error():
    """Environmental failure → deterministic ENV-ERROR, no distill."""
    passed, msg = _evaluate_gate_result(
        settings=None,
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=127,
        out="sh: 1: yamllint: not found",
        retry_on_failure=False,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry good",
        is_test_gate=True,
    )
    assert passed is False
    assert msg.startswith(ENV_ERROR_PREFIX)
    assert "yamllint" in msg


def test_evaluate_gate_result_rc_124_timeout():
    """Sandbox timeout (rc=124) → deterministic ENV-ERROR, no distill."""
    passed, msg = _evaluate_gate_result(
        settings=None,
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=124,
        out="command timed out after 3600s",
        retry_on_failure=False,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry good",
        is_test_gate=True,
    )
    assert passed is False
    assert msg.startswith(ENV_ERROR_PREFIX)
    assert "timed out" in msg
    assert "rc=124" in msg


def test_evaluate_gate_result_retry_success(monkeypatch, tmp_path):
    """retry_on_failure=True, second run succeeds → pass with retry message."""
    from types import SimpleNamespace

    from robotsix_mill.sandbox import SandboxError

    call_count = [0]

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        call_count[0] += 1
        # The first call to fake_run IS the retry (original run already in rc/out).
        return (0, "second run ok")

    monkeypatch.setattr(
        "robotsix_mill.sandbox",
        SimpleNamespace(run=fake_run, SandboxError=SandboxError),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.testing._distill_failure",
        lambda *a, **kw: "distilled",
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = _evaluate_gate_result(
        settings=s,
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=1,
        out="first run failed",
        retry_on_failure=True,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry worked",
        is_test_gate=True,
    )
    assert passed is True
    assert msg == "retry worked"
    assert call_count[0] == 1  # one retry call


def test_evaluate_gate_result_retry_both_fail(monkeypatch, tmp_path):
    """retry_on_failure=True, both runs fail → distill the second output."""
    from types import SimpleNamespace

    from robotsix_mill.sandbox import SandboxError

    call_count = [0]

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        call_count[0] += 1
        # The first call to fake_run IS the retry — make it fail.
        return (1, "run 2 failed")

    monkeypatch.setattr(
        "robotsix_mill.sandbox",
        SimpleNamespace(run=fake_run, SandboxError=SandboxError),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.testing._distill_failure",
        lambda *a, **kw: "distilled from second run",
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = _evaluate_gate_result(
        settings=s,
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=1,
        out="first run failed",
        retry_on_failure=True,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry worked",
        is_test_gate=True,
    )
    assert passed is False
    assert msg == "distilled from second run"
    assert call_count[0] == 1


def test_evaluate_gate_result_retry_sandbox_error(monkeypatch, tmp_path):
    """retry_on_failure=True, sandbox raises → fail with sandbox message."""
    from types import SimpleNamespace

    from robotsix_mill.sandbox import SandboxError

    monkeypatch.setattr(
        "robotsix_mill.agents.testing._distill_failure",
        lambda *a, **kw: "distilled",
    )

    def fake_run_error(cmd, *, repo_dir, settings, install_project, sandbox_image):
        raise SandboxError("sandbox crashed")

    monkeypatch.setattr(
        "robotsix_mill.sandbox",
        SimpleNamespace(run=fake_run_error, SandboxError=SandboxError),
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = _evaluate_gate_result(
        settings=s,
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=1,
        out="first run failed",
        retry_on_failure=True,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry worked",
        is_test_gate=True,
    )
    assert passed is False
    assert "sandbox unavailable" in msg


def test_evaluate_gate_result_no_retry_distills(monkeypatch):
    """retry_on_failure=False → distill immediately."""
    monkeypatch.setattr(
        "robotsix_mill.agents.testing._distill_failure",
        lambda *a, **kw: "distilled diagnosis",
    )
    passed, msg = _evaluate_gate_result(
        settings=None,
        repo_dir=Path("/fake"),
        cmd="pytest",
        rc=1,
        out="tests failed",
        retry_on_failure=False,
        sandbox_image=None,
        file_map=None,
        success_msg="all good",
        retry_success_msg="retry good",
        is_test_gate=True,
    )
    assert passed is False
    assert msg == "distilled diagnosis"


# ---------------------------------------------------------------------------
# run_test_agent
# ---------------------------------------------------------------------------


def test_run_test_agent_no_command(monkeypatch, tmp_path):
    """Empty test command → pass without running sandbox."""
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_test_command", lambda _: ""
    )
    s = _make_settings_for_gate(tmp_path)
    s.test_command = ""
    passed, msg = run_test_agent(settings=s, repo_dir=Path("/fake"))
    assert passed is True
    assert "no test gate configured" in msg


def test_run_test_agent_sandbox_error(monkeypatch, tmp_path):
    """SandboxError → fail with message."""
    from types import SimpleNamespace

    from robotsix_mill.sandbox import SandboxError

    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_test_command",
        lambda _: "pytest",
    )

    def fake_run_error(*a, **kw):
        raise SandboxError("sandbox down")

    monkeypatch.setattr(
        "robotsix_mill.sandbox",
        SimpleNamespace(run=fake_run_error, SandboxError=SandboxError),
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_test_agent(settings=s, repo_dir=Path("/fake"))
    assert passed is False
    assert "sandbox unavailable" in msg


def test_run_test_agent_passes(monkeypatch, tmp_path):
    """Successful sandbox run → pass."""
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_test_command",
        lambda _: "pytest",
    )

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        return (0, "all tests passed")

    monkeypatch.setattr(
        "robotsix_mill.sandbox", type("Fake", (), {"run": staticmethod(fake_run)})()
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_test_agent(settings=s, repo_dir=Path("/fake"))
    assert passed is True
    assert msg == "all tests passed"


def test_run_test_agent_fails_with_distill(monkeypatch, tmp_path):
    """Failing sandbox run → distill."""
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_test_command",
        lambda _: "pytest",
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.testing._distill_failure",
        lambda *a, **kw: "distilled: 3 failures",
    )

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        return (1, "3 tests failed")

    monkeypatch.setattr(
        "robotsix_mill.sandbox", type("Fake", (), {"run": staticmethod(fake_run)})()
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_test_agent(settings=s, repo_dir=Path("/fake"))
    assert passed is False
    assert msg == "distilled: 3 failures"


def test_run_test_agent_loads_file_map(tmp_path, monkeypatch):
    """file_map=None triggers _load_file_map from artifacts/file_map.json."""
    from robotsix_mill.agents import testing

    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_test_command",
        lambda _: "pytest",
    )

    # Create the workspace checkout layout: repo dir + sibling artifacts/
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fm = artifacts / "file_map.json"
    fm.write_text(json.dumps([{"file": "src/x.py"}]))

    # Wrap _load_file_map so we can verify it was actually invoked
    original_load = testing._load_file_map
    load_called = False

    def _load_spy(repo_dir):
        nonlocal load_called
        load_called = True
        return original_load(repo_dir)

    monkeypatch.setattr(testing, "_load_file_map", _load_spy)

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        return (0, "ok")

    monkeypatch.setattr(
        "robotsix_mill.sandbox", type("Fake", (), {"run": staticmethod(fake_run)})()
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_test_agent(settings=s, repo_dir=repo)
    assert passed is True
    assert load_called


def test_run_test_agent_repo_config_sandbox_image(monkeypatch, tmp_path):
    """repo_config.sandbox_image is forwarded to sandbox.run."""
    from robotsix_mill.config import RepoConfig

    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_test_command",
        lambda _: "pytest",
    )

    captured_image = []

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        captured_image.append(sandbox_image)
        return (0, "ok")

    monkeypatch.setattr(
        "robotsix_mill.sandbox", type("Fake", (), {"run": staticmethod(fake_run)})()
    )

    s = _make_settings_for_gate(tmp_path)
    rc = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        sandbox_image="custom-image:latest",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    passed, msg = run_test_agent(settings=s, repo_dir=tmp_path, repo_config=rc)
    assert passed is True
    assert captured_image[0] == "custom-image:latest"


# ---------------------------------------------------------------------------
# _distill_failure
# ---------------------------------------------------------------------------


def test_distill_failure_no_api_key(tmp_path):
    """No openrouter_api_key set → raw tail without distill agent."""
    from robotsix_mill.agents.testing import _distill_failure

    result = _distill_failure(None, tmp_path, 1, "some failure output")
    assert result.startswith("tests failed (rc=1); raw tail:")
    assert "some failure output" in result


def test_distill_failure_no_api_key_tail_truncated(tmp_path):
    """No API key → tail is limited to last 1500 chars."""
    from robotsix_mill.agents.testing import _distill_failure

    long_out = "A" * 10_000
    result = _distill_failure(None, tmp_path, 2, long_out)
    assert result.startswith("tests failed (rc=2); raw tail:")
    # 1500 chars of raw tail after the prefix
    tail_section = result.split("raw tail:")[-1]
    assert len(tail_section) <= 1510  # 1500 + newline


def test_distill_failure_with_api_key_exception(monkeypatch, tmp_path):
    """API key set, but distill agent errors → degrade gracefully."""
    from robotsix_mill.config import Settings, Secrets
    from robotsix_mill.core import db

    # Minimal Settings object so settings.test_model doesn't fail
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(s, board_id="test-board")

    fake_secrets = Secrets(openrouter_api_key="sk-fake")
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.get_secrets", lambda: fake_secrets
    )

    # Patch objects at their source modules (the function imports them
    # locally via relative imports from .base, .yaml_loader, etc.).
    # Give FakeDef a .model to avoid AttributeError during _distill_failure.
    monkeypatch.setattr(
        "robotsix_mill.agents.yaml_loader.load_agent_definition",
        lambda _: type("FakeDef", (), {"model": None})(),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        lambda *a, **kw: type("FakeAgent", (), {})(),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.base._safe_close",
        lambda *a: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.fs_tools.build_fs_tools",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.explore.make_explore_tool",
        lambda *a, **kw: type("FakeTool", (), {})(),
    )

    def _raise_run(agent, factory, *a, **kw):
        raise Exception("distill failed")

    monkeypatch.setattr(
        "robotsix_mill.agents.retry.run_agent",
        _raise_run,
    )

    from robotsix_mill.agents.testing import _distill_failure

    result = _distill_failure(s, tmp_path, 1, "some failure")
    assert result.startswith("tests failed (rc=1); distill error")
    assert "distill failed" in result


def test_distill_failure_with_file_map_no_api_key(tmp_path):
    """file_map is included in the scope note (visible when no API key)."""
    from robotsix_mill.agents.testing import _distill_failure

    result = _distill_failure(
        None, tmp_path, 1, "failure", file_map=["src/a.py", "src/b.py"]
    )
    # Without API key, file_map is built but the distill branch is skipped.
    # The scope_note is only passed to the distill agent (the model), not
    # appended to the raw tail. So we just verify the raw tail structure.
    assert result.startswith("tests failed (rc=1); raw tail:")


# ---------------------------------------------------------------------------
# run_smoke_agent
# ---------------------------------------------------------------------------


def test_run_smoke_agent_no_command(monkeypatch, tmp_path):
    """Empty smoke command → pass without running sandbox."""
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_smoke_command", lambda _: ""
    )
    s = _make_settings_for_gate(tmp_path)
    s.smoke_command = ""
    passed, msg = run_smoke_agent(settings=s, repo_dir=Path("/fake"))
    assert passed is True
    assert "no smoke gate configured" in msg


def test_run_smoke_agent_passes(monkeypatch, tmp_path):
    """Successful sandbox run → pass with 'smoke passed'."""
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_smoke_command",
        lambda _: "make smoke",
    )

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        return (0, "smoke ok")

    monkeypatch.setattr(
        "robotsix_mill.sandbox", type("Fake", (), {"run": staticmethod(fake_run)})()
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_smoke_agent(settings=s, repo_dir=Path("/fake"))
    assert passed is True
    assert msg == "smoke passed"


def test_run_smoke_agent_fails_with_distill(monkeypatch, tmp_path):
    """Failing smoke run → distill."""
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_smoke_command",
        lambda _: "make smoke",
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.testing._distill_failure",
        lambda *a, **kw: "distilled smoke failure",
    )

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        return (2, "smoke failed")

    monkeypatch.setattr(
        "robotsix_mill.sandbox", type("Fake", (), {"run": staticmethod(fake_run)})()
    )

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_smoke_agent(settings=s, repo_dir=Path("/fake"))
    assert passed is False
    assert msg == "distilled smoke failure"


# ---------------------------------------------------------------------------
# _check_pyproject_toml
# ---------------------------------------------------------------------------


def test_check_pyproject_toml_valid(tmp_path):
    """Valid TOML → None."""
    toml_path = tmp_path / "pyproject.toml"
    toml_path.write_text("[project]\nname = 'test'\n")
    assert _check_pyproject_toml(tmp_path) is None


def test_check_pyproject_toml_no_file(tmp_path):
    """Missing pyproject.toml → None (non-Python repo, no-op)."""
    assert not (tmp_path / "pyproject.toml").exists()
    assert _check_pyproject_toml(tmp_path) is None


def test_check_pyproject_toml_invalid(tmp_path):
    """Invalid TOML → error string with 'invalid TOML' and the parse error."""
    toml_path = tmp_path / "pyproject.toml"
    toml_path.write_text("[project]\nname = \n")
    result = _check_pyproject_toml(tmp_path)
    assert result is not None
    assert "invalid TOML" in result


# ---------------------------------------------------------------------------
# run_test_agent / run_smoke_agent with broken pyproject.toml
# ---------------------------------------------------------------------------


def test_run_test_agent_broken_pyproject_toml_short_circuits(monkeypatch, tmp_path):
    """run_test_agent returns (False, ...) for broken TOML without hitting sandbox."""
    from types import SimpleNamespace

    from robotsix_mill.sandbox import SandboxError  # noqa: F401 — ensures module is loaded

    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_test_command",
        lambda _: "pytest",
    )

    sandbox_called = False

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        nonlocal sandbox_called
        sandbox_called = True
        return (0, "ok")

    monkeypatch.setattr(
        "robotsix_mill.sandbox",
        SimpleNamespace(run=fake_run, SandboxError=SandboxError),
    )

    # Create a broken pyproject.toml
    toml_path = tmp_path / "pyproject.toml"
    toml_path.write_text("[project]\nname = \n")

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert "invalid TOML" in msg
    assert sandbox_called is False


def test_run_smoke_agent_broken_pyproject_toml_short_circuits(monkeypatch, tmp_path):
    """run_smoke_agent returns (False, ...) for broken TOML without hitting sandbox."""
    from types import SimpleNamespace

    from robotsix_mill.sandbox import SandboxError  # noqa: F401 — ensures module is loaded

    monkeypatch.setattr(
        "robotsix_mill.agents.testing.load_repo_smoke_command",
        lambda _: "make smoke",
    )

    sandbox_called = False

    def fake_run(cmd, *, repo_dir, settings, install_project, sandbox_image):
        nonlocal sandbox_called
        sandbox_called = True
        return (0, "ok")

    monkeypatch.setattr(
        "robotsix_mill.sandbox",
        SimpleNamespace(run=fake_run, SandboxError=SandboxError),
    )

    # Create a broken pyproject.toml
    toml_path = tmp_path / "pyproject.toml"
    toml_path.write_text("[project]\nname = \n")

    s = _make_settings_for_gate(tmp_path)
    passed, msg = run_smoke_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert "invalid TOML" in msg
    assert sandbox_called is False


# ---------------------------------------------------------------------------
# is_network_dependent_failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "out,expected",
    [
        # JSONDecodeError on empty response — hallmark of blocked network
        (
            "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
            True,
        ),
        # httpx ConnectError
        ("httpx.ConnectError: [Errno 111] Connection refused", True),
        # requests ConnectionError
        (
            "requests.exceptions.ConnectionError: "
            "HTTPConnectionPool(host='api.openai.com', port=443): "
            "Max retries exceeded",
            True,
        ),
        # ConnectionRefusedError
        ("ConnectionRefusedError: [Errno 111] Connection refused", True),
        # RemoteDisconnected (httpx/httpcore)
        ("httpcore.RemoteDisconnected: Server disconnected", True),
        # DNS failure — glibc
        ("socket.gaierror: [Errno -2] Name or service not known", True),
        # DNS failure — macOS
        ("socket.gaierror: [Errno 8] nodename nor servname provided", True),
        # getaddrinfo
        ("getaddrinfo ENOTFOUND api.langfuse.com", True),
        # Temporary failure in name resolution
        ("Temporary failure in name resolution", True),
        # Failed to resolve (httpx)
        ("Failed to resolve 'api.openai.com'", True),
        # Combine: connection + JSONDecodeError
        (
            "httpx.ConnectError\njson.decoder.JSONDecodeError: "
            "Expecting value: line 1 column 1 (char 0)",
            True,
        ),
        # --- False cases: genuine test failures ---
        # Plain AssertionError
        ("AssertionError: assert 1 == 2", False),
        # pytest assertion
        ("E       assert 1 == 2", False),
        # JSONDecodeError on NON-empty data (fixture with stray brace)
        (
            "json.decoder.JSONDecodeError: Expecting ',' delimiter: "
            "line 42 column 15 (char 981)",
            False,
        ),
        # Normal test failure output
        ("FAILED tests/test_foo.py::test_bar - assert 1 == 2", False),
        # Empty string
        ("", False),
        # ImportError (not network)
        ("ModuleNotFoundError: No module named 'foo'", False),
    ],
)
def test_is_network_dependent_failure(out, expected):
    assert is_network_dependent_failure(out) == expected
