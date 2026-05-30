"""Tests for the email provider detection subsystem."""

from __future__ import annotations

import os
import urllib.error
from pathlib import Path
from unittest import mock

import pydantic
import pytest

from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.detect import (
    DetectedProvider,
    DetectionError,
    MailProvider,
    autoconfig_lookup,
    detect_provider,
    provider_to_config,
    render_config,
)


class _FakeResp:
    """Minimal stand-in for an ``http.client.HTTPResponse`` used by urlopen."""

    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


_ISPDB_XML = """\
<clientConfig version="1.1">
  <emailProvider id="example.net">
    <incomingServer type="imap">
      <hostname>imap.example.net</hostname>
      <port>993</port>
      <socketType>SSL</socketType>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>smtp.example.net</hostname>
      <port>587</port>
      <socketType>STARTTLS</socketType>
    </outgoingServer>
  </emailProvider>
</clientConfig>
"""

# ---------------------------------------------------------------------------
# DetectedProvider — validation
# ---------------------------------------------------------------------------


def test_detected_provider_valid_construction() -> None:
    """A DetectedProvider with both required hosts constructs fine."""
    dp = DetectedProvider(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    assert dp.imap_host == "imap.example.com"
    assert dp.smtp_host == "smtp.example.com"


def test_detected_provider_defaults() -> None:
    """Fields not supplied fall back to their declared defaults."""
    dp = DetectedProvider(imap_host="imap.example.com", smtp_host="smtp.example.com")
    assert dp.imap_port == 993
    assert dp.imap_tls_mode == "direct-tls"
    assert dp.smtp_port == 587
    assert dp.smtp_tls_mode == "starttls"


def test_detected_provider_missing_imap_host() -> None:
    """Missing imap_host raises pydantic.ValidationError."""
    with pytest.raises(pydantic.ValidationError):
        DetectedProvider(smtp_host="smtp.example.com")  # type: ignore[call-arg]


def test_detected_provider_missing_smtp_host() -> None:
    """Missing smtp_host raises pydantic.ValidationError."""
    with pytest.raises(pydantic.ValidationError):
        DetectedProvider(imap_host="imap.example.com")  # type: ignore[call-arg]


def test_detected_provider_imap_port_zero() -> None:
    """imap_port=0 violates ge=1."""
    with pytest.raises(pydantic.ValidationError):
        DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_port=0,
        )


def test_detected_provider_imap_port_negative() -> None:
    """imap_port=-1 violates ge=1."""
    with pytest.raises(pydantic.ValidationError):
        DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_port=-1,
        )


def test_detected_provider_imap_port_over_max() -> None:
    """imap_port=65536 violates le=65535."""
    with pytest.raises(pydantic.ValidationError):
        DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_port=65536,
        )


def test_detected_provider_invalid_imap_tls_mode() -> None:
    """An invalid imap_tls_mode string raises pydantic.ValidationError."""
    with pytest.raises(pydantic.ValidationError) as exc:
        DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_tls_mode="bad",
        )
    assert "imap_tls_mode" in str(exc.value)
    assert "bad" in str(exc.value)


def test_detected_provider_invalid_smtp_tls_mode() -> None:
    """An invalid smtp_tls_mode string raises pydantic.ValidationError."""
    with pytest.raises(pydantic.ValidationError) as exc:
        DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            smtp_tls_mode="bad",
        )
    assert "smtp_tls_mode" in str(exc.value)
    assert "bad" in str(exc.value)


def test_detected_provider_accepts_valid_tls_modes() -> None:
    """All three valid TLS modes are accepted for both fields."""
    for mode in ("starttls", "direct-tls", "none"):
        dp = DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_tls_mode=mode,
            smtp_tls_mode=mode,
        )
        assert dp.imap_tls_mode == mode
        assert dp.smtp_tls_mode == mode


# ---------------------------------------------------------------------------
# MailProvider
# ---------------------------------------------------------------------------


def test_mail_provider_construction_all_fields() -> None:
    """MailProvider can be constructed with every field explicit."""
    mp = MailProvider(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        imap_port=143,
        imap_tls_mode="starttls",
        smtp_port=465,
        smtp_tls_mode="direct-tls",
    )
    assert mp.imap_host == "imap.example.com"
    assert mp.smtp_host == "smtp.example.com"
    assert mp.imap_port == 143
    assert mp.imap_tls_mode == "starttls"
    assert mp.smtp_port == 465
    assert mp.smtp_tls_mode == "direct-tls"


