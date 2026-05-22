# Configuration reference

Full reference for every `MILL_*` environment variable (and a few key
non-prefixed vars) from `config.py:Settings`.  This mirrors
`.env` and `secrets.env.example`; `.env` is the committed canonical inline config with
production-ready defaults, `secrets.env.example` is the template for credentials.

---

## 1. Core models

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_MODEL` | `model` | `deepseek/deepseek-v4-pro` | `str` | Coordinator model — reads/edits the repo, delegates to sub-agents |
| `MILL_EXPLORE_MODEL` | `explore_model` | `deepseek/deepseek-v4-pro` | `str` | Scout sub-agent — returns concise pointers, never whole files |
| `MILL_TEST_MODEL` | `test_model` | `deepseek/deepseek-v4-pro` | `str` | Test sub-agent — distills suite failures into diagnosis |
| `MILL_REFINE_MODEL` | `refine_model` | `deepseek/deepseek-v4-pro` | `str` | Refine agent — authors engineering specs from drafts |
| `MILL_ANSWER_MODEL` | `answer_model` | `deepseek/deepseek-v4-pro` | `str` | Answer agent — investigative Q&A via repo + web + traces |
| `MILL_RETROSPECT_MODEL` | `retrospect_model` | `deepseek/deepseek-v4-pro` | `str` | Retrospect agent — audits finished tickets; proposes improvements |
| `MILL_AUDIT_MODEL` | `audit_model` | `deepseek/deepseek-v4-pro` | `str` | Audit agent — meta-audit for quality/security coverage gaps |
| `MILL_DEDUP_MODEL` | `dedup_model` | `deepseek/deepseek-v4-pro` | `str` | Dedup agent — pre-refine duplicate/already-done check |
| `MILL_WEB_RESEARCH_MODEL` | `web_research_model` | `deepseek/deepseek-v4-pro` | `str` | Web-research sub-agent — web lookups, conclusion only |
| `MILL_REVIEW_MODEL` | `review_model` | `deepseek/deepseek-v4-pro` | `str` | Review agent — blind dual-model diff audit (opt-in) |
| `MILL_TRACE_INSPECTOR_MODEL` | `trace_inspector_model` | `deepseek/deepseek-v4-pro` | `str` | Trace-inspector sub-agent — inspects full Langfuse observation tree |
| `MILL_TEST_GAP_MODEL` | `test_gap_model` | `deepseek/deepseek-v4-pro` | `str` | Test-gap agent — identifies modules with zero dedicated tests |
| `MILL_AGENT_CHECK_MODEL` | `agent_check_model` | `deepseek/deepseek-v4-pro` | `str` | Agent-check agent — audits agent definitions for coherence |
| `MILL_HEALTH_MODEL` | `health_model` | `deepseek/deepseek-v4-pro` | `str` | Health agent — codebase-health across 6 dimensions |
| `MILL_SURVEY_MODEL` | `survey_model` | `deepseek/deepseek-v4-pro` | `str` | Survey agent — discovers OSS projects; proposes improvements |
| `MILL_RATE_LIMIT_FALLBACK_MODEL` | `rate_limit_fallback_model` | `""` (empty = disabled) | `str` | Fallback model when rate-limit retries exhausted |

---

## 2. Request limits

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_COORDINATOR_REQUEST_LIMIT` | `coordinator_request_limit` | `200` | `int` | Per-ticket request cap for the implement (coordinator) agent |
| `MILL_EXPLORE_REQUEST_LIMIT` | `explore_request_limit` | `20` | `int` | Per-call request cap for the explore sub-agent |
| `MILL_TEST_REQUEST_LIMIT` | `test_request_limit` | `8` | `int` | Per-call request cap for the test sub-agent |
| `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `web_research_request_limit` | `8` | `int` | Per-call request cap for the web-research sub-agent |
| `MILL_DEDUP_REQUEST_LIMIT` | `dedup_request_limit` | `4` | `int` | Per-call request cap for the dedup check |

---

## 3. Worker pool

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_MAX_CONCURRENCY` | `max_concurrency` | `4` | `int` | Max parallel tickets in the worker pool |
| `MILL_MAX_FIX_ITERATIONS` | `max_fix_iterations` | `8` | `int` | Max implement→test fix loop iterations before BLOCK |
| `MILL_MAX_STUCK_CYCLES` | `max_stuck_cycles` | `3` | `int` | Re-entries to same stage without progress before BLOCK |
| `MILL_MAX_SPEND_USD_PER_TICKET` | `max_spend_usd_per_ticket` | `0.0` | `float` | Dollar cap per ticket (0.0 = disabled); enforced in `worker.py:_check_progress` |

---

