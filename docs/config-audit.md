# Configuration Audit

> **SUPERSEDED (historical).** This audit predates the single-file config
> migration. The mill now reads ONE file — `config/config.json` (else the
> committed `config/config.example.json`), with secrets under a top-level
> `secrets:` block. The multi-file layout described below
> (`mill.defaults.yaml` / `mill.local.yaml` / `mill.production.yaml` /
> `secrets.yaml`) no longer exists. See `docs/configuration.md` for the
> current model; this doc is kept for historical analysis only.

> Generated from a full-repo survey of `config.py`, `config/config.example.json`,
> `docs/configuration.md`, `Dockerfile`,
> `docker-compose.yml`, CI workflows, shell scripts, and all source files
> that read config values.
>
> **Date**: 2026-05-23

---

## 1. Complete Inventory

Every configuration value consumed anywhere in the repo.  **114** env-var
aliases are defined on `Settings` (`config.py`); the table below includes
every one plus the Docker‑/compose‑only vars and computed properties that
other code depends on.

### Legend

| Column | Meaning |
|--------|---------|
| **Env var** | Wire name (`alias=` on `Field`) or `—` for computed-only |
| **Field** | Python attribute on `Settings` (computed `@property` in *italics*) |
| **Default** | `Field(default=…)` unless overridden by Dockerfile/compose/CI |
| **Type** | Pydantic type annotation |
| **Source** | `Settings` (Field), `os.environ` (raw `os.environ.get`), `Dockerfile`, `compose`, `CI`, `compose-subst` (variable substitution in docker-compose.yml) |
| **Sensitivity** | `secret` · `identifying` · `non-sensitive` |
| **YAML** | `default` (in `mill.defaults.yaml`) / `secret` (in `secrets.yaml`) / `absent` |
| **Docs** | `§N` reference in `configuration.md` or `missing` |
| **Consumers** | Files that read this value |
| **Notes** | Any cross-reference caveat |

---

### 1.1  Core — API keys & secrets

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `OPENROUTER_API_KEY` | `openrouter_api_key` | `None` | `str\|None` | Settings | **secret** | absent | Non-prefixed | `config.py`, all agents via `Settings()` | Set only in `secrets.env`; absent in `.env` |
| `FORGE_TOKEN` | `forge_token` | `None` | `str\|None` | Settings | **secret** | absent | Non-prefixed | `forge/auth.py`, `forge/base.py` | PAT alternative to GitHub App; set in `secrets.env` |
| `GITHUB_APP_ID` | `github_app_id` | `None` | `str\|None` | Settings | **secret** | absent | Non-prefixed | `forge/auth.py` | Set in `secrets.env` |
| `GITHUB_APP_PRIVATE_KEY` | `github_app_private_key` | `None` | `str\|None` | Settings | **secret** | absent | Non-prefixed | `forge/auth.py` | Inline PEM; alternative to `*_PATH` |
| `GITHUB_APP_PRIVATE_KEY_PATH` | `github_app_private_key_path` | `None` | `str\|None` | Settings | **secret** | active | Non-prefixed | `forge/auth.py`, `docker-compose.yml` | Host path; bind-mounted into container |
| `NTFY_TOKEN` | `ntfy_token` | `None` | `str\|None` | Settings | **secret** | absent | Non-prefixed | `notify.py` | Set in `secrets.env` |

