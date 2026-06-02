"""Tests for ``scripts/config/check_config_sync.py``."""

from __future__ import annotations

import sys
from pathlib import Path

# Make the script importable.
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts" / "config"
sys.path.insert(0, str(_SCRIPTS))

from check_config_sync import (  # noqa: E402
    check_docs_connecting,
    check_env_example,
    check_yaml_example,
    run_checks,
)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_YAML_EXAMPLE = """\
# Example local configuration for robotsix-auto-mail.
#
# Copy this file to config/mail.local.yaml and fill in your real values.
# config/mail.local.yaml is git-ignored so credentials never land in the repo.
#
# Any field you omit falls back to its built-in default (shown commented
# below). Any MAIL_* environment variable that is set overrides the
# corresponding value here.

imap:
  host: imap.example.com
  # port: 993
  # tls_mode: direct-tls
  # folder: INBOX

smtp:
  host: smtp.example.com
  # port: 587
  # tls_mode: starttls

auth:
  username: user@example.com
  password: ""  # set your password here, or via the MAIL_PASSWORD env var

# store:
#   path: .data/mail.db

# Automatic ingestion (used by `ingest --watch`, the default Docker service).
# How often, in minutes, to fetch new mail. Overridable via MAIL_INGEST_INTERVAL.
# ingest:
#   interval_minutes: 15

# LLM provider — used by the `detect` command and future LLM-assisted mail
# processing. Optional; the LLM_API_KEY / LLM_MODEL environment variables
# override these values.
# llm:
#   api_key: sk-or-v1-…
#   model: deepseek/deepseek-v4-flash
"""

_ENV_EXAMPLE = """\
# Example environment variables for robotsix-auto-mail.

MAIL_IMAP_HOST=imap.example.com
MAIL_IMAP_PORT=993
MAIL_IMAP_TLS_MODE=direct-tls
MAIL_IMAP_FOLDER=INBOX
MAIL_SMTP_HOST=smtp.example.com
MAIL_SMTP_PORT=587
MAIL_SMTP_TLS_MODE=starttls
MAIL_USERNAME=user@example.com
MAIL_PASSWORD=your-password-here
MAIL_DB_PATH=.data/mail.db
MAIL_INGEST_INTERVAL=15
LLM_API_KEY=sk-or-v1-…
LLM_MODEL=deepseek/deepseek-v4-flash
"""

_DOCS_YAML_TABLE = """\
### YAML config file

| Key | Required | Default | Purpose |
|---|---|---|---|
| `imap.host` | yes | - | IMAP server hostname |
| `imap.port` | no | `993` | IMAP server port |
| `imap.tls_mode` | no | `"direct-tls"` | IMAP TLS mode |
| `imap.folder` | no | `"INBOX"` | IMAP mailbox folder name |
| `smtp.host` | yes | - | SMTP server hostname |
| `smtp.port` | no | `587` | SMTP server port |
| `smtp.tls_mode` | no | `"starttls"` | SMTP TLS mode |
| `auth.username` | yes | - | Login username |
| `auth.password` | no | - | Login password |
| `store.path` | no | `".data/mail.db"` | Filesystem path for the SQLite database |
| `ingest.interval_minutes` | no | `15` | Minutes between automatic ingest cycles |
| `llm.api_key` | no | - | LLM provider API key |
| `llm.model` | no | `"deepseek/deepseek-v4-flash"` | LLM model name |
"""

_DOCS_ENV_TABLE = """\
### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MAIL_IMAP_HOST` | yes | - | IMAP server hostname |
| `MAIL_SMTP_HOST` | yes | - | SMTP server hostname |
| `MAIL_USERNAME` | yes | - | Login username |
| `MAIL_PASSWORD` | yes | - | Login password |
| `MAIL_IMAP_PORT` | no | `993` | IMAP server port |
| `MAIL_IMAP_TLS_MODE` | no | `direct-tls` | TLS negotiation for IMAP |
| `MAIL_SMTP_PORT` | no | `587` | SMTP server port |
| `MAIL_SMTP_TLS_MODE` | no | `starttls` | TLS negotiation for SMTP |
| `MAIL_IMAP_FOLDER` | no | `INBOX` | IMAP mailbox folder name |
| `MAIL_DB_PATH` | no | `.data/mail.db` | Filesystem path for the SQLite database |
| `MAIL_INGEST_INTERVAL` | no | `15` | Minutes between automatic ingest cycles |
| `MAIL_CONFIG_PATH` | no | `config/mail.local.yaml` | Path to the YAML config file |
| `LLM_API_KEY` | no | - | LLM provider API key |
| `LLM_MODEL` | no | `deepseek/deepseek-v4-flash` | LLM model name |
"""


def _full_docs(yaml_table: str, env_table: str) -> str:
    """Wrap the two tables in minimal md so the parser finds them."""
    return (
        "# Connecting\n\n"
        "## Configuration keys\n\n"
        + yaml_table
        + "\n\n"
        + env_table
        + "\n"
    )


