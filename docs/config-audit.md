# Configuration Audit

> **Date**: 2026-05-23
> **Scope**: Every configuration value consumed in the repository — inventory, provenance, sensitivity, and cross-reference discrepancies between code, `.env`, and documentation.
> **Status quo**: `pydantic-settings` `BaseSettings` model (`config.py:Settings`, 113 `Field()` definitions) loaded from `.env` + `secrets.env`, plus raw `os.environ` bypasses, Dockerfile `ENV` hardcodes, and `docker-compose.yml` overrides.

---

## 1. Complete Inventory

Each entry lists: env-var alias, Python field name, default, type, source(s), sensitivity, presence in `.env`, presence in `docs/configuration.md`, consumers, and notes.

### 1.1 Core Models

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `OPENROUTER_API_KEY` | `openrouter_api_key` | `None` | `str\|None` | secret | absent | §Non-prefixed | config.py, all agent files via `Settings` | In `secrets.env.example`; required for any LLM call |
| `MILL_MODEL` | `model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | **active** | §1 | config.py, coordinator agent | Coordinator model |
| `MILL_EXPLORE_MODEL` | `explore_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1 | config.py, explore sub-agent | Commented out in `.env`, defaults to same as coordinator |
| `MILL_TEST_MODEL` | `test_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1 | config.py, test sub-agent | |
| `MILL_REFINE_MODEL` | `refine_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1 | config.py, refine agent | |
| `MILL_ANSWER_MODEL` | `answer_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | absent | §1 | config.py, answer agent | Not mentioned in `.env` at all |
| `MILL_RETROSPECT_MODEL` | `retrospect_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §10 | config.py, retrospect agent | |
| `MILL_AUDIT_MODEL` | `audit_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §11 | config.py, audit agent | |
| `MILL_DEDUP_MODEL` | `dedup_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §17 | config.py, dedup agent | |
| `MILL_TRIAGE_MODEL` | `triage_model` | `openai/gpt-4o-mini` | `str` | non-sensitive | absent | **missing** | config.py, refine-triage pass | Undocumented; cheap classification model for pre-refine triage |
| `MILL_WEB_RESEARCH_MODEL` | `web_research_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §8 | config.py, web-research sub-agent | |
| `MILL_REVIEW_MODEL` | `review_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | **active** | §1, §6 | config.py, review agent | Overridden to same value as default in `.env` |
| `MILL_TRACE_INSPECTOR_MODEL` | `trace_inspector_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §10 | config.py, trace-inspector sub-agent | |
| `MILL_TEST_GAP_MODEL` | `test_gap_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §14 | config.py, test-gap agent | |
| `MILL_AGENT_CHECK_MODEL` | `agent_check_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §15 | config.py, agent-check agent | |
| `MILL_HEALTH_MODEL` | `health_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §13 | config.py, health agent | |
| `MILL_SURVEY_MODEL` | `survey_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | §1, §16 | config.py, survey agent | |
| `MILL_RATE_LIMIT_FALLBACK_MODEL` | `rate_limit_fallback_model` | `""` (empty) | `str` | non-sensitive | **active** (empty) | §1, §4 | config.py | Set to empty string (disabled) in `.env` |
| `MILL_DOC_MODEL` | `doc_model` | `deepseek/deepseek-v4-pro` | `str` | non-sensitive | commented-out | **missing** | config.py, documentation agent | Undocumented |
| `MILL_AUTO_APPROVE_MODEL` | `auto_approve_model` | `openai/gpt-4o-mini` | `str` | non-sensitive | commented-out | §6 | config.py, auto-approve triage | |

### 1.2 Request Limits

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_COORDINATOR_REQUEST_LIMIT` | `coordinator_request_limit` | `200` | `int` | non-sensitive | commented-out | §2 | config.py | |
| `MILL_EXPLORE_REQUEST_LIMIT` | `explore_request_limit` | `20` | `int` | non-sensitive | commented-out | §2 | config.py | |
| `MILL_TEST_REQUEST_LIMIT` | `test_request_limit` | `8` | `int` | non-sensitive | commented-out | §2 | config.py | |
| `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `web_research_request_limit` | `8` | `int` | non-sensitive | **active** | §2, §8 | config.py | |
| `MILL_DEDUP_REQUEST_LIMIT` | `dedup_request_limit` | `4` | `int` | non-sensitive | commented-out | §2, §17 | config.py | |

### 1.3 Worker Pool & Safety Nets

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_MAX_CONCURRENCY` | `max_concurrency` | `4` | `int` | non-sensitive | commented-out | §3 | config.py | |
| `MILL_MAX_FIX_ITERATIONS` | `max_fix_iterations` | `8` | `int` | non-sensitive | commented-out | §3 | config.py | |
| `MILL_MAX_STUCK_CYCLES` | `max_stuck_cycles` | `3` | `int` | non-sensitive | **active** | §3 | config.py | |
| `MILL_MAX_SPEND_USD_PER_TICKET` | `max_spend_usd_per_ticket` | `0.0` | `float` | non-sensitive | **active** | §3 | config.py | Set to 0.0 (disabled) in `.env` |