### 1.2  Core — model selection

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_MODEL` | `model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1 | All agent files via `Settings()` | Coordinator |
| `MILL_EXPLORE_MODEL` | `explore_model` | `deepseek/deepseek-v4-flash` | `str` | Settings | non-sensitive | default | §1 | `stages/implement.py`, explore sub-agent | ⚠️ `docs/configuration.md` §1 states the wrong default (`deepseek/deepseek-v4-pro`); the code default is `-flash`. |
| `MILL_TEST_MODEL` | `test_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1 | Test distillation sub-agent | |
| `MILL_REFINE_MODEL` | `refine_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1 | `stages/refine.py` | |
| `MILL_ANSWER_MODEL` | `answer_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1 | `stages/answer.py` | |
| `MILL_RETROSPECT_MODEL` | `retrospect_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §10 | `stages/retrospect.py` | Mentioned in both §1 and §10 |
| `MILL_AUDIT_MODEL` | `audit_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §11 | `audit_runner.py` | Mentioned in both §1 and §11 |
| `MILL_DEDUP_MODEL` | `dedup_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §17 | `stages/refine.py` (pre-refine dedup) | Mentioned in both §1 and §17 |
| `MILL_WEB_RESEARCH_MODEL` | `web_research_model` | `deepseek/deepseek-v4-flash` | `str` | Settings | non-sensitive | default | §1, §8 | Web-research sub-agent | Mentioned in both §1 and §8 |
| `MILL_REVIEW_MODEL` | `review_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §6 | `stages/review.py` | Mentioned in both §1 and §6 |
| `MILL_TRACE_INSPECTOR_MODEL` | `trace_inspector_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §10 | Trace-inspector sub-agent | Mentioned in both §1 and §10 |
| `MILL_TEST_GAP_MODEL` | `test_gap_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §14 | `test_gap_runner.py` | Mentioned in both §1 and §14 |
| `MILL_AGENT_CHECK_MODEL` | `agent_check_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §15 | `agent_check_runner.py` | Mentioned in both §1 and §15 |
| `MILL_HEALTH_MODEL` | `health_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §13 | `health_runner.py` | Mentioned in both §1 and §13 |
| `MILL_SURVEY_MODEL` | `survey_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | §1, §16 | `survey_runner.py` | Mentioned in both §1 and §16 |
| `MILL_RATE_LIMIT_FALLBACK_MODEL` | `rate_limit_fallback_model` | `""` (empty = disabled) | `str` | Settings | non-sensitive | default | §1, §4 | `runtime/model.py` (retry logic) | Mentioned in both §1 and §4 |
| `MILL_TRIAGE_MODEL` | `triage_model` | `openai/gpt-4o-mini` | `str` | Settings | non-sensitive | default | **missing** | `stages/refine.py` (pre-refine triage) | ⚠️ Undocumented |
| `MILL_DOC_MODEL` | `doc_model` | `deepseek/deepseek-v4-pro` | `str` | Settings | non-sensitive | default | **missing** | `stages/documenting.py` | ⚠️ Undocumented |
| `MILL_DOC_CLASSIFIER_MODEL` | `doc_classifier_model` | `openai/gpt-4o-mini` | `str` | Settings | non-sensitive | default | §1 | `stages/document.py`, doc-classifier gate | Cheap classifier gate — inspects diff + spec to decide whether the change is user-facing |
| `MILL_AUTO_APPROVE_MODEL` | `auto_approve_model` | `openai/gpt-4o-mini` | `str` | Settings | non-sensitive | default | §6 | `stages/refine.py` | |
| `MILL_SCOPE_TRIAGE_MODEL` | `scope_triage_model` | `openai/gpt-4o-mini` | `str` | Settings | non-sensitive | default | §1 | `stages/implement.py`, `agents/scope_triage.py` | Cheap classifier for out-of-scope file changes |

### 1.3  Core — request limits & safety nets

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_PER_PASS_REQUEST_BUDGET` | `coordinator_request_limit` | `500` | `int` | Settings | non-sensitive | default | §2 | `stages/implement.py` | Hard upper bound 5000 |
| `MILL_TEST_REQUEST_LIMIT` | `test_request_limit` | `8` | `int` | Settings | non-sensitive | default | §2 | Test sub-agent | |
| `MILL_MAX_FIX_ITERATIONS` | `max_fix_iterations` | `8` | `int` | Settings | non-sensitive | default | §3 | `stages/implement.py` | |
| `MILL_MODEL_REQUEST_TIMEOUT` | `model_request_timeout` | `900.0` | `float` | Settings | non-sensitive | default | §4 | `runtime/model.py` | |
| `MILL_MAX_CONCURRENCY` | `max_concurrency` | `4` | `int` | Settings | non-sensitive | default | §3 | `runtime/worker.py` | |
| `MILL_RATE_LIMIT_FALLBACK_RETRIES` | `rate_limit_fallback_retries` | `3` | `int` | Settings | non-sensitive | default | §4 | `runtime/model.py` | |
| `MILL_EXPLORE_REQUEST_LIMIT` | `explore_request_limit` | `100` | `int` | Settings | non-sensitive | default | §2 | Explore sub-agent | |
| `MILL_CONSULT_REQUEST_LIMIT` | `consult_request_limit` | `15` | `int` | Settings | non-sensitive | default | §2 | Domain-expert consultation sub-agent (`consult_expert`) | |
| `MILL_DEDUP_REQUEST_LIMIT` | `dedup_request_limit` | `4` | `int` | Settings | non-sensitive | default | §2, §17 | Pre-refine dedup check | Mentioned in both §2 and §17 |
| `MILL_DOC_CLASSIFIER_REQUEST_LIMIT` | `doc_classifier_request_limit` | `3` | `int` | Settings | non-sensitive | default | §2 | `stages/document.py`, doc-classifier gate | Per-call cap for the cheap doc-classifier agent |
| `MILL_MAX_STUCK_CYCLES` | `max_stuck_cycles` | `3` | `int` | Settings | non-sensitive | default | §3 | `runtime/worker.py` | |
| `MILL_MAX_SPEND_USD_PER_TICKET` | `max_spend_usd_per_ticket` | `0.0` | `float` | Settings | non-sensitive | default | §3 | `runtime/worker.py` | `0.0` = disabled |
| `MILL_STAGE_TIMEOUT_SECONDS` | `stage_timeout_seconds` | `1800` | `int` | Settings | non-sensitive | default | §3 | `runtime/worker.py` | `≤ 0` disables |
| `MILL_STAGE_TIMEOUT_OVERRIDES` | `stage_timeout_overrides` | `{}` | `dict` | Settings | non-sensitive | default | §3 | `runtime/worker.py` | JSON dict; `"stage": 0` disables per-stage |
| `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `web_research_request_limit` | `8` | `int` | Settings | non-sensitive | default | §2, §8 | Web-research sub-agent | Mentioned in both §2 and §8 |

