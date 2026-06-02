"""Tests for the mail configuration subsystem."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from unittest import mock

import pytest

from robotsix_auto_mail.config import (
    DEFAULT_LLM_MODEL,
    ConfigurationError,
    MailConfig,
    load,
    load_llm,
)

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
    assert cfg.imap_folder == "INBOX"


def test_mailconfig_imap_folder_explicit() -> None:
    """imap_folder can be set explicitly."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="u",
        password="p",
        imap_folder="Archive",
    )
    assert cfg.imap_folder == "Archive"


def test_mailconfig_is_immutable() -> None:
    """MailConfig is frozen - no attribute assignment after creation."""
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
        assert cfg.imap_folder == "INBOX"


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
        "MAIL_IMAP_FOLDER": "Archive",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = MailConfig.from_env()
        assert cfg.imap_port == 143
        assert cfg.imap_tls_mode == "starttls"
        assert cfg.smtp_port == 465
        assert cfg.smtp_tls_mode == "direct-tls"
        assert cfg.imap_folder == "Archive"


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
# from_yaml
# ---------------------------------------------------------------------------


def test_from_yaml_example_file() -> None:
    """The bundled example YAML file is valid and parses correctly."""
    cfg = MailConfig.from_yaml("config/mail.local.example.yaml")
    assert cfg.imap_host == "imap.example.com"
    assert cfg.imap_port == 993
    assert cfg.imap_tls_mode == "direct-tls"
    assert cfg.smtp_host == "smtp.example.com"
    assert cfg.smtp_port == 587
    assert cfg.smtp_tls_mode == "starttls"
    assert cfg.username == "user@example.com"
    assert cfg.password == ""
    assert cfg.imap_folder == "INBOX"


def test_from_yaml_defaults_for_missing_fields(tmp_path: Path) -> None:
    """Fields missing from YAML fall back to defaults."""
    yaml_file = tmp_path / "minimal.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.example.com

smtp:
  host: smtp.example.com

auth:
  username: u
  password: p
"""
    )
    cfg = MailConfig.from_yaml(yaml_file)
    assert cfg.imap_port == 993
    assert cfg.imap_tls_mode == "direct-tls"
    assert cfg.smtp_port == 587
    assert cfg.smtp_tls_mode == "starttls"
    assert cfg.imap_folder == "INBOX"


def test_from_yaml_custom_imap_folder(tmp_path: Path) -> None:
    """imap_folder can be set via YAML imap.folder key."""
    yaml_file = tmp_path / "folder.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.example.com
  folder: Archive

smtp:
  host: smtp.example.com

auth:
  username: u
  password: p
"""
    )
    cfg = MailConfig.from_yaml(yaml_file)
    assert cfg.imap_folder == "Archive"


def test_from_yaml_missing_required_fields(tmp_path: Path) -> None:
    """Missing required YAML fields → ConfigurationError with all names."""
    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text(
        """\
imap:
  port: 993

smtp:
  tls_mode: none
"""
    )
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_yaml(yaml_file)
    msg = str(exc.value)
    assert "imap.host" in msg
    assert "smtp.host" in msg
    assert "auth.username" in msg
    # auth.password is not required — it can come from the MAIL_PASSWORD env var


def test_from_yaml_invalid_tls_mode(tmp_path: Path) -> None:
    """Invalid TLS mode in YAML → ConfigurationError."""
    yaml_file = tmp_path / "bad_tls.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.example.com
  tls_mode: bad-mode

smtp:
  host: smtp.example.com

auth:
  username: u
  password: p
"""
    )
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_yaml(yaml_file)
    msg = str(exc.value)
    assert "imap.tls_mode" in msg
    assert "bad-mode" in msg


def test_from_yaml_malformed_file(tmp_path: Path) -> None:
    """Malformed YAML → ConfigurationError."""
    yaml_file = tmp_path / "malformed.yaml"
    yaml_file.write_text("this: [is not: valid: YAML")
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_yaml(yaml_file)
    assert "Invalid YAML" in str(exc.value)


def test_from_yaml_file_not_found(tmp_path: Path) -> None:
    """Missing file → FileNotFoundError (not swallowed)."""
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        MailConfig.from_yaml(missing)


def test_from_yaml_wrong_type_for_field(tmp_path: Path) -> None:
    """Field with wrong type (e.g. port as string) → ConfigurationError."""
    yaml_file = tmp_path / "bad_port.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.example.com
  port: not-a-number

smtp:
  host: smtp.example.com

auth:
  username: u
  password: p
"""
    )
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_yaml(yaml_file)
    msg = str(exc.value)
    assert "port" in msg


