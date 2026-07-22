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
        langfuse_public_key="pk-lf",
        langfuse_secret_key="sk-lf",
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
    assert s.langfuse_public_key == "pk-lf"
    assert s.langfuse_secret_key == "sk-lf"
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
    assert r.startswith("Secrets(") and r.endswith(")")


def test_repr_redacts_all_fields():
    """Every model field appears as '***' in the repr."""
    s = Secrets(openrouter_api_key="real-key")
    r = repr(s)
    for name in Secrets.model_fields:
        assert f"{name}='***'" in r, f"missing redacted field {name} in {r!r}"


# ===========================================================================
#  model_dump redaction
# ===========================================================================


def test_model_dump_redacted():
    """model_dump(redact=True) returns '***' for every field."""
    s = Secrets(openrouter_api_key="sk-abc", forge_token="ghp-xyz")
    d = s.model_dump(redact=True)
    assert d == {name: "***" for name in Secrets.model_fields}


def test_model_dump_raw():
    """model_dump(redact=False) returns the real values."""
    s = Secrets(openrouter_api_key="sk-raw")
    d = s.model_dump(redact=False)
    assert d["openrouter_api_key"] == "sk-raw"
    assert d["forge_token"] is None


def test_model_dump_default_is_redacted():
    """model_dump() without args redacts (default redact=True)."""
    s = Secrets(openrouter_api_key="sk-default")
    d = s.model_dump()
    assert d["openrouter_api_key"] == "***"


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


def test_getattribute_no_log_for_model_dump(caplog):
    """Accessing model_dump (a special name) is NOT logged."""
    s = Secrets()
    with caplog.at_level(logging.DEBUG, logger="robotsix_mill.config.secrets"):
        _ = s.model_dump
    assert "Secrets.model_dump accessed by" not in caplog.text


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
