#!/usr/bin/env bash
# Run the management-plane service: HTTP API + the event-driven worker.
# Tickets emitted via the API are picked up immediately — no scheduler.
set -euo pipefail

exec robotsix-mill serve