def test_from_yaml_validate_false_skips_required_checks(
    tmp_path: Path,
) -> None:
    """validate=False skips required-field validation (used for defaults)."""
    yaml_file = tmp_path / "defaults.yaml"
    yaml_file.write_text(
        """\
imap:
  host: ""
  port: 993

smtp:
  host: ""

auth:
  username: ""
  password: ""
"""
    )
    # With validate=True (default), missing required fields should error.
    with pytest.raises(ConfigurationError):
        MailConfig.from_yaml(yaml_file, validate=True)

    # With validate=False, it should succeed — defaults loader path.
    cfg = MailConfig.from_yaml(yaml_file, validate=False)
    assert cfg.imap_host == ""
    assert cfg.imap_port == 993
    assert cfg.smtp_host == ""
    assert cfg.username == ""
    assert cfg.password == ""


def test_from_yaml_null_file_produces_defaults(tmp_path: Path) -> None:
    """A YAML file containing only null / empty → defaults (with validate=False)."""
    yaml_file = tmp_path / "null.yaml"
    yaml_file.write_text("")
    cfg = MailConfig.from_yaml(yaml_file, validate=False)
    assert cfg.imap_host == ""
    assert cfg.imap_port == 993
    assert cfg.imap_tls_mode == "direct-tls"
    assert cfg.smtp_host == ""
    assert cfg.smtp_port == 587
    assert cfg.smtp_tls_mode == "starttls"
    assert cfg.username == ""
    assert cfg.password == ""


def test_from_yaml_root_not_mapping(tmp_path: Path) -> None:
    """A YAML file whose root is not a mapping → ConfigurationError."""
    yaml_file = tmp_path / "list.yaml"
    yaml_file.write_text("- item1\n- item2\n")
    with pytest.raises(ConfigurationError) as exc:
        MailConfig.from_yaml(yaml_file)
    assert "mapping" in str(exc.value).lower()


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


def test_load_fallback_to_yaml(tmp_path: Path) -> None:
    """No env vars → load() falls back to the YAML file at given path."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.file.com

smtp:
  host: smtp.file.com

auth:
  username: file_user
  password: file_pass
"""
    )
    env: dict[str, str] = {"MAIL_CONFIG_PATH": str(yaml_file)}
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load()
        assert cfg.imap_host == "imap.file.com"
        assert cfg.smtp_host == "smtp.file.com"
        assert cfg.username == "file_user"
        assert cfg.password == "file_pass"


def test_load_env_overrides_file(tmp_path: Path) -> None:
    """Single env var overrides the corresponding YAML field."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.file.com

smtp:
  host: smtp.file.com

auth:
  username: file_user
  password: file_pass
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(yaml_file),
        "MAIL_IMAP_HOST": "imap.env.com",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load()
        # env wins for IMAP host
        assert cfg.imap_host == "imap.env.com"
        # SMTP still from file
        assert cfg.smtp_host == "smtp.file.com"
        assert cfg.username == "file_user"


def test_load_env_overrides_file_folder(tmp_path: Path) -> None:
    """MAIL_IMAP_FOLDER env var overrides the YAML folder."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.file.com
  folder: INBOX

smtp:
  host: smtp.file.com

auth:
  username: file_user
  password: file_pass
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(yaml_file),
        "MAIL_IMAP_FOLDER": "Archive",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load()
        assert cfg.imap_folder == "Archive"


def test_load_missing_config_file() -> None:
    """No env vars AND no config file → ConfigurationError."""
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": "/nonexistent/path/mail.yaml",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError):
            load()