### 1.4 Transient Retry & Timeout

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_MODEL_REQUEST_TIMEOUT` | `model_request_timeout` | `900.0` | `float` | non-sensitive | commented-out | §4 | config.py | |
| `MILL_TRANSIENT_RETRIES` | `transient_retries` | `4` | `int` | non-sensitive | commented-out | §4 | config.py | |
| `MILL_TRANSIENT_BACKOFF_BASE` | `transient_backoff_base` | `2.0` | `float` | non-sensitive | commented-out | §4 | config.py | |
| `MILL_TRANSIENT_BACKOFF_CAP` | `transient_backoff_cap` | `30.0` | `float` | non-sensitive | commented-out | §4 | config.py | |
| `MILL_RATE_LIMIT_BACKOFF_BASE` | `rate_limit_backoff_base` | `30.0` | `float` | non-sensitive | commented-out | §4 | config.py | |
| `MILL_RATE_LIMIT_BACKOFF_CAP` | `rate_limit_backoff_cap` | `120.0` | `float` | non-sensitive | commented-out | §4 | config.py | |
| `MILL_RATE_LIMIT_FALLBACK_RETRIES` | `rate_limit_fallback_retries` | `3` | `int` | non-sensitive | commented-out | §4 | config.py | |

### 1.5 Management Plane

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_DATA_DIR` | `data_dir` | `.mill-data` | `Path` | non-sensitive | commented-out | §5 | config.py, all `*_memory_file` properties | **Dockerfile hardcodes `ENV MILL_DATA_DIR=/data`** — overrides default |
| `MILL_API_HOST` | `api_host` | `127.0.0.1` | `str` | non-sensitive | commented-out | §5 | config.py | **Dockerfile hardcodes `ENV MILL_API_HOST=0.0.0.0`** |
| `MILL_API_PORT` | `api_port` | `8077` | `int` | non-sensitive | **active** | §5 | config.py | |
| `MILL_API_URL` | `api_url` | `http://127.0.0.1:8077` | `str` | identifying | **active** | §5 | config.py, CLI client | **Dockerfile hardcodes identical value**; `dev/mill-autoupdate.sh` also hardcodes `http://localhost:8077` |

### 1.6 Forge Delivery

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `FORGE_KIND` | `forge_kind` | `none` | `Literal["github","gitlab","none"]` | non-sensitive | **active** (`github`) | §Non-prefixed | config.py | |
| `FORGE_REMOTE_URL` | `forge_remote_url` | `None` | `str\|None` | identifying | **active** | §Non-prefixed | config.py | |
| `FORGE_TOKEN` | `forge_token` | `None` | `str\|None` | secret | absent | §Non-prefixed | config.py | In `secrets.env.example` |
| `FORGE_TARGET_BRANCH` | `forge_target_branch` | `main` | `str` | non-sensitive | **active** | §Non-prefixed | config.py | |
| `FORGE_AUTH` | `forge_auth` | `token` | `Literal["token","app"]` | non-sensitive | **active** (`app`) | §Non-prefixed | config.py | |
| `GITHUB_APP_ID` | `github_app_id` | `None` | `str\|None` | secret | absent | §Non-prefixed | config.py | In `secrets.env.example` |
| `GITHUB_APP_PRIVATE_KEY` | `github_app_private_key` | `None` | `str\|None` | secret | absent | §Non-prefixed | config.py | In `secrets.env.example` (inline PEM) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | `github_app_private_key_path` | `None` | `str\|None` | identifying | **active** | §Non-prefixed | config.py, `docker-compose.yml` (var sub) | Path consumed by both Settings and compose volume mount |
| `MILL_GITHUB_API_URL` | `github_api_url` | `https://api.github.com` | `str` | identifying | commented-out | §19 | config.py | |
| `MILL_GITLAB_API_URL` | `gitlab_api_url` | `https://gitlab.com/api/v4` | `str` | identifying | absent | **missing** | config.py | Undocumented |

