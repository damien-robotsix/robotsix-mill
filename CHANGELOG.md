## 0.0.0 (unreleased)

- **docs**: document `MILL_RUN_HEALTH_*` extra fields (`MILL_RUN_HEALTH_WINDOW_HOURS`,
  `MILL_RUN_HEALTH_TARGET_REPO_ID`, `MILL_RUN_HEALTH_MEMORY_PATH`) in
  `docs/configuration.md` section 12 (periodic agents) with a dedicated
  `#### run_health` subsection listing all 5 env vars with YAML paths,
  defaults, and descriptions.
- **docs**: document `MILL_DB_MAINTENANCE_*` and `MILL_SANDBOX_REAPER_*`
  environment variables in `docs/configuration.md` section 12 (periodic
  agents), including `db_maintenance` and `sandbox_reaper` subsections
  with their env vars, defaults, and descriptions.
- **deps**: relax `requires-python` from `>=3.14,<3.15` back to `>=3.14`
  and revert `uv.lock` `requires-python` from `==3.14.*` to `>=3.14`.
  The `==3.14.*` lockfile format may not be recognized by Dependabot's
  `uv` ecosystem parser, causing graph-submission failures on every push.
- **docs**: add missing pipeline circuit-breaker env vars to
  `docs/configuration.md` — `codeql_fp_triage_enabled` in §7 (Approval &
  review), and `auto_fix_max_cycles`, `ping_pong_max_alternations`,
  `ticket_state_cycle_limit` in §11 (Pipeline tail). (mill: env doc sync: missing-from-docs — pipeline circuit-breaker env vars (4 fields) (20260626T204331Z-env-doc-sync-missing-from-docs-pipeline-7768))
- **cleanup**: remove redundant `from typing import Any` from the
  `TYPE_CHECKING` block in `src/robotsix_mill/runners/trace_review_runner.py`
  (`Any` is already imported at module level).
- **ci**: raise Dependabot `uv` ecosystem `open-pull-requests-limit` from 0
  back to 1. The `[tool.uv.sources]` migration resolved the inline-URL
  parsing issue, but `limit: 0` (graph-submission-only mode) still triggers
  a Dependabot bug with git-backed packages. With `limit: 1` the full
  version-check pipeline runs and handles them correctly.
- **deps**: switch back from inline PEP 508 `@ git+https://` URLs to
  `[tool.uv.sources]` for all git dependencies. The inline-URL approach
  (e9a52c3f) still triggers Dependabot `uv` ecosystem graph-submission
  failures on main; `[tool.uv.sources]` is the configuration the grapher
  can parse correctly.
- **deps**: remove `[tool.uv.sources]` and inline all git dependencies as
  PEP 508 `@ git+https://` entries in `[project].dependencies` and
  `[dependency-groups].dev`.  The `pin_pep508_entry` Dependabot bug was
  actually in the reverse direction: `[tool.uv.sources]` is what the `uv`
  ecosystem grapher cannot resolve, while inline PEP 508 URLs work correctly.
  Revert `dependabot.yml` `open-pull-requests-limit` from 1 back to 0 — the
  graph-submission-only mode is now sufficient.
- **deps**: restore `[tool.uv.sources]` git-dependency resolution (bare names
  in `[project].dependencies`, full commit SHAs in `[tool.uv.sources]`) and
  remove inline PEP 508 `@ git+https://` URLs. The inline-URL configuration
  (restored in a prior commit) still causes Dependabot `uv` ecosystem
  graph-submission failures even with `open-pull-requests-limit: 1`. The
  `[tool.uv.sources]` + `limit: 1` combination runs the full version-check
  pipeline which handles git-backed packages correctly.
- **deps**: raise Dependabot `uv` ecosystem `open-pull-requests-limit` from 0
  to 1 to work around a known Dependabot bug in graph-submission-only mode.
  When the limit is 0 the `uv` grapher runs a reduced pipeline that cannot
  resolve `[tool.uv.sources]`-backed git dependencies; with limit ≥1 the full
  version-check pipeline runs and handles git-backed packages correctly.
- **deps**: complete the git-dependency migration: remove the remaining inline
  `@ git+https://` PEP 508 direct references from `[project].dependencies` and
  `[dependency-groups].dev` (they were already mirrored in `[tool.uv.sources]`).
  The `pin_pep508_entry` Dependabot bug drops inline URLs, so keeping them
  causes the `uv` ecosystem graph-submission to fail even when
  `[tool.uv.sources]` coexists. Switch `security-audit.yml` from `pip install`
  to `uv sync` / `uv run` so the audit jobs continue to function without the
  PEP 508 inline references.