### 1.4  Core — memory & reference files

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_MAX_MEMORY_CHARS` | `max_memory_chars` | `8000` | `int` | Settings | non-sensitive | default | §18 | All memory-ledger reads | |
| `MILL_DEDUP_LOOKBACK_DAYS` | `dedup_lookback_days` | `30` | `int` | Settings | non-sensitive | default | §17 | `stages/refine.py` (dedup) | |

### 1.5  Management plane

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_DATA_DIR` | `data_dir` | `.mill-data` | `Path` | Settings + Dockerfile | identifying | commented-out | §5 | `core/db.py`, `runtime/api.py`, all `*_runner.py`, `cli.py` | Dockerfile overrides to `/data` |
| `MILL_DEFAULT_REPO_ID` | `default_repo_id` | `""` | `str` | Settings | non-sensitive | default | §6 | `core/service.py` (legacy ticket resolution) | Backward-compat: auto-assigned board_id for pre-migration tickets |
| `MILL_API_HOST` | `api_host` | `127.0.0.1` | `str` | Settings + Dockerfile | non-sensitive | commented-out | §5 | `runtime/api.py` | Dockerfile overrides to `0.0.0.0` |
| `MILL_API_PORT` | `api_port` | `8077` | `int` | Settings | non-sensitive | active (`8077`) | §5 | `runtime/api.py` | |
| `MILL_API_URL` | `api_url` | `http://127.0.0.1:8077` | `str` | Settings + Dockerfile | identifying | active | §5 | `cli.py` | Dockerfile sets same value |

### 1.6  Forge delivery

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `FORGE_KIND` | `forge_kind` | `none` | `Literal["github","gitlab","auto","none"]` | Settings | non-sensitive | default | Non-prefixed | `forge/base.py`, all `forge/*.py` | `auto` detects forge kind from remote URL hostname |
| `FORGE_REMOTE_URL` | `forge_remote_url` | `None` | `str\|None` | Settings | identifying | absent | Non-prefixed | `forge/base.py`, all `forge/*.py` | |
| `FORGE_TARGET_BRANCH` | `forge_target_branch` | `main` | `str` | Settings | non-sensitive | default | Non-prefixed | `forge/base.py` | |
| `FORGE_AUTH` | `forge_auth` | `token` | `Literal["token","app"]` | Settings | non-sensitive | default | Non-prefixed | `forge/auth.py` | |
| `MILL_GITHUB_API_URL` | `github_api_url` | `https://api.github.com` | `str` | Settings | identifying | default | §19 | `forge/github.py` | For GitHub Enterprise |
| `MILL_GITLAB_API_URL` | `gitlab_api_url` | `https://gitlab.com/api/v4` | `str` | Settings | identifying | default | **missing** | `forge/gitlab.py` | ⚠️ Undocumented |

