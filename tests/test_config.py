"""Tests for the mail configuration subsystem."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from unittest import mock

import pytest

from robotsix_auto_mail.config import ConfigurationError, MailConfig, load

# ---------------------------------------------------------------------------
# MailConfig basics
# ---------------------------------------------------------------------------


def test_mailconfig_construction_defaults() -> None:
    """All required fields supplied; defaults kick in for optional fields."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
    )
    assert cfg.imap_port == 993
    assert cfg.imap_tls_mode == "direct-tls"
    assert cfg.smtp_port == 587
    assert cfg.smtp_tls_mode == "starttls"


def test_mailconfig_is_immutable() -> None:
    """MailConfig is frozen – no attribute assignment after creation."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="u",
        password="p",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.imap_host = "other"  # type: ignore[misc]


def test_mailconfig_repr_redacts_password() -> None:
    """repr() must NOT include the password value."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="u",
        password="s3cret",
    )
    r = repr(cfg)
    assert "s3cret" not in r
    assert "<redacted>" in r


def test_mailconfig_str_redacts_password() -> None:
    """str() must NOT include the password value."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="u",
        password="s3cret",
    )
    s = str(cfg)
    assert "s3cret" not in s
    assert "<redacted>" in s


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_from_env_all_required_present() -> None:
    """All required env vars set → valid config."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.example.com",
        "MAIL_SMTP_HOST": "smtp.example.com",
        "MAIL_USERNAME": "user@example.com",
        "MAIL_PASSWORD": "s3cret",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = MailConfig.from_env()
        assert cfg.imap_host == "imap.example.com"
        assert cfg.smtp_host == "smtp.example.com"
        assert cfg.username == "user@example.com"
        assert cfg.password == "s3cret"


def test_from_env_defaults_used_when_absent() -> None:
    """Optional env vars missing → defaults are used."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.example.com",
        "MAIL_SMTP_HOST": "smtp.example.com",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = MailConfig.from_env()
        assert cfg.imap_port == 993
        assert cfg.imap_tls_mode == "direct-tls"
        assert cfg.smtp_port == 587
        assert cfg.smtp_tls_mode == "starttls"


def test_from_env_optional_fields_applied() -> None:
    """All env vars, including optional, are read correctly."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.example.com",
        "MAIL_IMAP_PORT": "143",
        "MAIL_IMAP_TLS_MODE": "starttls",
        "MAIL_SMTP_HOST": "smtp.example.com",
        "MAIL_SMTP_PORT": "465",
        "MAIL_SMTP_TLS_MODE": "direct-tls",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = MailConfig.from_env()
        assert cfg.imap_port == 143
        assert cfg.imap_tls_mode == "starttls"
        assert cfg.smtp_port == 465
        assert cfg.smtp_tls_mode == "direct-tls"


