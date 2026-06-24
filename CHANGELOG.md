## 0.0.0 (unreleased)

- Fix `build_agent()`: pass `model` through to `provider.build_agent()` in the Claude-SDK branch so the `refine_claude_model` setting (default `sonnet`) actually takes effect, right-sizing the refine stage off Opus while staying on the same subscription transport.
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