### 1.7  Implement stage

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_TEST_COMMAND` | `test_command` | `pytest -q` | `str` | Settings | non-sensitive | default | §19 | `stages/implement.py`, `agents/testing.py` | Global fallback only. Precedence: per-repo `.robotsix-mill/config.yaml` `test_command` (read by `repo_settings.load_repo_test_command`) > `repos.yaml` per-repo `test_command` > this. |
| `MILL_BRANCH_PREFIX` | `branch_prefix` | `mill/` | `str` | Settings | non-sensitive | default | §19 | `forge/*.py` | |
| `MILL_COMMAND_TIMEOUT` | `command_timeout` | `1800` | `int` | Settings | non-sensitive | default | §7 | `sandbox.py` | Listed in §7 (sandbox), not §19 |
| `MILL_SKILLS_DIR` | `skills_dir` | `skills` | `Path` | Settings | non-sensitive | default | §21 | `stages/refine.py`, `stages/implement.py` | |

### 1.8  Command sandbox

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_SANDBOX_IMAGE` | `sandbox_image` | `python:3.14-slim` | `str` | Settings | non-sensitive | default | §7 | `sandbox.py` | |
| `MILL_SANDBOX_MEMORY` | `sandbox_memory` | `2g` | `str` | Settings | non-sensitive | default | §7 | `sandbox.py` | |
| `MILL_SANDBOX_PIDS_LIMIT` | `sandbox_pids_limit` | `512` | `int` | Settings | non-sensitive | default | §7 | `sandbox.py` | |
| `MILL_SANDBOX_READONLY` | `sandbox_readonly` | `true` | `bool` | Settings | non-sensitive | default | §7 | `sandbox.py` | |
| `MILL_DATA_VOLUME` | `data_volume` | `mill_data` | `str` | Settings | non-sensitive | default | §7 | `sandbox.py` | Fallback when `*_MOUNT` is unset |
| `MILL_SANDBOX_DATA_MOUNT` | `sandbox_data_mount` | `None` | `str\|None` | Settings + compose | identifying | absent | §7 | `sandbox.py` | Overrides `*_VOLUME`; set by docker-compose to `${PWD}/.data` |

### 1.9  Web research & fetch

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_WEB_SEARCH` | `web_search` | `true` | `bool` | Settings | non-sensitive | default | §8 | `stages/refine.py`, `stages/implement.py` | |
| `MILL_FETCH_IMAGE` | `fetch_image` | `curlimages/curl:8.17.0` | `str` | Settings | non-sensitive | default | §8 | `sandbox.py` (fetch container) | |
| `MILL_WEB_FETCH_MAX_BYTES` | `web_fetch_max_bytes` | `2000000` | `int` | Settings | non-sensitive | default | §8 | `sandbox.py` (fetch) | |
| `MILL_WEB_FETCH_TIMEOUT` | `web_fetch_timeout` | `30` | `int` | Settings | non-sensitive | default | §8 | `sandbox.py` (fetch) | |

### 1.10  Approval & review gates

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_REQUIRE_APPROVAL` | `require_approval` | `true` | `bool` | Settings | non-sensitive | default | §6 | `stages/refine.py`, `runtime/worker.py` | |
| `MILL_AUTO_APPROVE_ENABLED` | `auto_approve_enabled` | `false` | `bool` | Settings | non-sensitive | default | §6 | `stages/refine.py` | Local YAML overrides default `false` → `true` |
| `MILL_REVIEW_ENABLED` | `review_enabled` | `false` | `bool` | Settings | non-sensitive | default | §6 | `runtime/worker.py` | Local YAML overrides default `false` → `true` |
| `MILL_AUTO_MERGE_ENABLED` | `auto_merge_enabled` | `false` | `bool` | Settings | non-sensitive | default | **missing** | `stages/merge/` | ⚠️ Undocumented |
| `MILL_REFINE_TRIAGE_ENABLED` | `refine_triage_enabled` | `true` | `bool` | Settings | non-sensitive | default | **missing** | `stages/refine.py` | ⚠️ Undocumented |
| `MILL_SPEC_REVIEW_ENABLED` | `spec_review_enabled` | `false` | `bool` | Settings | non-sensitive | default | **missing** | `stages/refine.py` | ⚠️ Undocumented |
| `MILL_REVIEW_MAX_ROUNDS` | `review_max_rounds` | `3` | `int` | Settings | non-sensitive | default | **missing** | `stages/review.py` | ⚠️ Undocumented |
### 1.11  Retrospect stage

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_RETROSPECT_SPAWN_DRAFTS` | `retrospect_spawn_drafts` | `true` | `bool` | Settings | non-sensitive | default | §10 | `stages/retrospect.py` | |
| `MILL_RETROSPECT_MEMORY_PATH` | `retrospect_memory_path` | `None` | `Path\|None` | Settings | non-sensitive | default | §10 | `stages/retrospect.py` | |

### 1.12  Pipeline tail (merge stage)

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_MERGE_POLL_SECONDS` | `merge_poll_seconds` | `120` | `int` | Settings | non-sensitive | default | §9 | `stages/merge/` | |
| `MILL_PRUNE_CLONE_ON_CLOSE` | `prune_clone_on_close` | `true` | `bool` | Settings | non-sensitive | default | **missing** | `core/service.py` (ticket close) | ⚠️ Undocumented |
| `MILL_MAX_ARCHIVED_TICKETS` | `max_archived_tickets` | `100` | `int` | Settings | non-sensitive | default | **missing** | `core/service.py` (ticket purge) | ⚠️ Undocumented |
| `MILL_REBASE_MAX_ATTEMPTS` | `rebase_max_attempts` | `5` | `int` | Settings | non-sensitive | default | §9 | `stages/merge/` | |
| `MILL_CI_FIX_MAX_ATTEMPTS` | `ci_fix_max_attempts` | `2` | `int` | Settings | non-sensitive | default | §9 | `stages/merge/` | |

### 1.13  CI monitor (log cap only; enabled/interval are per-repo)

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_CI_LOG_MAX_BYTES` | `ci_log_max_bytes` | `65536` | `int` | Settings | non-sensitive | default | §20 | CI monitor / CI-fix agent | global operational cap |
| `—` | *ci_monitor_enabled* | `True` | `bool` | RepoConfig | non-sensitive | repos.yaml | — | `runtime/worker.py` | per-repo field in `config/repos.yaml` |
| `—` | *ci_monitor_interval_seconds* | `900` | `int` | RepoConfig | non-sensitive | repos.yaml | — | `runtime/worker.py` | per-repo field in `config/repos.yaml` |

### 1.14  Periodic agents — audit

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_AUDIT_PERIODIC` | `audit_periodic` | `false` | `bool` | Settings | non-sensitive | default | §11 | `audit_runner.py`, `runtime/worker.py` | |
| `MILL_AUDIT_INTERVAL_SECONDS` | `audit_interval_seconds` | `86400` | `int` | Settings | non-sensitive | default | §11 | `runtime/worker.py` | Local YAML overrides to 1 week |

### 1.15  Periodic agents — trace-health

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_TRACE_HEALTH_PERIODIC` | `trace_health_periodic` | `false` | `bool` | Settings | non-sensitive | default | §12 | `trace_health_runner.py`, `runtime/worker.py` | |
| `MILL_TRACE_HEALTH_INTERVAL_SECONDS` | `trace_health_interval_seconds` | `86400` | `int` | Settings | non-sensitive | default | §12 | `runtime/worker.py` | Local YAML overrides to 1 week |

### 1.16  Periodic agents — health

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_HEALTH_PERIODIC` | `health_periodic` | `false` | `bool` | Settings | non-sensitive | default | §13 | `health_runner.py`, `runtime/worker.py` | |
| `MILL_HEALTH_INTERVAL_SECONDS` | `health_interval_seconds` | `86400` | `int` | Settings | non-sensitive | default | §13 | `runtime/worker.py` | |

### 1.17  Periodic agents — test-gap

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_TEST_GAP_PERIODIC` | `test_gap_periodic` | `false` | `bool` | Settings | non-sensitive | default | §14 | `test_gap_runner.py`, `runtime/worker.py` | |
| `MILL_TEST_GAP_INTERVAL_SECONDS` | `test_gap_interval_seconds` | `86400` | `int` | Settings | non-sensitive | default | §14 | `runtime/worker.py` | |

### 1.18  Periodic agents — agent-check

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_AGENT_CHECK_PERIODIC` | `agent_check_periodic` | `false` | `bool` | Settings | non-sensitive | default | §15 | `agent_check_runner.py`, `runtime/worker.py` | |
| `MILL_AGENT_CHECK_INTERVAL_SECONDS` | `agent_check_interval_seconds` | `86400` | `int` | Settings | non-sensitive | default | §15 | `runtime/worker.py` | |

### 1.19  Periodic agents — survey

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_SURVEY_PERIODIC` | `survey_periodic` | `true` | `bool` | Settings | non-sensitive | default | **missing** | `survey_runner.py`, `runtime/worker.py` | ⚠️ Undocumented; default `true` (on by default) |
| `MILL_SURVEY_INTERVAL_SECONDS` | `survey_interval_seconds` | `86400` | `int` | Settings | non-sensitive | default | §16 | `runtime/worker.py` | |

### 1.20  Periodic agents — diagnostic

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_DIAGNOSTIC_PERIODIC` | `diagnostic_periodic` | `false` | `bool` | Settings | non-sensitive | `periodic.diagnostic.enabled` | §12 | `diagnostic_runner.py`, `runtime/worker.py` | Deterministic pass (no LLM) |
| `MILL_DIAGNOSTIC_INTERVAL_SECONDS` | `diagnostic_interval_seconds` | `86400` | `int` | Settings | non-sensitive | `periodic.diagnostic.interval_seconds` | §12 | `runtime/worker.py` | |
| `MILL_DIAGNOSTIC_TARGET_REPO_ID` | `diagnostic_target_repo_id` | `robotsix-mill` | `str` | Settings | non-sensitive | `periodic.diagnostic.target_repo_id` | §12 | `diagnostic_runner.py` | Single-repo fallback when monitored list is empty |
| `MILL_DIAGNOSTIC_MONITORED_REPO_IDS` | `diagnostic_monitored_repo_ids` | `[]` | `list[str]` | Settings | non-sensitive | `periodic.diagnostic.monitored_repo_ids` | §12 | `diagnostic_runner.py` | Repos monitored each pass; empty -> falls back to `target_repo_id` |

### 1.21  Action-agent memory paths

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `MILL_IMPLEMENT_MEMORY_PATH` | `implement_memory_path` | `None` | `Path\|None` | Settings | non-sensitive | default | §18 | `stages/implement.py` | |
| `MILL_REFINE_MEMORY_PATH` | `refine_memory_path` | `None` | `Path\|None` | Settings | non-sensitive | default | §18 | `stages/refine.py` | |
| `MILL_CI_FIX_MEMORY_PATH` | `ci_fix_memory_path` | `None` | `Path\|None` | Settings | non-sensitive | default | §18 | CI-fix agent | |
| `MILL_REBASE_MEMORY_PATH` | `rebase_memory_path` | `None` | `Path\|None` | Settings | non-sensitive | default | §18 | Rebase agent | |
| `MILL_CI_PATTERNS_PATH` | `ci_patterns_path` | `None` | `Path\|None` | Settings | non-sensitive | default | §18 | CI-fix agent (pattern memory) | |

### 1.22  Notifications

| Env var | Field | Default | Type | Source | Sensitivity | YAML | Docs | Consumers | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `NTFY_URL` | `ntfy_url` | `None` | `str\|None` | Settings | identifying | absent | Non-prefixed | `notify.py` | Empty string disables |

### 1.23  Computed properties (derived from the above)

| Property | Derivation | Type | Consumers | Notes |
|---|---|---|---|---|
| `epic_workspaces_dir` | `data_dir / "epic_workspaces"` | `Path` | `sandbox.py` | Bind-mount target for epic workspace in sandbox containers |
| `tracing_enabled` | `bool(get_secrets().langfuse_base_url and get_secrets().langfuse_public_key and get_secrets().langfuse_secret_key)` | `bool` | `runtime/tracing.py`, `runtime/api.py` | Gate: all 3 must be truthy; populated from `RepoConfig` at startup |
| `retrospect_memory_file` | `retrospect_memory_path or data_dir / "retrospect_memory.md"` | `Path` | `stages/retrospect.py` | |
| `implement_memory_file` | `implement_memory_path or data_dir / "implement_memory.md"` | `Path` | `stages/implement.py` | |
| `refine_memory_file` | `refine_memory_path or data_dir / "refine_memory.md"` | `Path` | `stages/refine.py` | |
| `ci_fix_memory_file` | `ci_fix_memory_path or data_dir / "ci_fix_memory.md"` | `Path` | CI-fix agent | |
| `rebase_memory_file` | `rebase_memory_path or data_dir / "rebase_memory.md"` | `Path` | Rebase agent | |
| `ci_patterns_file` | `ci_patterns_path or data_dir / "ci_patterns.json"` | `Path` | CI-fix agent (pattern memory) | |

### 1.24  Compose/CI overrides and non-Settings vars

This table covers vars that appear outside `config.py` — either as
compose `environment:` overrides of Settings fields (rows 5–7), a
Settings field also consumed by compose variable substitution (row 2),
or genuinely non-Settings vars consumed only by tooling/CI (rows 1, 3, 4).

| Env var | Source | Default | Where set | Sensitivity | YAML | Consumers | Notes |
|---|---|---|---|---|---|---|---|
| `DOCKER_GID` | compose-subst + `dev/mill-autoupdate.sh` | `999` | Shell env or `.env` (compose-subst only), `dev/mill-autoupdate.sh` (exports from `getent group docker`) | non-sensitive | absent | `docker-compose.yml` (`group_add`), `dev/mill-autoupdate.sh` | NOT a `Settings` field; ignored by pydantic-settings (`extra="ignore"`). Docker orchestration, not app config. |
| `GITHUB_APP_PRIVATE_KEY_PATH` (compose use) | compose-subst | `/dev/null` | `docker-compose.yml` volumes: `${GITHUB_APP_PRIVATE_KEY_PATH:-/dev/null}` | identifying | secret | `docker-compose.yml` (volume bind-mount) | Same env var as the Settings field; compose reads it independently for the bind-mount path. Set via shell environment or `config/secrets.yaml`. |
| `GIT_BASE_REF` | CI | (none) | `.github/workflows/ci.yml` env block | non-sensitive | absent | `tests/test_migration_guard.py` via `os.environ.get("GIT_BASE_REF", "origin/main")` | Workflow-internal only; never in `Settings`. |
| `SKIP_MIGRATION_GUARD` | CI / developer shell | (none) | Manually exported by developers | non-sensitive | absent | `tests/test_migration_guard.py` via `os.environ.get("SKIP_MIGRATION_GUARD")` | Escape hatch; not in `Settings`. |
| `MILL_SANDBOX_DATA_MOUNT` (compose) | compose | `${PWD}/.data` | `docker-compose.yml` environment: | identifying | default (compose override) | `sandbox.py` via `Settings()` | Compose expands `$PWD` on the host; listed in §1.8. |

---

## 2. Cross-Reference Checks

### 2.1  Vars present in `config.py` but missing from `docs/configuration.md`

These 10 env vars are defined with `Field()` in `config.py` but have **no entry**
in `docs/configuration.md`:

| # | Env var | Python field | Default | In YAML? |
|---|---------|-------------|---------|----------|
| 1 | `MILL_TRIAGE_MODEL` | `triage_model` | `openai/gpt-4o-mini` | default |
| 2 | `MILL_DOC_MODEL` | `doc_model` | `deepseek/deepseek-v4-pro` | default |
| 3 | `MILL_PRUNE_CLONE_ON_CLOSE` | `prune_clone_on_close` | `true` | default |
| 4 | `MILL_MAX_ARCHIVED_TICKETS` | `max_archived_tickets` | `100` | default |
| 5 | `MILL_AUTO_MERGE_ENABLED` | `auto_merge_enabled` | `false` | default |
| 6 | `MILL_REFINE_TRIAGE_ENABLED` | `refine_triage_enabled` | `true` | default |
| 7 | `MILL_SPEC_REVIEW_ENABLED` | `spec_review_enabled` | `false` | default |
| 8 | `MILL_REVIEW_MAX_ROUNDS` | `review_max_rounds` | `3` | default |
| 9 | `MILL_SURVEY_PERIODIC` | `survey_periodic` | `true` | default |
| 10 | `MILL_GITLAB_API_URL` | `gitlab_api_url` | `https://gitlab.com/api/v4` | default |

> The spec's known list incorrectly included `MILL_SURVEY_INTERVAL_SECONDS`
> (which IS in §16).

### 2.2  Non-Settings vars

| Env var | Value | Where consumed | Notes |
|---|---|---|---|
| `DOCKER_GID` | `999` | `docker-compose.yml` (`group_add: ["${DOCKER_GID:-999}"]`), `dev/mill-autoupdate.sh` (`export DOCKER_GID`) | **Not an app config var.** Docker socket group ownership for the non-root container user. pydantic-settings ignores it (`extra="ignore"`). Set via shell environment (docker-compose variable substitution only). |

### 2.3  Vars read via raw `os.environ` bypassing `Settings`

| Env var | File | Line(s) | Usage |
|---|---|---|---|
| `SKIP_MIGRATION_GUARD` | `tests/test_migration_guard.py` | L48 (`os.environ.get`) | Escape hatch for CI migration guard |
| `GIT_BASE_REF` | `tests/test_migration_guard.py` | L54 (`os.environ.get`) | Base ref for git diff in CI |
| `PYTHONPATH` | `tests/runtime/test_tracing.py` | L140 | Subprocess environment; test-only. Standard Python runtime variable, not an app-config concern. |

### 2.4  Dual-source vars (read from both `Settings` AND raw `os.environ`)

| Env var | Settings access | Raw `os.environ` access | Impact |
|---|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | `Settings.langfuse_public_key` → `tracing_enabled` property | `tracing.py:_tracing_enabled()` L58, `_ensure_tracing()` L90 | `tracing.py` **never calls `Settings()`** — it checks env vars directly at module-load time to decide whether to import heavy OTel packages. So `LANGFUSE_*` values set via `Settings()` constructor args (not `os.environ`) would be invisible to `tracing.py`. In practice this works because compose exports them into the real environment. |
| `LANGFUSE_SECRET_KEY` | `Settings.langfuse_secret_key` → `tracing_enabled` property | `tracing.py:_tracing_enabled()` L59, `_ensure_tracing()` L91 | Same as above. |
| `LANGFUSE_BASE_URL` | `Settings.langfuse_base_url` → `tracing_enabled` property | `tracing.py:_ensure_tracing()` L83 | Same as above; `tracing.py` defaults to `https://cloud.langfuse.com` when unset. |

### 2.5  Vars hardcoded in `Dockerfile` vs. their YAML/default equivalents

| Env var | `Dockerfile` value | `config.py` default | YAML default / override | Discrepancy |
|---|---|---|---|---|
| `MILL_DATA_DIR` | `/data` | `.mill-data` | default (`.mill-data`) | Intentional: container always uses `/data` (a volume mount). Dev mode uses the repo-local default. |
| `MILL_API_HOST` | `0.0.0.0` | `127.0.0.1` | default (`127.0.0.1`) | Intentional: container binds on all interfaces so published ports work. |
| `MILL_API_URL` | `http://127.0.0.1:8077` | `http://127.0.0.1:8077` | default (`http://127.0.0.1:8077`) | No discrepancy — all three match. |

### 2.6  Vars set only in `docker-compose.yml environment:`

| Env var | Compose value | `config.py` default | YAML | Override effect |
|---|---|---|---|---|
| `MILL_SANDBOX_DATA_MOUNT` | `${PWD}/.data` | `None` | absent | Forces bind-mount path; overrides `MILL_DATA_VOLUME` |

CI monitor enabling/interval were previously set here but are now per-repo
fields in `config/repos.yaml` (``RepoConfig.ci_monitor_enabled`` /
``RepoConfig.ci_monitor_interval_seconds``).

This is the **only** var that compose sets directly in the `environment:` block. Everything else flows through the YAML config pipeline (`mill.defaults.yaml` → `mill.local.yaml` → `os.environ`).

### 2.7  Vars consumed by `docker-compose.yml` variable substitution (not `environment:`)

| Variable | Usage | Source |
|---|---|---|
| `${DOCKER_GID:-999}` | `group_add:` list | Shell environment or `.env` (docker-compose substitution only, not read by the app) |
| `${GITHUB_APP_PRIVATE_KEY_PATH:-/dev/null}` | `volumes:` bind-mount source + target | Shell environment or `config/secrets.yaml` |
| `${PWD}/.data` | `MILL_SANDBOX_DATA_MOUNT` value in `environment:` | Host shell (compose resolves `$PWD`) |

---

## 3. Requirements Summary

### 3.1  Desired structure

The current configuration namespace is a **flat 115-key list** under a single
`Settings` class. Every value — from model names to memory paths to Docker
image tags — lives in the same namespace with a `MILL_` prefix convention that
is purely advisory (no enforcement). The YAML config pipeline (`config/mill.defaults.yaml` → `config/mill.local.yaml` → `config/mill.production.yaml`) provides logical grouping through nested YAML keys, with environment-variable overrides for operational flexibility.

A replacement config architecture should provide:
- **Grouping**: core (models, limits, timeouts), secrets (keys, tokens),
  forge (delivery auth + target), sandbox (Docker images, limits),
  periodic-agents (one group per agent), paths (data dir, memory ledgers).
- **Per-environment overrides**: dev (`config/mill.local.yaml`), CI (inert — only
  `GIT_BASE_REF`), staging/prod (`docker-compose.yml` `environment:` +
  mounted secrets file). Each environment selects its own overlay, not a
  single monolith.
- **Separation of orchestration vars**: `DOCKER_GID` should not pollute the
  app config namespace at all.

### 3.2  Type safety gaps

pydantic-settings provides type coercion (`str`, `int`, `float`, `bool`,
`Path`, `Literal`) but **no semantic validation**:

- **No range checks**: `MILL_MAX_CONCURRENCY=0` or `=-1` is accepted.
  `MILL_MODEL_REQUEST_TIMEOUT=0` disables timeouts silently.
- **No cross-field consistency**: `FORGE_AUTH=app` but `GITHUB_APP_ID` unset
  → runtime error in `forge/auth.py`, not a startup validation failure.
  `FORGE_KIND=github` but `FORGE_REMOTE_URL` unset → same.
- **No required-if logic**: Many `str|None` fields are effectively required
  in certain modes (e.g. `FORGE_TOKEN` must be set when
  `FORGE_AUTH=token`), but the schema treats them as always-optional.
- **No URL/format validation**: `MILL_API_URL` accepts any string. `NTFY_URL`
  accepts any string (empty = disabled is convention, not enforced).
- **Path existence not checked**: `MILL_SKILLS_DIR`, `GITHUB_APP_PRIVATE_KEY_PATH`,
  `MILL_DATA_DIR` — none are validated for existence at startup.

### 3.3  Secrets management

Secrets currently live in `os.environ` after pydantic-settings loads
`config/secrets.yaml`. This means:

- **Any library import** can read `OPENROUTER_API_KEY` via `os.environ`
  (and `tracing.py` already does for `LANGFUSE_*`).
- **No access control**: there's no distinction between a module that
  legitimately needs a secret (e.g. `forge/auth.py` needs `FORGE_TOKEN`)
  and one that shouldn't see it.
- **Secrets are plaintext in `os.environ`**: visible to any subprocess
  (sandbox containers, `subprocess.run`, debuggers). The sandbox containers
  are `--network none` but if a future change adds env passthrough, secrets
  would leak.
- **11 secrets in total**: `OPENROUTER_API_KEY`, `FORGE_TOKEN`,
  `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_PRIVATE_KEY_PATH`,
  `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`,
  `NTFY_TOKEN`, `LANGFUSE_PROJECT_ID`, `NTFY_URL` (the last two are
  identifying rather than secret but still sensitive).

A dedicated secrets path (e.g. a `Secrets` class separate from `Settings`,
or a `get_secret()` accessor that logs access) would provide auditability
and prevent accidental exposure.

### 3.4  Environment story

> **Update (2026-05):** The YAML config pipeline has been implemented.
> See [`configuration.md`](configuration.md) for the current environment story:
> - **Dev**: `config/mill.local.yaml` (gitignored per-developer overrides).
> - **CI**: no config file needed (tests use `Settings(k=v)` kwargs and temp YAML).
> - **Prod**: `config/mill.production.yaml` + `config/secrets.yaml` (bind-mounted into container).

### 3.5  Documentation sync gap

- **12 fields** defined in `config.py` are **completely absent** from
  `docs/configuration.md` (see §2.1).
- **4 fields** are in `config.py` but intentionally absent from
  `config/mill.defaults.yaml` (all are secrets):
  `OPENROUTER_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
  `LANGFUSE_PROJECT_ID`.
- **Documentation is manually maintained** — no mechanical cross-check
  between `config.py` and `docs/configuration.md` exists. A script or test that
  diffs the two would prevent drift.

### 3.6  Raw `os.environ` bypass

`src/robotsix_mill/runtime/tracing.py` reads `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL` directly from `os.environ`
**without instantiating `Settings`** (the import at L26 exists only for
`init()`'s type annotation; `_tracing_enabled()` and `_ensure_tracing()`
both reach directly into `os.environ`). This is a deliberate performance
choice (the file avoids importing heavy OTel packages unless tracing is
enabled, and checking `os.environ` is faster than constructing a `Settings`
object). However, it creates a **dual-source problem**:

- `Settings.tracing_enabled` depends on `self.langfuse_public_key` etc.
  (from the `Settings` model, populated by pydantic-settings).
- `tracing._tracing_enabled()` reads `os.environ` directly.
- If someone instantiates `Settings(LANGFUSE_PUBLIC_KEY="pk-...")` without
  exporting to `os.environ`, `tracing_enabled` would be `True` but
  `_ensure_tracing()` would fail because `os.environ` has no such key.

In practice this doesn't break because docker-compose and local dev both set
these in the real environment. But a config redesign should route tracing
through a single accessor — either `Settings` or a dedicated "tracing config"
singleton — so the two code paths can't diverge.

---

## 4. Methodology

This audit was produced by:

1. Extracting every `Field()` definition from `config.py` (115 env-var
   aliases + computed `@property` entries).
2. Reading `config/mill.defaults.yaml` for all committed default values.
3. Reading `config/secrets.example.yaml` for the credentials template.
4. Cross-referencing every `Field` against `docs/configuration.md`'s 21
   sections and non-prefixed table.
5. Scanning `Dockerfile` for `ENV` directives, `docker-compose.yml` for
   `environment:` and variable substitution, `.github/workflows/ci.yml` for
   `env:` blocks, and `dev/mill-autoupdate.sh` for `export` statements.
6. Searching the entire repo for `os.environ.get`, `os.environ[...]`, and
   `Settings()` / `load_settings()` to build the consumers column.
7. Classifying every value by sensitivity (`secret`, `identifying`,
   `non-sensitive`) based on whether it is a token/key/password, a
   URL/username/hostname, or a tuning knob/feature flag.