def test_from_env_missing_required_multiple() -> None:
    """Missing multiple required vars → error lists all of them."""
    env: dict[str, str] = {
        "MAIL_SMTP_HOST": "smtp.example.com",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            MailConfig.from_env()
        msg = str(exc.value)
        assert "MAIL_IMAP_HOST" in msg
        assert "MAIL_SMTP_HOST" not in msg  # this one IS set
        assert "MAIL_USERNAME" in msg
        assert "MAIL_PASSWORD" in msg


def test_from_env_missing_all_required() -> None:
    """No env vars at all → error lists every required var."""
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            MailConfig.from_env()
        msg = str(exc.value)
        for key in (
            "MAIL_IMAP_HOST",
            "MAIL_SMTP_HOST",
            "MAIL_USERNAME",
            "MAIL_PASSWORD",
        ):
            assert key in msg


def test_from_env_invalid_port() -> None:
    """Non-integer port → ConfigurationError."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.example.com",
        "MAIL_IMAP_PORT": "not-a-number",
        "MAIL_SMTP_HOST": "smtp.example.com",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            MailConfig.from_env()
        msg = str(exc.value)
        assert "MAIL_IMAP_PORT" in msg
        assert "not-a-number" in msg


def test_from_env_invalid_tls_mode() -> None:
    """Invalid TLS mode → ConfigurationError."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.example.com",
        "MAIL_IMAP_TLS_MODE": "tls-1.3",
        "MAIL_SMTP_HOST": "smtp.example.com",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            MailConfig.from_env()
        msg = str(exc.value)
        assert "MAIL_IMAP_TLS_MODE" in msg
        assert "tls-1.3" in msg


def test_from_env_invalid_smtp_tls_mode() -> None:
    """Invalid SMTP TLS mode → ConfigurationError."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.example.com",
        "MAIL_SMTP_HOST": "smtp.example.com",
        "MAIL_SMTP_TLS_MODE": "nonexistent",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            MailConfig.from_env()
        msg = str(exc.value)
        assert "MAIL_SMTP_TLS_MODE" in msg


# ---------------------------------------------------------------------------
# from_toml
# ---------------------------------------------------------------------------


def test_from_toml_example_file() -> None:
    """The bundled example TOML file is valid and parses correctly."""
    cfg = MailConfig.from_toml("config/mail.example.toml")
    assert cfg.imap_host == "imap.example.com"
    assert cfg.imap_port == 993
    assert cfg.imap_tls_mode == "direct-tls"
    assert cfg.smtp_host == "smtp.example.com"
    assert cfg.smtp_port == 587
    assert cfg.smtp_tls_mode == "starttls"
    assert cfg.username == "user@example.com"
    assert cfg.password == "s3cret"


def test_from_toml_defaults_for_missing_fields(tmp_path: Path) -> None:
    """Fields missing from TOML fall back to defaults."""
    toml_file = tmp_path / "minimal.toml"
    toml_file.write_text(
        """\
[imap]
host = "imap.example.com"

[smtp]
host = "smtp.example.com"

[auth]
username = "u"
password = "p"
"""
    )
    cfg = MailConfig.from_toml(toml_file)
    assert cfg.imap_port == 993
    assert cfg.imap_tls_mode == "direct-tls"
    assert cfg.smtp_port == 587
    assert cfg.smtp_tls_mode == "starttls"


def test_from_toml_missing_required_fields(tmp_path: Path) -> None:
    """Missing required TOML fields → ConfigurationError with all names."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text(
        """\
[imap]
port = 993

[smtp]
tls_mode = "none"
"""
    )
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_toml(toml_file)
    msg = str(exc.value)
    assert "imap.host" in msg
    assert "smtp.host" in msg
    assert "auth.username" in msg
    assert "auth.password" in msg


def test_from_toml_invalid_tls_mode(tmp_path: Path) -> None:
    """Invalid TLS mode in TOML → ConfigurationError."""
    toml_file = tmp_path / "bad_tls.toml"
    toml_file.write_text(
        """\
[imap]
host = "imap.example.com"
tls_mode = "bad-mode"

[smtp]
host = "smtp.example.com"

[auth]
username = "u"
password = "p"
"""
    )
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_toml(toml_file)
    msg = str(exc.value)
    assert "imap.tls_mode" in msg
    assert "bad-mode" in msg


def test_from_toml_malformed_file(tmp_path: Path) -> None:
    """Malformed TOML → ConfigurationError."""
    toml_file = tmp_path / "malformed.toml"
    toml_file.write_text("this is not valid TOML {{{")
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_toml(toml_file)
    assert "Invalid TOML" in str(exc.value)


def test_from_toml_file_not_found(tmp_path: Path) -> None:
    """Missing file → FileNotFoundError (not swallowed)."""
    missing = tmp_path / "does_not_exist.toml"
    with pytest.raises(FileNotFoundError):
        MailConfig.from_toml(missing)


def test_from_toml_wrong_type_for_field(tmp_path: Path) -> None:
    """Field with wrong type (e.g. port as string) → ConfigurationError."""
    toml_file = tmp_path / "bad_port.toml"
    toml_file.write_text(
        """\
[imap]
host = "imap.example.com"
port = "not-a-number"

[smtp]
host = "smtp.example.com"

[auth]
username = "u"
password = "p"
"""
    )
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_toml(toml_file)
    assert "port" in str(exc.value)


# ---------------------------------------------------------------------------
# load() convenience function
# ---------------------------------------------------------------------------