### 1.7 Implement Stage

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_TEST_COMMAND` | `test_command` | `pytest -q` | `str` | non-sensitive | **active** | §19 | config.py | |
| `MILL_BRANCH_PREFIX` | `branch_prefix` | `mill/` | `str` | non-sensitive | **active** | §19 | config.py | |
| `MILL_COMMAND_TIMEOUT` | `command_timeout` | `900` | `int` | non-sensitive | **active** | §7 | config.py | Listed in sandbox section of docs |

### 1.8 Command Sandbox

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_SANDBOX_IMAGE` | `sandbox_image` | `python:3.14-slim` | `str` | non-sensitive | **active** | §7 | config.py | |
| `MILL_SANDBOX_MEMORY` | `sandbox_memory` | `2g` | `str` | non-sensitive | commented-out | §7 | config.py | |
| `MILL_SANDBOX_PIDS_LIMIT` | `sandbox_pids_limit` | `512` | `int` | non-sensitive | commented-out | §7 | config.py | |
| `MILL_SANDBOX_READONLY` | `sandbox_readonly` | `true` | `bool` | non-sensitive | commented-out | §7 | config.py | |
| `MILL_DATA_VOLUME` | `data_volume` | `mill_data` | `str` | non-sensitive | **active** | §7 | config.py | |
| `MILL_SANDBOX_DATA_MOUNT` | `sandbox_data_mount` | `None` | `str\|None` | identifying | commented-out | §7 | config.py, `docker-compose.yml` | **docker-compose sets `MILL_SANDBOX_DATA_MOUNT=${PWD}/.data`**; cleared in `tests/conftest.py:_no_dotenv` |

### 1.9 Web Research & Fetch

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_WEB_SEARCH` | `web_search` | `true` | `bool` | non-sensitive | **active** | §8 | config.py | |
| `MILL_FETCH_IMAGE` | `fetch_image` | `curlimages/curl:8.17.0` | `str` | non-sensitive | **active** | §8 | config.py | |
| `MILL_WEB_FETCH_MAX_BYTES` | `web_fetch_max_bytes` | `2000000` | `int` | non-sensitive | **active** | §8 | config.py | |
| `MILL_WEB_FETCH_TIMEOUT` | `web_fetch_timeout` | `30` | `int` | non-sensitive | **active** | §8 | config.py | |

### 1.10 Skills

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_SKILLS_DIR` | `skills_dir` | `skills` | `Path` | non-sensitive | **active** | §21 | config.py | |

### 1.11 Approval & Review Gate

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_REQUIRE_APPROVAL` | `require_approval` | `true` | `bool` | non-sensitive | **active** | §6 | config.py | |
| `MILL_AUTO_APPROVE_ENABLED` | `auto_approve_enabled` | `false` | `bool` | non-sensitive | **active** (`true`) | §6 | config.py | `.env` overrides default from `false`→`true` |
| `MILL_REVIEW_ENABLED` | `review_enabled` | `false` | `bool` | non-sensitive | **active** (`true`) | §6 | config.py | `.env` overrides default from `false`→`true` |
| `MILL_AUTO_MERGE_ENABLED` | `auto_merge_enabled` | `false` | `bool` | non-sensitive | **active** (`true`) | **missing** | config.py | Undocumented; `.env` overrides default |
| `MILL_REFINE_TRIAGE_ENABLED` | `refine_triage_enabled` | `true` | `bool` | non-sensitive | absent | **missing** | config.py | Undocumented |
| `MILL_SPEC_REVIEW_ENABLED` | `spec_review_enabled` | `false` | `bool` | non-sensitive | absent | **missing** | config.py | Undocumented |
| `MILL_REVIEW_MAX_ROUNDS` | `review_max_rounds` | `3` | `int` | non-sensitive | absent | **missing** | config.py | Undocumented |

### 1.12 Retrospect Stage

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_RETROSPECT_SPAWN_DRAFTS` | `retrospect_spawn_drafts` | `true` | `bool` | non-sensitive | commented-out | §10 | config.py | |
| `MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY` | `retrospect_deep_analysis_frequency` | `10` | `int` | non-sensitive | commented-out | §10 | config.py | |
| `MILL_RETROSPECT_MEMORY_PATH` | `retrospect_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §10 | config.py, retrospect agent | |
| `MILL_TRACE_INSPECTOR_MEMORY_PATH` | `trace_inspector_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §10 | config.py, trace inspector | |