## 4. Transient retry & timeout

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_TRANSIENT_RETRIES` | `transient_retries` | `4` | `int` | Max retries for transient network/model failures (429, 5xx, timeouts) |
| `MILL_TRANSIENT_BACKOFF_BASE` | `transient_backoff_base` | `2.0` | `float` | Base seconds for exponential backoff (jittered) |
| `MILL_TRANSIENT_BACKOFF_CAP` | `transient_backoff_cap` | `30.0` | `float` | Max seconds between transient retries |
| `MILL_RATE_LIMIT_BACKOFF_BASE` | `rate_limit_backoff_base` | `30.0` | `float` | Base seconds for rate-limit backoff (longer window) |
| `MILL_RATE_LIMIT_BACKOFF_CAP` | `rate_limit_backoff_cap` | `120.0` | `float` | Max seconds between rate-limit retries |
| `MILL_RATE_LIMIT_FALLBACK_RETRIES` | `rate_limit_fallback_retries` | `3` | `int` | Consecutive rate-limit failures before switching to fallback model |
| `MILL_RATE_LIMIT_FALLBACK_MODEL` | `rate_limit_fallback_model` | `""` | `str` | Fallback model for rate-limit exhaustion (see §1) |
| `MILL_MODEL_REQUEST_TIMEOUT` | `model_request_timeout` | `900.0` | `float` | Hard per-call timeout in seconds for every model request |

---

## 5. Management plane

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_DATA_DIR` | `data_dir` | `.mill-data` | `Path` | Data directory for DB, workspaces, and memory ledgers |
| `MILL_API_HOST` | `api_host` | `127.0.0.1` | `str` | FastAPI listen address |
| `MILL_API_PORT` | `api_port` | `8077` | `int` | FastAPI listen port |
| `MILL_API_URL` | `api_url` | `http://127.0.0.1:8077` | `str` | Base URL the CLI client uses to reach the API |

---

## 6. Approval & review

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_REQUIRE_APPROVAL` | `require_approval` | `true` | `bool` | Pause after refine for human approval (`awaiting_approval` state) |
| `MILL_REVIEW_ENABLED` | `review_enabled` | `false` | `bool` | Enable dual-model code review stage before deliver |
| `MILL_REVIEW_MODEL` | `review_model` | `deepseek/deepseek-v4-pro` | `str` | Review agent model (see §1) |

---

## 7. Sandbox

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_SANDBOX_IMAGE` | `sandbox_image` | `python:3.14-slim` | `str` | Docker image for disposable sandbox containers |
| `MILL_SANDBOX_MEMORY` | `sandbox_memory` | `2g` | `str` | Memory limit for sandbox containers |
| `MILL_SANDBOX_PIDS_LIMIT` | `sandbox_pids_limit` | `512` | `int` | PID limit for sandbox containers |
| `MILL_SANDBOX_READONLY` | `sandbox_readonly` | `true` | `bool` | Mount sandbox rootfs read-only (except tmpfs `/tmp`) |
| `MILL_COMMAND_TIMEOUT` | `command_timeout` | `900` | `int` | Wall-clock cap (seconds) for sandbox shell/test commands |
| `MILL_DATA_VOLUME` | `data_volume` | `mill_data` | `str` | Named Docker volume for data (fallback when not bind-mounted) |
| `MILL_SANDBOX_DATA_MOUNT` | `sandbox_data_mount` | `None` | `str \| None` | Host path for bind-mounted data directory (overrides `MILL_DATA_VOLUME`) |

---

## 8. Web research

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_WEB_SEARCH` | `web_search` | `true` | `bool` | Enable web-search capability (delegated to sub-agent) |
| `MILL_WEB_RESEARCH_MODEL` | `web_research_model` | `deepseek/deepseek-v4-pro` | `str` | Web-research sub-agent model (see §1) |
| `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `web_research_request_limit` | `8` | `int` | Per-call request cap for web research (see §2) |
| `MILL_FETCH_IMAGE` | `fetch_image` | `curlimages/curl:8.17.0` | `str` | Docker image for isolated `web_fetch` container |
| `MILL_WEB_FETCH_MAX_BYTES` | `web_fetch_max_bytes` | `2000000` | `int` | Max bytes fetched per URL |
| `MILL_WEB_FETCH_TIMEOUT` | `web_fetch_timeout` | `30` | `int` | Timeout (seconds) per web fetch |

---