def test_load_env_only() -> None:
    """load() with all env vars set returns env config (no TOML needed)."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.env.com",
        "MAIL_SMTP_HOST": "smtp.env.com",
        "MAIL_USERNAME": "env_user",
        "MAIL_PASSWORD": "env_pass",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load()
        assert cfg.imap_host == "imap.env.com"
        assert cfg.smtp_host == "smtp.env.com"
        assert cfg.username == "env_user"
        assert cfg.password == "env_pass"


def test_load_fallback_to_toml(tmp_path: Path) -> None:
    """No env vars → load() falls back to TOML at given path."""
    toml_file = tmp_path / "test.toml"
    toml_file.write_text(
        """\
[imap]
host = "imap.toml.com"

[smtp]
host = "smtp.toml.com"

[auth]
username = "toml_user"
password = "toml_pass"
"""
    )
    env: dict[str, str] = {"MAIL_CONFIG_PATH": str(toml_file)}
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load()
        assert cfg.imap_host == "imap.toml.com"
        assert cfg.smtp_host == "smtp.toml.com"
        assert cfg.username == "toml_user"
        assert cfg.password == "toml_pass"


def test_load_env_overrides_toml(tmp_path: Path) -> None:
    """Single env var overrides the corresponding TOML field."""
    toml_file = tmp_path / "test.toml"
    toml_file.write_text(
        """\
[imap]
host = "imap.toml.com"

[smtp]
host = "smtp.toml.com"

[auth]
username = "toml_user"
password = "toml_pass"
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(toml_file),
        "MAIL_IMAP_HOST": "imap.env.com",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load()
        # env wins for IMAP host
        assert cfg.imap_host == "imap.env.com"
        # SMTP still from TOML
        assert cfg.smtp_host == "smtp.toml.com"
        assert cfg.username == "toml_user"


def test_load_missing_config_file() -> None:
    """No env vars AND no config file → ConfigurationError."""
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": "/nonexistent/path/mail.toml",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError):
            load()


# ---------------------------------------------------------------------------
# ConfigurationError
# ---------------------------------------------------------------------------


def test_load_re_raises_on_invalid_value_not_missing(tmp_path: Path) -> None:
    """load() must NOT fall back to TOML when env has an invalid value.

    If from_env() fails because of an invalid value (e.g. a non-integer
    port), the user explicitly set the env var — falling back to TOML
    would silently swallow their typo.
    """
    toml_file = tmp_path / "test.toml"
    toml_file.write_text(
        """\
[imap]
host = "imap.toml.com"

[smtp]
host = "smtp.toml.com"

[auth]
username = "toml_user"
password = "toml_pass"
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(toml_file),
        "MAIL_IMAP_HOST": "imap.env.com",
        "MAIL_SMTP_HOST": "smtp.env.com",
        "MAIL_USERNAME": "env_user",
        "MAIL_PASSWORD": "env_pass",
        "MAIL_IMAP_PORT": "not-a-number",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            load()
        msg = str(exc.value)
        assert "MAIL_IMAP_PORT" in msg
        assert "not-a-number" in msg


def test_load_re_raises_on_invalid_tls_not_missing(tmp_path: Path) -> None:
    """load() must re-raise when TLS mode is invalid, even if all
    required fields are present."""
    toml_file = tmp_path / "test.toml"
    toml_file.write_text(
        """\
[imap]
host = "imap.toml.com"

[smtp]
host = "smtp.toml.com"

[auth]
username = "toml_user"
password = "toml_pass"
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(toml_file),
        "MAIL_IMAP_HOST": "imap.env.com",
        "MAIL_SMTP_HOST": "smtp.env.com",
        "MAIL_USERNAME": "env_user",
        "MAIL_PASSWORD": "env_pass",
        "MAIL_IMAP_TLS_MODE": "tls-9.9",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            load()
        msg = str(exc.value)
        assert "MAIL_IMAP_TLS_MODE" in msg
        assert "tls-9.9" in msg


def test_configuration_error_is_exception() -> None:
    """ConfigurationError is a proper Exception subclass."""
    err = ConfigurationError("test message")
    assert isinstance(err, Exception)
    assert str(err) == "test message"
    assert err.message == "test message"


def test_configuration_error_missing_only_default() -> None:
    """missing_only defaults to False."""
    err = ConfigurationError("test")
    assert err.missing_only is False


def test_configuration_error_missing_only_true() -> None:
    """missing_only can be set to True."""
    err = ConfigurationError("test", missing_only=True)
    assert err.missing_only is True