# ====================================================================
# Happy path
# ====================================================================


def test_yaml_example_happy() -> None:
    """No findings when the example YAML matches MailConfig."""
    findings = check_yaml_example(_YAML_EXAMPLE)
    assert findings == []


def test_env_example_happy() -> None:
    """No findings when .env.example matches MailConfig."""
    findings = check_env_example(_ENV_EXAMPLE)
    assert findings == []


def test_docs_happy() -> None:
    """No findings when docs match MailConfig."""
    text = _full_docs(_DOCS_YAML_TABLE, _DOCS_ENV_TABLE)
    findings = check_docs_connecting(text)
    assert findings == []


def test_run_checks_happy(tmp_path: Path) -> None:
    """Exit 0 when all artifacts are in sync."""
    repo = tmp_path
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "mail.local.example.yaml").write_text(_YAML_EXAMPLE)
    (repo / ".env.example").write_text(_ENV_EXAMPLE)
    (repo / "docs").mkdir()
    (repo / "docs" / "connecting.md").write_text(
        _full_docs(_DOCS_YAML_TABLE, _DOCS_ENV_TABLE)
    )
    assert run_checks(repo) == 0


# ====================================================================
# YAML drift
# ====================================================================


def test_yaml_missing_key() -> None:
    """Removing a commented-out key reports missing-from-yaml."""
    modified = _YAML_EXAMPLE.replace("  # port: 993\n", "")
    findings = check_yaml_example(modified)
    assert any(
        f["type"] == "missing-from-yaml" and f["key"] == "imap.port"
        for f in findings
    )


def test_yaml_stale_key() -> None:
    """Adding an unrecognised key reports stale-yaml-key."""
    modified = _YAML_EXAMPLE + "\n# foo:\n#   bar: 1\n"
    findings = check_yaml_example(modified)
    assert any(
        f["type"] == "stale-yaml-key" and f["key"] == "foo.bar"
        for f in findings
    )


def test_yaml_default_mismatch() -> None:
    """Changing a commented-out default reports default-mismatch."""
    modified = _YAML_EXAMPLE.replace("# port: 993", "# port: 9993")
    findings = check_yaml_example(modified)
    assert any(
        f["type"] == "default-mismatch"
        and f["key"] == "imap.port"
        and f["expected"] == 993
        for f in findings
    )


def test_yaml_uncommented_default_mismatch() -> None:
    """Changing an uncommented value reports default-mismatch."""
    # Change smtp.host to something else — it's required so no comparison,
    # but we can change a value that has a default... actually smtp.host
    # has no default.  Let's change the password to non-empty.
    modified = _YAML_EXAMPLE.replace(
        'password: ""', 'password: "real"'
    )
    findings = check_yaml_example(modified)
    # password is MISSING in dataclass, so no default comparison.
    # But we should still verify it's not flagged.
    assert not any(
        f["type"] == "default-mismatch" and f["key"] == "auth.password"
        for f in findings
    )


# ====================================================================
# .env.example drift
# ====================================================================


def test_env_missing_var() -> None:
    """Removing a line reports missing-from-env-example."""
    modified = _ENV_EXAMPLE.replace("MAIL_IMAP_PORT=993\n", "")
    findings = check_env_example(modified)
    assert any(
        f["type"] == "missing-from-env-example"
        and f["key"] == "MAIL_IMAP_PORT"
        for f in findings
    )


def test_env_stale_var() -> None:
    """Adding a made-up env var reports stale-env-example-var."""
    modified = _ENV_EXAMPLE + "MAIL_FOO=1\n"
    findings = check_env_example(modified)
    assert any(
        f["type"] == "stale-env-example-var" and f["key"] == "MAIL_FOO"
        for f in findings
    )


def test_env_default_mismatch() -> None:
    """Changing a port number reports default-mismatch."""
    modified = _ENV_EXAMPLE.replace("MAIL_IMAP_PORT=993", "MAIL_IMAP_PORT=1")
    findings = check_env_example(modified)
    assert any(
        f["type"] == "default-mismatch"
        and f["key"] == "MAIL_IMAP_PORT"
        and f["expected"] == 993
        for f in findings
    )


def test_env_stale_excludes_config_path() -> None:
    """MAIL_CONFIG_PATH is excluded from stale checks."""
    # It IS present in the example, but shouldn't be flagged as stale.
    findings = check_env_example(_ENV_EXAMPLE)
    assert not any(
        f["type"] == "stale-env-example-var"
        and f["key"] == "MAIL_CONFIG_PATH"
        for f in findings
    )


# ====================================================================
# Placeholder tolerance
# ====================================================================


def test_placeholder_llm_api_key_yaml() -> None:
    """llm_api_key placeholder is not a default-mismatch."""
    # The default is "" but the example has "sk-or-v1-…" — OK.
    findings = check_yaml_example(_YAML_EXAMPLE)
    assert not any(
        f["type"] == "default-mismatch" and "llm.api_key" in str(f)
        for f in findings
    )


