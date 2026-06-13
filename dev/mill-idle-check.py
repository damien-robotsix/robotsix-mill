#!/usr/bin/env python3
"""Exit 0 if the mill is busy (expensive work in progress), 1 if idle.

Used as the ``--idle-check-cmd`` for ``robotsix-autoupdate``.
Polls the mill API to detect active periodic passes (``/runs``) and
in-flight ticket stages (``/tickets``).

Convention (matching the bash heredoc in ``dev/mill-autoupdate.sh``):
  exit 0 = busy  (do NOT restart)
  exit 1 = idle  (safe to restart)
"""

from __future__ import annotations

import argparse
import json
import urllib.request

DEFAULT_API = "http://localhost:8077"

BUSY_STATES = {"draft", "ready", "done", "rebasing", "fixing_ci"}


def get(api: str, path: str) -> list[dict] | None:
    """Fetch *path* from the mill API.  Returns parsed JSON list or ``None``."""
    try:
        with urllib.request.urlopen(api + path, timeout=10) as r:
            return json.load(r)  # type: ignore[no-any-return]
    except Exception:
        return None


def is_busy(api: str) -> bool:
    """Return ``True`` if a periodic pass is running or a ticket stage is active."""
    runs = get(api, "/runs")
    if runs and any(r.get("status") == "running" for r in runs):
        return True  # a periodic pass is in flight

    tickets = get(api, "/tickets")
    if tickets and any(
        t.get("state") in BUSY_STATES and not t.get("unmet_deps") for t in tickets
    ):
        return True  # a ticket stage is being worked

    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exit 0 if the mill is busy, 1 if idle.",
    )
    parser.add_argument(
        "--api",
        default=DEFAULT_API,
        help=f"API base URL (default: {DEFAULT_API})",
    )
    args = parser.parse_args()

    if is_busy(args.api):
        return 0  # busy
    return 1  # idle


if __name__ == "__main__":
    raise SystemExit(main())
