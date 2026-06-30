#!/usr/bin/env python3
"""Split the central-deploy config-target into the files the mill loads.

central-deploy writes a single operator form (``config/config.yaml``, see
``config/config.yaml``) into the mill-config volume.  The mill's config
loader, however, reads a *flat* ``config/secrets.yaml`` (the Secrets model)
and a non-secret YAML overlay ``config/mill.local.yaml`` layered over
``config/mill.defaults.yaml``.  This script bridges the two.

Behaviour (idempotent; safe to run on every container start):

* The ``secrets:`` sub-map is written to ``<config_dir>/secrets.yaml``
  (mode 0600), dropping blank/None leaves so unset secrets fall back to
  the Secrets model's ``None`` defaults rather than empty strings.
* Everything else is written to ``<config_dir>/mill.local.yaml`` as the
  operator overlay.
* Two deploy invariants are forced into the overlay regardless of the
  form: ``service.api_host = 0.0.0.0`` (the gateway reaches the container
  over its bridge IP, not loopback) and ``service.data_dir = /data`` (the
  mounted volume), matching ``config/mill.local.example.yaml``.

Called from ``entrypoint.sh`` only in deploy mode (DOCKER_HOST set).  Never
runs in the dev stack, where the developer's own mill.local.yaml /
secrets.yaml are bind-mounted and must not be clobbered.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import yaml


# central-deploy marks a sensitive leaf with this literal sentinel in the
# onboarding template (config/config.yaml). The deployed config it writes back
# never contains the sentinel (central-deploy resolves it to the real value or
# ""), but treat it as blank here as a defensive last line of defense so a
# stray "SECRET" can never be ingested as a real credential.
_CONFIG_SECRET_SENTINEL = "SECRET"  # noqa: S105 — sentinel marker, not a credential


def _is_blank(value: Any) -> bool:
    return (
        value is None
        or value == _CONFIG_SECRET_SENTINEL
        or (isinstance(value, str) and value.strip() == "")
    )


def split_config(config_path: str, config_dir: str) -> None:
    with open(config_path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        raise SystemExit(f"{config_path}: expected a top-level YAML mapping")

    # Pull out the secrets sub-map; the remainder is the overlay.
    raw_secrets = doc.pop("secrets", {}) or {}
    if not isinstance(raw_secrets, dict):
        raise SystemExit(f"{config_path}: 'secrets:' must be a mapping")
    secrets = {k: v for k, v in raw_secrets.items() if not _is_blank(v)}

    overlay: dict[str, Any] = dict(doc)
    # Deploy invariants (see module docstring) — force, do not merge-if-absent.
    service = dict(overlay.get("service") or {})
    service["api_host"] = "0.0.0.0"  # noqa: S104 — must bind all interfaces so the central-deploy gateway can reach the container over its bridge IP
    service["data_dir"] = "/data"
    overlay["service"] = service

    secrets_path = os.path.join(config_dir, "secrets.yaml")
    local_path = os.path.join(config_dir, "mill.local.yaml")

    # Write secrets with restrictive permissions before any content lands.
    fd = os.open(secrets_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(secrets, fh, default_flow_style=False, sort_keys=True)

    with open(local_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(overlay, fh, default_flow_style=False, sort_keys=True)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: split_config.py <config-target-path> <config-dir>",
            file=sys.stderr,
        )
        return 2
    split_config(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
