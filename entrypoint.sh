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
  if [[ -n "${DOCKER_HOST:-}" ]]; then
    # ---- central-deploy mode -------------------------------------------
    # Docker is reached over TCP (a socket-proxy sibling), so there is NO
    # host docker.sock to join — skip the GID step entirely. Instead,
    # reconcile the config volume that central-deploy populated.
    #
    # The mill now reads a SINGLE config file: /app/config/config.json,
    # which central-deploy writes into the mill-config volume and which
    # holds every non-secret knob plus a top-level `secrets:` block. No
    # split / overlay reconstruction is needed. Lock it down (it carries
    # real credentials) and hand config + data to the unprivileged runtime
    # user so it can read config.json and write its DB.
    if [[ -f /app/config/config.json ]]; then
      chmod 600 /app/config/config.json 2>/dev/null || true
    fi
    chown -R mill:mill /app/config 2>/dev/null || true
    chown mill:mill /data 2>/dev/null || true
  elif [[ -S /var/run/docker.sock ]]; then
    SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
    if [[ "$SOCK_GID" =~ ^[0-9]+$ ]] && [[ "$SOCK_GID" -gt 0 ]]; then
      if ! getent group "$SOCK_GID" >/dev/null; then
        groupadd --gid "$SOCK_GID" docker_host
      fi
      SOCK_GROUP_NAME="$(getent group "$SOCK_GID" | cut -d: -f1)"
      usermod -aG "$SOCK_GROUP_NAME" mill
    fi
  fi
  # Raise the soft file-descriptor limit. Docker's `ulimits.nofile` in
  # compose only sets the hard limit; the soft stays at 1024 by default,
  # which workers exhaust quickly when many spawn git/trivy/agent
  # subprocesses in parallel (saw "[Errno 24] Too many open files"
  # cascading workers to 0). runuser preserves limits inherited from
  # the parent shell, so raise the soft limit here before the exec.
  ulimit -n 65536 || true
  exec runuser -u mill -- robotsix-mill serve
fi

ulimit -n 65536 || true
exec robotsix-mill serve
