#!/usr/bin/env bash
# Browser-level board smoke check (Tier 2).
#
# Thin wrapper invoked by the path-scoped smoke gate: it runs the
# Playwright board browser driver and propagates its exit code so a
# non-zero result reds the gate.
#
# Kept SEPARATE from Tier 1's scripts/smoke_board.sh (process-startup +
# HTTP health/endpoint smoke) by design — this is the headless-Chromium
# DOM/console verification layer that catches rendering breakages an API
# smoke cannot see. Requires Playwright + Chromium, which are baked into
# the sandbox image (see sandbox/Dockerfile).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"
exec python scripts/board_browser_check.py