def test_mail_provider_default_values() -> None:
    """MailProvider ports and TLS modes have expected defaults."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    assert mp.imap_port == 993
    assert mp.imap_tls_mode == "direct-tls"
    assert mp.smtp_port == 587
    assert mp.smtp_tls_mode == "starttls"


def test_mail_provider_is_immutable() -> None:
    """MailProvider is frozen — no attribute assignment after creation."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        mp.imap_host = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# provider_to_config
# ---------------------------------------------------------------------------


def test_provider_to_config_maps_correctly() -> None:
    """provider_to_config maps all MailProvider fields to MailConfig."""
    mp = MailProvider(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        imap_port=143,
        imap_tls_mode="starttls",
        smtp_port=465,
        smtp_tls_mode="direct-tls",
    )
    cfg = provider_to_config(mp, "user@example.com")
    assert cfg.imap_host == "imap.example.com"
    assert cfg.imap_port == 143
    assert cfg.imap_tls_mode == "starttls"
    assert cfg.smtp_host == "smtp.example.com"
    assert cfg.smtp_port == 465
    assert cfg.smtp_tls_mode == "direct-tls"
    assert cfg.username == "user@example.com"


def test_provider_to_config_password_defaults_empty() -> None:
    """The password field defaults to the empty string."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    cfg = provider_to_config(mp, "user@example.com")
    assert cfg.password == ""


def test_provider_to_config_password_forwarded() -> None:
    """An explicit password is forwarded to MailConfig."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    cfg = provider_to_config(mp, "user@example.com", password="s3cret")
    assert cfg.password == "s3cret"


def test_provider_to_config_imap_folder_defaults_to_inbox() -> None:
    """imap_folder defaults to 'INBOX'."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    cfg = provider_to_config(mp, "user@example.com")
    assert cfg.imap_folder == "INBOX"


def test_provider_to_config_default_db_path() -> None:
    """db_path defaults to '.data/mail.db' when not overridden."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    cfg = provider_to_config(mp, "user@example.com")
    assert cfg.db_path == ".data/mail.db"


def test_provider_to_config_explicit_db_path() -> None:
    """An explicit db_path is forwarded to MailConfig."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    cfg = provider_to_config(mp, "user@example.com", db_path="custom/path.db")
    assert cfg.db_path == "custom/path.db"


# ---------------------------------------------------------------------------
# render_config — YAML
# ---------------------------------------------------------------------------


def test_render_config_contains_imap_fields() -> None:
    """Output contains the imap section with correct values."""
    mp = MailProvider(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    cfg = provider_to_config(mp, "user@example.com")
    output = render_config(cfg)
    assert "imap:" in output
    assert "host: imap.example.com" in output
    assert "port: 993" in output
    assert "tls_mode: direct-tls" in output
    assert "folder: INBOX" in output


def test_render_config_contains_smtp_fields() -> None:
    """Output contains the smtp section with correct values."""
    mp = MailProvider(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    cfg = provider_to_config(mp, "user@example.com")
    output = render_config(cfg)
    assert "smtp:" in output
    assert "host: smtp.example.com" in output
    assert "port: 587" in output
    assert "tls_mode: starttls" in output


def test_render_config_empty_password_has_comment() -> None:
    """With no password, auth.password is '' with a fill-in note."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    cfg = provider_to_config(mp, "user@example.com")
    output = render_config(cfg)
    assert 'password: ""' in output
    assert "MAIL_PASSWORD" in output


def test_render_config_writes_password() -> None:
    """A supplied password is written into auth.password and round-trips."""
    mp = MailProvider(imap_host="ih", smtp_host="sh")
    cfg = provider_to_config(mp, "user@example.com", password="s3:cret#1")
    output = render_config(cfg)
    assert '"s3:cret#1"' in output


def test_render_config_round_trip(tmp_path: Path) -> None:
    """Output can be parsed back by MailConfig.from_yaml()."""
    mp = MailProvider(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        imap_port=143,
        imap_tls_mode="starttls",
        smtp_port=465,
        smtp_tls_mode="direct-tls",
    )
    cfg = provider_to_config(mp, "user@example.com", password="s3:cret#1")
    output = render_config(cfg)

    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(output)

    parsed = MailConfig.from_yaml(yaml_file)
    assert parsed.imap_host == "imap.example.com"
    assert parsed.imap_port == 143
    assert parsed.imap_tls_mode == "starttls"
    assert parsed.smtp_host == "smtp.example.com"
    assert parsed.smtp_port == 465
    assert parsed.smtp_tls_mode == "direct-tls"
    assert parsed.username == "user@example.com"
    assert parsed.password == "s3:cret#1"


