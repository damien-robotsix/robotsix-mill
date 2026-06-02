"""Mail configuration subsystem.

Provides ``MailConfig``, a frozen dataclass that holds IMAP and SMTP
connection parameters, with two loaders: ``from_env()`` (environment
variables) and ``from_yaml()`` (a YAML file).

Configuration resolves through a single, predictable cascade — see
``load()``: code defaults → YAML file → environment variables (which
win field-by-field).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, Final, NamedTuple

import yaml

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigurationError(Exception):
    """Raised when the mail configuration is invalid or incomplete.

    Attributes:
        message: Human-readable error description.
        missing_only: True when the *only* problem is missing required
            fields (no invalid values).  Used by ``load()`` to decide
            whether falling back to the YAML config file is appropriate.
    """

    def __init__(
        self, message: str, *, missing_only: bool = False
    ) -> None:
        super().__init__(message)
        self.message = message
        self.missing_only = missing_only

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Valid TLS modes
# ---------------------------------------------------------------------------

_VALID_TLS_MODES = frozenset({"starttls", "direct-tls", "none"})

# Default TLS modes for IMAP and SMTP connections.
DEFAULT_IMAP_TLS_MODE = "direct-tls"
DEFAULT_SMTP_TLS_MODE = "starttls"

# Default location for the SQLite store: a ``.data/`` directory next to the
# current working directory (git-ignored), keeping the repo root clean.
DEFAULT_DB_PATH = ".data/mail.db"

# Default YAML config file path (used by ``load()`` and ``load_llm()``).
DEFAULT_CONFIG_PATH = "config/mail.local.yaml"

# Default LLM model for the ``detect`` command (and future mail processing).
DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-flash"

# Default interval (minutes) between automatic ingest cycles in watch mode.
DEFAULT_INGEST_INTERVAL_MINUTES = 15


def _check_tls_mode(label: str, value: str) -> None:
    if value not in _VALID_TLS_MODES:
        raise ConfigurationError(
            f"{label} must be one of {sorted(_VALID_TLS_MODES)!r}, "
            f"got {value!r}"
        )


def _parse_int(label: str, raw: str) -> int:
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise ConfigurationError(
            f"{label} must be an integer, got {raw!r}"
        ) from None


# ---------------------------------------------------------------------------
# Per-field spec table (single source of truth)
# ---------------------------------------------------------------------------

# Sentinel marking a field with no real default value: callers (env loader,
# YAML loader) decide what to use as a fallback when the value is absent.
_REQUIRED: Final[object] = object()


class _FieldSpec(NamedTuple):
    """How to read one ``MailConfig`` field from the env and the YAML file.

    ``env_key`` is the environment variable name; ``yaml_path`` is a dotted
    ``section.key`` pair (exactly two segments).  ``kind`` selects the
    parser / validator: ``"str"``, ``"int"`` or ``"tls_mode"``.  ``default``
    is the value used when the source is absent and the field is not
    required for that source (or :data:`_REQUIRED` if no real default
    exists).  ``required_in_env`` / ``required_in_yaml`` are intentionally
    independent — ``password`` is required in env but not in YAML.
    """

    field_name: str
    env_key: str
    yaml_path: str
    kind: str
    default: Any
    required_in_env: bool
    required_in_yaml: bool


_FIELD_SPECS: Final[tuple[_FieldSpec, ...]] = (
    _FieldSpec("imap_host", "MAIL_IMAP_HOST", "imap.host",
               "str", _REQUIRED, True, True),
    _FieldSpec("imap_port", "MAIL_IMAP_PORT", "imap.port",
               "int", 993, False, False),
    _FieldSpec("imap_tls_mode", "MAIL_IMAP_TLS_MODE", "imap.tls_mode",
               "tls_mode", DEFAULT_IMAP_TLS_MODE, False, False),
    _FieldSpec("imap_folder", "MAIL_IMAP_FOLDER", "imap.folder",
               "str", "INBOX", False, False),
    _FieldSpec("smtp_host", "MAIL_SMTP_HOST", "smtp.host",
               "str", _REQUIRED, True, True),
    _FieldSpec("smtp_port", "MAIL_SMTP_PORT", "smtp.port",
               "int", 587, False, False),
    _FieldSpec("smtp_tls_mode", "MAIL_SMTP_TLS_MODE", "smtp.tls_mode",
               "tls_mode", DEFAULT_SMTP_TLS_MODE, False, False),
    _FieldSpec("username", "MAIL_USERNAME", "auth.username",
               "str", _REQUIRED, True, True),
    # password: required in env, but optional in YAML (env can supply it).
    _FieldSpec("password", "MAIL_PASSWORD", "auth.password",
               "str", _REQUIRED, True, False),
    _FieldSpec("db_path", "MAIL_DB_PATH", "store.path",
               "str", DEFAULT_DB_PATH, False, False),
    _FieldSpec("llm_api_key", "LLM_API_KEY", "llm.api_key",
               "str", "", False, False),
    _FieldSpec("llm_model", "LLM_MODEL", "llm.model",
               "str", DEFAULT_LLM_MODEL, False, False),
    _FieldSpec("ingest_interval_minutes", "MAIL_INGEST_INTERVAL",
               "ingest.interval_minutes", "int",
               DEFAULT_INGEST_INTERVAL_MINUTES, False, False),
)

# Each yaml_path must be exactly ``section.key`` — the YAML loader splits
# on the single dot.  Validated once at import time so a typo here fails
# immediately rather than at first use.
for _s in _FIELD_SPECS:
    assert _s.yaml_path.count(".") == 1, (  # noqa: S101  # nosec B101
        f"_FieldSpec.yaml_path must have exactly one dot, "
        f"got {_s.yaml_path!r}"
    )


# ---------------------------------------------------------------------------
# MailConfig
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MailConfig:
    """Immutable application settings: mail server connection parameters
    plus optional LLM credentials used by ``detect`` (and future mail
    processing).

    Credentials are stored in memory as plain ``str`` values but the
    ``password`` and ``llm_api_key`` fields are masked in ``repr`` / ``str``.
    """

    imap_host: str
    smtp_host: str
    username: str
    password: str

    imap_port: int = 993
    imap_tls_mode: str = DEFAULT_IMAP_TLS_MODE
    smtp_port: int = 587
    smtp_tls_mode: str = DEFAULT_SMTP_TLS_MODE

    db_path: str = DEFAULT_DB_PATH
    imap_folder: str = "INBOX"

    # LLM provider settings — optional; only needed for the `detect`
    # subcommand and future LLM-assisted mail processing.
    llm_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL

    # Minutes between automatic ingest cycles (`ingest --watch`).
    ingest_interval_minutes: int = DEFAULT_INGEST_INTERVAL_MINUTES

    # -- masking -----------------------------------------------------------

    _SECRET_FIELDS = ("password", "llm_api_key")

    def __repr__(self) -> str:
        cls = type(self).__name__
        fields = dataclasses.fields(self)
        parts = []
        for f in fields:
            val = getattr(self, f.name)
            if f.name in self._SECRET_FIELDS:
                parts.append(f"{f.name}=<redacted>")
            else:
                parts.append(f"{f.name}={val!r}")
        return f"{cls}({', '.join(parts)})"

    def __str__(self) -> str:
        return self.__repr__()

    # -- loaders -----------------------------------------------------------

    @classmethod
    def from_env(cls) -> MailConfig:
        """Build a ``MailConfig`` from environment variables.

        Required env vars:
          ``MAIL_IMAP_HOST``, ``MAIL_SMTP_HOST``, ``MAIL_USERNAME``,
          ``MAIL_PASSWORD``

        Optional env vars (with defaults):
          ``MAIL_IMAP_PORT`` (993), ``MAIL_IMAP_TLS_MODE`` (direct-tls),
          ``MAIL_SMTP_PORT`` (587), ``MAIL_SMTP_TLS_MODE`` (starttls)

        Returns:
            A fully-populated ``MailConfig``.

        Raises:
            ConfigurationError: If any required variable is missing or
                any value is invalid.
        """
        missing: list[str] = []
        errors: list[str] = []
        kwargs: dict[str, Any] = {}

        for spec in _FIELD_SPECS:
            raw = os.environ.get(spec.env_key, "")
            if not raw:
                if spec.required_in_env:
                    missing.append(spec.env_key)
                    kwargs[spec.field_name] = ""
                else:
                    kwargs[spec.field_name] = spec.default
                continue
            if spec.kind == "str":
                kwargs[spec.field_name] = raw
            elif spec.kind == "int":
                try:
                    kwargs[spec.field_name] = int(raw)
                except ValueError:
                    errors.append(
                        f"{spec.env_key} must be an integer, got {raw!r}"
                    )
                    kwargs[spec.field_name] = spec.default
            else:  # "tls_mode"
                if raw not in _VALID_TLS_MODES:
                    errors.append(
                        f"{spec.env_key} must be one of "
                        f"{sorted(_VALID_TLS_MODES)!r}, got {raw!r}"
                    )
                kwargs[spec.field_name] = raw

        # -- final validation ----------------------------------------------

        msgs: list[str] = []
        if missing:
            msgs.append(
                "Missing required environment variable(s): "
                + ", ".join(sorted(missing))
            )
        msgs.extend(errors)
        if msgs:
            # If *only* missing-required-field errors (no invalid
            # values), flag the error so load() can safely fall back to
            # the YAML file.  Invalid values mean the user explicitly set
            # an env var — falling back would silently swallow their typo.
            raise ConfigurationError(
                "\n".join(msgs),
                missing_only=bool(missing and not errors),
            )

        return cls(**kwargs)

    @classmethod
    def _parse_config_dict(
        cls, data: dict[str, object], path: Path, *, validate: bool = True
    ) -> MailConfig:
        errors: list[str] = []
        kwargs: dict[str, Any] = {}
        # Memoise top-level section lookups so we don't re-validate
        # the same mapping for every field that lives under it.
        sections: dict[str, dict[str, object]] = {}

        for spec in _FIELD_SPECS:
            section_name, key_name = spec.yaml_path.split(".", 1)
            if section_name not in sections:
                sections[section_name] = _get_table(data, section_name) or {}
            section = sections[section_name]

            if spec.kind == "int":
                kwargs[spec.field_name] = _get_int(
                    section, key_name, spec.default, path
                )
            elif spec.kind == "tls_mode":
                value = _get_str(section, key_name, spec.default)
                if value not in _VALID_TLS_MODES:
                    errors.append(
                        f"{spec.yaml_path} must be one of "
                        f"{sorted(_VALID_TLS_MODES)!r}, got {value!r}"
                    )
                kwargs[spec.field_name] = value
            else:  # "str"
                default_str = "" if spec.default is _REQUIRED else spec.default
                kwargs[spec.field_name] = _get_str(
                    section, key_name, default_str
                )

        # -- required fields (skipped when validate=False) -----------------

        if validate:
            missing: list[str] = []
            for spec in _FIELD_SPECS:
                if spec.required_in_yaml and not kwargs[spec.field_name]:
                    missing.append(spec.yaml_path)
            if missing:
                errors.append(
                    "Missing required field(s): " + ", ".join(missing)
                )

        if errors:
            raise ConfigurationError("\n".join(errors))

        return cls(**kwargs)

    @classmethod
    def from_yaml(
        cls, path: str | Path, *, validate: bool = True
    ) -> MailConfig:
        """Build a ``MailConfig`` from a YAML file.

        The file is expected to follow this structure::

            imap:
              host: imap.example.com
              port: 993
              tls_mode: direct-tls

            smtp:
              host: smtp.example.com
              port: 587
              tls_mode: starttls

            auth:
              username: user@example.com
              password: s3cret

            llm:
              api_key: sk-or-v1-…
              model: deepseek/deepseek-v4-flash

        All fields are optional; missing fields fall back to the same
        defaults as ``from_env()``.

        Args:
            path: Filesystem path to the YAML file.
            validate: If True (the default), raise ConfigurationError
                when required fields are empty.  Set to False to load a
                partial file that intentionally leaves required fields
                blank (e.g. round-tripping ``detect`` output in tests).

        Returns:
            A fully-populated ``MailConfig``.

        Raises:
            ConfigurationError: If the file cannot be parsed or (when
                *validate* is True) if required fields are missing.
            FileNotFoundError: If *path* does not exist.
        """
        path = Path(path)

        try:
            raw = path.read_text()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot read config file {path}: {exc}"
            ) from exc

        try:
            data: object = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigurationError(
                f"Invalid YAML in {path}: {exc}"
            ) from exc

        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigurationError(
                f"YAML root must be a mapping, got {type(data).__name__}"
            )

        return cls._parse_config_dict(data, path, validate=validate)


# ---------------------------------------------------------------------------
# Self-consistency: ``_FIELD_SPECS`` must enumerate every dataclass field
# exactly once.  If they drift apart, import fails immediately — making
# "add a new field" a one-place edit.
# ---------------------------------------------------------------------------

_spec_names = {s.field_name for s in _FIELD_SPECS}
_dc_names = {f.name for f in dataclasses.fields(MailConfig)}
assert _spec_names == _dc_names, (  # noqa: S101  # nosec B101
    f"_FIELD_SPECS / MailConfig drift: "
    f"missing from specs={_dc_names - _spec_names}, "
    f"missing from dataclass={_spec_names - _dc_names}"
)


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------


def load() -> MailConfig:
    """Load ``MailConfig`` through a single cascade: defaults → file → env.

    1.  Call ``MailConfig.from_env()``.  If all required fields are
        present in the environment, return immediately (env wins).
    2.  Otherwise, if *only* required fields are missing (no invalid
        values), load the YAML config file at ``MAIL_CONFIG_PATH``
        (defaulting to ``config/mail.local.yaml``).
    3.  *Re-apply* environment variables on top, so any ``MAIL_*`` var
        that IS set overrides the corresponding file value field-by-field.

    Defaults live in the ``MailConfig`` dataclass — fields absent from
    both the file and the environment fall back to those.

    If ``from_env()`` fails because of an invalid value (e.g. a
    non-integer port), the error is re-raised immediately — the user
    explicitly set an env var and a typo should not be silently
    swallowed by a file fallback.
    """
    # — attempt from_env alone —
    try:
        return MailConfig.from_env()
    except ConfigurationError as exc:
        if not exc.missing_only:
            raise

    # — load the YAML config file —
    config_path = Path(
        os.environ.get("MAIL_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    )
    try:
        file_cfg = MailConfig.from_yaml(config_path)
    except FileNotFoundError:
        raise ConfigurationError(
            f"Config file not found: {config_path}"
        ) from None

    # — env vars override file values field-by-field —
    return _merge_env(file_cfg)


def load_llm() -> tuple[str, str]:
    """Resolve ``(api_key, model)`` for LLM features through the same
    cascade as :func:`load`, but *without* requiring the mail fields.

    Order: ``LLM_API_KEY`` / ``LLM_MODEL`` environment variables win;
    otherwise the ``llm:`` section of the YAML config file at
    ``MAIL_CONFIG_PATH`` (default ``config/mail.local.yaml``) is consulted.
    The model falls back to :data:`DEFAULT_LLM_MODEL`.

    This is separated from :func:`load` because ``detect`` runs before a
    complete mail configuration exists — it only needs the LLM settings.
    """
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")

    if not api_key or not model:
        config_path = Path(
            os.environ.get("MAIL_CONFIG_PATH", DEFAULT_CONFIG_PATH)
        )
        if config_path.exists():
            try:
                file_cfg = MailConfig.from_yaml(config_path, validate=False)
            except (ConfigurationError, FileNotFoundError, OSError):
                file_cfg = None
            if file_cfg is not None:
                api_key = api_key or file_cfg.llm_api_key
                model = model or file_cfg.llm_model

    return api_key, model or DEFAULT_LLM_MODEL


def _merge_env(base: MailConfig) -> MailConfig:
    """Return a new ``MailConfig`` where any set env var overrides *base*."""
    kwargs: dict[str, Any] = {}
    for spec in _FIELD_SPECS:
        raw = os.environ.get(spec.env_key, "")
        if raw:
            if spec.kind == "int":
                kwargs[spec.field_name] = _parse_int(spec.env_key, raw)
            elif spec.kind == "tls_mode":
                _check_tls_mode(spec.env_key, raw)
                kwargs[spec.field_name] = raw
            else:
                kwargs[spec.field_name] = raw
        else:
            kwargs[spec.field_name] = getattr(base, spec.field_name)
    return MailConfig(**kwargs)


# ---------------------------------------------------------------------------
# Internal helpers for YAML parsing
# ---------------------------------------------------------------------------


def _get_table(
    data: dict[str, object], key: str
) -> dict[str, object] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"Config key {key!r} must be a table/mapping, "
            f"got {type(value).__name__}"
        )
    return value


def _get_str(section: dict[str, object], key: str, default: str) -> str:
    value = section.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigurationError(
            f"Config key {key!r} must be a string, got {type(value).__name__}"
        )
    return value


def _get_int(
    section: dict[str, object], key: str, default: int, path: Path
) -> int:
    value = section.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigurationError(
            f"Config key {key!r} must be an integer, got bool ({value!r})"
        )
    if not isinstance(value, int):
        raise ConfigurationError(
            f"Config key {key!r} must be an integer, got {type(value).__name__}"
        )
    return value
