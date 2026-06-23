## 0.0.0 (unreleased)

- Wire `periodic.test_gap.request_limit` to YAML config so operators can tune the test-gap agent's request cap without code changes.
- Add `core.claude_max_concurrency` YAML path mapping and defaults entry so operators can configure Claude SDK concurrency via YAML instead of only the `MILL_CLAUDE_MAX_CONCURRENCY` env var.
- Wire `pipeline.ci_fix_wait_poll_interval_s` and `pipeline.ci_fix_wait_timeout_s` to YAML config so operators can tune CI-fix wait timing without code changes.
- Fixed `docs/configuration.md`: corrected the documented default of `stage_timeout_overrides` from `{}` to `{"refine": 900}` to match the Pydantic model default, and added a note explaining the built-in refine-stage cap.
- Update `MillBoardAdapter` docstring to remove outdated fallback language now that `robotsix_board` is a required dependency.
