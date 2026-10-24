#!/usr/bin/env bash
# Single definition of the mypy gate shared by CI (ci.yml), `make lint` and
# the pre-commit hook: strict mypy, failing only on errors not already in
# mypy-baseline.txt.  No `pipefail` on purpose — the exit status comes from
# the baseline filter, exactly as CI has always evaluated it.
set -eu
cd "$(dirname "$0")/.."
uv run mypy src/ --strict | uv run mypy-baseline filter --baseline-path mypy-baseline.txt
