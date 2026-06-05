# Agent guide

## Project layout

Each robotsix_llmio module uses the per-module layout: code in `src/robotsix_llmio/<module>/`, tests in `tests/<module>/test_*.py`, and docs in `docs/<module>/index.md`. Never place test files in the flat `tests/` root, and register every module's `src`/`tests`/`docs` paths in `docs/modules.yaml`.

**Rule:** When adding a new test or source module under `tests/` or `src/robotsix_llmio/`, register its path in `docs/modules.yaml` in the same change — the manifest must stay in sync with the actual module tree.

## CI / workflows

**Rule:** A GitHub Actions step that uploads a *required* artifact (e.g. an SBOM) MUST set `if: always()` so it still runs when an earlier step in the same job exits non-zero. A non-zero exit from any preceding step (e.g. `pip-audit`, lint, tests) skips all later steps in that job, which makes an `if-no-files-found: error` backstop unreachable and silently drops the artifact — do not rely on a preceding audit/lint/test step staying green to guarantee the upload runs.