def test_load_file_supplies_defaults_env_overrides(tmp_path: Path) -> None:
    """load() takes file values, with env winning field-by-field and
    dataclass defaults filling the rest."""
    local_file = tmp_path / "mail.local.yaml"
    local_file.write_text(
        """\
imap:
  host: imap.file.com

smtp:
  host: smtp.from.local.com

auth:
  username: file_user
  password: file_pass

store:
  path: /file/path/mail.db
"""
    )

    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(local_file),
        "MAIL_SMTP_HOST": "smtp.from.env.com",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load()

    # imap.host from the file.
    assert cfg.imap_host == "imap.file.com"
    # SMTP from file, overridden by env.
    assert cfg.smtp_host == "smtp.from.env.com"
    # Auth from file.
    assert cfg.username == "file_user"
    assert cfg.password == "file_pass"
    # port / tls_mode fall back to dataclass defaults (not in file/env).
    assert cfg.imap_port == 993
    assert cfg.imap_tls_mode == "direct-tls"
    assert cfg.smtp_port == 587
    assert cfg.smtp_tls_mode == "starttls"
    # db_path from file.
    assert cfg.db_path == "/file/path/mail.db"
    # imap_folder falls back to the default.
    assert cfg.imap_folder == "INBOX"


# ---------------------------------------------------------------------------
# ConfigurationError
# ---------------------------------------------------------------------------


def test_load_re_raises_on_invalid_value_not_missing(tmp_path: Path) -> None:
    """load() must NOT fall back to the file when env has an invalid value.

    If from_env() fails because of an invalid value (e.g. a non-integer
    port), the user explicitly set the env var — falling back to the
    file would silently swallow their typo.
    """
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.file.com

smtp:
  host: smtp.file.com

auth:
  username: file_user
  password: file_pass
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(yaml_file),
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
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.file.com

smtp:
  host: smtp.file.com

auth:
  username: file_user
  password: file_pass
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(yaml_file),
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


# ---------------------------------------------------------------------------
# from_yaml: password not required (it can be supplied via MAIL_PASSWORD)
# ---------------------------------------------------------------------------


def test_from_yaml_missing_auth_password_ok(tmp_path: Path) -> None:
    """from_yaml with validate=True does NOT require auth.password."""
    yaml_file = tmp_path / "no_pass.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.example.com

smtp:
  host: smtp.example.com

auth:
  username: user@example.com
"""
    )
    cfg = MailConfig.from_yaml(yaml_file, validate=True)
    assert cfg.password == ""
    assert cfg.username == "user@example.com"


# -- from_env still requires MAIL_PASSWORD --------------------------------


def test_from_env_still_requires_mail_password() -> None:
    """from_env raises ConfigurationError when MAIL_PASSWORD is missing."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "imap.example.com",
        "MAIL_SMTP_HOST": "smtp.example.com",
        "MAIL_USERNAME": "user@example.com",
        # MAIL_PASSWORD intentionally missing
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(ConfigurationError) as exc:
            MailConfig.from_env()
        msg = str(exc.value)
        assert "MAIL_PASSWORD" in msg


# ---------------------------------------------------------------------------
# LLM settings (llm: section + LLM_* env vars)
# ---------------------------------------------------------------------------


def test_llm_defaults_when_absent() -> None:
    """llm fields default to empty key + the default model."""
    cfg = MailConfig(
        imap_host="i", smtp_host="s", username="u", password="p"
    )
    assert cfg.llm_api_key == ""
    assert cfg.llm_model == DEFAULT_LLM_MODEL


def test_llm_api_key_redacted_in_repr() -> None:
    """repr()/str() must NOT leak the LLM API key."""
    cfg = MailConfig(
        imap_host="i",
        smtp_host="s",
        username="u",
        password="p",
        llm_api_key="sk-or-secret",
    )
    assert "sk-or-secret" not in repr(cfg)
    assert "sk-or-secret" not in str(cfg)
    assert "<redacted>" in repr(cfg)


def test_from_yaml_reads_llm_section(tmp_path: Path) -> None:
    """from_yaml parses the optional llm: section."""
    yaml_file = tmp_path / "with_llm.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.example.com

smtp:
  host: smtp.example.com

auth:
  username: u
  password: p

llm:
  api_key: sk-or-from-file
  model: anthropic/claude-3-haiku
