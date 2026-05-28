"""Mail configuration subsystem.

Provides ``MailConfig``, a frozen dataclass that holds IMAP and SMTP
connection parameters, and several classmethods / convenience functions
for loading it from environment variables or TOML files.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigurationError(Exception):
    """Raised when the mail configuration is invalid or incomplete.

    Attributes:
        message: Human-readable error description.
        missing_only: True when the *only* problem is missing required
            fields (no invalid values).  Used by ``load()`` to decide
            whether TOML fallback is appropriate.
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
    """Immutable mail server connection parameters.

    Credentials are stored in memory as plain ``str`` values but the
    ``password`` field is masked in ``repr`` / ``str``.
    """

    imap_host: str
    smtp_host: str
    username: str
    password: str

    imap_port: int = 993
    imap_tls_mode: str = "direct-tls"
    smtp_port: int = 587
    smtp_tls_mode: str = "starttls"

    db_path: str = "mail.db"

    # -- masking -----------------------------------------------------------

    def __repr__(self) -> str:
        cls = type(self).__name__
        fields = dataclasses.fields(self)
        parts = []
        for f in fields:
            val = getattr(self, f.name)
            if f.name == "password":
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

        db_path = os.environ.get("MAIL_DB_PATH", "mail.db")

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
            # values), flag the error so load() can safely fall back
            # to TOML.  Invalid values mean the user explicitly set an
            # env var — falling back would silently swallow their typo.
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
        )

    @classmethod
    def from_toml(cls, path: str | Path) -> MailConfig:
        """Build a ``MailConfig`` from a TOML file.

        The file is expected to follow this structure::

            [imap]
            host = "imap.example.com"
            port = 993
            tls_mode = "direct-tls"

            [smtp]
            host = "smtp.example.com"
            port = 587
            tls_mode = "starttls"

            [auth]
            username = "user@example.com"
            password = "s3cret"

        All fields are optional; missing fields fall back to the same
        defaults as ``from_env()``.

        Args:
            path: Filesystem path to the TOML file.

        Returns:
            A fully-populated ``MailConfig``.

        Raises:
            ConfigurationError: If the file cannot be parsed or if
                required fields are missing.
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
            data: dict[str, object] = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"Invalid TOML in {path}: {exc}"
            ) from exc

        # -- extract sections ----------------------------------------------

        imap_section = _get_table(data, "imap") or {}
        smtp_section = _get_table(data, "smtp") or {}
        auth_section = _get_table(data, "auth") or {}

        # -- read fields with defaults -------------------------------------

        imap_host = _get_str(imap_section, "host", "")
        imap_port = _get_int(imap_section, "port", 993, path)
        imap_tls_mode = _get_str(imap_section, "tls_mode", "direct-tls")

        smtp_host = _get_str(smtp_section, "host", "")
        smtp_port = _get_int(smtp_section, "port", 587, path)
        smtp_tls_mode = _get_str(smtp_section, "tls_mode", "starttls")

        username = _get_str(auth_section, "username", "")
        password = _get_str(auth_section, "password", "")

        store_section = _get_table(data, "store") or {}
        db_path = _get_str(store_section, "path", "mail.db")

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

        # -- required fields -----------------------------------------------

        missing: list[str] = []
        for label, value in [
            ("imap.host", imap_host),
            ("smtp.host", smtp_host),
            ("auth.username", username),
            ("auth.password", password),
        ]:
            if not value:
                missing.append(label)

        if missing:
            errors.append(
                "Missing required field(s) in TOML: "
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
        )


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------


def load() -> MailConfig:
    """Load ``MailConfig``, preferring environment variables over TOML.

    1. Call ``MailConfig.from_env()``.  If all required fields are
       present, return immediately (env wins).
    2. Otherwise, if *only* required fields are missing (no invalid
       values), determine the TOML path via the ``MAIL_CONFIG_PATH``
       env var (defaulting to ``config/mail.toml``) and call
       ``MailConfig.from_toml()``.
    3. Then *re-apply* environment variables on top, so that any env
       var that IS set overrides the corresponding TOML value.

    If ``from_env()`` fails because of an invalid value (e.g. a
    non-integer port), the error is re-raised immediately — the user
    explicitly set an env var and a typo should not be silently
    swallowed by a TOML fallback.
    """
    # Determine TOML path early (used for fallback).
    toml_path = Path(os.environ.get("MAIL_CONFIG_PATH", "config/mail.toml"))

    # Attempt from_env alone first.
    try:
        env_cfg = MailConfig.from_env()
        return env_cfg
    except ConfigurationError as exc:
        # Only fall back to TOML when the *only* problem is missing
        # required fields — invalid values (e.g. MAIL_IMAP_PORT=abc)
        # mean the user explicitly set an env var and should get an
        # error instead of a silent TOML fallback.
        if not exc.missing_only:
            raise

    # Load from TOML (will raise if file missing / invalid).
    try:
        toml_cfg = MailConfig.from_toml(toml_path)
    except FileNotFoundError:
        raise ConfigurationError(
            f"Config file not found: {toml_path}"
        ) from None

    # Merge: env vars override TOML values field-by-field.
    return _merge_env_onto_toml(toml_cfg)


def _merge_env_onto_toml(base: MailConfig) -> MailConfig:
    """Return a new ``MailConfig`` where any set env var overrides *base*."""
    env_map: dict[str, str] = {
        "imap_host": "MAIL_IMAP_HOST",
        "imap_port": "MAIL_IMAP_PORT",
        "imap_tls_mode": "MAIL_IMAP_TLS_MODE",
        "smtp_host": "MAIL_SMTP_HOST",
        "smtp_port": "MAIL_SMTP_PORT",
        "smtp_tls_mode": "MAIL_SMTP_TLS_MODE",
        "username": "MAIL_USERNAME",
        "password": "MAIL_PASSWORD",
        "db_path": "MAIL_DB_PATH",
    }

    kwargs: dict[str, str | int] = {}
    for field_name, env_key in env_map.items():
        raw = os.environ.get(env_key, "")
        if raw:
            # Port fields need int parsing.
            if field_name in ("imap_port", "smtp_port"):
                kwargs[field_name] = _parse_int(env_key, raw)
            else:
                kwargs[field_name] = raw
            # Validate TLS modes when supplied via env.
            if field_name in ("imap_tls_mode", "smtp_tls_mode") and raw:
                _check_tls_mode(env_key, raw)
        else:
            kwargs[field_name] = getattr(base, field_name)

    # If any TLS overrides were supplied, they've been validated above;
    # also validate the ones that came from TOML (already done in
    # from_toml, but belt-and-suspenders).
    for field_name in ("imap_tls_mode", "smtp_tls_mode"):
        val = kwargs[field_name]
        if isinstance(val, str):
            _check_tls_mode(field_name, val)

    return MailConfig(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Internal helpers for TOML parsing
# ---------------------------------------------------------------------------


def _get_table(
    data: dict[str, object], key: str
) -> dict[str, object] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"TOML key {key!r} must be a table, got {type(value).__name__}"
        )
    return value


def _get_str(section: dict[str, object], key: str, default: str) -> str:
    value = section.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigurationError(
            f"TOML key {key!r} must be a string, got {type(value).__name__}"
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
            f"TOML key {key!r} must be an integer, got bool ({value!r})"
        )
    if not isinstance(value, int):
        raise ConfigurationError(
            f"TOML key {key!r} must be an integer, got {type(value).__name__}"
        )
    return value