## 9. Pipeline tail (merge stage)

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_MERGE_POLL_SECONDS` | `merge_poll_seconds` | `120` | `int` | Poll interval for PR merge/CI status |
| `MILL_REBASE_MAX_ATTEMPTS` | `rebase_max_attempts` | `5` | `int` | Max rebase LLM invocations before BLOCK |
| `MILL_CI_FIX_MAX_ATTEMPTS` | `ci_fix_max_attempts` | `2` | `int` | Max CI-fix LLM invocations before BLOCK |

---

## 10. Retrospect

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_RETROSPECT_MODEL` | `retrospect_model` | `deepseek/deepseek-v4-pro` | `str` | Retrospect agent model (see §1) |
| `MILL_RETROSPECT_SPAWN_DRAFTS` | `retrospect_spawn_drafts` | `true` | `bool` | Allow retrospect to file improvement draft tickets |
| `MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY` | `retrospect_deep_analysis_frequency` | `10` | `int` | How many retrospect runs between deep trace analyses |
| `MILL_RETROSPECT_MEMORY_PATH` | `retrospect_memory_path` | `None` | `Path \| None` | Override path for retrospect memory ledger; defaults to `<data_dir>/retrospect_memory.md` |
| `MILL_TRACE_INSPECTOR_MODEL` | `trace_inspector_model` | `deepseek/deepseek-v4-pro` | `str` | Trace-inspector sub-agent model (see §1) |
| `MILL_TRACE_INSPECTOR_MEMORY_PATH` | `trace_inspector_memory_path` | `None` | `Path \| None` | Override path for trace-inspector memory; defaults to `<data_dir>/trace_inspector_memory.md` |

---

## 11. Audit agent

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_AUDIT_MODEL` | `audit_model` | `deepseek/deepseek-v4-pro` | `str` | Audit agent model (see §1) |
| `MILL_AUDIT_PERIODIC` | `audit_periodic` | `false` | `bool` | Enable periodic audit passes |
| `MILL_AUDIT_INTERVAL_SECONDS` | `audit_interval_seconds` | `3600` | `int` | Seconds between automatic audit passes |
| `MILL_AUDIT_MEMORY_PATH` | `audit_memory_path` | `None` | `Path \| None` | Override path for audit memory ledger; defaults to `<data_dir>/audit_memory.md` |

---

## 12. Trace-health

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_TRACE_HEALTH_PERIODIC` | `trace_health_periodic` | `false` | `bool` | Enable periodic trace-health checks |
| `MILL_TRACE_HEALTH_INTERVAL_SECONDS` | `trace_health_interval_seconds` | `86400` | `int` | Seconds between checks (minimum 3600 enforced in worker) |

---

## 13. Health agent

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_HEALTH_MODEL` | `health_model` | `deepseek/deepseek-v4-pro` | `str` | Health agent model (see §1) |
| `MILL_HEALTH_PERIODIC` | `health_periodic` | `false` | `bool` | Enable periodic codebase-health passes |
| `MILL_HEALTH_INTERVAL_SECONDS` | `health_interval_seconds` | `86400` | `int` | Seconds between automatic health passes |
| `MILL_HEALTH_MEMORY_PATH` | `health_memory_path` | `None` | `Path \| None` | Override path for health memory ledger; defaults to `<data_dir>/health_memory.md` |

---

## 14. Test-gap agent

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_TEST_GAP_MODEL` | `test_gap_model` | `deepseek/deepseek-v4-pro` | `str` | Test-gap agent model (see §1) |
| `MILL_TEST_GAP_PERIODIC` | `test_gap_periodic` | `false` | `bool` | Enable periodic test-gap passes |
| `MILL_TEST_GAP_INTERVAL_SECONDS` | `test_gap_interval_seconds` | `86400` | `int` | Seconds between automatic test-gap passes |
| `MILL_TEST_GAP_MEMORY_PATH` | `test_gap_memory_path` | `None` | `Path \| None` | Override path for test-gap memory ledger; defaults to `<data_dir>/test_gap_memory.md` |

---

## 15. Agent-check

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_AGENT_CHECK_MODEL` | `agent_check_model` | `deepseek/deepseek-v4-pro` | `str` | Agent-check model (see §1) |
| `MILL_AGENT_CHECK_PERIODIC` | `agent_check_periodic` | `false` | `bool` | Enable periodic agent-check passes |
| `MILL_AGENT_CHECK_INTERVAL_SECONDS` | `agent_check_interval_seconds` | `86400` | `int` | Seconds between checks (minimum 60 enforced in worker) |
| `MILL_AGENT_CHECK_MEMORY_PATH` | `agent_check_memory_path` | `None` | `Path \| None` | Override path for agent-check memory ledger; defaults to `<data_dir>/agent_check_memory.md` |

---

## 16. Survey

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_SURVEY_MODEL` | `survey_model` | `deepseek/deepseek-v4-pro` | `str` | Survey agent model (see §1) |
| `MILL_SURVEY_MEMORY_PATH` | `survey_memory_path` | `None` | `Path \| None` | Override path for survey memory ledger; defaults to `<data_dir>/survey_memory.md` |

---