# ---------------------------------------------------------------------------
# detect_provider — integration-style tests
# ---------------------------------------------------------------------------


def test_detect_provider_success() -> None:
    """Mock the Agent; detect_provider returns expected MailProvider."""
    with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=True):
        mock_run_result = mock.MagicMock()
        mock_run_result.output = DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_port=993,
            imap_tls_mode="direct-tls",
            smtp_port=587,
            smtp_tls_mode="starttls",
        )
        mock_agent_instance = mock.MagicMock()
        mock_agent_instance.run_sync.return_value = mock_run_result
        mock_agent_cls = mock.MagicMock(return_value=mock_agent_instance)

        with mock.patch("pydantic_ai.Agent", mock_agent_cls):
            result = detect_provider("user@example.com")

        assert isinstance(result, MailProvider)
        assert result.imap_host == "imap.example.com"
        assert result.smtp_host == "smtp.example.com"
        assert result.imap_port == 993
        assert result.imap_tls_mode == "direct-tls"
        assert result.smtp_port == 587
        assert result.smtp_tls_mode == "starttls"


def test_detect_provider_passes_api_key_arg() -> None:
    """When api_key is passed as argument, it's used instead of env var."""
    with mock.patch.dict(os.environ, {}, clear=True):
        mock_run_result = mock.MagicMock()
        mock_run_result.output = DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
        )
        mock_agent_instance = mock.MagicMock()
        mock_agent_instance.run_sync.return_value = mock_run_result
        mock_agent_cls = mock.MagicMock(return_value=mock_agent_instance)

        with mock.patch("pydantic_ai.Agent", mock_agent_cls):
            result = detect_provider(
                "user@example.com", api_key="sk-arg-key"
            )

        assert result.imap_host == "imap.example.com"


def test_detect_provider_llm_call_error() -> None:
    """When Agent.run_sync raises, DetectionError wraps the original message."""
    with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=True):
        mock_agent_instance = mock.MagicMock()
        mock_agent_instance.run_sync.side_effect = RuntimeError(
            "LLM API timeout"
        )
        mock_agent_cls = mock.MagicMock(return_value=mock_agent_instance)

        with mock.patch("pydantic_ai.Agent", mock_agent_cls):
            with pytest.raises(DetectionError) as exc:
                detect_provider("user@example.com")

        assert "LLM API timeout" in str(exc.value)


def test_detect_provider_missing_api_key() -> None:
    """No api_key arg and no LLM_API_KEY env var → DetectionError."""
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(DetectionError) as exc:
            detect_provider("user@example.com")
        assert "LLM_API_KEY" in str(exc.value)


def test_detect_provider_llm_model_fallback() -> None:
    """When LLM_MODEL env var is unset, the default model is used."""
    # Only set LLM_API_KEY, leave LLM_MODEL unset.
    with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=True):
        mock_run_result = mock.MagicMock()
        mock_run_result.output = DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
        )
        mock_agent_instance = mock.MagicMock()
        mock_agent_instance.run_sync.return_value = mock_run_result
        mock_agent_cls = mock.MagicMock(return_value=mock_agent_instance)

        # We also mock OpenAIChatModel to capture the model name.
        mock_model_instance = mock.MagicMock()
        mock_model_cls = mock.MagicMock(return_value=mock_model_instance)

        with mock.patch(
            "pydantic_ai.Agent", mock_agent_cls
        ), mock.patch(
            "pydantic_ai.models.openai.OpenAIChatModel", mock_model_cls
        ):
            detect_provider("user@example.com")

        # OpenAIChatModel should have been called with the default model.
        call_args = mock_model_cls.call_args
        assert call_args is not None
        # model_name is passed as keyword arg
        assert call_args[1].get("model_name") == "deepseek/deepseek-v4-flash"


