"""Direct unit tests for the shared periodic-pass runner infrastructure.

Covers ``_clone_token``, ``_forge_token``, ``run_periodic_pass`` edge
cases, and the ``PERIODIC_PASS_CONFIGS`` registry.
"""

import logging
import subprocess
from unittest.mock import MagicMock

import pytest

from robotsix_mill.runners.periodic_runner import (
    PERIODIC_PASS_CONFIGS,
    _clone_token,
    _forge_token,
    run_periodic_pass,
)
from robotsix_mill.config import Secrets, Settings
from robotsix_mill.core import db


# ------------------------------------------------------------------ helpers


def _test_repo_config():
    """Synthetic RepoConfig for periodic-runner tests."""
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


def _fake_agent_pass_result(updated_memory="mem", drafts_created=None):
    """Return a MagicMock that looks like an AgentPassResult."""
    if drafts_created is None:
        drafts_created = []
    r = MagicMock()
    r.updated_memory = updated_memory
    r.drafts_created = drafts_created
    return r


# ------------------------------------------------------------------ _clone_token


def test_clone_token_returns_token_on_success(monkeypatch):
    """_clone_token returns the token string from github_token."""

    def fake_github_token(settings, repo_config=None):
        return "ghp_fake_token"

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.github_token", fake_github_token
    )

    result = _clone_token(Settings(), _test_repo_config())
    assert result == "ghp_fake_token"


def test_clone_token_returns_none_on_runtime_error(monkeypatch):
    """_clone_token returns None when github_token raises RuntimeError."""

    def fake_github_token(settings, repo_config=None):
        raise RuntimeError("no token")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.github_token", fake_github_token
    )

    result = _clone_token(Settings(), _test_repo_config())
    assert result is None


# ------------------------------------------------------------------ _forge_token


def test_forge_token_returns_secret(monkeypatch):
    """_forge_token returns forge_token from secrets."""
    fake_secrets = Secrets(forge_token="forge-tok-123")
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.get_secrets", lambda: fake_secrets
    )

    result = _forge_token(Settings(), _test_repo_config())
    assert result == "forge-tok-123"


# ------------------------------------------------------------------ run_periodic_pass


def test_run_periodic_pass_repo_config_none_raises_value_error(tmp_path, monkeypatch):
    """Calling with repo_config=None raises ValueError."""
    settings = _make_settings(tmp_path)
    config = PERIODIC_PASS_CONFIGS["audit"]

    with pytest.raises(ValueError, match="repo_config is required"):
        run_periodic_pass(
            session_id="s",
            repo_config=None,
            config=config,
            settings=settings,
        )


def test_run_periodic_pass_clone_failure_sets_repo_dir_none(tmp_path, monkeypatch):
    """When git_ops.clone raises CalledProcessError, repo_dir=None is
    passed to run_agent_pass and a valid result dataclass is returned."""
    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )

    def fake_clone(url, dest, branch, token):
        raise subprocess.CalledProcessError(1, "git")

    from robotsix_mill.vcs import git_ops

    monkeypatch.setattr(git_ops, "clone", fake_clone)

    captured_repo_dir = {}

    def fake_run_agent_pass(
        agent_fn,
        memory_file,
        source_label,
        service,
        settings,
        origin_session,
        max_drafts,
        repo_dir,
    ):
        captured_repo_dir["value"] = repo_dir
        return _fake_agent_pass_result()

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass", fake_run_agent_pass
    )

    result = run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["audit"],
        settings=settings,
    )

    assert captured_repo_dir["value"] is None
    assert isinstance(result, PERIODIC_PASS_CONFIGS["audit"].result_dataclass)
    assert result.session_id == "test-sid"


def test_run_periodic_pass_clone_failure_logs_warning(tmp_path, monkeypatch, caplog):
    """Clone failure emits a warning-level log with 'clone failed'."""
    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )

    def fake_clone(url, dest, branch, token):
        raise subprocess.CalledProcessError(1, "git")

    from robotsix_mill.vcs import git_ops

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass",
        lambda **kw: _fake_agent_pass_result(),
    )

    with caplog.at_level(logging.WARNING):
        run_periodic_pass(
            session_id="test-sid",
            repo_config=_test_repo_config(),
            config=PERIODIC_PASS_CONFIGS["audit"],
            settings=settings,
        )

    assert any("clone failed" in record.message for record in caplog.records)


