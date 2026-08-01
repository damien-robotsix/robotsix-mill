## 0.0.0 (unreleased)

- Board UI: add **Re‑implement** button for tickets in `human_mr_approval` — posts a change-request comment via the existing `request-implementation-changes` API, transitions the ticket back to `READY` so the implement agent re-runs against the existing PR branch without requiring the PR to be closed first.
- Cap web_fetch calls per web_research sub-agent invocation at 4 (new `web_research_fetch_max_calls` setting), raise `web_research_request_limit` from 8 to 12, and return a distinct "budget exhausted" error when the sub-agent hits `UsageLimitExceeded` — prevents the sub-agent from exhausting its request budget on excessive fetches before synthesizing an answer.
- Fix Re‑implement button flicker on fresh drawer open by including it in `updateMergeButton()` (both render paths now build identical HTML for `ticket-merge-btn-area`). Also add `.reimplement-btn:disabled` to the in-flight-lock CSS list so the button dims during `lockWhile`.
- Audit agent: add `run_command` batching guidance to prevent serial tool-call storms (chain multiple one-liners with `&&` or delegate to `explore`/`parallel_explore`).
- Document board-hygiene settings (draft TTL auto-close, open-ticket cap, rollup epics) in the configuration reference
- `wait_for_ci` now includes the GitHub Actions `run_id` in the `CI_FAILING` output
  prefix (e.g. `[sha: 04cdd8f, run: 30399400000]`), so the ci-fix agent can pass it
  directly to `fetch_ci_logs` without blindly guessing run IDs.
- `ask_user` tool now emits an operator notification (via the existing ntfy channel) when it opens an `[ASK_USER]` thread, so the operator sees the question before the 3-day timeout fires.
- Auto-merge is now **opt-out** (default `True`) instead of opt-in. The global
  `auto_merge_enabled` setting defaults to `True` — merging green PRs is the
  mill's job, no toggle required. Set `auto_merge_enabled: false` to opt a
  specific repo out.
- Branch-protection rejections during auto-merge now transition the ticket to
  `blocked` with the forge error message recorded in history, instead of
  silently parking in `human_mr_approval`.
- Classify transient CI failures (ECONNRESET, buildkit boot timeouts, setup-uv fetch errors, runner shutdowns, etc.) before spawning a blocking `ci_fix_dependency` ticket. Transient failures now trigger automatic workflow re-runs (up to `ci_transient_max_retries`, default 3) instead of immediately spawning a fix ticket.
- `resume-blocked` for CI-failed tickets now refreshes the PR branch (rebase + empty-commit) **before** evaluating CI, so a transient flake that has since resolved un-sticks in one resume instead of re-reading the same stale failing run forever.
- fix(implement): advance to DELIVERABLE when the branch has committed-ahead work and the agent produces no new file edits (non-zero tool calls but zero edit calls), instead of looping until the spawn limit trips
- Added `tail` query parameter to `GET /tickets/{id}/history`, returning the last N events in chronological order without requiring the caller to know the total event count.
- `read_ticket` tool now fetches only the most recent 50 history events (``order="desc"``, ``limit=50``) so the final event is always visible even for tickets with very large histories. The chat skill doc now documents the history endpoint's ``limit``, ``offset``, and ``order`` query params.
- Emit a SPAWN_LIMIT_EXHAUSTED diagnostic event when the implement spawn
  counter hits its limit, with the last attempt's summary tail included in
  the event reason. Agents (including the periodic diagnostic agent) can
  now discover spawn-limit exhaustion programmatically via the shared
  diagnostic event store.
