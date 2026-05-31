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
    imap_tls_mode: str = "direct-tls"
    smtp_port: int = 587
    smtp_tls_mode: str = "starttls"

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

        # -- helpers that collect instead of raising immediately -----------

        def _required(env_key: str, field_name: str) -> str:
            value = os.environ.get(env_key, "")
            if not value:
                missing.append(env_key)
                return ""
            return value

        def _optional_int(
            env_key: str, field_name: str, default: int
        ) -> int:
            raw = os.environ.get(env_key, "")
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                errors.append(f"{env_key} must be an integer, got {raw!r}")
                return default

        def _optional_tls(env_key: str, field_name: str, default: str) -> str:
            raw = os.environ.get(env_key, "")
            if not raw:
                return default
            if raw not in _VALID_TLS_MODES:
                errors.append(
                    f"{env_key} must be one of {sorted(_VALID_TLS_MODES)!r}, "
                    f"got {raw!r}"
                )
            return raw

        # -- collect -------------------------------------------------------

        imap_host = _required("MAIL_IMAP_HOST", "imap_host")
        smtp_host = _required("MAIL_SMTP_HOST", "smtp_host")
        username = _required("MAIL_USERNAME", "username")
        password = _required("MAIL_PASSWORD", "password")

        imap_port = _optional_int("MAIL_IMAP_PORT", "imap_port", 993)
        smtp_port = _optional_int("MAIL_SMTP_PORT", "smtp_port", 587)
        imap_tls_mode = _optional_tls(
            "MAIL_IMAP_TLS_MODE", "imap_tls_mode", "direct-tls"
        )
        smtp_tls_mode = _optional_tls(
            "MAIL_SMTP_TLS_MODE", "smtp_tls_mode", "starttls"
        )

        db_path = os.environ.get("MAIL_DB_PATH", DEFAULT_DB_PATH)
        imap_folder = os.environ.get("MAIL_IMAP_FOLDER", "INBOX")

        llm_api_key = os.environ.get("LLM_API_KEY", "")
        llm_model = os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL)

        ingest_interval_minutes = _optional_int(
            "MAIL_INGEST_INTERVAL",
            "ingest_interval_minutes",
            DEFAULT_INGEST_INTERVAL_MINUTES,
        )

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

        return cls(
            imap_host=imap_host,
            imap_port=imap_port,
            imap_tls_mode=imap_tls_mode,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_tls_mode=smtp_tls_mode,
            username=username,
            password=password,
            db_path=db_path,
            imap_folder=imap_folder,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            ingest_interval_minutes=ingest_interval_minutes,
        )

    @classmethod
    def _parse_config_dict(
        cls, data: dict[str, object], path: Path, *, validate: bool = True
    ) -> MailConfig:
        # -- extract sections ----------------------------------------------

        imap_section = _get_table(data, "imap") or {}
        smtp_section = _get_table(data, "smtp") or {}
        auth_section = _get_table(data, "auth") or {}

        # -- read fields with defaults -------------------------------------

        imap_host = _get_str(imap_section, "host", "")
        imap_port = _get_int(imap_section, "port", 993, path)
        imap_tls_mode = _get_str(imap_section, "tls_mode", "direct-tls")
        imap_folder = _get_str(imap_section, "folder", "INBOX")

        smtp_host = _get_str(smtp_section, "host", "")
        smtp_port = _get_int(smtp_section, "port", 587, path)
        smtp_tls_mode = _get_str(smtp_section, "tls_mode", "starttls")

        username = _get_str(auth_section, "username", "")
        password = _get_str(auth_section, "password", "")

        store_section = _get_table(data, "store") or {}
        db_path = _get_str(store_section, "path", DEFAULT_DB_PATH)

        llm_section = _get_table(data, "llm") or {}
        llm_api_key = _get_str(llm_section, "api_key", "")
        llm_model = _get_str(llm_section, "model", DEFAULT_LLM_MODEL)

        ingest_section = _get_table(data, "ingest") or {}
        ingest_interval_minutes = _get_int(
            ingest_section,
            "interval_minutes",
            DEFAULT_INGEST_INTERVAL_MINUTES,
            path,
        )

        # -- validate TLS modes --------------------------------------------

        errors: list[str] = []
        for label, value in [
            ("imap.tls_mode", imap_tls_mode),
            ("smtp.tls_mode", smtp_tls_mode),
        ]:
            if value not in _VALID_TLS_MODES:
                errors.append(
                    f"{label} must be one of {sorted(_VALID_TLS_MODES)!r}, "
                    f"got {value!r}"
                )

        # -- required fields (skipped when validate=False) -----------------

        if validate:
            missing: list[str] = []
            for label, value in [
                ("imap.host", imap_host),
                ("smtp.host", smtp_host),
                ("auth.username", username),
            ]:
                if not value:
                    missing.append(label)

            if missing:
                errors.append(
                    "Missing required field(s): "
                    + ", ".join(missing)
                )

        if errors:
            raise ConfigurationError("\n".join(errors))

        return cls(
            imap_host=imap_host,
            imap_port=imap_port,
            imap_tls_mode=imap_tls_mode,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_tls_mode=smtp_tls_mode,
            username=username,
            password=password,
            db_path=db_path,
            imap_folder=imap_folder,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            ingest_interval_minutes=ingest_interval_minutes,
        )

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
    env_map: dict[str, str] = {
        "imap_host": "MAIL_IMAP_HOST",
        "imap_port": "MAIL_IMAP_PORT",
        "imap_tls_mode": "MAIL_IMAP_TLS_MODE",
        "smtp_host": "MAIL_SMTP_HOST",
        "smtp_port": "MAIL_SMTP_PORT",
        "smtp_tls_mode": "MAIL_SMTP_TLS_MODE",
        "username": "MAIL_USERNAME",
        "password": "MAIL_PASSWORD",  # nosec B105
        "db_path": "MAIL_DB_PATH",
        "imap_folder": "MAIL_IMAP_FOLDER",
        "llm_api_key": "LLM_API_KEY",
        "llm_model": "LLM_MODEL",
        "ingest_interval_minutes": "MAIL_INGEST_INTERVAL",
    }

    kwargs: dict[str, str | int] = {}
    for field_name, env_key in env_map.items():
        raw = os.environ.get(env_key, "")
        if raw:
            # Integer fields need parsing.
            if field_name in (
                "imap_port",
                "smtp_port",
                "ingest_interval_minutes",
            ):
                kwargs[field_name] = _parse_int(env_key, raw)
            else:
                kwargs[field_name] = raw
            # Validate TLS modes when supplied via env.
            if field_name in ("imap_tls_mode", "smtp_tls_mode") and raw:
                _check_tls_mode(env_key, raw)
        else:
            kwargs[field_name] = getattr(base, field_name)

    # If any TLS overrides were supplied, they've been validated above;
    # also validate the ones that came from the file (already done in
    # from_yaml, but belt-and-suspenders).
    for field_name in ("imap_tls_mode", "smtp_tls_mode"):
        val = kwargs[field_name]
        if isinstance(val, str):
            _check_tls_mode(field_name, val)

    return MailConfig(**kwargs)  # type: ignore[arg-type]


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