### 1.13 Pipeline Tail (Merge Stage)

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_MERGE_POLL_SECONDS` | `merge_poll_seconds` | `120` | `int` | non-sensitive | commented-out | §9 | config.py | |
| `MILL_REBASE_MAX_ATTEMPTS` | `rebase_max_attempts` | `5` | `int` | non-sensitive | commented-out | §9 | config.py | `.env` comment says default 2 but actual Field default is 5 |
| `MILL_CI_FIX_MAX_ATTEMPTS` | `ci_fix_max_attempts` | `2` | `int` | non-sensitive | commented-out | §9 | config.py | |
| `MILL_PRUNE_CLONE_ON_CLOSE` | `prune_clone_on_close` | `true` | `bool` | non-sensitive | commented-out | **missing** | config.py | Undocumented |
| `MILL_MAX_ARCHIVED_TICKETS` | `max_archived_tickets` | `100` | `int` | non-sensitive | absent | **missing** | config.py | Undocumented |

### 1.14 CI Monitor

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_CI_MONITOR_PERIODIC` | `ci_monitor_periodic` | `false` | `bool` | non-sensitive | commented-out | §20 | config.py, `docker-compose.yml` | **docker-compose sets `MILL_CI_MONITOR_PERIODIC=true`** |
| `MILL_CI_MONITOR_INTERVAL_SECONDS` | `ci_monitor_interval_seconds` | `3600` | `int` | non-sensitive | commented-out | §20 | config.py, `docker-compose.yml` | **docker-compose sets `MILL_CI_MONITOR_INTERVAL_SECONDS=600`** |
| `MILL_CI_LOG_MAX_BYTES` | `ci_log_max_bytes` | `65536` | `int` | non-sensitive | commented-out | §20 | config.py | |

### 1.15 Periodic Agents: Audit

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_AUDIT_PERIODIC` | `audit_periodic` | `false` | `bool` | non-sensitive | **active** (`true`) | §11 | config.py | `.env` overrides default |
| `MILL_AUDIT_INTERVAL_SECONDS` | `audit_interval_seconds` | `3600` | `int` | non-sensitive | **active** (`604800`) | §11 | config.py | Overridden to 7 days in `.env` |
| `MILL_AUDIT_MEMORY_PATH` | `audit_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §11 | config.py, audit agent | |