- Add reverse-check invariant (#4) to `scripts/check_config_docs_sync.py`: `_check_stale_no_doc_exceptions` flags every `_MODEL_FIELDS_NOT_IN_DOCS` entry whose env var now resolves in `docs/config/configuration.md`, so a field that gains documentation without removing its exception causes the script to exit non-zero.
- Added "Config & docs conventions" section to `AGENT.md`: when `config.example.json` intentionally diverges from a Settings model default, document the override in `docs/config/configuration.md` using the established `sandbox.image` pattern.
- Refine: short-circuit to human approval when the triage classifier fails instead of falling through to the expensive opus refine agent. Saves ~$1.84 per ticket when the cheap triage model is unavailable.
- Added `mypy_baseline` periodic agent: monitors `mypy-baseline.txt` and `mypy-baseline-test.txt` entry counts, detects growth across file boundaries, and files draft tickets categorized by error code (`[type-arg]`, `[no-untyped-def]`, etc.) — closes the gap where pre-commit (staged-files-only) and advisory CI let cross-file mypy regressions silently grow the baseline.
- Auto-answer: replying to an open `[ASK_USER]` thread on an `awaiting_user_reply` ticket now automatically closes the thread and resumes the ticket — no separate close-thread step required.
- Add `exclude-newer = "7 days"` to `[tool.uv]` in `pyproject.toml` — newly-published package versions (< 7 days old) are excluded from `uv lock --upgrade` resolution, providing a cooldown window for the community to detect malicious releases before they propagate via automated Renovate bumps. (mill: Add `exclude-newer` dependency cooldown to `[tool.uv]` for supply-chain defense-in-depth (20260729T174433Z-add-exclude-newer-dependency-cooldown-to-abc0))
- Auto-heal stuck auto-merge tickets whose PR is merely out-of-date with the target branch: mill now calls the forge's update-branch API (server-side) and retries auto-merge after CI re-runs, instead of bouncing to `human_mr_approval`. Genuine failures (conflict, red CI) still bounce as before.
- Cleaned up ~70 stale entries from `_MODEL_FIELDS_NOT_IN_DOCS` in `scripts/check_config_docs_sync.py`: removed fields already documented in `docs/config/configuration.md`, moved `deliver_max_identical_blocks` and `refine_web_fetch_*` to the config-file-only group, and removed two nonexistent fields (`trace_review_max_inspector_runs_per_pass`, `sandbox_push_token`). Added `review_diff_max_chars` to the default-mismatch exceptions (doc uses `200_000` for readability; model stores `200000`).
- Document the intentional `refine_trivial_model_level` override in `config.example.json` (`3` for flat-cost Claude subscription vs. code default `2` for pay-per-token DeepSeek Pro) in `docs/config/configuration.md`.
- Fix false "already satisfied" closure on resumed tickets: when the ticket branch carries ahead-of-target WIP commits (resume-preserve) but the work has not been merged into origin/main, the implement stage now proceeds to CODE_REVIEW → deliver instead of short-circuiting to DONE. This prevents stranding unmerged work and the gap from regrowing.
- `explore` sub-agent: stop retrying on non-transient errors (e.g. account-out-of-credits 402) instead of burning 3 retry attempts against a model that will never succeed.
- Wire `credit_balance` pass into `_PASS_REGISTRY` so it is triggerable
  via the board UI and API, not only on its hourly poll schedule.
- Refine stage: add output-length guidance to system prompt (target 2500–4000 words, prefer bullets, aim for ~4000 tokens). Lower `refine_trivial_model_level` default from 3 (Claude sonnet) to 2 (DeepSeek Pro) so straightforward gap-fill tickets use a cheaper model, saving ~$0.90 per trivial refine.
- **Stale-spec fingerprint guard now durably suppressed after `resume-blocked`.**  When an operator resumes a blocked ticket with a justification note, the guard's cleared state persists across subsequent spawns for the same spec fingerprint — the ticket only re-blocks when the spec actually changes.  Previously the guard re-triggered on the next implement attempt even after a manual resume, forcing repeated operator intervention.
- Wire per-repo `auto_merge_enabled` from `repos.yaml`/`repos.json` through `load_repos_config()` to `RepoConfig`. Previously the field was declared and documented but never passed from the config data, so per-repo opt-in was silently ignored.
- Document `MILL_DIAGNOSTIC_EVENTS_PATH` env var in the periodic-agents config table (`docs/config/configuration.md`).
- Document `gates.delta_context_retry_enabled` (`MILL_DELTA_CONTEXT_RETRY_ENABLED`, default `true`) in the configuration reference under section 11.3 Refine routing.
- config-sync: `max_refine_passes_per_ticket` — already documented in section 11.2 (Stages tuning) of `docs/config/configuration.md`; no docs change needed.
- Document `max_refine_passes_per_ticket` (`MILL_MAX_REFINE_PASSES_PER_TICKET`, default 3) in the configuration reference under "Stages tuning"
- Tighten implement agent tool-use discipline: forbid `run_command` for trivial
  file-content lookups (`grep`, `cat`, `ls`, `head`, `tail`, `wc` on known paths)
  and direct the model to use `read_file` or `explore` instead.
- Add documentation row for `implement_max_spawns_per_ticket` in `docs/config/configuration.md`.
- Document `implement_stall_threshold` in the configuration reference table (Section 3).
- mill: ci_fix: narrow success criterion to dispatched check(s) only — agent now reports DONE when its assigned check(s) pass even if unrelated pre-existing failures remain red, and compares new CI_FAILING summaries against the original dispatch summary to determine scope
- **Fix:** spawn counter increment moved to after all preflight guards pass, so blocks from late guards (stale respawn, stall detector, cycle cap, tool/skill/workspace integrity) no longer burn a spawn slot.  Previously the counter was incremented early in preflight and later checks could block the spawn without any LLM work, exhausting the 3/3 budget with near-zero spend and no implement-cycle traces.
- Added deterministic fast-path for trivial config-only changes (new presence/config files ≤40 lines). Bypasses the LLM coordinator entirely for tickets that only add fresh `.yaml`/`.toml`/`.md`/etc. files — handled via `_handle_trivial_config_change` mirroring the rename-only pattern.
- Eliminate duplicate `_CODQL_CHECK_NAMES` definition in `ci_fix_codeql.py`; import from canonical definition in `ci_fix_helpers` instead.
- Add `POST /tickets/{id}/answer` endpoint so the chat agent can deliver an operator-supplied answer to a ticket in `awaiting_user_reply`. Posts the answer to the open `[ASK_USER]` thread, closes it, and auto-resumes the ticket back to its originating state.
- Add missing `.robotsix-mill/periodic/roadmap_sync.yaml` presence file so the periodic scheduler discovers and runs the `roadmap_sync` workflow.
- Fix vulture "unused variable 'frame'" warning in `src/robotsix_mill/runtime/tracing.py` by prefixing the unused signal handler parameter with `_`.
- **config**: Moved `deploy_api_url` to its correct alphabetical position in `config.example.json` and removed it from the config-sync exception set in `scripts/check_config_sync.py`, permanently enforcing its presence (`deploy_api_url` is no longer an optional/exception field).
- Add `deploy_api_url` to `config/config.example.json` and document it in `docs/config/configuration.md` (section 6 Service).
- Wire `roadmap_sync` as a fully scheduled periodic pass: add `roadmap_sync_periodic`/`roadmap_sync_interval_seconds` settings fields, register it in `_BUILTIN_KINDS` as `schedule_only`, and add the runner to `_SCHEDULE_ONLY_RUNNERS` so the periodic supervisor can schedule it automatically.
- `_revert_standard_configs` no longer auto-reverts standard config files
  (`.pre-commit-config.yaml`, `docker-compose.yml`, `mkdocs.yml`) when the
  ticket's `file_map` declares them as relevant scope; they now pass through
  to the scope-triage LLM for normal evaluation.
- Remove stale agent references (`maintenance.yaml`, `periodic/cost_analyst.yaml`) from AGENT.md example lists; both files were deleted in earlier commits.
- Enabled expanded Ruff lint rules (SIM, C4, LOG, G, ERA, PGH, RUF, PT) in
  ``pyproject.toml``. Applied ~540 auto-fixes across 223 files (``ruff --fix``
  + ``--unsafe-fixes``) for safe transformations like nested-with flattening,
  contextlib.suppress, collapsible-ifs, needless-bool simplification,
  collection-literal conversions, and Yoda-condition fixes. Grandfathered
  remaining pre-existing violations via per-file-ignores: RUF001-003 and
  ERA001 blanket-suppressed for ``src/**`` and ``scripts/**``, RUF012
  per-file for 6 source modules, PT/SIM/RUF059/RUF012/ERA/E402/PGH004/B017/
  B018 noise blanket-suppressed for ``tests/**``, SIM103 for ``dev/**``.
  Recovered ~80 bandit/other violations exposed when RUF100 stripped
  inline ``# noqa:`` comments during the unsafe-fix pass — added per-file
  ignores for those too. All gates pass: ruff check, ruff format, mypy
  (no new errors), deptry, vulture.
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
- Extended `scripts/check_config_sync.py` with invariant 4: validates code-comment `Default N` values against the actual `Field(default=N)` in the Settings model. Scans `_settings_periodic.py` and `_settings_core.py` for comment-stated numeric defaults, resolves the associated field name, and reports mismatches. Fixed the stale `roadmap_sync_interval_seconds` comment (said 86400/daily, actual 604800).
- Fix zero-edit implement loop persisting after #2552: the resume-with-ahead-branch
  path in `_detect_no_change_contradiction` was gated on `_any_repo_has_changes`
  returning False, which bundles both working-tree cleanliness AND branch-not-ahead.
  On a resume the branch IS ahead (prior commits from earlier passes), so the
  guard never fired and the ticket fell through to CODE_REVIEW → re-implement →
  spawn-limit BLOCKED.  Add a new exit path that detects "resuming + clean working
  tree + branch ahead" and routes directly to DONE (already satisfied).
- Added 12 unit tests for the GitLab pagination helper `_paginated_get` (single/multi-page, empty, HTTP errors, item transforms, parameter forwarding).
- Implement stage scope guardrail: auto-revert standard config files
  (``.pre-commit-config.yaml``, ``docker-compose.yml``, ``mkdocs.yml``)
  when they appear out-of-scope after an agent pass.  These repo
  scaffolding files are occasionally regenerated by the agent (driven
  by AGENT.md conventions) even when the ticket scope is narrow — the
  guardrail now reverts them to ``origin/<target>`` instead of blocking
  or consuming LLM tokens on scope-triage.
- Decompose the 598-line `ReviewStage.run` method into an orchestrator (~95 lines) and six private helpers: `_resolve_diff_and_metadata`, `_clone_cross_repo_workflows`, `_resolve_review_level`, `_validate_action_shas`, `_handle_review_verdict`, and `_handle_request_changes`. Each helper encapsulates a distinct responsibility (diff computation, cross-repo cloning, model-level routing, action-ref validation, verdict routing, and REQUEST_CHANGES processing). Behaviour is unchanged — all 57 review-stage tests pass.
- Implement stage: zero-edit runs with all gates green now route to
  DONE (already satisfied) instead of re-spawning and eventually
  hitting the spawn-limit BLOCKED.  Two paths fixed: resuming with
  empty diff in `_detect_no_change_contradiction`, and resuming with
  zero tool calls in `_verify_repo_changes`.
- Pin `pymdown-extensions>=11.0.0` in the docs dependency group to resolve GHSA-9xwg-3r6f-jcx2 (path traversal in the b64 extension, fixed in 11.0.0).
- Extract ``preflight`` gate checks from ``PhaseCoordinatorMixin`` to a new
  ``phase_coordinator_preflight.py`` module (reduces ``phase_coordinator.py``
  from 1414 → 1123 lines).
- **fix:** newly created `ci_fix_dependency` tickets are now transitioned to `READY` immediately so the worker's poll loop picks them up, preventing the silent deadlock where a draft fix ticket was created but never enqueued.
- Add CI gate (`check_sourcekind_frontend_parity.py`) to prevent Python `SourceKind` / JS `SOURCE_CLASS` / CSS `.src-*` drift. Fix existing drift: add missing `user`, `repo_description_sync`, and `config_standard` JS entries; remove duplicate `docstring_coverage` key.
- Add `pytest-randomly>=4.1.0` to dev dependencies for randomized test-order detection
- Implement the config-ownership standard component config surface: `GET /config`, `PUT /config` (with validation and secret rejection), `GET /config/versions`, and `POST /config/rollback`. Secrets are always masked as `**********` on read and rejected on write — they remain env-injected by the deploy plane. A Settings panel in the board UI (⚙ Settings) lets operators view and edit component-owned config fields at runtime without a redeploy, with a version-history tab for rollback.
- Bump `pypdf` minimum constraint from `>=5` to `>=6.14.2` to pick up fixes for CVE-2026-59935, CVE-2026-59936, CVE-2026-59937, and CVE-2026-59938.
- Deliverable-stage config-standard footprint check now only flags files that the ticket branch actually touched (added, modified, or deleted). Pre-existing fleet-standard files like `.pre-commit-config.yaml` and `docker-compose.yml` no longer block delivery when they are not part of the branch's change set.
- Add missing `module_size` entry to `SOURCE_CLASS` map in `board-mill.js` and corresponding `.src-module_size` CSS rule in `board-mill.css`, so tickets with `SourceKind.MODULE_SIZE` render with the correct source badge instead of the fallback `.src-user` badge.
- Fix four stale Langfuse credential descriptions in `docs/config/configuration.md`: the Secrets reference table (5 rows), footnote ¹, `deployed_log_folder` prose, **and the Repos registry intro paragraph** all described the data flow backward. Now correctly describe the global secrets block → `Secrets` → `_apply_global_langfuse` → `RepoConfig` path, and that all repos share the same global Langfuse configuration.
- Add `docs/stages/implement.md` — a narrative design document covering the implement-stage lifecycle, fix iteration loop, resume path, submodule breakdown, config knobs, scope-triage sub-gate, and cross-spawn stall guard.
- Config-standard footprint validation at deliverable stage now only inspects files the ticket's branch actually changed (three-dot diff), ignoring pre-existing repo files that the ticket never touched. Previously, files like `.pre-commit-config.yaml` that exist in the repo but were not part of the ticket's diff would block deliverable.
- Document `sandbox_push_token` in the Secrets reference table (`docs/config/configuration.md`).
- `commit_all()` is now no-op-safe: after staging with `git add -A`, it checks `git status --porcelain` and returns silently if nothing is staged, avoiding a `CalledProcessError` from `git commit` on a clean tree (e.g. review-fix passes whose net diff is a pure deletion). The implement stage's `_finalize` also wraps `commit_all()` calls with stderr capture so that git failure diagnostics appear in ticket history instead of just the command line.
- Periodic agents (survey, audit, health, etc.) now catch the Claude SDK degenerate "error result: success" exception and degrade gracefully to a no-op pass with preserved memory, matching the refine agent's existing guard.
- Config-standard 4-file footprint enforcement: CI gate rejects PRs
  adding files outside the canonical footprint, deploy-time validation
  blocks out-of-footprint files before push, and the refine stage
  enumerates the approved footprint for config-standard tickets.
- Bring sandbox git-push bridge credential under the managed Secrets surface
  with a dedicated ``sandbox_push_token`` field, a push-access health probe
  in ``/health/ready``, and distinct ``PUSH_AUTH_ERROR`` classification so
  operators can distinguish a credential blind spot from a code defect.
- Fix malformed base URL in lifecycle wrapper: `deploy_api_url` now validates the `https?://` scheme at config time, and `check_deploy_freshness()` normalizes bare hostnames by prepending `https://` when a scheme is missing, so httpx never sees a URL it can't parse.
- Rewrite `docs/langfuse/observability.md` to describe the current global `secrets:` block mechanism instead of the removed per-repo `langfuse:` blocks. Removed all references to `langfuse_from` inheritance.
- Remove dead re-exports ``_status`` and ``_is_openrouter_upstream_error`` from ``agents.retry`` — neither symbol had any callers.
- Add dedicated unit tests for ``_paginated_get`` (10 tests covering single/multi-page, 401 retry, exception fallback, boundary cases, URL/params forwarding).
- Implement agent now has a robotsix-standards-specific README TOC rule
  that explicitly covers the three prose tables (Every repository,
  Deployable components, Deployment system), closing the remaining gap
  where new standards pages updated mkdocs.yml and docs/index.md but
  missed README.md.
- Extract `_persist_artifacts_and_run_guardrail` helper from duplicated block in `_handle_rename_only_change` and `_handle_spec_exact_edits` (copy-paste cleanup).
- Add deterministic programmatic gates (meta runner + refine stage) that reject tickets proposing to enable internal (non-portable) periodic workflows on managed repos, using the data-driven portability map in `workflow_portability.py` — stops `state_sync` and other mill-only workflows from being proposed for non-mill repos before implement is ever reached.
- Mill now handles empty GitHub repos gracefully: when cloning a freshly-created repo with no commits, `git_ops.clone()` auto-seeds an initial commit (author: robotsix-mill bot, message: "Initial bootstrap commit") instead of failing. This eliminates the long-standing papercut where every new repo registration required a manual first commit before the mill could process tickets against it.
- Install tini as the container PID 1 via the Docker image ENTRYPOINT, so orphaned subprocess grandchildren (e.g. ``git`` spawned by the transient ``claude`` CLI that exits before waiting on its children) are reaped instead of accumulating as zombies. Observed as 4067 defunct ``git`` processes after ~21h uptime when running under central-deploy (whose v1 contract does not support ``init: true``).
- Annotate advanced/expert-only config settings with `"advanced": true` in the JSON schema so the deploy UI can hide them behind an "Show advanced settings" toggle. Common operator-facing settings (URLs, feature toggles, secrets, paths) remain visible.
- Add workflow portability classification (`is_portable`, `render_workflow_portability`) to the periodic loader, derived from the existing `_BUILTIN_KINDS` kind map. Refine and meta agents now receive a data-driven **Workflow Portability** table and gate internal-workflow enablement proposals (e.g. `state_sync`, `frontend_sync`) instead of hardcoding individual workflow names.
- Add `vulture` (dead-code detection) to the implement stage's mandatory pre-flight checks, alongside ruff, mypy, and deptry, so lint failures are caught locally before the PR/CI round-trip.
- Emit `CI_FAILURE` diagnostic events on every ticket entering `fixing_ci`, with a stable normalized failure key so recurring failure modes cluster. Add `RecurringCIFailureCheck` diagnostic check that auto-files fix-proposal draft tickets when a failure key has been hit by enough distinct tickets (threshold configurable via `diagnostic_ci_failure_threshold`, default 3).
- Remove outdated per-repo `langfuse:` blocks from `docs/config/configuration.md` — Langfuse credentials are configured globally in the `secrets:` block, not per-repo.
- Extract special-case edit handlers from `implementation_logic.py` into new `implementation_editing.py` module — `_verify_repo_changes`, `_handle_rename_only_change`, `_handle_spec_exact_edits`, and `_find_insertion_point` now live in `_ImplementationEditingMixin` (~565 lines moved).
- Extract scope-guardrail + preflight tests (~1238 lines) from
  `tests/stages/implement/test_implement.py` into new
  `tests/stages/implement/test_implement_preflight.py` (module-size split)
- Enable `module_size` periodic agent by adding its per-repo presence file (`.robotsix-mill/periodic/module_size.yaml`).
- Fix Alembic proxy-registry race condition: add global ``_alembic_lock`` to serialize all ``_run_alembic_migrations`` calls across boards, preventing ``KeyError: 'config'`` when concurrent ``init_db`` calls collide on Alembic's process-global proxy registry (observed as CI flake in ``test_generate_children_applies_epic_body`` under xdist).
- Replace cached `github_token()` with on-demand `github_push_token()` in deliver and periodic runner push paths (changelog autofill, roadmap sync, pin bump), following the same per-push App-token renewal pattern already used by ci_fix/rebase. Eliminates stale-token push failures when a cached App installation token expires mid-flight.
- Implement agent now checks and updates `README.md` TOC tables when
  creating, renaming, or removing documentation pages under `docs/`,
  preventing the recurring TOC-drift defect seen in robotsix-standards.
- Implement agent: add investigation-only ticket detection. When a ticket spec asks the implement agent to investigate a failure on a different board/repo, the agent now produces a diagnosis via `no_change_needed` (written to `implement.md`) instead of making spurious code edits, since the implement stage has no cross-repo trace/log access.
- Add `hypothesis` as a dev dependency and introduce property-based
  tests for pure-function invariants: `parse_duration`/`format_duration`
  round-trip, `normalize` idempotency, `_slug` invariants, and
  `_stamp_frontmatter`/`_parse_frontmatter` body-lossless round-trip.
- Fix ``_validate_changelog_fragments``: import validation logic in-process
  from ``_changelog_validate`` instead of shelling out to a script
  resolved via ``parents[3]`` (which pointed at ``src/``, not the repo
  root, so the validation never ran in production).
- Add docstring to `CaseTolerantEnum.process_bind_param` in `src/robotsix_mill/core/models.py`.
- Add docstring to `CaseTolerantEnum.process_result_value` in `src/robotsix_mill/core/models.py`.
- Add ``scripts/validate-changelog.py`` — pre-commit changelog fragment validator that ensures trailing newlines and ``docs/modules.yaml`` registration, called automatically by the implement stage's ``_finalize`` before committing. Also fixes a missing trailing newline in ``maybe_generate_towncrier_fragment``.
- `github_push_token()` now requests `workflows:write` alongside
  `contents:write` when minting a GitHub App installation access
  token, fixing push failures for pushes that touch
  `.github/workflows/` files.
- Add docstring to `RunEntry` dataclass in `run_registry.py` documenting all seven fields and lifecycle semantics.
- `doc_classifier` system prompt refined with explicit user-facing criteria (new public API, config field changes, exception contracts, CLI changes) and standardized classification format with examples
- Implement stage: clear cached summary and reference files on resume-blocked to prevent the agent from being fed its own prior output as context, which caused byte-identical replay across cycles.  Preserve stall-detection state in `implement_stall_state.json` so the cross-spawn stall guard survives operator-initiated resume/reset cycles.  The preflight stall guard now falls back to this JSON file when `implement.md` is absent.
- `review_revision.py`: migrate from unscoped `github_token()` to `github_push_token()` (scoped `contents:write`) for force-push and pre-push reconcile fetch, matching the ci_fix/rebase push paths (PR #2483).
- Expand sandbox-path guard in doc and review agent prompts: the old
  prompt only warned against reading from outside paths (`/tmp/`, etc.);
  now also warns against writing — `write_file` and `edit_file` will
  reject them too. Prevents recurring ~$0.002-per-occurrence tool-call
  waste from agents attempting to write to unreachable paths.
- git push operations (ci_fix, rebase) now use `github_push_token()`, which mints a fresh, least-privilege GitHub App installation token scoped to `contents: write` on the target repository — eliminating the dependency on long-lived, expiring PATs for push authentication.
- Fix: implement stage now clears stale conversation state on spawn-limit BLOCK and resume_blocked, so corrective operator feedback is loaded into a fresh agent conversation instead of being drowned out by a replayed prior transcript.
- Add cross-spawn stall guard to implement stage: detects when the agent's output is byte-identical across consecutive BLOCKED cycles, stops consuming spawn rounds before the limit is exhausted, and blocks with an actionable diagnostic that surfaces unaddressed review comment IDs.  Controlled by new ``implement_stall_threshold`` setting (default 2).
- Fingerprint guard now respects operator force-retry: `resume-blocked` with a justification note (or any BLOCKED→READY transition with a note) clears the stale-spec guard for exactly one implement cycle, instead of silently re-blocking on an unchanged fingerprint.
  The automatic-refusal diagnostic now names `resume-blocked` as a remedy alongside the existing spec-update and reset-fingerprint options.
- Register the `module_size` periodic agent in all three registration points: ``_BUILTIN_KINDS`` (periodic loader), ``_PASS_REGISTRY`` (passes API), and CLI ``_RUNNERS`` + ``add_parser`` (``robotsix-mill module-size`` subcommand).
- Add `.shellcheckrc` with Bash dialect and external-sources settings for consistent shellcheck behavior across scripts.
- Add shellcheck pre-commit hook (`shellcheck-py`, severity=warning) to lint shell scripts at commit time.
- Add `lint-sh` Makefile target that runs `shellcheck` on all shell scripts, and chain it into the `lint` target.
- Add `module_size` to the periodic-agent lists in `docs/agents/agent-yaml-schema.md` (category and read_ticket fields).
- Stale `CHANGES_REQUESTED` forge reviews are now actively dismissed instead of only being silently discarded. Added `dismiss_review` to the Forge interface (`base.py`, `github_pr.py`, `gitlab/core.py`) and extended `_pr_review_status` to return `review_id`. The core fix in `_review_changes_requested_outcome` detects stale reviews regardless of `review_feedback_enabled`, preventing approved MRs from bouncing back to `human_mr_approval` on a stale review artifact.
- Remove the review-artifact requirement from the auto-merge eligibility gate.
  The upstream `human_mr_approval` operator gate is the authoritative review
  decision point; the redundant downstream artifact check in
  `_auto_merge_eligible()` has been removed.  Approved, CI-green tickets in
  `waiting_auto_merge` now merge to `done` without bouncing back to
  `human_mr_approval`.
- Add `alembic check` drift gate in CI (`mill-specific` job) to catch un-generated migrations when models change.  Also add `make check-migrations` target for local use.
- Implement stage: edit-claim contradiction guard now retries within the pass
  (with diagnostic feedback) instead of immediately BLOCKING on fresh runs,
  preventing the BLOCKED→READY→BLOCKED loop across stage-level retries. The
  guard also produces a detailed root-cause diagnostic (missing args, lost
  edits, un-replayable tool kind) via the new ``_build_edit_claim_diagnostic``
  helper.
- Remove dead no-op function ``_validate_cross_repo_forge_compat`` from
  ``config/repos.py`` and its lone call site. Both GitHub and GitLab adapters
  already support cross-fork MRs.
- Add module-level docstrings to ``cli/ticket.py`` and ``cli/serve.py``.
- Relocate implement-stage test files into `tests/stages/implement/` subdirectory.
- Refactor `_evaluate_test_results` and `_run_scope_guardrail`: extract six cohesive helpers (`_run_smoke_gate`, `_detect_no_change_contradiction`, `_verify_repo_changes`, `_clean_binary_artifacts`, `_filter_vendored_deps`, `_run_scope_triage_classification`) to reduce complexity hotspots (~60% line-count reduction in both functions).
- Review stage: ``_verify_action_sha()`` now accepts an optional ``token`` parameter and uses ``git_ops._authed_url()`` to authenticate ``git ls-remote`` calls against private repos — previously unauthenticated calls produced false-positive "SHA not found" errors for private repos like ``damien-robotsix/robotsix-github-workflows``.  Added ``_reusable_workflow_sha_refs_from_diff()`` to extract SHA refs from reusable-workflow ``uses:`` lines (previously skipped by ``_action_refs_from_diff()``).  ``ReviewStage.run()`` fetches the GitHub token and passes it to both the action-ref and reusable-workflow-ref validation loops.
- Resolve `skills_dir` / `language_instructions_dir` robustly: skill
  injection, the language-snippet loader, and the implement preflight now
  fall back to the packaged resource directories (with a one-time warning)
  when the configured directory doesn't exist, instead of hard-blocking
  every ticket with "missing skill file". Also drop the CWD-relative
  `skills_dir`/`language_instructions_dir` entries from
  `config.example.json` — they resolve against /app in the container and
  were the source of the 2026-07-19 board-wide preflight blocks (the fix
  ticket itself could not run: circular dependency).
- Fix `Worker.stop()` to handle `ExceptionGroup` wrapping `CancelledError`
  in Python ≥3.11 by using `except*` instead of bare `except`.
- Fix pipeline-wide agent-run crash: bump `robotsix-llmio` pin past the
  sync-wrapper fix (sync `call_with_retry`/`run_agent` invoked the caller's
  `run_sync`-style fn inside `asyncio.run()`, breaking every draft-refine and
  triage call), and add a running-loop guard to mill's `run_agent` mirroring
  #2451: when called with an event loop running (e.g. on the Claude SDK's
  loop), the retry session is delegated to a thread so `run_sync` can create
  its own loop.
- Fix redraft/re-block loop for tickets with existing all-green branches: implement stage now detects when a remote branch has green CI but no open PR and routes to IMPLEMENT_COMPLETE so the deliver stage re-opens the PR instead of re-running the implement loop. Also add a guard ensuring every BLOCKED transition records a reason in the history event.
- Add deploy-freshness gate to prevent wasted implement attempts on stale worker images. The implement preflight and resume-blocked paths now check ``GET /services/mill`` (when ``deploy_api_url`` is configured) and park tickets with an explicit "awaiting redeploy" note when the running image predates the latest digest.
- Add `state_sync` to the periodic-agent lists in `docs/agents/agent-yaml-schema.md` (category field reference and `read_ticket` field reference).
- Update `docs/agents/agent-yaml-schema.md`: replace stale `board` skill references with the three actual skills (`ask_user_guardrails`, `board-read`, `board-report`) and reflect `refine.yaml`'s real `skills` list
- Implement stage now bootstraps empty remote repos (no commits, no branches) with an initial README commit instead of blocking the ticket. Ports the cd2c pattern from the periodic meta agent's `clone_all_repos` path.
- Correct stale `modules: true` opt-in claim in `AGENT.md`: `refine.yaml` has opted in, `meta.yaml` explicitly sets `modules: false`.
- Remove dead `.src-security-posture` CSS rule from board-mill.css (no matching SourceKind enum member exists)
- `human_mr_approval`: discard stale `REQUEST_CHANGES` reviews when the PR head has changed since the review was cast (compare `review.commit_id` against `pr.head.sha`). Prevents the verified 7-cycle verdict-replay loop that dominated tickets where the diff issue was externally remediated.
- Fix `language_instructions_dir` default to resolve via `importlib.resources` instead of a bare relative `Path`, so the built-in language snippets are found in installed (container) mode. Add a preflight check that hard-blocks when the directory is absent, catching container-only path-resolution gaps before a model pass opens.
- Fix: meta-ticket workspace setup crashes on freshly-created empty repos — `build_meta_workspace` now detects empty remotes and bootstraps them with an initial commit, matching the existing `clone_all_repos` behaviour.
- Implement stage preflight now hard-blocks (instead of silently degrading) when the agent definition has no tools, a referenced skill file is missing, or the workspace directory is inaccessible. Each failure includes the specific path/condition in the error note, preventing the zero-tool-call no-op loop seen on non-mill boards.
- Bump `robotsix-llmio` git pin past 2026-07-16 to pick up
  optional-`RunContext` fix (`_tool_converter` now accepts tools
  with `ctx: RunContext[None] = None`, resolving a Claude SDK
  `takes_ctx=True` block on `read_file` and similar tools).
- Clear stale review artifact and stage-outcome cache on successful rebase so the review gate re-evaluates the current diff instead of replaying a cached REQUEST_CHANGES verdict. Also invalidate the review cache when the auto-merge eligibility gate detects a stale (head-SHA-mismatched) verdict.
- Fix zero-edit implement loop persisting after prior fix: the `reprompt_if_unstructed` guard now checks for zero tool calls BEFORE the `isinstance(expected_type)` short-circuit, so structured `ImplementResult` envelopes with no tool calls trigger a re-prompt (unless `no_change_needed=True`). Also, the per-pass stuck-loop detection in `_implement_loop` now computes `progress` regardless of `has_diff`, so leftover changes from a prior session cannot mask a current pass that contributed zero tool calls.
- Survey periodic pass now classifies findings as repo-specific or fleet-wide convention candidates, and can file companion tickets on the ``robotsix-standards`` board for generalizable conventions. The runner supports cross-board ticket creation via the new ``draft_target_repo_ids`` field on ``PeriodicAgentResult``, with creation-time dedup on the target board to prevent duplicate standards proposals across repos.
- Add zero-tool-call guard in implement stage: a pass where the agent issues no tool calls and produces no diff now surfaces a distinct BLOCKED error immediately in both the retry loop and the resume→CODE_REVIEW path, rather than masquerading as a generic no-edit stall. (mill: Implement agent spins with zero tool calls / zero edits on robotsix-auto-mail workspace (20260718T152204Z-implement-agent-spins-with-zero-tool-cal-1620))
- Resolve six trivial `# type: ignore` suppressions with proper type annotations, shrinking the mypy baseline by 4 entries (708→704).
- `state_sync` is now a mill-internal periodic agent (kind `mill_only`) — it no longer appears as an opt-in presence-file pass for managed repos. The agent continues to run against robotsix-mill on its existing schedule via the periodic supervisor's mill-repo guard.
- Add `verify_diff` tool to the implement agent: replaces 3-5 `run_command` grep/awk verification calls per `edit_file` with a single `git diff --stat` call plus optional expected-file cross-check. Registered in `ToolRegistry` category `git` and steered by a new "Batch verification with `verify_diff`" prompt section.
- Add module-level docstrings to `runtime/worker/processing.py` and `runtime/worker/epic.py`, matching the style of the other worker submodules.
- `resume-blocked` now only resets the implement spawn counter when the ticket was actually blocked at the spawn limit (counter ≥ `implement_max_spawns_per_ticket`), and records the reset as a history event ("spawn counter reset via resume-blocked"). Tickets blocked from READY for other reasons keep their counter intact.
- Add tiered test-run policy to implement agent prompt: targeted tests first, broader related tests second, never escalate to full suite (pipeline job).
- Add batching discipline rule to implement agent prompt: batch `git grep` / `run_command` questions into a single `explore` or `parallel_explore` call to reduce round-trips and wall-clock cost.
- Add `changelog_autofill_periodic` and `changelog_autofill_interval_seconds` settings fields, giving the changelog-autofill schedule-only pass a configurable kill-switch and interval (previously hardcoded to 86400 s with no disable option).
- GitLab forge: implement cross-project merge request support via `target_project_id` when `head_repo` is provided, matching the GitHub adapter's cross-fork PR workflow. Remove the `NotImplementedError` stub and the `_validate_cross_repo_forge_compat` guard that rejected `cross_repo_target` for GitLab.
- Document stage: deterministic short-circuit for doc-only diffs (all paths are `.md` or under `docs/`), skipping the classifier + doc agent and saving $0.005–0.01 per occurrence.
- Add class-level docstring to `PeriodicPassesMixin` describing its per-repo periodic pass orchestration.
- Added docstring to ``health_ready`` endpoint in ``_health.py`` documenting the readiness probe's Args, Returns shape, and 503-on-failure behaviour.
- Add docstring to `WorkerPool.start()` method in `src/robotsix_mill/runtime/worker/core.py`.
- Merge gate: stale review verdicts no longer block auto-merge after a rebase. The review artifact now records the branch head SHA; when the current PR head differs the stale verdict is ignored. Prevents the merge gate from re-posting byte-identical REQUEST_CHANGES verdicts that no longer apply to the rebased branch.
- Document `sandbox.image` dev-vs-prod dual default: the Pydantic model default is `python:3.14-slim` for lightweight local development, while the production JSON config overrides to `robotsix/mill-sandbox:latest` (includes `uv` and toolchain). Added inline docstring comment and updated config docs table to match.
- Fix orphan `agent run` Langfuse traces: propagate OTel/contextvars across
  `ThreadPoolExecutor` boundaries in watchdog/timeout helpers so pydantic-ai
  agent spans nest under the stage-named root trace instead of creating
  unattributed root spans.
- Update `docs/agents/agent-yaml-schema.md` to match the current `AgentDefinition` model: replace `model` with `level`, replace `web` with `web_knowledge`, add missing field docs (`list_epic_children`, `list_threads`, `ask_user`, `inject_agent_md`, `inject_language_conventions`, `max_tokens`), update category listings and tools table, fix `read_ticket` section.
- Fix `coordinator_timeout_seconds` model default drift: changed from 900 to 600 in `_settings_core.py` to match `config/config.example.json` and documentation.
- Fixed typo `rebasin` → `rebasing` in the valid State values list in the chat-skill endpoint docstring.
- Save conversation state on `AgentBudgetError` (budget exhaustion) so
  the implement agent can resume from where it left off instead of
  restarting from scratch. The BLOCKED→READY resume path now loads
  saved conversation state alongside `previous_attempt_summary`.
- Auto-generate board passes dropdown from the pass registry; remove hand-wired routes and buttons for trace_health, langfuse_cleanup, meta, and run_health — all passes now trigger via the generic ``POST /passes/{pass_id}/run`` endpoint.  Passes are grouped by kind (LLM Agents, Runners, Global) in the dropdown.
- **Board UI**: replaced hand-wired "Agents" dropdown with a dynamically-populated "Passes" dropdown driven by the periodic pass registry (`GET /passes` + `POST /passes/{pass_id}/run`). Passes are grouped by kind (LLM Agents / Runners). Adding a new pass to `_PASS_REGISTRY` is now the only wiring needed to make it manually triggerable from the board.
- Sync `STATE_TRACE` in `board-mill.js` with the canonical `STAGE_FOR_STATE` mapping from `states.py`: corrected `ready`→`"implement"`, `implement_complete`→`"merge"`, `rebasing`→`"merge"`, `done`→`"retrospect"`; added missing `draft: "refine"`; removed terminal `closed` (no stage).
- Extract shared standards-awareness prompt block into `agent_definitions/_shared/standards-awareness.yaml` and add `!include` resolution to `yaml_loader.py`. Survey and audit agents now consume the canonical block via `!include` instead of maintaining separate copies.
- Route small mechanical refactors (≤40 lines, no new files) to level-1 review model, reducing review cost for fully-prescribed extraction/move tickets by ~10×.
- Remove deprecated `env_doc_sync` periodic agent (agent definition, implementation module, route, CLI, board UI, config settings, and all test coverage). Env-var documentation consistency is now governed by robotsix-standards policy with audit enforcement.
- Remove the `security_posture` periodic agent entirely: delete the agent definition, source module, tests, runner config, CLI entry, HTTP route, board UI button, settings fields, and all code/docs references. Security posture is being codified in robotsix-standards as an auditable standard.
- New periodic agent `docstring_coverage`: scans Python source modules for public functions, classes, and methods with zero docstring, prioritizes by complexity, and files draft tickets. Includes YAML definition, Python module, presence file, SourceKind entry, settings, periodic-runner registration, CLI/API/board-UI wiring, and test suite.
- Add module-level docstring to `src/robotsix_mill/dev_tooling/__init__.py`.
- Add module-level docstrings to worker submodules (`core.py`, `poll_loops.py`, `periodic_passes.py`), describing each mixin's role in the event-driven consumer assembly.
- Fix SQLite engine leak in Alembic migrations: `alembic/env.py` now disposes its engine after each run, and `init_db` skips redundant `create_all` + Alembic passes when the board is already initialized. Together these eliminate a file-descriptor leak that caused "unable to open database file" errors in CI under test suites with many tests sharing a worker process.
- Fix infinite auto-approval loop: the mechanical draft fast-path now rejects empty/whitespace drafts, preventing tickets with empty descriptions from being auto-approved in a cycle (approve → refine produces empty body → fast-path approves again).
- ci_fix: rebase onto main before scanning CI so stale branches produce a fresh run against current main; include branch HEAD SHA in the consecutive-identical failure fingerprint to prevent the re-block loop on already-resolved upstream failures; clear depends-on after spawning an out-of-scope dependency fix so the operator's resume-blocked is not silently parked by the unmet-dependency gate
- Changed the Pydantic default for `api_host` from `"127.0.0.1"` to `"0.0.0.0"` to match the shipped `config/config.example.json`. Updated `docs/config/configuration.md` accordingly, closing a three-way config-drift gap.
- Fix stale `tester` reference in `docs/agents/agent-yaml-schema.md` — renamed to `run_tests` to match the renamed agent definition.
- Remove 11 backward-compat aliases (`AuditPassResult`, `AgentCheckPassResult`, etc.) from `periodic_runner.py`; all callers now import `PeriodicPassResult` directly.
- Register five missing CLI subcommands (`state-sync`, `env-doc-sync`, `frontend-sync`, `security-posture`, `triage-boilerplate`) in argparse so they are reachable from the command line.
- Deduplicate ``_resolve_repo_config`` by delegating repo-id resolution to
  ``_resolve_repo_id``; collapse three identical ``elif`` arms in
  ``_run_and_print`` into a single ``elif cmd in (...)`` block.
- Change all 14 built-in periodic workflow defaults from daily (86400 s) to weekly (604800 s): `agent_check`, `bc_check`, `completeness_check`, `diagnostic`, `env_doc_sync`, `frontend_sync`, `health`, `meta`, `module_curator`, `repo_description_sync`, `run_health`, `state_sync`, `survey`, `test_gap`. Per-repo overrides via `.robotsix-mill/periodic/<name>.yaml` (`interval:` field) are unchanged — repos that need faster cadence can override back to `1d`.
- Sandbox test timeouts (rc=124) now produce a deterministic ENV-ERROR diagnosis
  instead of invoking the expensive LLM distiller, letting the fix-loop circuit
  breaker fire immediately without burning 30+ requests per cycle.
- Add `MILL_MEMBER_SYNC_PERIODIC` and `MILL_MEMBER_SYNC_INTERVAL_SECONDS` env var rows to the "Env-var-only periodic agents" table in `docs/config/configuration.md`.
- Document `MILL_CONFIG_SYNC_PERIODIC` and `MILL_CONFIG_SYNC_INTERVAL_SECONDS` env vars in the "Env-var-only periodic agents" table (`docs/config/configuration.md`).
- docs: add `stale_branch_cleanup` to footnote ² exception list in `docs/config/configuration.md` so that its 86400 s (1 day) default is documented, matching the Pydantic model default.
- Docs: add `langfuse_cleanup` to the footnote ² list of agents that default to 86400 s (1 day) interval in `docs/config/configuration.md`.
- Updated the periodic-agent generic interval default in `docs/config/configuration.md` from `86400` (1 day) to `604800` (7 days), matching the actual model defaults for the majority of periodic agents. Added a footnote listing the agents that default to `86400` (1 day) or `3600` (1 hour).
- Document `implement_pass_timeout` / `MILL_IMPLEMENT_PASS_TIMEOUT` in the config reference table (section 2, Request limits).
- Document `sandbox_op_timeout` / `MILL_SANDBOX_OP_TIMEOUT` in the configuration reference (section 9, Sandbox).
- Added the ``diagnostic`` periodic pass to ``_SCHEDULE_ONLY_RUNNERS`` so repos with a ``diagnostic.yaml`` presence file have it scheduled (previously it was silently skipped). Extended ``scripts/check_builtin_kinds.py`` with invariant 5 to catch future ``schedule_only`` entries that are missing scheduler wiring.
- Document `ci_debt_recheck` periodic agent in `docs/config/configuration.md`: add to periodic agent list and document `MILL_CI_DEBT_RECHECK_PERIODIC` (default `true`) and `MILL_CI_DEBT_RECHECK_INTERVAL_SECONDS` (default `3600`) env vars
- Docs: added `roadmap_sync`, `frontend_sync`, and `pin_bump` to the periodic agent list and env-var reference table in `docs/config/configuration.md`.
- Document `docstring_coverage` and `module_size` periodic agents in `docs/config/configuration.md` — add them to the periodic agent list and add dedicated subsections with env-var tables.
- Add `repo-description-sync` CLI subcommand, wiring the already-registered periodic pass into the CLI `_RUNNERS` dict, add_parser block, and `_resolve_repo_config` branch.
- Added `changelog-autofill` CLI subcommand, wiring the existing schedule_only runner for manual invocation.
- Add missing `.robotsix-mill/periodic/roadmap_sync.yaml` presence file so the periodic scheduler discovers and runs the `roadmap_sync` workflow.