- **deps**: revert the `[tool.uv.sources]`-only approach and restore inline
  PEP 508 `@ git+https://` direct references for all git dependencies. The
  `uv`-sources-only configuration (bare names in `[project].dependencies`,
  git URLs in `[tool.uv.sources]`) caused a Dependabot `uv` ecosystem
  graph-submission failure on every push. The prior working configuration
  (inline PEP 508 URLs with no `[tool.uv.sources]` table) is restored.
  The `security-audit.yml` `uv` migration is kept — `uv sync` / `uv run`
  handles inline PEP 508 URLs correctly.
- **deps**: pin `robotsix-yaml-config` to a specific commit to resolve a
  `uv lock` conflict with `robotsix-modules`' transitive pin; fixes the
  Dependabot `uv` ecosystem graph-submission failure on main.
- **observability**: enrich agent trace root spans with ticket state, retry
  attempt, transient error reason, review rounds, dispatch count, and blocked/
  paused origin — metadata previously only in the SQLite row or in-memory
  counters is now stamped onto Langfuse traces so expensive/runaway tickets
  are diagnosable from trace data alone.
- **periodic**: remove orphan `.robotsix-mill/periodic/board_cleanup.yaml` and
  `cost_reconciliation.yaml` presence files that had no implementation anywhere
  in the codebase, and drop the matching dead Ruff per-file-ignore entry for
  the non-existent `cost_reconciliation_runner.py`.
- **config_syncing**: document missing `recent_proposals` and `verified_proposals`
  parameters in `run_config_sync_agent` docstring.
- **board UI**: add missing `runFrontendSync()` button to the Agents dropdown
  menu in `board_html.py` — the `frontend_sync` `llm_agent` was the only one
  without a user-facing trigger in the board.
- **refine**: re-resolve clone target from the ticket's current `board_id` at
  clone time so a ticket migrated between boards before refine runs clones the
  destination board's repo (not the stale creation-time repo).
- **ci_poll**: extract duplicated PR status-check preamble into shared `_check_pr_baseline` helper, removing ~135 lines of near-identical code across three poll methods.
- **docs**: add missing `core.limits.max_openrouter_marginal_usd_per_ticket` row to configuration reference section 3 table.
- **docs**: add `core.limits.refine_requests_simple` to the request limits table in
  `docs/configuration.md`.
- **docs/configuration.md**: add missing `stale_branch_cleanup` to the named
  periodic-agent roster in section 12.
- **docs**: add `dependabot_ingest` to the periodic agent roster in
  `configuration.md` section 12 (was missing from the 22-agent list).
- **board-mill.js**: add `asked: "answer"` to `STATE_TRACE` so the history
  timeline renders `ASKED` transitions with the "answer" stage label instead
  of a bare state name.
- **board CSS**: add `.s-asked` state colour (`#f59e0b` amber) so column
  headers and event chips for the `ASKED` inquiry state render with colour.
- **board UI**: add `maintenance` entry to `STATE_TRACE` in `board-mill.js`
  so the history timeline labels maintenance transitions correctly.
- **board-mill.css**: add missing `.s-answered` state colour (teal, `#14b8a6`)
  so that column headers and event chips for the `ANSWERED` state render with
  the appropriate colour.
- **board-mill**: add missing `SOURCE_CLASS` entry and CSS badge for
  `dependabot_alerts` source kind — tickets from the dependabot-alerts scanner
  now show a dedicated amber/orange badge instead of the generic "USER" badge.
- **refine agent**: extend triage-findings instruction to prevent wasted
  `read_file` calls on paths already confirmed as nonexistent during triage.
- **trace_review**: suppress false-positive `tool_errors` flag for `run_command`
  outputs containing the expected git rebase conflict notification
  (`error: could not apply`), which the rebase agent produces when it
  encounters conflicts it is expected to resolve.
- **trace_review env-var docs**: add dedicated sub-table under section 12 of
  `docs/configuration.md` documenting all 22 `MILL_TRACE_REVIEW_*` environment
  variables with their YAML paths, defaults, and descriptions.
- Document all 16 `MILL_DATA_DIR_AUDIT_*` environment variables in
  `docs/configuration.md` section 12, including the three standard
  periodic-agent fields and the thirteen agent-specific GC/size-threshold
  settings.