### 1.16 Periodic Agents: Trace-Health

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_TRACE_HEALTH_PERIODIC` | `trace_health_periodic` | `false` | `bool` | non-sensitive | **active** (`true`) | §12 | config.py | `.env` overrides default |
| `MILL_TRACE_HEALTH_INTERVAL_SECONDS` | `trace_health_interval_seconds` | `86400` | `int` | non-sensitive | **active** (`604800`) | §12 | config.py | Overridden to 7 days in `.env` |

### 1.17 Periodic Agents: Test-Gap

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_TEST_GAP_PERIODIC` | `test_gap_periodic` | `false` | `bool` | non-sensitive | commented-out | §14 | config.py | |
| `MILL_TEST_GAP_INTERVAL_SECONDS` | `test_gap_interval_seconds` | `86400` | `int` | non-sensitive | commented-out | §14 | config.py | |
| `MILL_TEST_GAP_MEMORY_PATH` | `test_gap_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §14 | config.py, test-gap agent | |

### 1.18 Periodic Agents: Agent-Check

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_AGENT_CHECK_PERIODIC` | `agent_check_periodic` | `false` | `bool` | non-sensitive | commented-out | §15 | config.py | |
| `MILL_AGENT_CHECK_INTERVAL_SECONDS` | `agent_check_interval_seconds` | `86400` | `int` | non-sensitive | commented-out | §15 | config.py | |
| `MILL_AGENT_CHECK_MEMORY_PATH` | `agent_check_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §15 | config.py, agent-check agent | Also present as empty assignment in `.env` |

### 1.19 Periodic Agents: Health

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_HEALTH_PERIODIC` | `health_periodic` | `false` | `bool` | non-sensitive | commented-out | §13 | config.py | |
| `MILL_HEALTH_INTERVAL_SECONDS` | `health_interval_seconds` | `86400` | `int` | non-sensitive | commented-out | §13 | config.py | |
| `MILL_HEALTH_MEMORY_PATH` | `health_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §13 | config.py, health agent | |

### 1.20 Periodic Agents: Survey

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_SURVEY_PERIODIC` | `survey_periodic` | `true` | `bool` | non-sensitive | **active** | **missing** | config.py | Undocumented; default is `true` ("default yes") |
| `MILL_SURVEY_INTERVAL_SECONDS` | `survey_interval_seconds` | `604800` | `int` | non-sensitive | **active** | **missing** | config.py | Undocumented |
| `MILL_SURVEY_MEMORY_PATH` | `survey_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §16 | config.py, survey agent | |

### 1.21 Memory Paths

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_MAX_MEMORY_CHARS` | `max_memory_chars` | `8000` | `int` | non-sensitive | commented-out | §18 | config.py | |
| `MILL_IMPLEMENT_MEMORY_PATH` | `implement_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §18 | config.py, implement agent | |
| `MILL_REFINE_MEMORY_PATH` | `refine_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §18 | config.py, refine agent | |
| `MILL_CI_FIX_MEMORY_PATH` | `ci_fix_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §18 | config.py, ci-fix agent | |
| `MILL_REBASE_MEMORY_PATH` | `rebase_memory_path` | `None` | `Path\|None` | non-sensitive | commented-out | §18 | config.py, rebase agent | |

### 1.22 Dedup Guard

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `MILL_DEDUP_LOOKBACK_DAYS` | `dedup_lookback_days` | `30` | `int` | non-sensitive | commented-out | §17 | config.py | |
| `MILL_DEDUP_LOOKBACK_COMMITS` | `dedup_lookback_commits` | `20` | `int` | non-sensitive | commented-out | §17 | config.py | |

### 1.23 Tracing

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `LANGFUSE_BASE_URL` | `langfuse_base_url` | `None` | `str\|None` | identifying | **active** (empty) | §Non-prefixed | config.py + `tracing.py` (raw `os.environ`) | **Dual-source**: read via Settings AND `os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")` in tracing.py |
| `LANGFUSE_PUBLIC_KEY` | `langfuse_public_key` | `None` | `str\|None` | secret | absent | §Non-prefixed | config.py + `tracing.py` (raw `os.environ`) | **Dual-source**: in `secrets.env.example`; `tracing.py` reads from `os.environ` directly |
| `LANGFUSE_SECRET_KEY` | `langfuse_secret_key` | `None` | `str\|None` | secret | absent | §Non-prefixed | config.py + `tracing.py` (raw `os.environ`) | **Dual-source**: in `secrets.env.example`; `tracing.py` reads from `os.environ` directly |
| `LANGFUSE_PROJECT_ID` | `langfuse_project_id` | `None` | `str\|None` | identifying | absent | §Non-prefixed | config.py | |

### 1.24 Notifications

| Env var | Field | Default | Type | Sensitivity | `.env` | Docs (§) | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|
| `NTFY_URL` | `ntfy_url` | `None` | `str\|None` | identifying | **active** (empty) | §Non-prefixed | config.py | |
| `NTFY_TOKEN` | `ntfy_token` | `None` | `str\|None` | secret | absent | §Non-prefixed | config.py | In `secrets.env.example` |

### 1.25 Non-Settings Variables

These are consumed from the environment but are **not** defined as `Field()` on `Settings`. They are read by Docker tooling, shell scripts, CI, or raw `os.environ`.

| Env var | Source | Sensitivity | `.env` | Consumers | Notes |
|---|---|---|---|---|---|
| `DOCKER_GID` | `.env` → `docker-compose.yml` variable substitution | non-sensitive | **active** (`999`) | `docker-compose.yml` (`group_add: "${DOCKER_GID:-999}"`), `dev/mill-autoupdate.sh` (computed + exported) | Not an app-config value; Docker orchestration knob. Ignored by `Settings` (via `extra="ignore"`). `mill-autoupdate.sh` computes it from `getent group docker` and exports it for `docker compose build`. |
| `GIT_BASE_REF` | `.github/workflows/ci.yml` | non-sensitive | absent | `tests/test_migration_guard.py` (raw `os.environ.get`) | CI-internal only; set by GitHub Actions workflow, never in `.env` |
| `SKIP_MIGRATION_GUARD` | raw `os.environ` | non-sensitive | absent | `tests/test_migration_guard.py` (raw `os.environ.get`) | Ad-hoc escape hatch for CI migration guard; never in `.env` |

### 1.26 Dockerfile `ENV` Hardcodes vs. `.env` / Defaults

