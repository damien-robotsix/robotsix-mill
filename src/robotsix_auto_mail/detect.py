"""Email provider auto-detection.

Two complementary detectors return IMAP/SMTP settings for an email address:

* :func:`autoconfig_lookup` — queries the Mozilla ISPDB and the domain's own
  autoconfig endpoint over HTTPS (no LLM, very accurate for known providers
  and many custom domains). Uses only the standard library.
* :func:`detect_provider` — asks an LLM, optionally with feedback describing a
  previous failed attempt so it can refine a non-obvious guess.

All ``pydantic_ai`` imports are lazy so the rest of the CLI works without the
optional ``[llm]`` dependency.
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

import pydantic

from robotsix_auto_mail.config import (
    DEFAULT_DB_PATH,
    DEFAULT_LLM_MODEL,
    MailConfig,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DetectionError(Exception):
    """Raised when provider detection fails for any reason."""


# ---------------------------------------------------------------------------
# Pydantic model — structured LLM output contract
# ---------------------------------------------------------------------------


class DetectedProvider(pydantic.BaseModel):
    """Structured output the LLM must return — validated by pydantic."""

    imap_host: str = pydantic.Field(..., min_length=1)
    imap_port: int = pydantic.Field(default=993, ge=1, le=65535)
    imap_tls_mode: str = pydantic.Field(default="direct-tls")
    smtp_host: str = pydantic.Field(..., min_length=1)
    smtp_port: int = pydantic.Field(default=587, ge=1, le=65535)
    smtp_tls_mode: str = pydantic.Field(default="starttls")

    @pydantic.field_validator("imap_tls_mode", "smtp_tls_mode")
    @classmethod
    def _validate_tls_mode(cls, v: str) -> str:
        if v not in {"starttls", "direct-tls", "none"}:
            raise ValueError(
                f"TLS mode must be one of starttls, direct-tls, none; got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# Internal dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MailProvider:
    """Lightweight, serialisable struct for detected mail parameters."""

    imap_host: str
    smtp_host: str
    imap_port: int = 993
    imap_tls_mode: str = "direct-tls"
    smtp_port: int = 587
    smtp_tls_mode: str = "starttls"


# ---------------------------------------------------------------------------
# System prompt — embeds all known provider data
# ---------------------------------------------------------------------------

_DETECT_SYSTEM_PROMPT = """\
You are an email provider configuration expert. Given an email address, \
return the correct IMAP and SMTP server settings as a JSON object.

**TLS mode rules:**
- `direct-tls`: TLS from the first byte — used on IMAP port 993 and SMTP \
port 465.
- `starttls`: plain connection upgraded to TLS via STARTTLS — used on \
IMAP port 143 and SMTP port 587.
- `none`: no TLS — for local/dev only.

**Known provider settings (use these exact values when the domain matches):**

| Provider | IMAP Host | IMAP Port | IMAP TLS | SMTP Host | SMTP Port | SMTP TLS |
|---|---|---|---|---|---|---|
| Gmail / Google Workspace | `imap.gmail.com` | 993 | `direct-tls` | `smtp.gmail.com` | 587 | `starttls` |
| Outlook / Hotmail / Live / MS365 | `outlook.office365.com` | 993 | `direct-tls` | `smtp.office365.com` | 587 | `starttls` |
| Yahoo Mail | `imap.mail.yahoo.com` | 993 | `direct-tls` | `smtp.mail.yahoo.com` | 587 | `starttls` |
| iCloud | `imap.mail.me.com` | 993 | `direct-tls` | `smtp.mail.me.com` | 587 | `starttls` |
| Fastmail | `imap.fastmail.com` | 993 | `direct-tls` | `smtp.fastmail.com` | 587 | `starttls` |
| Zoho Mail | `imap.zoho.com` | 993 | `direct-tls` | `smtp.zoho.com` | 587 | `starttls` |
| Proton Mail Bridge | `127.0.0.1` | 1143 | `none` | `127.0.0.1` | 1025 | `none` |
| GMX | `imap.gmx.com` | 993 | `direct-tls` | `mail.gmx.com` | 587 | `starttls` |
| mail.com | `imap.mail.com` | 993 | `direct-tls` | `smtp.mail.com` | 587 | `starttls` |
| Yandex Mail | `imap.yandex.com` | 993 | `direct-tls` | `smtp.yandex.com` | 587 | `starttls` |
| QQ Mail | `imap.qq.com` | 993 | `direct-tls` | `smtp.qq.com` | 587 | `starttls` |
| AOL Mail | `imap.aol.com` | 993 | `direct-tls` | `smtp.aol.com` | 587 | `starttls` |
| Mail.ru | `imap.mail.ru` | 993 | `direct-tls` | `smtp.mail.ru` | 587 | `starttls` |