def test_run_periodic_pass_uses_clone_token_fn_when_configured(tmp_path, monkeypatch):
    """For a config with clone_token_fn=_clone_token, verify _clone_token
    is called (not _forge_token). Uses audit config as the test subject."""
    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )

    def fake_clone(url, dest, branch, token):
        (dest / ".git").mkdir(parents=True)

    from robotsix_mill.vcs import git_ops

    monkeypatch.setattr(git_ops, "clone", fake_clone)

    # _clone_token delegates to github_token, so mock that and verify
    # it's called.
    github_token_calls = []

    def fake_github_token(settings, repo_config=None):
        github_token_calls.append(True)
        return "ghp_tok"

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.github_token", fake_github_token
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass",
        lambda **kw: _fake_agent_pass_result(),
    )

    run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["audit"],  # clone_token_fn=_clone_token
        settings=settings,
    )

    assert len(github_token_calls) == 1


def test_run_periodic_pass_uses_forge_token_when_clone_token_fn_none(
    tmp_path, monkeypatch
):
    """For a config with clone_token_fn=None (e.g. health), verify
    _forge_token is called (which delegates to get_secrets)."""
    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )

    def fake_clone(url, dest, branch, token):
        (dest / ".git").mkdir(parents=True)

    from robotsix_mill.vcs import git_ops

    monkeypatch.setattr(git_ops, "clone", fake_clone)

    # _forge_token calls get_secrets().forge_token, so mock get_secrets
    # and verify it's called.
    get_secrets_calls = []

    def fake_get_secrets():
        get_secrets_calls.append(True)
        return Secrets(forge_token="ftok")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.get_secrets", fake_get_secrets
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass",
        lambda **kw: _fake_agent_pass_result(),
    )

    run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["health"],  # clone_token_fn=None
        settings=settings,
    )

    assert len(get_secrets_calls) == 1


def test_run_periodic_pass_resolves_extra_kwargs_fn(tmp_path, monkeypatch):
    """The agent_check config's extra_kwargs_fn injects memory_dir."""
    settings = _make_settings(tmp_path)

    captured_keywords = {}

    def fake_run_agent_pass(agent_fn, **kw):
        captured_keywords["keywords"] = agent_fn.keywords
        return _fake_agent_pass_result()

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass", fake_run_agent_pass
    )

    run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["agent_check"],
        settings=settings,
    )

    assert "memory_dir" in captured_keywords["keywords"]
    assert captured_keywords["keywords"]["memory_dir"] == settings.data_dir


def test_run_periodic_pass_resolves_max_drafts_fn(tmp_path, monkeypatch):
    """The completeness_check config's max_drafts_fn returns MAX_GAPS
    and the value is forwarded to run_agent_pass."""
    from robotsix_mill.agents import completeness_check

    settings = _make_settings(tmp_path)

    captured_max_drafts = {}

    def fake_run_agent_pass(agent_fn, max_drafts, **kw):
        captured_max_drafts["value"] = max_drafts
        return _fake_agent_pass_result()

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass", fake_run_agent_pass
    )

    run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["completeness_check"],
        settings=settings,
    )

    assert captured_max_drafts["value"] == completeness_check.MAX_GAPS


def test_run_periodic_pass_returns_result_dataclass(tmp_path, monkeypatch):
    """run_periodic_pass returns the correct result dataclass with
    expected fields populated."""
    from robotsix_mill.runners.periodic_runner import AuditPassResult

    settings = _make_settings(tmp_path)

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass",
        lambda **kw: _fake_agent_pass_result(
            updated_memory="test-mem",
            drafts_created=[{"id": "1", "title": "t"}],
        ),
    )

    result = run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["audit"],
        settings=settings,
    )

    assert isinstance(result, AuditPassResult)
    assert result.updated_memory == "test-mem"
    assert result.drafts_created == [{"id": "1", "title": "t"}]
    assert result.session_id == "test-sid"


def test_run_periodic_pass_imports_agent_module_lazily(tmp_path, monkeypatch):
    """importlib.import_module is called with the correct dotted path
    for a given config."""
    import importlib as importlib_mod

    settings = _make_settings(tmp_path)

    import_calls = []
    fake_module = MagicMock()
    # The agent function must be callable so partial() works.
    fake_agent_fn = MagicMock()
    fake_module.run_audit_agent = fake_agent_fn

    def fake_import_module(name, package=None):
        import_calls.append((name, package))
        return fake_module

    monkeypatch.setattr(importlib_mod, "import_module", fake_import_module)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass",
        lambda **kw: _fake_agent_pass_result(),
    )

    run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["audit"],
        settings=settings,
    )

    assert len(import_calls) == 1
    assert import_calls[0][0] == ".agents.auditing"
    assert import_calls[0][1] == "robotsix_mill"