| Env var | Dockerfile value | Field default | `.env` value | Conflict? |
|---|---|---|---|---|
| `MILL_DATA_DIR` | `/data` | `.mill-data` | commented-out | **Dockerfile overrides default** — container always uses `/data` |
| `MILL_API_HOST` | `0.0.0.0` | `127.0.0.1` | commented-out | **Dockerfile overrides default** — container binds all interfaces |
| `MILL_API_URL` | `http://127.0.0.1:8077` | `http://127.0.0.1:8077` | **active** (same value) | No conflict — identical |

### 1.27 Docker Compose `environment:` Overrides

| Env var | docker-compose value | Field default | `.env` value | Override? |
|---|---|---|---|---|
| `MILL_SANDBOX_DATA_MOUNT` | `${PWD}/.data` | `None` | commented-out | **Yes** — compose always sets this; essential for sandbox sibling containers |
| `MILL_CI_MONITOR_PERIODIC` | `true` | `false` | commented-out (`false`) | **Yes** — compose enables CI monitor regardless of `.env` |
| `MILL_CI_MONITOR_INTERVAL_SECONDS` | `600` | `3600` | commented-out | **Yes** — compose shortens poll interval |

---

## 2. Cross-Reference Checks

### 2.1 Vars in `config.py` but missing from `docs/configuration.md`

The following 11 fields are defined in `Settings` but have no entry in `docs/configuration.md`:

| Env var | Python field | Default | Notes |
|---|---|---|---|
| `MILL_TRIAGE_MODEL` | `triage_model` | `openai/gpt-4o-mini` | Cheap pre-refine triage classification model |
| `MILL_DOC_MODEL` | `doc_model` | `deepseek/deepseek-v4-pro` | Documentation agent model |
| `MILL_PRUNE_CLONE_ON_CLOSE` | `prune_clone_on_close` | `true` | Delete workspace clone on ticket close |
| `MILL_MAX_ARCHIVED_TICKETS` | `max_archived_tickets` | `100` | Max terminal-state tickets to retain |
| `MILL_AUTO_MERGE_ENABLED` | `auto_merge_enabled` | `false` | Auto-merge green PRs via forge API |
| `MILL_REFINE_TRIAGE_ENABLED` | `refine_triage_enabled` | `true` | Skip full refine for implementation-ready drafts |
| `MILL_SPEC_REVIEW_ENABLED` | `spec_review_enabled` | `false` | Post-refinement spec review pass |
| `MILL_REVIEW_MAX_ROUNDS` | `review_max_rounds` | `3` | Max code-review round-trips before escalation |
| `MILL_SURVEY_PERIODIC` | `survey_periodic` | `true` | Enable periodic survey (default on) |
| `MILL_SURVEY_INTERVAL_SECONDS` | `survey_interval_seconds` | `604800` | Seconds between survey passes |
| `MILL_GITLAB_API_URL` | `gitlab_api_url` | `https://gitlab.com/api/v4` | GitLab API base URL |

**Note**: The ticket's known list was verified and is exhaustive — exactly these 11 are missing.

### 2.2 Vars in `.env` but absent from `config.py`

| Env var | `.env` value | Notes |
|---|---|---|
| `DOCKER_GID` | `999` | Consumed by `docker-compose.yml` via `${DOCKER_GID:-999}` variable substitution and computed/exported by `dev/mill-autoupdate.sh`. Ignored by `Settings` (via `model_config.extra="ignore"`). This is a Docker orchestration knob, not app config. |

### 2.3 Vars read via raw `os.environ` bypassing `Settings`

| File | Variable(s) | How read | Notes |
|---|---|---|---|
| `src/robotsix_mill/runtime/tracing.py` | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | `os.environ.get()` | `_tracing_enabled()` checks keys; `_ensure_tracing()` reads base URL. `init(settings)` is a no-op stub. |
| `tests/test_migration_guard.py` | `SKIP_MIGRATION_GUARD`, `GIT_BASE_REF` | `os.environ.get()` | CI-only ad-hoc vars; never intended for `Settings` |
| `tests/runtime/test_tracing.py` | `**os.environ` (all vars) | `subprocess.run(env=env)` | Test helper captures full env for subprocess; low risk but leaks ambient vars |

### 2.4 Dual-Source Vars (Settings + raw `os.environ`)

