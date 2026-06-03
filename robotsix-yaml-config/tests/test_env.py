"""Tests for overlay_env_vars."""

from __future__ import annotations

from robotsix_yaml_config import overlay_env_vars


def test_overlay_prefix_lookup_hits(monkeypatch):
    monkeypatch.setenv("APP_HOST", "example.com")
    config = {"host": "localhost"}
    result = overlay_env_vars(config, "APP")
    assert result == {"host": "example.com"}
    assert result is config


def test_overlay_str_default_unchanged_value(monkeypatch):
    monkeypatch.setenv("APP_NAME", "mill")
    config = {"name": "default"}
    # No type hint → str → value used verbatim.
    assert overlay_env_vars(config, "APP") == {"name": "mill"}


def test_overlay_int_coercion(monkeypatch):
    monkeypatch.setenv("APP_PORT", "8080")
    config = {"port": 3000}
    result = overlay_env_vars(config, "APP", {"port": int})
    assert result == {"port": 8080}
    assert isinstance(result["port"], int)


def test_overlay_float_coercion(monkeypatch):
    monkeypatch.setenv("APP_RATIO", "0.25")
    config = {"ratio": 1.0}
    result = overlay_env_vars(config, "APP", {"ratio": float})
    assert result == {"ratio": 0.25}


def test_overlay_bool_false_string(monkeypatch):
    monkeypatch.setenv("APP_DEBUG", "false")
    config = {"debug": True}
    result = overlay_env_vars(config, "APP", {"debug": bool})
    assert result == {"debug": False}


def test_overlay_bool_truthy_spellings(monkeypatch):
    for raw in ("1", "true", "TRUE", "yes", "on", "On"):
        monkeypatch.setenv("APP_FLAG", raw)
        config = {"flag": False}
        result = overlay_env_vars(config, "APP", {"flag": bool})
        assert result == {"flag": True}, raw


def test_overlay_bool_falsy_spellings(monkeypatch):
    for raw in ("0", "false", "FALSE", "no", "off", ""):
        monkeypatch.setenv("APP_FLAG", raw)
        config = {"flag": True}
        result = overlay_env_vars(config, "APP", {"flag": bool})
        assert result == {"flag": False}, raw


def test_overlay_unset_leaves_config_unchanged(monkeypatch):
    monkeypatch.delenv("APP_HOST", raising=False)
    config = {"host": "localhost", "port": 3000}
    result = overlay_env_vars(config, "APP", {"port": int})
    assert result == {"host": "localhost", "port": 3000}


def test_overlay_does_not_add_missing_keys(monkeypatch):
    monkeypatch.setenv("APP_EXTRA", "value")
    config = {"host": "localhost"}
    result = overlay_env_vars(config, "APP")
    assert result == {"host": "localhost"}
    assert "extra" not in result


def test_overlay_uppercases_key(monkeypatch):
    # config key is lowercase; env var must be the uppercased form.
    monkeypatch.setenv("APP_DATA_DIR", "/data")
    config = {"data_dir": "/tmp"}
    assert overlay_env_vars(config, "APP") == {"data_dir": "/data"}