def test_run_periodic_pass_no_forge_remote_skips_clone(tmp_path, monkeypatch):
    """When no forge_remote_url is configured, repo_dir=None and no clone
    is attempted."""
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL

    clone_attempted = []

    def fake_clone(url, dest, branch, token):
        clone_attempted.append(True)

    from robotsix_mill.vcs import git_ops

    monkeypatch.setattr(git_ops, "clone", fake_clone)

    captured_repo_dir = {}

    def fake_run_agent_pass(agent_fn, repo_dir, **kw):
        captured_repo_dir["value"] = repo_dir
        return _fake_agent_pass_result()

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass", fake_run_agent_pass
    )

    run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=PERIODIC_PASS_CONFIGS["audit"],
        settings=settings,
    )

    assert len(clone_attempted) == 0
    assert captured_repo_dir["value"] is None


def test_run_periodic_pass_requires_repo_short_circuits_on_clone_failure(
    tmp_path, monkeypatch
):
    """For a requires_repo=True config (module_curator), a clone failure
    short-circuits before the agent: run_agent_pass is NOT called and a
    no-op result (empty drafts, correct session_id) is returned."""
    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )

    def fake_clone(url, dest, branch, token):
        raise subprocess.CalledProcessError(1, "git")

    from robotsix_mill.vcs import git_ops

    monkeypatch.setattr(git_ops, "clone", fake_clone)

    agent_pass_calls = []

    def fake_run_agent_pass(**kw):
        agent_pass_calls.append(True)
        return _fake_agent_pass_result()

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass", fake_run_agent_pass
    )

    config = PERIODIC_PASS_CONFIGS["module_curator"]
    result = run_periodic_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
        config=config,
        settings=settings,
    )

    assert len(agent_pass_calls) == 0
    assert isinstance(result, config.result_dataclass)
    assert result.drafts_created == []
    assert result.session_id == "test-sid"


def test_run_periodic_pass_requires_repo_short_circuit_logs_warning(
    tmp_path, monkeypatch, caplog
):
    """The module_curator short-circuit logs a warning naming the pass."""
    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )

    def fake_clone(url, dest, branch, token):
        raise subprocess.CalledProcessError(1, "git")

    from robotsix_mill.vcs import git_ops

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_pass",
        lambda **kw: _fake_agent_pass_result(),
    )

    with caplog.at_level(logging.WARNING):
        run_periodic_pass(
            session_id="test-sid",
            repo_config=_test_repo_config(),
            config=PERIODIC_PASS_CONFIGS["module_curator"],
            settings=settings,
        )

    assert any(
        "module_curator pass skipped" in record.message for record in caplog.records
    )


# ------------------------------------------------------------------ PERIODIC_PASS_CONFIGS registry


def test_periodic_pass_configs_registry_has_all_eleven_entries():
    """All 11 periodic passes are registered."""
    expected = {
        "audit",
        "agent_check",
        "bc_check",
        "survey",
        "completeness_check",
        "copy_paste",
        "forge_parity",
        "config_sync",
        "health",
        "module_curator",
        "test_gap",
    }
    assert set(PERIODIC_PASS_CONFIGS.keys()) == expected


def test_periodic_pass_configs_requires_repo_only_module_curator():
    """Only the module_curator entry sets requires_repo=True; all other
    registry entries keep the default False."""
    assert PERIODIC_PASS_CONFIGS["module_curator"].requires_repo is True
    for key, cfg in PERIODIC_PASS_CONFIGS.items():
        if key == "module_curator":
            continue
        assert cfg.requires_repo is False, f"{key}.requires_repo should be False"


def test_periodic_pass_configs_each_has_required_fields():
    """Every config has label, source_kind, agent_module_attr,
    agent_fn_name, memory_filename, workspace_subdir, result_dataclass
    set to non-None values."""
    required = [
        "label",
        "source_kind",
        "agent_module_attr",
        "agent_fn_name",
        "memory_filename",
        "workspace_subdir",
        "result_dataclass",
    ]
    for key, cfg in PERIODIC_PASS_CONFIGS.items():
        for field in required:
            assert getattr(cfg, field, None) is not None, f"{key}.{field} is None"
