## 0.0.0 (unreleased)

- Add per-ticket circuit breaker: `max_traces_per_ticket` (trace-count guard, default 15) and `max_openrouter_marginal_usd_per_ticket` (OpenRouter spend guard, default $3.00), wired through settings, YAML config aliases, and `config/mill.defaults.yaml`; integrated into `Worker._check_progress` with Langfuse `session_traces()` to block runaway loops that the dollar cap may miss.

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
