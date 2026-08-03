"""Unit tests for the ``Secrets`` model and its cached accessors."""

from __future__ import annotations

import logging

from robotsix_mill.config.secrets import (
    Secrets,
    _reset_secrets,
    get_secrets,
    load_secrets,
)

# ===========================================================================
#  Construction from kwargs
# ===========================================================================


def test_secrets_from_kwargs():
    """Construct Secrets with explicit field values and verify each."""
    s = Secrets(
        openrouter_api_key="sk-test",
        forge_token="ghp_fake",
        forge_repo_create_token="ghp_create",
        sandbox_push_token="ghp_push_bridge",
        github_app_id="12345",
        github_app_private_key="-----BEGIN RSA PRIVATE KEY-----",
        openrouter_management_key="mgmt-key",
        ntfy_url="https://ntfy.example.com",
        ntfy_token="tk-ntfy",
    )
    assert s.openrouter_api_key == "sk-test"
    assert s.forge_token == "ghp_fake"
    assert s.forge_repo_create_token == "ghp_create"
    assert s.sandbox_push_token == "ghp_push_bridge"
    assert s.github_app_id == "12345"
    assert s.github_app_private_key == "-----BEGIN RSA PRIVATE KEY-----"
    assert s.openrouter_management_key == "mgmt-key"
    assert s.ntfy_url == "https://ntfy.example.com"
    assert s.ntfy_token == "tk-ntfy"


def test_secrets_defaults_are_none():
    """All fields default to None when no kwargs or JSON data provided."""
    s = Secrets()
    for name in Secrets.model_fields:
        assert getattr(s, name) is None, (
            f"expected {name}=None, got {getattr(s, name)!r}"
        )


# ===========================================================================
#  repr redaction
# ===========================================================================


def test_repr_redacts_all_values():
    """repr(Secrets(...)) must contain '***' for every field, never a raw value."""
    s = Secrets(openrouter_api_key="sk-abc123", forge_token="ghp_secret")
    r = repr(s)
    assert "sk-abc123" not in r
    assert "ghp_secret" not in r
    assert "***" in r
    # repr format: Secrets(openrouter_api_key='***', forge_token='***', ...)
    assert r.startswith("Secrets(")
    assert r.endswith(")")


def test_repr_redacts_all_fields():
    """Every model field appears as '***' in the repr."""
    s = Secrets(openrouter_api_key="real-key")
    r = repr(s)
    for name in Secrets.model_fields:
        assert f"{name}='***'" in r, f"missing redacted field {name} in {r!r}"


# ===========================================================================
#  __getattribute__ debug logging
# ===========================================================================


def test_getattribute_logs_on_field_access(caplog):
    """Accessing a public field logs at DEBUG with the caller module name."""
    s = Secrets(openrouter_api_key="sk-log")
    with caplog.at_level(logging.DEBUG, logger="robotsix_mill.config.secrets"):
        _ = s.openrouter_api_key
    assert "Secrets.openrouter_api_key accessed by" in caplog.text


def test_getattribute_logs_caller_module(caplog):
    """The log message includes the caller's __name__ (this test module)."""
    s = Secrets(forge_token="tk")
    with caplog.at_level(logging.DEBUG, logger="robotsix_mill.config.secrets"):
        _ = s.forge_token
    assert __name__ in caplog.text


def test_getattribute_no_log_for_model_fields(caplog):
    """Accessing model_fields (a special name) is NOT logged."""
    import warnings

    s = Secrets()
    with caplog.at_level(logging.DEBUG, logger="robotsix_mill.config.secrets"):
        # model_fields access on an instance triggers a Pydantic
        # deprecation warning (since V2.11), but that's orthogonal to
        # the logging exclusion we're testing.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _ = s.model_fields
    assert "Secrets.model_fields accessed by" not in caplog.text


def test_getattribute_no_log_for_private_attr(caplog):
    """Accessing a private attribute (starts with _) is NOT logged."""
    s = Secrets()
    with caplog.at_level(logging.DEBUG, logger="robotsix_mill.config.secrets"):
        # __class__ is in the exclusion set
        _ = s.__class__
    assert "Secrets.__class__ accessed by" not in caplog.text


# ===========================================================================
#  Caching: get_secrets / _reset_secrets
# ===========================================================================


def test_get_secrets_returns_secrets_instance():
    """get_secrets() returns a Secrets instance."""
    s = get_secrets()
    assert isinstance(s, Secrets)


def test_get_secrets_caches(monkeypatch):
    """get_secrets() returns the same object on repeated calls."""
    _reset_secrets()
    s1 = get_secrets()
    s2 = get_secrets()
    assert s1 is s2


def test_reset_secrets_clears_cache(monkeypatch):
    """_reset_secrets() clears the cache so get_secrets() builds a fresh one."""
    _reset_secrets()
    s1 = get_secrets()
    _reset_secrets()
    s2 = get_secrets()
    assert s1 is not s2


def test_get_secrets_respects_module_attribute():
    """get_secrets() reads the package-level _secrets attribute at call time,
    so assigning it directly is visible."""
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="injected")
    result = get_secrets()
    assert result.openrouter_api_key == "injected"
    # Clean up so other tests aren't affected.
    _reset_secrets()


# ===========================================================================
#  load_secrets
# ===========================================================================


def test_load_secrets_returns_secrets():
    """load_secrets() returns a Secrets instance."""
    s = load_secrets()
    assert isinstance(s, Secrets)