## 17. Dedup guard

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_DEDUP_MODEL` | `dedup_model` | `deepseek/deepseek-v4-pro` | `str` | Dedup agent model (see §1) |
| `MILL_DEDUP_REQUEST_LIMIT` | `dedup_request_limit` | `4` | `int` | Per-call request cap (see §2) |
| `MILL_DEDUP_LOOKBACK_DAYS` | `dedup_lookback_days` | `30` | `int` | Days back to consider closed tickets as dup candidates |
| `MILL_DEDUP_LOOKBACK_COMMITS` | `dedup_lookback_commits` | `20` | `int` | Recent commits to inspect for "already done" |

---

## 18. Memory paths

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_IMPLEMENT_MEMORY_PATH` | `implement_memory_path` | `None` | `Path \| None` | Override path for implement memory; defaults to `<data_dir>/implement_memory.md` |
| `MILL_REFINE_MEMORY_PATH` | `refine_memory_path` | `None` | `Path \| None` | Override path for refine memory; defaults to `<data_dir>/refine_memory.md` |
| `MILL_CI_FIX_MEMORY_PATH` | `ci_fix_memory_path` | `None` | `Path \| None` | Override path for CI-fix memory; defaults to `<data_dir>/ci_fix_memory.md` |
| `MILL_REBASE_MEMORY_PATH` | `rebase_memory_path` | `None` | `Path \| None` | Override path for rebase memory; defaults to `<data_dir>/rebase_memory.md` |
| `MILL_MAX_MEMORY_CHARS` | `max_memory_chars` | `8000` | `int` | Max characters loaded from any memory ledger per agent pass |

---

## 19. Delivery

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_BRANCH_PREFIX` | `branch_prefix` | `mill/` | `str` | Prefix for deliver-stage branch names |
| `MILL_TEST_COMMAND` | `test_command` | `pytest -q` | `str` | Command run to verify the implementation (empty = skip) |
| `MILL_GITHUB_API_URL` | `github_api_url` | `https://api.github.com` | `str` | GitHub API base URL (override for GitHub Enterprise) |

---

## 20. CI monitor

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_CI_MONITOR_PERIODIC` | `ci_monitor_periodic` | `false` | `bool` | Enable periodic target-branch CI failure monitoring |
| `MILL_CI_MONITOR_INTERVAL_SECONDS` | `ci_monitor_interval_seconds` | `3600` | `int` | Seconds between CI monitor polls |
| `MILL_CI_LOG_MAX_BYTES` | `ci_log_max_bytes` | `65536` | `int` | Max bytes fetched per CI job log |

---

## 21. Skills

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `MILL_SKILLS_DIR` | `skills_dir` | `skills` | `Path` | Directory of skill docs injected into agent system prompts |

---

## Non-prefixed vars

These are consumed by `Settings` but use conventional names without the `MILL_` prefix.

| Env var | Python field | Default | Type | Description |
|---|---|---|---|---|
| `OPENROUTER_API_KEY` | `openrouter_api_key` | `None` | `str \| None` | OpenRouter API key (required for any LLM call) |
| `FORGE_KIND` | `forge_kind` | `none` | `Literal["github","gitlab","none"]` | Forge platform for delivery |
| `FORGE_REMOTE_URL` | `forge_remote_url` | `None` | `str \| None` | Remote URL for clone + push |
| `FORGE_TOKEN` | `forge_token` | `None` | `str \| None` | PAT for forge authentication |
| `FORGE_TARGET_BRANCH` | `forge_target_branch` | `main` | `str` | Target branch for PRs |
| `FORGE_AUTH` | `forge_auth` | `token` | `Literal["token","app"]` | Auth mode: `token` (PAT) or `app` (GitHub App) |
| `GITHUB_APP_ID` | `github_app_id` | `None` | `str \| None` | GitHub App ID (when `FORGE_AUTH=app`) |
| `GITHUB_APP_PRIVATE_KEY` | `github_app_private_key` | `None` | `str \| None` | GitHub App private key (inline) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | `github_app_private_key_path` | `None` | `str \| None` | GitHub App private key (file path) |
| `LANGFUSE_BASE_URL` | `langfuse_base_url` | `None` | `str \| None` | Langfuse base URL (tracing) |
| `LANGFUSE_PUBLIC_KEY` | `langfuse_public_key` | `None` | `str \| None` | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | `langfuse_secret_key` | `None` | `str \| None` | Langfuse secret key |
| `LANGFUSE_PROJECT_ID` | `langfuse_project_id` | `None` | `str \| None` | Langfuse project ID (optional) |
| `NTFY_URL` | `ntfy_url` | `None` | `str \| None` | ntfy.sh topic URL for notifications |
| `NTFY_TOKEN` | `ntfy_token` | `None` | `str \| None` | ntfy.sh bearer token (optional) |

---

## See also

- [README.md](../README.md) — project overview and quickstart
- [docs/agents.md](agents.md) — maps every model var to its agent
- [`.env`](../.env) — committed canonical config with production defaults
- [`secrets.env.example`](../secrets.env.example) — credentials template