def test_detect_provider_llm_model_from_env() -> None:
    """When LLM_MODEL env var is set, it overrides the default."""
    with mock.patch.dict(
        os.environ,
        {"LLM_API_KEY": "sk-test", "LLM_MODEL": "custom/model"},
        clear=True,
    ):
        mock_run_result = mock.MagicMock()
        mock_run_result.output = DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
        )
        mock_agent_instance = mock.MagicMock()
        mock_agent_instance.run_sync.return_value = mock_run_result
        mock_agent_cls = mock.MagicMock(return_value=mock_agent_instance)

        mock_model_instance = mock.MagicMock()
        mock_model_cls = mock.MagicMock(return_value=mock_model_instance)

        with mock.patch(
            "pydantic_ai.Agent", mock_agent_cls
        ), mock.patch(
            "pydantic_ai.models.openai.OpenAIChatModel", mock_model_cls
        ):
            detect_provider("user@example.com")

        call_args = mock_model_cls.call_args
        assert call_args is not None
        assert call_args[1].get("model_name") == "custom/model"


def test_detect_provider_explicit_model_arg() -> None:
    """The model= keyword argument takes precedence over env var."""
    with mock.patch.dict(
        os.environ,
        {"LLM_API_KEY": "sk-test", "LLM_MODEL": "env/model"},
        clear=True,
    ):
        mock_run_result = mock.MagicMock()
        mock_run_result.output = DetectedProvider(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
        )
        mock_agent_instance = mock.MagicMock()
        mock_agent_instance.run_sync.return_value = mock_run_result
        mock_agent_cls = mock.MagicMock(return_value=mock_agent_instance)

        mock_model_instance = mock.MagicMock()
        mock_model_cls = mock.MagicMock(return_value=mock_model_instance)

        with mock.patch(
            "pydantic_ai.Agent", mock_agent_cls
        ), mock.patch(
            "pydantic_ai.models.openai.OpenAIChatModel", mock_model_cls
        ):
            detect_provider("user@example.com", model="explicit/model")

        call_args = mock_model_cls.call_args
        assert call_args is not None
        assert call_args[1].get("model_name") == "explicit/model"


# ---------------------------------------------------------------------------
# DetectionError
# ---------------------------------------------------------------------------


def test_detection_error_is_exception() -> None:
    """DetectionError is a proper Exception subclass."""
    err = DetectionError("test message")
    assert isinstance(err, Exception)
    assert str(err) == "test message"


def test_detection_error_chain() -> None:
    """DetectionError can chain from another exception."""
    cause = RuntimeError("original")
    err = DetectionError("wrapped")
    err.__cause__ = cause
    assert err.__cause__ is cause


# ---------------------------------------------------------------------------
# autoconfig_lookup
# ---------------------------------------------------------------------------


def test_autoconfig_lookup_parses_ispdb() -> None:
    """A valid clientConfig document is parsed into a MailProvider."""
    with mock.patch(
        "urllib.request.urlopen", return_value=_FakeResp(_ISPDB_XML)
    ):
        provider = autoconfig_lookup("user@example.net")
    assert provider is not None
    assert provider.imap_host == "imap.example.net"
    assert provider.imap_port == 993
    assert provider.imap_tls_mode == "direct-tls"  # SSL → direct-tls
    assert provider.smtp_host == "smtp.example.net"
    assert provider.smtp_port == 587
    assert provider.smtp_tls_mode == "starttls"  # STARTTLS → starttls


def test_autoconfig_lookup_network_error_returns_none() -> None:
    """A network failure yields None (caller falls back to the LLM)."""
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("no route"),
    ):
        assert autoconfig_lookup("user@example.net") is None


def test_autoconfig_lookup_garbage_returns_none() -> None:
    """A non-XML / unparseable body yields None after trying every URL."""
    with mock.patch(
        "urllib.request.urlopen", return_value=_FakeResp("not xml at all")
    ):
        assert autoconfig_lookup("user@example.net") is None


def test_autoconfig_lookup_missing_smtp_returns_none() -> None:
    """A document with an IMAP server but no SMTP server is rejected."""
    xml = """\
<clientConfig version="1.1">
  <emailProvider id="x">
    <incomingServer type="imap">
      <hostname>imap.example.net</hostname>
      <port>993</port>
      <socketType>SSL</socketType>
    </incomingServer>
  </emailProvider>
</clientConfig>
"""
    with mock.patch("urllib.request.urlopen", return_value=_FakeResp(xml)):
        assert autoconfig_lookup("user@example.net") is None
