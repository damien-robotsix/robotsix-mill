## 0.0.0 (unreleased)

- Wire `periodic.test_gap.request_limit` to YAML config so operators can tune the test-gap agent's request cap without code changes.
- Add `core.claude_max_concurrency` YAML path mapping and defaults entry so operators can configure Claude SDK concurrency via YAML instead of only the `MILL_CLAUDE_MAX_CONCURRENCY` env var.
- Wire `pipeline.ci_fix_wait_poll_interval_s` and `pipeline.ci_fix_wait_timeout_s` to YAML config so operators can tune CI-fix wait timing without code changes.