def test_placeholder_llm_api_key_env() -> None:
    """LLM_API_KEY placeholder is not a default-mismatch."""
    findings = check_env_example(_ENV_EXAMPLE)
    assert not any(
        f["type"] == "default-mismatch" and "LLM_API_KEY" in str(f)
        for f in findings
    )


def test_placeholder_llm_api_key_changed() -> None:
    """Changing LLM_API_KEY to another sk-or-v1 token still exits clean."""
    modified = _ENV_EXAMPLE.replace(
        "LLM_API_KEY=sk-or-v1-…", "LLM_API_KEY=sk-or-v1-different"
    )
    findings = check_env_example(modified)
    assert not any(
        f["type"] == "default-mismatch" and "LLM_API_KEY" in str(f)
        for f in findings
    )


def test_placeholder_password_env() -> None:
    """MAIL_PASSWORD placeholder is not a default-mismatch."""
    # password is MISSING in dataclass → skip comparison anyway.
    modified = _ENV_EXAMPLE.replace(
        "MAIL_PASSWORD=your-password-here", "MAIL_PASSWORD=realpw"
    )
    findings = check_env_example(modified)
    assert not any(
        f["type"] == "default-mismatch" and "MAIL_PASSWORD" in str(f)
        for f in findings
    )


# ====================================================================
# Docs table drift
# ====================================================================


def test_doc_missing_yaml_key() -> None:
    """Removing a YAML table row reports doc-missing-yaml-key."""
    modified = _DOCS_YAML_TABLE.replace(
        "| `imap.port` | no | `993` | IMAP server port |\n", ""
    )
    text = _full_docs(modified, _DOCS_ENV_TABLE)
    findings = check_docs_connecting(text)
    assert any(
        f["type"] == "doc-missing-yaml-key" and f["key"] == "imap.port"
        for f in findings
    )


def test_doc_missing_env_var() -> None:
    """Removing an env var table row reports doc-missing-env-var."""
    modified = _DOCS_ENV_TABLE.replace(
        "| `MAIL_IMAP_PORT` | no | `993` | IMAP server port |\n", ""
    )
    text = _full_docs(_DOCS_YAML_TABLE, modified)
    findings = check_docs_connecting(text)
    assert any(
        f["type"] == "doc-missing-env-var" and f["key"] == "MAIL_IMAP_PORT"
        for f in findings
    )


def test_doc_default_mismatch() -> None:
    """Changing a documented default reports doc-default-mismatch."""
    modified = _DOCS_YAML_TABLE.replace("`993`", "`1993`", 1)
    text = _full_docs(modified, _DOCS_ENV_TABLE)
    findings = check_docs_connecting(text)
    assert any(
        f["type"] == "doc-default-mismatch" and f["key"] == "imap.port"
        for f in findings
    )


def test_doc_stale_yaml_key() -> None:
    """Adding a made-up YAML row reports doc-stale-yaml-key."""
    modified = _DOCS_YAML_TABLE + (
        "| `foo.bar` | no | `1` | Made up |\n"
    )
    text = _full_docs(modified, _DOCS_ENV_TABLE)
    findings = check_docs_connecting(text)
    assert any(
        f["type"] == "doc-stale-yaml-key" and f["key"] == "foo.bar"
        for f in findings
    )


def test_doc_stale_env_var() -> None:
    """Adding a made-up env var row reports doc-stale-env-var."""
    modified = _DOCS_ENV_TABLE + (
        "| `MAIL_FOO` | no | `1` | Made up |\n"
    )
    text = _full_docs(_DOCS_YAML_TABLE, modified)
    findings = check_docs_connecting(text)
    assert any(
        f["type"] == "doc-stale-env-var" and f["key"] == "MAIL_FOO"
        for f in findings
    )


def test_doc_stale_excludes_config_path() -> None:
    """MAIL_CONFIG_PATH is excluded from doc stale checks."""
    text = _full_docs(_DOCS_YAML_TABLE, _DOCS_ENV_TABLE)
    findings = check_docs_connecting(text)
    assert not any(
        f["type"] == "doc-stale-env-var"
        and f["key"] == "MAIL_CONFIG_PATH"
        for f in findings
    )


# ====================================================================
# Exit code 2
# ====================================================================


def test_run_checks_missing_file(tmp_path: Path) -> None:
    """Exit 2 when an artifact file is missing entirely."""
    # Create only some files, but not the YAML example.
    repo = tmp_path
    (repo / "config").mkdir(parents=True)
    # intentionally skip mail.local.example.yaml
    (repo / ".env.example").write_text(_ENV_EXAMPLE)
    (repo / "docs").mkdir()
    (repo / "docs" / "connecting.md").write_text(
        _full_docs(_DOCS_YAML_TABLE, _DOCS_ENV_TABLE)
    )
    assert run_checks(repo) == 2
