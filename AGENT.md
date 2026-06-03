# Agent guide

## Project layout

Each robotsix_llmio module uses the per-module layout: code in `src/robotsix_llmio/<module>/`, tests in `tests/<module>/test_*.py`, and docs in `docs/<module>/index.md`. Never place test files in the flat `tests/` root, and register every module's `src`/`tests`/`docs` paths in `docs/modules.yaml`.