"""
    )
    cfg = MailConfig.from_yaml(yaml_file)
    assert cfg.llm_api_key == "sk-or-from-file"
    assert cfg.llm_model == "anthropic/claude-3-haiku"


def test_from_env_reads_llm_vars() -> None:
    """from_env picks up LLM_API_KEY / LLM_MODEL."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "i",
        "MAIL_SMTP_HOST": "s",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
        "LLM_API_KEY": "sk-env",
        "LLM_MODEL": "env/model",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = MailConfig.from_env()
        assert cfg.llm_api_key == "sk-env"
        assert cfg.llm_model == "env/model"


def test_load_llm_env_wins() -> None:
    """load_llm prefers the environment variables."""
    env: dict[str, str] = {
        "LLM_API_KEY": "sk-env",
        "LLM_MODEL": "env/model",
        # point at a path that does not exist so the file branch is skipped
        "MAIL_CONFIG_PATH": "/nonexistent/mail.yaml",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        assert load_llm() == ("sk-env", "env/model")


def test_load_llm_falls_back_to_file(tmp_path: Path) -> None:
    """load_llm reads the llm: section when env vars are absent."""
    yaml_file = tmp_path / "mail.local.yaml"
    yaml_file.write_text(
        """\
llm:
  api_key: sk-from-file
  model: file/model
"""
    )
    env: dict[str, str] = {"MAIL_CONFIG_PATH": str(yaml_file)}
    with mock.patch.dict(os.environ, env, clear=True):
        assert load_llm() == ("sk-from-file", "file/model")


def test_load_llm_env_key_file_model(tmp_path: Path) -> None:
    """load_llm mixes sources: env key + file model."""
    yaml_file = tmp_path / "mail.local.yaml"
    yaml_file.write_text("llm:\n  model: file/model\n")
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(yaml_file),
        "LLM_API_KEY": "sk-env",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        assert load_llm() == ("sk-env", "file/model")


def test_load_llm_default_model_when_nothing_set() -> None:
    """load_llm returns an empty key and the default model when unset."""
    env: dict[str, str] = {"MAIL_CONFIG_PATH": "/nonexistent/mail.yaml"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert load_llm() == ("", DEFAULT_LLM_MODEL)


# ---------------------------------------------------------------------------
# Ingest interval (ingest.interval_minutes + MAIL_INGEST_INTERVAL)
# ---------------------------------------------------------------------------


def test_ingest_interval_default() -> None:
    """ingest_interval_minutes defaults to 15."""
    cfg = MailConfig(
        imap_host="i", smtp_host="s", username="u", password="p"
    )
    assert cfg.ingest_interval_minutes == 15


def test_from_yaml_reads_ingest_interval(tmp_path: Path) -> None:
    """from_yaml parses the ingest.interval_minutes key."""
    yaml_file = tmp_path / "iv.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.example.com

smtp:
  host: smtp.example.com

auth:
  username: u
  password: p

ingest:
  interval_minutes: 5
"""
    )
    cfg = MailConfig.from_yaml(yaml_file)
    assert cfg.ingest_interval_minutes == 5


def test_from_env_reads_ingest_interval() -> None:
    """from_env reads MAIL_INGEST_INTERVAL."""
    env: dict[str, str] = {
        "MAIL_IMAP_HOST": "i",
        "MAIL_SMTP_HOST": "s",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
        "MAIL_INGEST_INTERVAL": "30",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        assert MailConfig.from_env().ingest_interval_minutes == 30


def test_load_env_overrides_ingest_interval(tmp_path: Path) -> None:
    """MAIL_INGEST_INTERVAL overrides the file's ingest.interval_minutes."""
    yaml_file = tmp_path / "mail.local.yaml"
    yaml_file.write_text(
        """\
imap:
  host: imap.file.com

smtp:
  host: smtp.file.com

auth:
  username: u
  password: p

ingest:
  interval_minutes: 5
"""
    )
    env: dict[str, str] = {
        "MAIL_CONFIG_PATH": str(yaml_file),
        "MAIL_INGEST_INTERVAL": "9",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        assert load().ingest_interval_minutes == 9