def test_load_secrets_explicit_file(tmp_path):
    """load_secrets with an explicit JSON file reads its secrets: block."""
    json_path = tmp_path / "secrets.json"
    json_path.write_text(
        '{"secrets": {"openrouter_api_key": "sk-from-file", "forge_token": "ghp-from-file"}}'
    )
    s = load_secrets(str(json_path))
    assert s.openrouter_api_key == "sk-from-file"
    assert s.forge_token == "ghp-from-file"


def test_load_secrets_missing_file_returns_defaults():
    """load_secrets with a non-existent file returns all-None Secrets."""
    s = load_secrets("/nonexistent/path/secrets.json")
    assert isinstance(s, Secrets)
    for name in Secrets.model_fields:
        assert getattr(s, name) is None, f"expected {name}=None for missing file"


def test_load_secrets_empty_file(tmp_path):
    """load_secrets with an empty JSON file returns all-None Secrets."""
    json_path = tmp_path / "empty.json"
    json_path.write_text("{}")
    s = load_secrets(str(json_path))
    for name in Secrets.model_fields:
        assert getattr(s, name) is None


# ===========================================================================
#  SECRET sentinel handling
# ===========================================================================


def test_secret_sentinel_treated_as_unset(tmp_path):
    """Values equal to the 'SECRET' sentinel are dropped, falling back to None."""
    json_path = tmp_path / "sentinel.json"
    json_path.write_text(
        '{"secrets": {"openrouter_api_key": "SECRET", "forge_token": "SECRET"}}'
    )
    s = load_secrets(str(json_path))
    assert s.openrouter_api_key is None
    assert s.forge_token is None


def test_secret_sentinel_mixed_with_real(tmp_path):
    """SECRET sentinel values are dropped while real values pass through."""
    json_path = tmp_path / "mixed.json"
    json_path.write_text(
        '{"secrets": {"openrouter_api_key": "sk-real", "forge_token": "SECRET"}}'
    )
    s = load_secrets(str(json_path))
    assert s.openrouter_api_key == "sk-real"
    assert s.forge_token is None


# ===========================================================================
#  Edge cases
# ===========================================================================


def test_kwargs_override_json_file(tmp_path):
    """Explicit kwargs override values from the JSON file."""
    json_path = tmp_path / "override.json"
    json_path.write_text(
        '{"secrets": {"openrouter_api_key": "sk-file", "forge_token": "ghp-file"}}'
    )
    s = Secrets(_secrets_file=str(json_path), openrouter_api_key="sk-override")
    assert s.openrouter_api_key == "sk-override"
    assert s.forge_token == "ghp-file"


def test_empty_secrets_block(tmp_path):
    """An empty secrets block yields all None."""
    json_path = tmp_path / "empty_block.json"
    json_path.write_text('{"secrets": {}}')
    s = load_secrets(str(json_path))
    for name in Secrets.model_fields:
        assert getattr(s, name) is None


# ===========================================================================
#  Main-config-file sourcing (regression: #2525 left Secrets with no source)
# ===========================================================================


def test_bare_secrets_reads_main_config_file(monkeypatch, tmp_path):
    """A bare ``Secrets()`` picks up the main config file's secrets block.

    The clean-cutover (#2525) rewrote Secrets to read only explicit kwargs
    and an explicit ``_secrets_file``, so every credential in the
    operator's ``config.json`` read back as None — mill could neither call
    an LLM nor authenticate to GitHub.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"settings": {"data_dir": "/tmp/x"},'
        ' "secrets": {"openrouter_api_key": "sk-main", "github_app_id": "999"}}'
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(cfg))
    s = Secrets()
    assert s.openrouter_api_key == "sk-main"
    assert s.github_app_id == "999"
    assert s.forge_token is None


def test_get_secrets_reads_main_config_file(monkeypatch, tmp_path):
    """The cached ``get_secrets()`` accessor sees the config file too.

    This is the accessor every call site uses (``get_secrets().openrouter_api_key``).
    """
    cfg = tmp_path / "config.json"
    cfg.write_text('{"secrets": {"openrouter_api_key": "sk-cached"}}')
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(cfg))
    _reset_secrets()
    assert get_secrets().openrouter_api_key == "sk-cached"


def test_robotsix_config_file_points_to_secrets_source(monkeypatch, tmp_path):
    """``ROBOTSIX_CONFIG_FILE`` is the single source for secrets."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"secrets": {"openrouter_api_key": "sk-from-cfg"}}')
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(cfg))
    assert Secrets().openrouter_api_key == "sk-from-cfg"


def test_explicit_secrets_file_beats_main_config(monkeypatch, tmp_path):
    """An explicit ``_secrets_file`` still wins over the main config file."""
    main = tmp_path / "config.json"
    main.write_text('{"secrets": {"openrouter_api_key": "sk-main"}}')
    explicit = tmp_path / "explicit.json"
    explicit.write_text('{"secrets": {"openrouter_api_key": "sk-explicit"}}')
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(main))
    assert Secrets(_secrets_file=str(explicit)).openrouter_api_key == "sk-explicit"


def test_kwargs_override_main_config_file(monkeypatch, tmp_path):
    """Explicit kwargs beat the main config file."""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"secrets": {"openrouter_api_key": "sk-main", "forge_token": "ghp-main"}}'
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(cfg))
    s = Secrets(openrouter_api_key="sk-kwarg")
    assert s.openrouter_api_key == "sk-kwarg"
    assert s.forge_token == "ghp-main"


def test_malformed_main_config_yields_all_none(monkeypatch, tmp_path):
    """A malformed config file means "all unset", never an exception."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json at all")
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(cfg))
    s = Secrets()
    for name in Secrets.model_fields:
        assert getattr(s, name) is None