- Document missing gate and refine-routing environment variables in
  `docs/configuration.md`: add `reviewer_agreement_gate_enabled` and
  `refine_mill_misroute_gate_enabled` to section 7, and add a new
  section 11.3 (Refine routing) covering the refine subscription model
  routing knobs (`refine_trivial_*`, `refine_subscription_*`,
  `refine_findings_*`, `max_re_refine_cycles_before_cheap`,
  `refine_delta_reuse_enabled`).
- Add `.github/ISSUE_TEMPLATE/config.yml` to disable blank issues and
  redirect questions, feature requests, and security reports to
  Discussions and the security policy page.
- Add `.github/PULL_REQUEST_TEMPLATE.md` with the PR checklist from
  `CONTRIBUTING.md` and a note for autonomous agents.
- **Module test layout alignment**: moved `tests/runtime/test_component_agent.py`
  to `tests/component_agent/test_component_agent.py`, matching the per-module
  directory convention used by every other module. Added
  `tests/component_agent/__init__.py`. Updated `docs/modules.yaml` accordingly.

- **Refine no-op guard**: refine passes that yielded a "no change needed"
  verdict (refiner returned `no_change_needed=True`, reviewer-agreement
  guard, or triage `NO_CHANGE` classifier) now route TASK-kind tickets
  without a branch toward `ready` (implementation) instead of auto-closing
  to `done`.  The `_guard_implementation_done` helper in
  `stages/refine/core.py` provides defense-in-depth — any future DONE
  outcome for a branchless TASK ticket that does not carry a recognised
  non-implementation prefix (dedup, freshness, obsolescence, misroute) is
  redirected to `ready`.  Maintenance tickets and human `mark_done` are
  unaffected.  The non-implementation close-prefix constants moved from
  `stages/refine/helpers.py` to `core/constants.py` so the guard can
  import them without a circular dependency.

- Exclude cross-reference sections (`## Reference`, `## See also`, `## Related work`, and any heading starting with `reference` or `see also`) from path-token extraction in `paths_excluding_out_of_scope`, preventing consumer-migration follow-ups from being filed for paths that are merely cross-referenced, not deliverables.
- Make the per-pass implement (coordinator) agent-request budget configurable
  via `MILL_PER_PASS_REQUEST_BUDGET` env var (or `core.limits.coordinator_requests`
  in YAML config).  Default raised from 200 to 500 so normal-sized tickets
  complete in a single pass.  Hard upper bound 5000 prevents runaway cost
  from misconfiguration.

- Extract duplicate `_paths_from_diff` from `document.py` and `review.py` into shared `vcs/git_ops.py`.
- Add `actionlint` step to `workflow-audit` job in `security-audit.yml` for workflow syntax checking, pinned to `rhysd/actionlint@v1.7.12`.

- Wire `frontend-sync` agent into the dispatch/UI layers: add POST route
  (`/frontend-sync`), CLI `_RUNNERS` entry, AGENT_COLORS key, and board
  button handler + window export in `board-mill.js`.

- Split `src/robotsix_mill/stages/refine/orchestration.py` (2181 lines) into focused sub-modules: `_result_paths.py` (no-change/promote/single/multi result handlers), `_triage.py` (triage skip logic), `_checkpoint.py` (save/load/clear checkpoint), `_reconcile.py` (short-circuit and side-effect application). The `orchestration.py` module is now a 499-line thin coordinator delegating to sub-module functions. Backward-compatible: `RefineAgentMixin` exposes delegation staticmethods and module-level re-exports preserve test monkeypatch targets.

- Add `robotsix-deploy` package: central deployment & lifecycle server scaffold with FastAPI /health and /ready endpoints, env-based config (`DEPLOY_*` prefix), Dockerfile, docker-compose, CI workflow calling the shared `python-ci.yml`, and GHCR publish workflow calling `docker-release.yml`.
- Fix `Dockerfile.deploy` builder stage missing `git` installation — `uv pip install` needs git to clone the git-sourced dependencies (robotsix-llmio, robotsix-yaml-config, robotsix-board-agent, robotsix-agent-comm, robotsix-board) when installing from the lockfile-generated requirements.txt.

- Fix `docs/configuration.md` — correct `core.limits.test_requests` documented default from `16` to `30` to match the model and YAML defaults.