| Env var | Settings field | Raw `os.environ` location | Notes |
|---|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | `langfuse_public_key` | `tracing.py:_tracing_enabled()`, `tracing.py:_ensure_tracing()` | `tracing.py` bypasses `Settings` entirely — `init(settings)` is a no-op |
| `LANGFUSE_SECRET_KEY` | `langfuse_secret_key` | `tracing.py:_tracing_enabled()`, `tracing.py:_ensure_tracing()` | Same as above |
| `LANGFUSE_BASE_URL` | `langfuse_base_url` | `tracing.py:_ensure_tracing()` | Settings default is `None`; tracing.py falls back to `"https://cloud.langfuse.com"` |

### 2.5 Dockerfile Hardcodes vs. Defaults

| Variable | Dockerfile | Field Default | Impact |
|---|---|---|---|
| `MILL_DATA_DIR` | `/data` | `.mill-data` | Container always uses `/data`; local dev uses `.mill-data` |
| `MILL_API_HOST` | `0.0.0.0` | `127.0.0.1` | Container binds all interfaces; local dev binds loopback only |
| `MILL_API_URL` | `http://127.0.0.1:8077` | identical | No divergence |

### 2.6 Docker Compose-Only Overrides

| Variable | Compose Value | Field Default | Notes |
|---|---|---|---|
| `MILL_SANDBOX_DATA_MOUNT` | `${PWD}/.data` | `None` | Required for sandbox sibling containers to mount the host data dir |
| `MILL_CI_MONITOR_PERIODIC` | `true` | `false` | Compose enables CI monitoring unconditionally |
| `MILL_CI_MONITOR_INTERVAL_SECONDS` | `600` | `3600` | Compose uses shorter 10-min poll interval |

---

## 3. Requirements Summary

### 3.1 Desired Structure

The current configuration is a single flat namespace of 113 `Field()` definitions on one `Settings` class, loaded from two flat `.env`-format files. There is no grouping beyond code comments (`# --- core ---`, `# --- management-plane service ---`, etc.). A replacement should provide:

- **Logical grouping**: core (models, limits, retry), secrets (API keys, tokens), forge (delivery), sandbox, periodic agents (audit, health, survey, test-gap, agent-check, trace-health, CI monitor), memory paths, and pipeline (approval, review, merge, retrospect) — each as a separate config section or file.
- **Per-environment overrides**: dev (`.env` + `secrets.env`), CI (minimal — only `GIT_BASE_REF`), staging/prod (docker-compose with mounted files and explicit `environment:` overrides).
- **Separation of orchestration variables** (`DOCKER_GID`) from application config.

### 3.2 Type Safety Gaps

- **No range validation**: `max_concurrency` has no upper bound; `model_request_timeout` has no minimum; `transient_retries` accepts negative values silently.
- **No cross-field consistency**: `FORGE_AUTH=app` with `GITHUB_APP_ID` unset would fail at runtime, not at config-load time. Similarly, `FORGE_KIND=github` + `FORGE_AUTH=token` but `FORGE_TOKEN` unset.
- **No required-if logic**: `require_approval=true` but `auto_approve_enabled` interacting with `refine_triage_enabled` and `spec_review_enabled` creates complex state-machine interactions with no validation.
- **`extra="ignore"`** silently discards unknown env vars — `DOCKER_GID` and any typos are swallowed without warning.

### 3.3 Secrets Management

