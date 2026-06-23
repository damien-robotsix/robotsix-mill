## 0.0.0 (unreleased)

- Trim auto-approve classification system prompt: stripped verbose example reason bullets and redundant formatting instructions to roughly halve cached input tokens on every OpenRouter pay-per-token call.
- Add `frontend_sync` periodic agent that cross-references Python enum values (State, SourceKind) against their mirrored CSS selectors (`.s-*`, `.src-*`) and JS maps (`SOURCE_CLASS`, `STATE_TRACE`, `AGENT_COLORS`), filing draft tickets for any drift between backend enums and frontend representations.
- Convert `Ticket.kind` and `TicketCreate.kind` from free-form `str` to a `TicketKind(StrEnum)` with `TASK`, `INQUIRY`, `EPIC` variants. Replaced all magic-string comparisons across 14 files. Added a comment referencing the canonical enum in `board-mill.js`.- Fix CSS badge class mismatch: `.src-env-sync` → `.src-env-doc-sync` so `env_doc_sync`-sourced tickets render with correct styling on the board.
- Wire `periodic.test_gap.request_limit` to YAML config so operators can tune the test-gap agent's request cap without code changes.
- Add `core.claude_max_concurrency` YAML path mapping and defaults entry so operators can configure Claude SDK concurrency via YAML instead of only the `MILL_CLAUDE_MAX_CONCURRENCY` env var.
- Wire `pipeline.ci_fix_wait_poll_interval_s` and `pipeline.ci_fix_wait_timeout_s` to YAML config so operators can tune CI-fix wait timing without code changes.
- Fixed `docs/configuration.md`: corrected the documented default of `stage_timeout_overrides` from `{}` to `{"refine": 900}` to match the Pydantic model default, and added a note explaining the built-in refine-stage cap.
- Update `MillBoardAdapter` docstring to remove outdated fallback language now that `robotsix_board` is a required dependency.
