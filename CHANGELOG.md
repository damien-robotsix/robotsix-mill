## 0.0.0 (unreleased)

- Add missing `addressing_review` entry to `STATE_TRACE` map in `board-mill.js`, fixing bare state-name display for the `ADDRESSING_REVIEW → HUMAN_MR_APPROVAL` transition in the drawer history timeline.
- Add missing `.s-implement_complete` CSS state colour (`--c: #3b82f6`) to `board-mill.css`, fixing rendering of column headers, event chips, and child-state badges for the `implement_complete` pipeline state.
- Add `.s-maintenance` CSS state-colour selector to `board-mill.css` so MAINTENANCE-state column headers and event chips render with the same gray as DRAFT.
- Add missing `.s-addressing_review` CSS selector to `board-mill.css` so tickets in `AddressingReview` state render with amber column-header and event-chip styling on the board.
- Add mandatory `read_file` verification gate to triage agent NO_CHANGE path, and prior-stage findings protection to dedup agent prompt, preventing hallucinated file-existence assertions that incorrectly close tickets.

- Enable `frontend_sync` periodic workflow for `robotsix-mill` by adding the per-repo opt-in file `.robotsix-mill/periodic/frontend_sync.yaml` that cross-references Python `State`/`SourceKind` enum values against CSS selectors and JS maps in `board-mill.{css,js}`.
- Add cross-repo import verification instruction to refine agent prompt: before referencing a symbol from a git-pinned dependency, verify it is importable; emit a ````prereq```` block on failure rather than assuming the module exists.
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