- Secrets currently live in `os.environ` after `pydantic-settings` loads `secrets.env`. Any library import (`import robotsix_mill.runtime.tracing`) can read them via `os.environ` — and `tracing.py` actually does.
- `secrets.env` is git-ignored but the flat format offers no protection against accidental logging, debug-print leakage, or subprocess inheritance.
- Seven fields are classified as **secret**: `OPENROUTER_API_KEY`, `FORGE_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `NTFY_TOKEN`.
- Three fields are **identifying** (URLs, paths): `FORGE_REMOTE_URL`, `GITHUB_APP_PRIVATE_KEY_PATH`, `LANGFUSE_BASE_URL`, `LANGFUSE_PROJECT_ID`, `NTFY_URL`, `MILL_GITHUB_API_URL`, `MILL_GITLAB_API_URL`, `MILL_API_URL`, `MILL_SANDBOX_DATA_MOUNT`.

A dedicated secrets path (file, vault, or OS keyring) with access gating is needed so secrets aren't ambiently available to every module.

### 3.4 Environment Story

| Environment | Current mechanism | Gaps |
|---|---|---|
| **Dev (local)** | `.env` + `secrets.env` loaded by `pydantic-settings`; `Settings` defaults for everything else | Works but fragile — any missing `.env` key falls back to Field default silently |
| **CI** | Only `GIT_BASE_REF` set by workflow; `_no_dotenv` fixture disables `.env` loading and clears credential vars | Hermetic but brittle — the list of vars cleared in `conftest.py` must be manually synced with `Settings` |
| **Staging/Prod (Docker)** | `docker-compose.yml`: `env_file` chain (`.env` + optional `secrets.env`), explicit `environment:` overrides, bind-mounted secrets | `MILL_SANDBOX_DATA_MOUNT` must be set in compose or sandbox breaks; `DOCKER_GID` must match host; `.env` values can be accidentally overridden by compose `environment:` |

### 3.5 Documentation Sync Gap

- **11 fields** defined in `config.py` have no entry in `docs/configuration.md` (see §2.1).
- The docs are hand-maintained and have no mechanical cross-check against the code.
- `.env` comments sometimes disagree with Field defaults: e.g. `.env` says `MILL_REBASE_MAX_ATTEMPTS` default is 2, but the Field default is 5.
- `docs/configuration.md` §6 "Approval & review" is missing `MILL_AUTO_MERGE_ENABLED`, `MILL_REFINE_TRIAGE_ENABLED`, `MILL_SPEC_REVIEW_ENABLED`, `MILL_REVIEW_MAX_ROUNDS`, and `MILL_DOC_MODEL`.
- `docs/configuration.md` §16 "Survey" is missing `MILL_SURVEY_PERIODIC` and `MILL_SURVEY_INTERVAL_SECONDS`.

Documentation should be generated from the schema or at least mechanically cross-checked in CI.

### 3.6 Raw `os.environ` Bypass

- **`tracing.py`** reads `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL` directly from `os.environ` — `init(settings: Settings)` is a documented no-op.
- This means tracing can activate based on env vars even if `Settings` hasn't been loaded, and the `Settings.tracing_enabled` property may disagree with `tracing._tracing_enabled()`.
- The fix: `tracing.py` should read from a `Settings` instance or a dedicated config accessor, not from `os.environ` directly. The current lazy-init pattern could use `Settings` as the single source of truth for tracing configuration.
- **`test_migration_guard.py`** reads `SKIP_MIGRATION_GUARD` and `GIT_BASE_REF` from raw `os.environ` — these are CI-ad-hoc and acceptable as-is, but should be documented as non-`Settings` escapes.

---

## Appendix A: Source Files Surveyed

| File | Lines | Role | Surveyed |
|---|---|---|---|
| `src/robotsix_mill/config.py` | 428 | Canonical `Settings` model (113 `Field()` definitions) | ✓ |
| `.env` | 185 | Committed canonical config template | ✓ |
| `secrets.env.example` | 20 | Template for gitignored credentials | ✓ |
| `Dockerfile` | 125 | Multi-stage build with `ENV` hardcodes | ✓ |
| `docker-compose.yml` | 54 | Runtime overrides and volume mounts | ✓ |
| `docs/configuration.md` | ~280 | Human-facing env-var reference (21 sections + non-prefixed) | ✓ |
| `src/robotsix_mill/runtime/tracing.py` | 234 | Raw `os.environ` reads for Langfuse | ✓ |
| `tests/conftest.py` | 121 | Hermetic fixtures clearing credential vars | ✓ |
| `tests/test_migration_guard.py` | 92 | Raw `os.environ` reads for CI escape hatches | ✓ |
| `dev/mill-autoupdate.sh` | 155 | Snapshots `.env`, exports `DOCKER_GID` | ✓ |
| `.github/workflows/ci.yml` | 31 | Sets `GIT_BASE_REF` | ✓ |
| `tests/runtime/test_tracing.py` | ~200 | Captures `**os.environ` for subprocess | ✓ |
| `entrypoint.sh` | 5 | No env vars set | ✓ |

## Appendix B: Field Count Summary

- **113** `Field()` definitions on `Settings`
- **11** `@property` computed fields (derived from Field values, not env vars)
- **3** non-`Settings` env vars (`DOCKER_GID`, `GIT_BASE_REF`, `SKIP_MIGRATION_GUARD`)
- **7** secret-classified vars
- **11** undocumented vars (in code but not in `docs/configuration.md`)
- **1** `.env`-only var not in `Settings` (`DOCKER_GID`)
- **3** vars with dual-source reads (Settings + raw `os.environ`)