- Enable `board_cleanup` periodic workflow for `robotsix-mill` by adding the per-repo opt-in file `.robotsix-mill/periodic/board_cleanup.yaml`.
- Enable `cost_reconciliation` periodic workflow for `robotsix-mill` by adding the per-repo opt-in file `.robotsix-mill/periodic/cost_reconciliation.yaml`.
- Extend `_served_reads` closure-scoped read-file dedup to cover full-file re-reads on the Claude SDK path (`ctx=None`), matching the pydantic-ai path behavior. Full re-reads of already-loaded files are now refused instead of silently re-serving cached content. Added `_served_reads` invalidation on `write_file`, `edit_file`, and `delete_file` so mutations clear the dedup record.

- Add missing `addressing_review` entry to `STATE_TRACE` map in `board-mill.js`, fixing bare state-name display for the `ADDRESSING_REVIEW → HUMAN_MR_APPROVAL` transition in the drawer history timeline.
- Add missing `.s-implement_complete` CSS state colour (`--c: #3b82f6`) to `board-mill.css`, fixing rendering of column headers, event chips, and child-state badges for the `implement_complete` pipeline state.
- Add `.s-maintenance` CSS state-colour selector to `board-mill.css` so MAINTENANCE-state column headers and event chips render with the same gray as DRAFT.
- Add missing `.s-addressing_review` CSS selector to `board-mill.css` so tickets in `AddressingReview` state render with amber column-header and event-chip styling on the board.
- Add `frontend_sync` entry to `SOURCE_CLASS` map in `board-mill.js` and corresponding `.src-frontend-sync` CSS badge class in `board-mill.css`, so tickets created by the frontend_sync periodic agent display a dedicated purple badge instead of the generic "user" fallback.
- Add mandatory `read_file` verification gate to triage agent NO_CHANGE path, and prior-stage findings protection to dedup agent prompt, preventing hallucinated file-existence assertions that incorrectly close tickets.

