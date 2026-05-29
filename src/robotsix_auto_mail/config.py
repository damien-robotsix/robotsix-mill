"""Mail configuration subsystem.

Provides ``MailConfig``, a frozen dataclass that holds IMAP and SMTP
connection parameters, and several classmethods / convenience functions
for loading it from environment variables, TOML files, or YAML files.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
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
    imap_folder: str = "INBOX"

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
        imap_folder = os.environ.get("MAIL_IMAP_FOLDER", "INBOX")

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
            imap_folder=imap_folder,
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

        All fields are optional; missing fields fall back to the same
        defaults as ``from_env()``.

        Args:
            path: Filesystem path to the YAML file.
            validate: If True (the default), raise ConfigurationError
                when required fields are empty.  Set to False when
                loading a defaults file that intentionally has blank
                required fields.

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

        # -- required fields (skipped when validate=False) -----------------

        if validate:
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
                    "Missing required field(s) in YAML: "
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
        imap_folder = _get_str(imap_section, "folder", "INBOX")

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
            imap_folder=imap_folder,
        )


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------


def load() -> MailConfig:
    """Load ``MailConfig``, preferring environment variables over file config.

    1.  Call ``MailConfig.from_env()``.  If all required fields are
        present, return immediately (env wins).
    2.  Otherwise, if *only* required fields are missing (no invalid
        values), determine the config path via the ``MAIL_CONFIG_PATH``
        env var (defaulting to ``config/mail.toml``) and load it.
    3.  If the path ends with ``.yaml`` or ``.yml``, load via
        ``from_yaml()``; otherwise via ``from_toml()``.
    4.  If a ``MAIL_DEFAULTS_PATH`` env var is set (or a file exists
        alongside the main config named ``mail.defaults.yaml``), the
        defaults file is loaded first and the main config is deep-merged
        on top — the defaults supply every missing key.
    5.  Then *re-apply* environment variables on top, so that any env
        var that IS set overrides the corresponding file value.

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

    # — determine file path —
    config_path = Path(
        os.environ.get("MAIL_CONFIG_PATH", "config/mail.toml")
    )

    # — load file config (YAML or TOML) —
    try:
        if config_path.suffix in (".yaml", ".yml"):
            file_cfg = MailConfig.from_yaml(config_path)
        else:
            file_cfg = MailConfig.from_toml(config_path)
    except FileNotFoundError:
        raise ConfigurationError(
            f"Config file not found: {config_path}"
        ) from None

    # — load defaults if available and deep-merge —
    defaults_path = os.environ.get("MAIL_DEFAULTS_PATH", "")
    if not defaults_path:
        # Auto-detect: mail.defaults.yaml alongside the main config.
        candidate = config_path.with_name("mail.defaults.yaml")
        if candidate.exists():
            defaults_path = str(candidate)

    if defaults_path:
        try:
            defaults_cfg = MailConfig.from_yaml(
                defaults_path, validate=False
            )
        except FileNotFoundError:
            defaults_cfg = None
        except ConfigurationError:
            defaults_cfg = None
        if defaults_cfg is not None:
            file_cfg = _deep_merge(defaults_cfg, file_cfg)

    # — env vars override file values field-by-field —
    return _merge_env_onto_toml(file_cfg)


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
        "imap_folder": "MAIL_IMAP_FOLDER",
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
# Deep merge helper for YAML defaults + local config
# ---------------------------------------------------------------------------


def _deep_merge(
    defaults: MailConfig, overrides: MailConfig
) -> MailConfig:
    """Return a new ``MailConfig`` where every non-default field in
    *overrides* replaces the corresponding field from *defaults*.

    A field is considered "non-default" if it differs from the
    ``MailConfig`` constructor's built-in default.  For string fields
    (host, username, password) this means non-empty; for int / TLS /
    folder / db_path fields this means not equal to the sentinel.

    This allows ``mail.defaults.yaml`` to supply every key and
    ``mail.local.yaml`` to only override the ones the operator cares
    about.
    """
    # The dataclass defaults are what from_yaml / from_toml
    # produce when a key is absent.  We compare against those.
    _d = MailConfig(
        imap_host="",
        smtp_host="",
        username="",
        password="",
    )
    return MailConfig(
        imap_host=_pick_str(overrides.imap_host, defaults.imap_host),
        smtp_host=_pick_str(overrides.smtp_host, defaults.smtp_host),
        username=_pick_str(overrides.username, defaults.username),
        password=_pick_str(overrides.password, defaults.password),
        imap_port=(
            defaults.imap_port
            if overrides.imap_port == _d.imap_port
            else overrides.imap_port
        ),
        smtp_port=(
            defaults.smtp_port
            if overrides.smtp_port == _d.smtp_port
            else overrides.smtp_port
        ),
        imap_tls_mode=(
            defaults.imap_tls_mode
            if overrides.imap_tls_mode == _d.imap_tls_mode
            else overrides.imap_tls_mode
        ),
        smtp_tls_mode=(
            defaults.smtp_tls_mode
            if overrides.smtp_tls_mode == _d.smtp_tls_mode
            else overrides.smtp_tls_mode
        ),
        imap_folder=(
            defaults.imap_folder
            if overrides.imap_folder == _d.imap_folder
            else overrides.imap_folder
        ),
        db_path=(
            defaults.db_path
            if overrides.db_path == _d.db_path
            else overrides.db_path
        ),
    )


def _pick_str(override: str, fallback: str) -> str:
    """Return *override* if non-empty, else *fallback*."""
    return override if override else fallback


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
