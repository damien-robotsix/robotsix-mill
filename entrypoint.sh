#!/bin/sh
set -eu

# ---------------------------------------------------------------------------
# robotsix-auto-mail entrypoint — pre-flight validation and optional
# config-file templating via envsubst before handing off to the Python CLI.
# ---------------------------------------------------------------------------

# Bypass config checks for flags that should never require config.
case "${1-}" in
    -h|--help|-V|--version|""|detect) exec robotsix-auto-mail "$@" ;;
esac

_TEMP_CONFIG=""

# Clean up any temporary config file on exit.
cleanup() {
    if [ -n "${_TEMP_CONFIG}" ] && [ -f "${_TEMP_CONFIG}" ]; then
        rm -f "${_TEMP_CONFIG}"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------

_have_env_vars=0
if [ -n "${MAIL_IMAP_HOST-}" ] && \
   [ -n "${MAIL_SMTP_HOST-}" ] && \
   [ -n "${MAIL_USERNAME-}" ] && \
   [ -n "${MAIL_PASSWORD-}" ]; then
    _have_env_vars=1
fi

_have_config_path=0
if [ -n "${MAIL_CONFIG_PATH-}" ]; then
    if [ -r "${MAIL_CONFIG_PATH}" ]; then
        _have_config_path=1
    else
        echo "Config file not found: ${MAIL_CONFIG_PATH}" >&2
        exit 1
    fi
fi

if [ "${_have_env_vars}" -eq 0 ] && [ "${_have_config_path}" -eq 0 ]; then
    cat >&2 <<EOF
Missing required configuration.

Provide either:
  • All four MAIL_* environment variables:
      MAIL_IMAP_HOST, MAIL_SMTP_HOST, MAIL_USERNAME, MAIL_PASSWORD
  • A YAML config file via MAIL_CONFIG_PATH

Examples:
  docker compose run -e MAIL_IMAP_HOST=… -e MAIL_SMTP_HOST=… \\
      -e MAIL_USERNAME=… -e MAIL_PASSWORD=… robotsix-auto-mail probe

  docker compose run robotsix-auto-mail probe
  (reads config from MAIL_CONFIG_PATH=\${MAIL_CONFIG_PATH:-config/mail.local.yaml})
EOF
    exit 1
fi

# ---------------------------------------------------------------------------
# Optional config-file templating via envsubst
# ---------------------------------------------------------------------------

if [ "${_have_config_path}" -eq 1 ] && command -v envsubst >/dev/null 2>&1; then
    _TEMP_CONFIG="$(mktemp /tmp/mail-config.XXXXXX)"
    envsubst < "${MAIL_CONFIG_PATH}" > "${_TEMP_CONFIG}"
    MAIL_CONFIG_PATH="${_TEMP_CONFIG}"
    export MAIL_CONFIG_PATH
fi

# ---------------------------------------------------------------------------
# Launch the application (replaces this shell process)
# ---------------------------------------------------------------------------

exec robotsix-auto-mail "$@"