- Enable `frontend_sync` periodic workflow for `robotsix-mill` by adding the per-repo opt-in file `.robotsix-mill/periodic/frontend_sync.yaml` that cross-references Python `State`/`SourceKind` enum values against CSS selectors and JS maps in `board-mill.{css,js}`.
- Add cross-repo import verification instruction to refine agent prompt: before referencing a symbol from a git-pinned dependency, verify it is importable; emit a ````prereq```` block on failure rather than assuming the module exists.
- Fix stale default in docs: `core.limits.doc_requests` is `16` (matches model and YAML), not `8`.
- Wire spec-review conciseness pass into the multi-scope degraded path so auto-approve receives the concise spec instead of the verbose original when a split result degrades with no valid children.
- Add `is_file()` pre-check in `list_dir` fs tool: when an agent calls `list_dir` on a file path, return a clear error message ("is a file, not a directory — use read_file") instead of the opaque `NotADirectoryError`.
- Cap SQLite WAL file at 2 MiB via `PRAGMA journal_size_limit` and add `PRAGMA wal_checkpoint(TRUNCATE)` to the periodic DB maintenance pass to prevent unbounded WAL growth.
- Enrich FastAPI OpenAPI schema with version, description, contact, license, servers, and tag descriptions parsed from `pyproject.toml`.
- Replace 9 inline `try: except Exception: pass` ALTER TABLE blocks in `init_db()` (one per column) with calls to `add_column_if_missing()`, which catches `sqlite3.OperationalError` specifically, consolidates all column migrations into a single transaction, and returns `bool` for whether each column was newly added. The helper lives in `src/robotsix_mill/core/sqlite_utils.py` (mirrors the API of `robotsix_llmio.core.sqlite_utils` from PR #255).
- explore sub-agent prompt: add "CONFIRM PATHS" grep guard and "NO GREP CHASING" rule to prevent wasteful grep calls on non-existent paths.
- refine agent prompt: add grep-avoidance guidance in the budget warning section — use `read_file` on a known path instead of a speculative `grep` when the file location is already known.
- Fix Dependabot `uv` ecosystem graph-submission failure: remove the redundant `[tool.uv.sources]` table from `pyproject.toml` (the `robotsix-llmio` git source is already declared via PEP 508 `@ git+https://` in `[project.dependencies]`); extend `_has_uv_sources()` in the sandbox to also detect PEP 508 `git+https://` direct references so the sandbox continues to prefer `uv sync` over `pip install` for projects with git dependencies.
- Remove stale `run_command` mention from `completeness_check` budget-warning boilerplate — the agent doesn't have `run_command` at runtime.
- Replace stale `awaiting_approval` state references with canonical `human_issue_approval` in docs (`approval-gate.md`, `audit-agent.md`, `configuration.md`, `docker-architecture.md`, `notifications.md`).
- Wire `frontend_sync` periodic agent into the dispatch pipeline: add `SourceKind.FRONTEND_SYNC`, register in `_BUILTIN_KINDS`, add `PeriodicPassConfig`, and create backward-compat runner stub.

- Replace bare string `kind` literals (`"task"`, `"epic"`, `"inquiry"`) with `TicketKind.TASK` / `TicketKind.EPIC` / `TicketKind.INQUIRY` in all test files; add `scripts/check_kind_literals.py` CI gate to prevent regressions.

- Wire `completeness_check_request_limit` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS` (`periodic.completeness_check.request_limit`) and default leaf in `config/mill.defaults.yaml`.
- Remove stale `changes/` directory and its two unused `.misc.md` changelog fragments; drop `changes/**/*` glob from `docs/modules.yaml` dev-tooling module paths.
- Add `fail_under = 80` to `[tool.coverage.report]` so `coverage report` fails when total coverage drops below 80%.

- Add 11 unit tests for `src/robotsix_mill/core/sqlite_utils.py` covering `_execute_sql`, `add_column_if_missing`, `run_additive_migrations`, and error paths; narrow `add_column_if_missing` exception catch to only suppress "duplicate column" errors (let `no such table` / syntax errors propagate instead of silently returning `False`).

- Add per-ticket circuit breaker: `max_traces_per_ticket` (trace-count guard, default 15) and `max_openrouter_marginal_usd_per_ticket` (OpenRouter spend guard, default $3.00), wired through settings, YAML config aliases, and `config/mill.defaults.yaml`; integrated into `Worker._check_progress` with Langfuse `session_traces()` to block runaway loops that the dollar cap may miss.
- Increase document stage `request_limit` from 8 to 16 to prevent `UsageLimitExceeded` errors on feature-sized tickets that need multiple file reads and edits.
- Deduplicate triage system prompt: remove redundant "Tool-use discipline" section (~378 tokens) and fold unique budget/history guidance into the "Tool: `read_file`" section (~92 tokens), saving ~286 input tokens per triage call.

- Refactor `ci_fix.py`: extract stateless helpers (formatters, hashing, `_FailingContext`) into `ci_fix_helpers.py` and CodeQL FP triage subsystem into `ci_fix_codeql.py`; update all importers.

- Wire `language_instructions_dir` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS` (`core.language_instructions_dir`), default leaf in `config/mill.defaults.yaml`, and documentation row in `docs/configuration.md`.
- Fix merge-gate stall on clean mergeable PRs: accept `mergeable_state == "unstable"` as promotable in `_ci_truly_green` (required gates passed, only non-required status non-green); add `pending` check-name list to `check_status`/`_derive_check_conclusion` return dicts; log precise blocking reason (conclusion + mergeable_state + pending checks) when re-polling `IMPLEMENT_COMPLETE`.

- Fix deptry dependency issues: add `opentelemetry-api` to the `tracing` extra (we import `opentelemetry.trace` directly) and add `opentelemetry-sdk`/`opentelemetry-exporter-otlp-proto-http` to the deptry DEP002 ignore list (they are needed transitively by `robotsix_llmio`).
- Fix `_ensure_tracing` to catch `ImportError` from `setup_langfuse_tracing()` so missing `opentelemetry` dependencies degrade gracefully instead of crashing ticket processing.
- Fix `ci_fix_request_limit` config drift: add YAML alias in `_YAML_PATH_TO_ALIAS` and default leaf `ci_fix_request_limit: 120` in `config/mill.defaults.yaml` so the setting is configurable via YAML (matching every other pipeline-level limit).
- Wire `bespoke_discovery_interval_seconds` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS`, default leaf in `config/mill.defaults.yaml`, and documentation row in `docs/configuration.md`.
- Wire `bespoke_periodic` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS`, default leaf in `config/mill.defaults.yaml`, and documentation row in `docs/configuration.md`.
- Strengthen refine agent budget warning: hard-cap `read_file` at ≤10 calls per generation, mandate `explore`/`parallel_explore` for multi-file work, and add a 20-tool-invocation stop-rule to force delegation to sub-agents after the first 20 `read_file`+`run_command` calls.
- Fix `run_doc_agent` crash on empty `board_id`: guard all memory-ledger operations (`memory_file_for`, `load_memory`, `persist_memory`) behind a non-empty `board_id` check so the doc agent runs without a memory ledger instead of raising `ValueError` — resolves non-blocking failures on meta-split child tickets where `board_id` was empty/unresolvable.
- Stamp `board_id=ticket.board_id` on split-child and umbrella-epic `TicketService.create()` calls in `orchestration.py` so split children carry the parent ticket's resolvable board rather than silently inheriting the service's `self.board_id`.
- Refactor `_triage_skip()` in refine orchestration: extract `_parse_prior_boards()`, `_anti_bounce_escalate()`, and `_persist_triage_complexity()` helpers to reduce function length and eliminate 9-level nesting in the MIGRATE migration-history parsing path.
- Fix `build_agent()`: pass `model` through to `provider.build_agent()` in the Claude-SDK branch so the `refine_claude_model` setting (default `sonnet`) actually takes effect, right-sizing the refine stage off Opus while staying on the same subscription transport.
- Remove dead `try/except ImportError` fallback for `robotsix_board.render_config_script` in `_health.py` — `robotsix-board` is a required runtime dependency, so the `except` branch was unreachable dead code. (mill: Remove dead robotsix-board fallback from _health.py (proposal 4226 follow-up) (20260623T180731Z-remove-dead-robotsix-board-fallback-from-d4de))
- Forward `max_tokens` to the Claude SDK provider in `build_agent()` so the YAML-defined `max_tokens` cap (e.g. 8192 for `refine.yaml`) is actually enforced, preventing ~$1.71 Opus output-cost spikes.
- Add JSON-only-output directive to epic-breakdown agent system prompt to prevent prose-before-JSON failures that trigger costly pydantic-ai retries.
- Document `MILL_CI_FIX_REQUEST_LIMIT` (default 120) in `docs/configuration.md` — the Pipeline tail section already listed other `ci_fix_*` knobs but omitted this one.
- Add `MILL_MAX_EVENTS_PER_TICKET` and `MILL_MAX_COMMENTS_PER_TICKET` to `docs/configuration.md` Pipeline tail table — both fields existed in code with defaults but were missing from the documentation.
- Fix `docs/configuration.md`: update documented default for `sandbox.image` from `robotsix/mill-sandbox:latest` to `python:3.14-slim` to match the Pydantic model default in `_settings_core.py`.
- Drop dead `body` parameter from `_is_noop_draft()` in `retrospect.py` — the function always ignored it, delegating to the title-only `is_noop_report`.
- Trim auto-approve classification system prompt: stripped verbose example reason bullets and redundant formatting instructions to roughly halve cached input tokens on every OpenRouter pay-per-token call.
- Remove dead `_absorb_findings_list_shape` model validator from `RetrospectResult` — the list-of-dicts findings edge case it handled has never been re-triggered and is untested.
- Remove dead backward-compat alias `BoardCleanupPassResult` from `periodic_runner.py` and its vulture whitelist entry.
- Add `frontend_sync` periodic agent that cross-references Python enum values (State, SourceKind) against their mirrored CSS selectors (`.s-*`, `.src-*`) and JS maps (`SOURCE_CLASS`, `STATE_TRACE`, `AGENT_COLORS`), filing draft tickets for any drift between backend enums and frontend representations.
- Convert `Ticket.kind` and `TicketCreate.kind` from free-form `str` to a `TicketKind(StrEnum)` with `TASK`, `INQUIRY`, `EPIC` variants. Replaced all magic-string comparisons across 14 files. Added a comment referencing the canonical enum in `board-mill.js`.- Fix CSS badge class mismatch: `.src-env-sync` → `.src-env-doc-sync` so `env_doc_sync`-sourced tickets render with correct styling on the board.
- Wire `periodic.test_gap.request_limit` to YAML config so operators can tune the test-gap agent's request cap without code changes.
- Document `MILL_LANGFUSE_CLEANUP_MAX_TRACES` env var in `docs/configuration.md` (default 1000, cap on retained traces per Langfuse project).
- Add `core.claude_max_concurrency` YAML path mapping and defaults entry so operators can configure Claude SDK concurrency via YAML instead of only the `MILL_CLAUDE_MAX_CONCURRENCY` env var.
- Wire `pipeline.ci_fix_wait_poll_interval_s` and `pipeline.ci_fix_wait_timeout_s` to YAML config so operators can tune CI-fix wait timing without code changes.
- Fixed `docs/configuration.md`: corrected the documented default of `stage_timeout_overrides` from `{}` to `{"refine": 900}` to match the Pydantic model default, and added a note explaining the built-in refine-stage cap.
- Update `MillBoardAdapter` docstring to remove outdated fallback language now that `robotsix_board` is a required dependency.