**Domain heuristics (when the domain isn't in the table above):**
- `@gmail.com` or `@googlemail.com` → Gmail settings.
- `@outlook.com`, `@outlook.*`, `@hotmail.com`, `@hotmail.*`, \
`@live.com`, `@live.*`, `@msn.com` → Outlook/Microsoft 365 settings.
- `@yahoo.com`, `@yahoo.*`, `@ymail.com`, `@rocketmail.com` → Yahoo \
settings.
- `@icloud.com`, `@me.com`, `@mac.com` → iCloud settings.
- `@fastmail.com`, `@fastmail.*` → Fastmail settings.
- `@zoho.com`, `@zoho.*` → Zoho settings.
- `@proton.me`, `@protonmail.com`, `@pm.me` → Proton Mail Bridge (localhost).
- `@gmx.com`, `@gmx.*` → GMX settings.
- `@mail.com` → mail.com settings.
- `@yandex.com`, `@yandex.*` → Yandex settings.
- `@qq.com` → QQ Mail settings.
- `@aol.com` → AOL settings.
- `@mail.ru`, `@inbox.ru`, `@list.ru`, `@bk.ru` → Mail.ru settings.
- `@126.com`, `@163.com` → NetEase: `imap.126.com`/`imap.163.com` port \
993 `direct-tls`, `smtp.126.com`/`smtp.163.com` port 587 `starttls`.
- For self-hosted / custom domains (e.g. `@example.com`): the typical \
pattern is `imap.<domain>` port 993 and `smtp.<domain>` port 587 — but \
many custom domains are hosted by a managed provider, so consider these too.

**Managed hosting of custom domains (the address domain is NOT the mail host):**
- Google Workspace → `imap.gmail.com` / `smtp.gmail.com`.
- Microsoft 365 / Exchange Online → `outlook.office365.com` / `smtp.office365.com`.
- Zoho-hosted → `imap.zoho.com` / `smtp.zoho.com` (or `.eu`/`.in` regional).
- Fastmail-hosted → `imap.fastmail.com` / `smtp.fastmail.com`.
- mailbox.org → `imap.mailbox.org` / `smtp.mailbox.org`.
- Migadu → `imap.migadu.com` / `smtp.migadu.com`.
- Gandi → `mail.gandi.net` (IMAP 993 direct-tls, SMTP 587 starttls).
- OVH → `ssl0.ovh.net` (IMAP 993 direct-tls, SMTP 587 starttls).
- Infomaniak → `mail.infomaniak.com`.
- Purelymail → `imap.purelymail.com` / `smtp.purelymail.com`.
- cPanel/Plesk shared hosting → often `mail.<domain>` or the server hostname.

When the obvious `imap.<domain>` is uncertain, prefer `mail.<domain>` or the \
provider patterns above. If you are given feedback that a previous guess \
failed, do NOT repeat it — propose a genuinely different host.

Return ONLY a JSON object matching the schema — no explanation, no markdown \
fences."""


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------


def detect_provider(
    email_address: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    feedback: str | None = None,
) -> MailProvider:
    """Detect IMAP/SMTP settings for *email_address* via an LLM.

    Args:
        email_address: The email address to detect provider settings for.
        model: OpenRouter model name.  Defaults to the ``LLM_MODEL`` env
            var or ``"deepseek/deepseek-v4-flash"``.
        api_key: OpenRouter API key.  Defaults to the ``LLM_API_KEY`` env
            var.  Required unless the env var is set.
        feedback: Optional description of a previous failed attempt (which
            host was tried and how it failed).  When provided, it is added
            to the prompt so the model can propose a different, non-obvious
            configuration instead of repeating the failed guess.

    Returns:
        A ``MailProvider`` with the detected settings.

    Raises:
        DetectionError: If the API key is missing, the LLM returns an
            invalid response, or any other error occurs.
    """
    # -- lazy imports so the rest of the CLI works without pydantic_ai --
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    # -- resolve API key --
    resolved_key = api_key or os.environ.get("LLM_API_KEY", "")
    if not resolved_key:
        raise DetectionError(
            "No LLM API key found — set the LLM_API_KEY environment "
            "variable or add an `llm.api_key` entry to your config file"
        )

    # -- resolve model --
    resolved_model = model or os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL)

    # -- build agent --
    provider = OpenRouterProvider(api_key=resolved_key)
    agent_model = OpenAIChatModel(
        model_name=resolved_model,
        provider=provider,
    )
    agent = Agent(
        model=agent_model,
        output_type=PromptedOutput(DetectedProvider),
    )

    # -- build the user message (+ optional refinement feedback) --
    user_message = email_address
    if feedback:
        user_message += (
            "\n\nThe previous configuration attempt FAILED:\n"
            f"{feedback}\n"
            "Propose a corrected configuration with a DIFFERENT host — "
            "do not repeat the failed guess."
        )

    # -- call LLM --
    try:
        result = agent.run_sync(_DETECT_SYSTEM_PROMPT + "\n\n" + user_message)
    except Exception as exc:
        raise DetectionError(str(exc)) from exc

    # -- extract and convert --
    detected: DetectedProvider = result.output
    return MailProvider(
        imap_host=detected.imap_host,
        imap_port=detected.imap_port,
        imap_tls_mode=detected.imap_tls_mode,
        smtp_host=detected.smtp_host,
        smtp_port=detected.smtp_port,
        smtp_tls_mode=detected.smtp_tls_mode,
    )


# ---------------------------------------------------------------------------
# Autoconfig (Mozilla ISPDB + domain autoconfig) — no LLM required
# ---------------------------------------------------------------------------

# Mozilla maps Thunderbird "socketType" values to our TLS-mode vocabulary.
_SOCKET_TYPE_TO_TLS = {
    "SSL": "direct-tls",
    "STARTTLS": "starttls",
    "plain": "none",
}


def _autoconfig_urls(email_address: str) -> list[str]:
    """Return candidate autoconfig URLs to try, most authoritative first."""
    domain = email_address.rpartition("@")[2].strip().lower()
    if not domain:
        return []
    quoted = urllib.parse.quote(email_address)
    return [
        # Mozilla ISPDB — central database keyed by domain.
        f"https://autoconfig.thunderbird.net/v1.1/{domain}",
        # Provider-hosted autoconfig (the Thunderbird autoconfig protocol).
        f"https://autoconfig.{domain}/mail/config-v1.1.xml"
        f"?emailaddress={quoted}",
    ]


def _parse_autoconfig_xml(xml_text: str) -> MailProvider | None:
    """Parse a Thunderbird ``clientConfig`` document into a MailProvider.

    Returns ``None`` when the document lacks a usable IMAP + SMTP pair.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return None

    def _server(kind: str, type_attr: str) -> dict[str, str] | None:
        for node in root.iter(kind):
            if node.get("type") == type_attr:
                host = (node.findtext("hostname") or "").strip()
                port = (node.findtext("port") or "").strip()
                socket = (node.findtext("socketType") or "").strip()
                if host:
                    return {"host": host, "port": port, "socket": socket}
        return None

    imap = _server("incomingServer", "imap")
    smtp = _server("outgoingServer", "smtp")
    if imap is None or smtp is None:
        return None

    def _port(value: str, default: int) -> int:
        try:
            return int(value)
        except ValueError:
            return default

    def _tls(socket: str, port: int) -> str:
        mode = _SOCKET_TYPE_TO_TLS.get(socket)
        if mode is not None:
            return mode
        # Fall back to a sensible default based on the port.
        return "direct-tls" if port == 993 else "starttls"

    imap_port = _port(imap["port"], 993)
    smtp_port = _port(smtp["port"], 587)
    return MailProvider(
        imap_host=imap["host"],
        imap_port=imap_port,
        imap_tls_mode=_tls(imap["socket"], imap_port),
        smtp_host=smtp["host"],
        smtp_port=smtp_port,
        smtp_tls_mode=_tls(smtp["socket"], smtp_port),
    )


def autoconfig_lookup(
    email_address: str, *, timeout: float = 5.0
) -> MailProvider | None:
    """Look up IMAP/SMTP settings via published autoconfig, without an LLM.

    Tries the Mozilla ISPDB and the domain's own autoconfig endpoint. Returns
    a :class:`MailProvider` on the first usable hit, or ``None`` if nothing
    resolves (unknown domain, network error, malformed document, …) — callers
    should then fall back to :func:`detect_provider`.
    """
    for url in _autoconfig_urls(email_address):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if getattr(resp, "status", 200) != 200:
                    continue
                xml_text = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        provider = _parse_autoconfig_xml(xml_text)
        if provider is not None:
            return provider
    return None


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def provider_to_config(
    provider: MailProvider,
    username: str,
    password: str = "",
    db_path: str = DEFAULT_DB_PATH,
) -> MailConfig:
    """Convert a ``MailProvider`` + username (+ optional password) into a
    ``MailConfig``.
    """
    return MailConfig(
        imap_host=provider.imap_host,
        imap_port=provider.imap_port,
        imap_tls_mode=provider.imap_tls_mode,
        smtp_host=provider.smtp_host,
        smtp_port=provider.smtp_port,
        smtp_tls_mode=provider.smtp_tls_mode,
        username=username,
        password=password,
        db_path=db_path,
        imap_folder="INBOX",
    )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def render_config(config: MailConfig) -> str:
    """Render a ``MailConfig`` as a valid YAML config file.

    When ``config.password`` is set it is written into ``auth.password``;
    otherwise the field is emitted as ``""`` with a note that the
    password can be supplied here or via the ``MAIL_PASSWORD`` env var.

    An ``llm:`` section is appended when ``config.llm_api_key`` is set, so
    that re-running ``detect`` over an existing file preserves the key.
    """
    if config.password:
        # json.dumps yields a double-quoted scalar that is valid YAML and
        # safely escapes any special characters in the password.
        password_line = f"password: {json.dumps(config.password)}"
    else:
        password_line = (
            'password: ""  # set your password here, '
            "or via the MAIL_PASSWORD env var"
        )

    text = f"""\
# Auto-detected mail configuration for {config.username}
# Generated by: robotsix-auto-mail detect
#
# Verify these settings before using — run `robotsix-auto-mail probe`.

imap:
  host: {config.imap_host}
  port: {config.imap_port}
  tls_mode: {config.imap_tls_mode}
  folder: {config.imap_folder}

smtp:
  host: {config.smtp_host}
  port: {config.smtp_port}
  tls_mode: {config.smtp_tls_mode}

auth:
  username: {config.username}
  {password_line}

store:
  path: {config.db_path}
"""

    if config.llm_api_key:
        text += f"""
llm:
  api_key: {json.dumps(config.llm_api_key)}
  model: {config.llm_model}
"""

    return text
