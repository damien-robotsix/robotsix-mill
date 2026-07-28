## 0.0.0 (unreleased)

- `_build_failing_summary` now includes an explicit pass/fail indicator (❌ FAILED: / ✅ PASSED:) per check instead of the ambiguous `Failing check #N` label. Check-run conclusions are now preserved through `_extract_annotations` (GitHub) and `_get_failed_jobs` (GitLab) so the summary can show the genuine status at a glance.
- Consolidate `runners` module into `agents.runners` sub-package to break the circular dependency between `agents` and `runners`.  All runner source files move to `src/robotsix_mill/agents/runners/` and test files to `tests/agents/runners/`.  A deprecation shim at `src/robotsix_mill/runners/__init__.py` preserves backward-compatible imports for one release cycle.
- Enable Ruff D (pydocstyle) rules with Google convention, suppressing D105/D107/D205/D415.  Auto-fixed ~260 mechanical violations; remaining D102/D417 gaps are per-file-ignored with FIXME markers for incremental cleanup.
- Enable pytest-xdist parallel test execution: add `-n auto` to CI pytest-args, `parallel = true` to coverage config, and restructure `make test` with `coverage combine` for accurate multi-worker coverage. Add `make test-fast` for no-coverage parallel runs.
- Fix `test_gap_interval_seconds` documented default in `docs/config/configuration.md`: `86400` → `604800` (7 days), matching the Pydantic model default in `_settings_periodic.py`.
- Fix stale code comment: survey interval default now correctly stated as 604800 (7 days) in `_settings_periodic.py`.
- Document `trace_review_min_confidence` in the `trace_review` config table (`docs/config/configuration.md`).
- Fix `survey_interval_seconds` documented default in `docs/config/configuration.md`: was `86400` (1 day), now matches the Pydantic model default of `604800` (7 days).
- Fix `docs/config/configuration.md` audit agent table: default for `MILL_AUDIT_INTERVAL_SECONDS` corrected from 86400 to 604800 (7 days), matching the Pydantic model field default.
- Fix `docs/config/configuration.md`: the `MILL_META_INTERVAL_SECONDS` default was documented as `86400` (1 day) but the model default is `604800` (7 days). Now documents the correct `604800` default.
- Fix stale `doc_request_limit` default in `agent_definitions/document.yaml`: both the budget description ("default 16" → "default 32") and tool-use discipline section ("budget (16)" → "budget (32)") now match the actual config default of 32.
- Fix `board_list_cache_ttl_seconds` default drift: changed Pydantic model default from `0.0` to `3.0` to match `config.example.json` and docs, making the board-list cache enabled by default as intended.
- Add `check-builtin-kinds` pre-commit hook to validate `_BUILTIN_KINDS` cross-sync across workflow portability, periodic passes, poll loops, and agent definitions.
- Updated all stale `config/repos.yaml` references in the Repos registry section of `docs/config/configuration.md` to reference `config/config.json`'s `"repos"` key, matching the actual loader behaviour.
- Add `check-builtin-kinds` pre-commit hook to validate `_BUILTIN_KINDS` cross-sync between `workflow_portability.py`, `_passes.py`, `poll_loops.py`, `.robotsix-mill/periodic/`, and `agent_definitions/periodic/`.
- Added `scripts/check_builtin_kinds.py` pre-commit hook that cross-validates `_BUILTIN_KINDS` against `.robotsix-mill/periodic/`, `agent_definitions/periodic/`, and `_passes.py` to prevent multi-site synchronization drift. Fixed the known inconsistency: added `"roadmap_sync": "schedule_only"` to `_BUILTIN_KINDS`. Registered the hook in `.pre-commit-config.yaml` and `.github/workflows/ci.yml`.
- Refine agent: add "Run pytest only once" guidance to combine all flags into a single invocation, avoiding wasted duplicate full-suite runs.
- Add `credit_balance` to `_BUILTIN_KINDS` as `"schedule_only"` so `kind_for("credit_balance")` returns the correct kind and `is_portable` returns `True` consistently with other schedule-only workflows.
- Reclassify `repo_description_sync` from `schedule_only` to `llm_agent` so that
  per-repo presence-file overrides (``prompt_overlay`` / ``system_prompt``) take
  effect at runtime. The custom runner now receives ``definition_override`` from
  the periodic-workflow dispatch path instead of loading the built-in YAML
  directly from disk.
- Fix stale `config/repos.yaml` references in the "Deployed log folder" section
  of `docs/config/configuration.md` — the repos config lives under the `"repos"`
  key of `config/config.json`, not in a standalone `config/repos.yaml`
- Post-rebase diff integrity check: after every rebase the pipeline now
  verifies that all implement-stage source files still appear in the
  branch diff vs merge-base.  If the rebase agent silently dropped a
  file (the PR's changes were discarded during conflict resolution),
  the ticket is BLOCKED with a diagnostic listing the dropped files.
  Excludes `CHANGELOG.md` and `changelog.d/` from the comparison.
- Stuck-loop detector in implement stage now considers committed branch-ahead-of-main work (not just working-tree changes), preventing false BLOCKED when a prior cycle already committed and pushed complete work
- Implement agent: add instruction to register new changelog fragment files in `docs/modules.yaml` under the `core` module's `paths` list.
