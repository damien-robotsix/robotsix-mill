#!/usr/bin/env bash
# Run the management-plane service: HTTP API + the event-driven worker.
# Tickets emitted via the API are picked up immediately — no scheduler.
set -euo pipefail

# When started as root with the host docker socket mounted, inherit the
# host's docker group GID (whatever it happens to be — 959, 999, 1000…)
# and add the mill user to a matching in-container group. Then drop
# privileges and exec as mill. This removes the need for an externally-
# supplied DOCKER_GID (env var / .env), which was both error-prone and
# host-specific.
#
# When NOT root (legacy image with USER mill baked in), assume the
# group_add path in docker-compose still applies and just exec.
if [[ "$(id -u)" == "0" ]]; then
  if [[ -S /var/run/docker.sock ]]; then
    SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
    if [[ "$SOCK_GID" =~ ^[0-9]+$ ]] && [[ "$SOCK_GID" -gt 0 ]]; then
      if ! getent group "$SOCK_GID" >/dev/null; then
        groupadd --gid "$SOCK_GID" docker_host
      fi
      SOCK_GROUP_NAME="$(getent group "$SOCK_GID" | cut -d: -f1)"
      usermod -aG "$SOCK_GROUP_NAME" mill
    fi
  fi
  exec runuser -u mill -- robotsix-mill serve
fi

exec robotsix-mill serve
