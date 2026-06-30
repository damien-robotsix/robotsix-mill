# RFC: Configuration System v2

> **SUPERSEDED (historical).** This RFC describes the multi-file YAML
> design (`mill.defaults.yaml` + `mill.local.yaml` + `mill.production.yaml`
> + `secrets.yaml`). The mill has since consolidated to a SINGLE file —
> `config/config.yaml` (else the committed `config/config.example.yaml`),
> with secrets under a top-level `secrets:` block. See
> `docs/configuration.md` for the current model. Kept for historical
> rationale only.

> **Status:** Draft — peer review pending
> **Date:** 2026-05-23
> **Supersedes:** `docs/config-audit.md` (analysis phase)
> **PoC template:** `config/config.example.yaml`

---

## 1. Motivation

The current configuration system (`src/robotsix_mill/config.py`) uses a flat
`Settings(BaseSettings)` class with 115 env-var aliases loaded from `.env` +
`secrets.env`.  A full-repo audit (`docs/config-audit.md`) identified seven
concrete problems:

1. **Flat namespace** — 115 keys with no grouping beyond human comments in
   `.env`.  Every model name, timeout, memory path, and feature flag lives in
   the same `MILL_*` namespace, making it hard to find related values and
   impossible to reason about them as coherent groups.

2. **Secrets intermingled** — 11 secret/token fields (`OPENROUTER_API_KEY`,
   `FORGE_TOKEN`, `LANGFUSE_*`, `NTFY_*`, GitHub App credentials) sit in the
   same `Settings` object as non-sensitive tuning knobs.  Any module that
   imports `Settings` can read every secret via plain attribute access — no
   access control, no audit trail.

3. **No semantic validation** — pydantic-settings provides type coercion only.
   `MILL_MAX_CONCURRENCY=0` or `=-1` passes silently.  `FORGE_AUTH=app` with
   `GITHUB_APP_ID=None` fails deep in `forge/auth.py` at runtime, not at
   startup.  No range checks, no cross-field consistency, no format
   validation.

4. **Inconsistent environment story** — dev uses `.env` (committed, causes
   `git diff` noise when developers tune locally).  CI blocks `env_file`
   entirely (`tests/conftest.py::_no_dotenv`).  Docker overrides are split
   across `Dockerfile` `ENV` (3 vars), `docker-compose.yml` `environment:`
   (3 vars), and compose `env_file:` (everything else).  No per-environment
   overlay file exists.

5. **Documentation drift** — 12 fields defined in `config.py` have no entry
   in `docs/configuration.md`.  Documentation is manually maintained with no
   mechanical cross-check.  One default value (`MILL_EXPLORE_MODEL`) is wrong
   in both `.env` comments and docs — the code default is `-flash` but both
   say `-pro`.

6. **Dual-source problem** — `runtime/tracing.py` reads `LANGFUSE_PUBLIC_KEY`,
   `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL` directly from `os.environ`,
   bypassing `Settings` entirely.  If someone constructs
   `Settings(LANGFUSE_PUBLIC_KEY=...)` without exporting to `os.environ`,
   `tracing_enabled` is `True` but the OTel exporter fails to find
   credentials.

7. **Orchestration pollution** — `DOCKER_GID` lives in `.env` and is loaded
   by `Settings.model_config` (silently ignored via `extra="ignore"`), but it
   is a Docker orchestration concern (socket group ownership), not an
   application config value.

This RFC proposes a YAML-based layered configuration system that addresses
all seven problems while preserving pydantic-settings compatibility and
providing a lossless migration path from the existing `.env` files.

---

## 2. Design principles

| Principle | Rationale |
|---|---|
| **Keep `pydantic-settings`** | 46 files import `Settings`; replacing the framework would cascade across the entire codebase.  The new system wraps `pydantic-settings` — loading YAML into Pydantic models, then exposing those models through the existing `Settings` interface during the migration window. |
| **YAML for structured config** | The repo already uses YAML for `agent_definitions/refine.yaml` and CI workflows.  YAML supports nesting (groups), comments, anchors/aliases (DRY for repeated model names), and is trivial to diff and review. |
| **One source of truth per value** | No consumer reads `os.environ` directly.  The config loader is the single entry point.  `tracing.py`'s dual-source problem is resolved by wiring `Secrets` explicitly rather than reaching into the environment. |
| **Secrets are opaque** | A secret is never accessible as a plain attribute on a general-purpose config object.  Secrets live in a separate `Secrets` model, loaded from a separate file, accessed only by code that explicitly imports `Secrets`.  Every access is logged at DEBUG level. |
| **Backward-compatible migration** | Existing `.env` values are mechanically translatable to the new format via a migration script.  During the migration window, the loader reads both old (`.env`) and new (YAML) sources, with YAML taking precedence.  After all consumers are updated, the `.env` path is retired. |
| **Committed defaults, local overrides** | `config/mill.defaults.yaml` is committed — it is the canonical source of truth for "what is configurable" and what the safe defaults are.  Per-developer and per-deployment overrides are gitignored. |

---

## 3. File layout

| File | Committed? | Role |
|---|---|---|
| `config/mill.defaults.yaml` | **Yes** | Canonical defaults for every knob.  The single source of truth for "what is configurable."  All 115 fields with their code-default values and documentation comments. |
| `config/mill.local.yaml` | **No** (gitignored) | Per-developer overrides.  Merged on top of defaults.  Developers who need different models, limits, or feature flags for local work put them here instead of editing the tracked `.env`. |
| `config/mill.production.yaml` | **No** (gitignored, host-mounted) | Production overrides.  Bind-mounted into the Docker container at a path specified by `MILL_CONFIG_FILE`.  Contains deployment-specific values (forge URLs, sandbox image pins, feature flags). |
| `config/secrets.yaml` | **No** (gitignored) | Secrets only — never committed.  Loaded separately from the main config into a dedicated `Secrets` Pydantic model.  File permissions should be `0600`. |
| `config/secrets.example.yaml` | **Yes** | Template for `config/secrets.yaml` with all keys listed but values empty — the moral equivalent of `secrets.env.example` today.  Safe to commit and review. |

All files use **YAML** (`.yaml`).  Justification:

- The repo already uses YAML for agent definitions, CI workflows, and
  `docker-compose.yml`.  No new parser dependency is needed — `pyyaml` is
  already a transitive dependency of the stack.
- YAML supports nested structure (groups), comments, and anchors/aliases —
  all three are needed to address the flat-namespace and DRY problems.
- YAML is human-readable and diff-friendly; 2-space indent convention
  matches the repo's existing YAML style.
- TOML was considered (Python-native, typed) but lacks anchors/aliases and
  the repo has no existing TOML files.  JSON lacks comments entirely.

---

## 4. Configuration schema (logical groups)

### 4.1 Group overview

| Group | Field count | Description |
|---|---|---|
| `core.models` | 19 | Per-agent model selection |
| `core.limits` | 16 | Request caps, concurrency, timeouts, retry knobs |
| `core.memory` | 5 | Memory ledger paths, max chars, reference file limits |
| `forge` | 6 | Forge platform, remote URL, target branch, auth mode, API URLs |
| `sandbox` | 9 | Docker images, resource limits, data mounts, command timeout, skills |
| `web` | 6 | Web-search toggle, model, request limit, fetch container + limits |
| `gates` | 9 | Approval, review, auto-merge, triage, spec-review feature flags + models |
| `pipeline` | 11 | Merge poll, rebase, ci-fix, retrospect, prune, archive, dedup lookback |
| `periodic.audit` | 4 | Audit agent: model, enabled, interval, memory path |
| `periodic.trace_health` | 2 | Trace-health check: enabled, interval |
| `periodic.health` | 4 | Health agent: model, enabled, interval, memory path |
| `periodic.test_gap` | 4 | Test-gap agent: model, enabled, interval, memory path |
| `periodic.agent_check` | 4 | Agent-check agent: model, enabled, interval, memory path |
| `periodic.survey` | 4 | Survey agent: model, enabled, interval, memory path |
| `periodic.ci_monitor` | 3 | CI monitor: enabled, interval, log max bytes |
| `service` | 4 | API host/port/URL, data directory |
| `secrets` | 10 | API keys, tokens, tracing credentials (separate file + model) |
| **Total** | **115** | |

### 4.2 Complete field-to-group mapping

Every field from `config.py`, keyed by its current env-var alias.  Fields
marked `→ secrets` are loaded from `config/secrets.yaml` into a separate
`Secrets` model, not from the main config files.

#### core.models

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_MODEL` | `core.models.coordinator` | `deepseek/deepseek-v4-pro` | |
| `MILL_EXPLORE_MODEL` | `core.models.explore` | `deepseek/deepseek-v4-flash` | |
| `MILL_TEST_MODEL` | `core.models.test` | `deepseek/deepseek-v4-pro` | |
| `MILL_REFINE_MODEL` | `core.models.refine` | `deepseek/deepseek-v4-pro` | |
| `MILL_ANSWER_MODEL` | `core.models.answer` | `deepseek/deepseek-v4-pro` | |
| `MILL_RETROSPECT_MODEL` | `core.models.retrospect` | `deepseek/deepseek-v4-pro` | |
| `MILL_AUDIT_MODEL` | `core.models.audit` | `deepseek/deepseek-v4-pro` | |
| `MILL_DEDUP_MODEL` | `core.models.dedup` | `deepseek/deepseek-v4-pro` | |
| `MILL_WEB_RESEARCH_MODEL` | `core.models.web_research` | `deepseek/deepseek-v4-flash` | |
| `MILL_REVIEW_MODEL` | `core.models.review` | `deepseek/deepseek-v4-pro` | |
| `MILL_TRACE_INSPECTOR_MODEL` | `core.models.trace_inspector` | `deepseek/deepseek-v4-pro` | |
| `MILL_TEST_GAP_MODEL` | `core.models.test_gap` | `deepseek/deepseek-v4-pro` | |
| `MILL_AGENT_CHECK_MODEL` | `core.models.agent_check` | `deepseek/deepseek-v4-pro` | |
| `MILL_HEALTH_MODEL` | `core.models.health` | `deepseek/deepseek-v4-pro` | |
| `MILL_SURVEY_MODEL` | `core.models.survey` | `deepseek/deepseek-v4-pro` | |
| `MILL_RATE_LIMIT_FALLBACK_MODEL` | `core.models.rate_limit_fallback` | `""` (empty = disabled) | |
| `MILL_TRIAGE_MODEL` | `core.models.triage` | `openai/gpt-4o-mini` | |
| `MILL_DOC_MODEL` | `core.models.doc` | `deepseek/deepseek-v4-pro` | |
| `MILL_AUTO_APPROVE_MODEL` | `core.models.auto_approve` | `openai/gpt-4o-mini` | |

#### core.limits

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_PER_PASS_REQUEST_BUDGET` | `core.limits.coordinator_requests` | `500` | Hard upper bound 5000 |
| `MILL_TEST_REQUEST_LIMIT` | `core.limits.test_requests` | `8` | |
| `MILL_EXPLORE_REQUEST_LIMIT` | `core.limits.explore_requests` | `20` | |
| `MILL_DEDUP_REQUEST_LIMIT` | `core.limits.dedup_requests` | `4` | |
| `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `core.limits.web_research_requests` | `8` | |
| `MILL_MAX_CONCURRENCY` | `core.limits.max_concurrency` | `4` | |
| `MILL_MAX_FIX_ITERATIONS` | `core.limits.max_fix_iterations` | `8` | |
| `MILL_MAX_STUCK_CYCLES` | `core.limits.max_stuck_cycles` | `3` | |
| `MILL_MAX_SPEND_USD_PER_TICKET` | `core.limits.max_spend_usd_per_ticket` | `0.0` | `0.0` = disabled |
| `MILL_STAGE_TIMEOUT_SECONDS` | `core.limits.stage_timeout_seconds` | `1800` | seconds; `≤ 0` disables |
| `MILL_STAGE_TIMEOUT_OVERRIDES` | `core.limits.stage_timeout_overrides` | `{}` | JSON dict; `"stage": 0` disables per-stage |
| `MILL_MODEL_REQUEST_TIMEOUT` | `core.limits.model_request_timeout` | `900.0` | seconds |
| `MILL_TRANSIENT_RETRIES` | `core.limits.transient_retries` | `4` | |
| `MILL_TRANSIENT_BACKOFF_BASE` | `core.limits.transient_backoff_base` | `2.0` | seconds |
| `MILL_TRANSIENT_BACKOFF_CAP` | `core.limits.transient_backoff_cap` | `30.0` | seconds |
| `MILL_RATE_LIMIT_BACKOFF_BASE` | `core.limits.rate_limit_backoff_base` | `30.0` | seconds |
| `MILL_RATE_LIMIT_BACKOFF_CAP` | `core.limits.rate_limit_backoff_cap` | `120.0` | seconds |
| `MILL_RATE_LIMIT_FALLBACK_RETRIES` | `core.limits.rate_limit_fallback_retries` | `3` | |

#### core.memory

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_MAX_MEMORY_CHARS` | `core.memory.max_memory_chars` | `8000` | |
| `MILL_REFERENCE_FILES_MAX_COUNT` | `core.memory.reference_files_max_count` | `5` | |
| `MILL_REFERENCE_FILES_MAX_TOTAL_LINES` | `core.memory.reference_files_max_total_lines` | `3000` | |
| `MILL_DEDUP_LOOKBACK_DAYS` | `core.memory.dedup_lookback_days` | `30` | |

#### forge

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `FORGE_KIND` | `forge.kind` | `none` | `github`, `gitlab`, or `none` |
| `FORGE_REMOTE_URL` | `forge.remote_url` | `null` | |
| `FORGE_TARGET_BRANCH` | `forge.target_branch` | `main` | |
| `FORGE_AUTH` | `forge.auth_mode` | `token` | `token` or `app` |
| `MILL_GITHUB_API_URL` | `forge.github_api_url` | `https://api.github.com` | GitHub Enterprise override |
| `MILL_GITLAB_API_URL` | `forge.gitlab_api_url` | `https://gitlab.com/api/v4` | Self-hosted GitLab override |
| `GITHUB_APP_PRIVATE_KEY_PATH` | `forge.github_app_private_key_path` | `null` | Path (not secret); host-mounted `.pem` |

#### sandbox

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_SANDBOX_IMAGE` | `sandbox.image` | `python:3.14-slim` | |
| `MILL_SANDBOX_MEMORY` | `sandbox.memory` | `2g` | |
| `MILL_SANDBOX_PIDS_LIMIT` | `sandbox.pids_limit` | `512` | |
| `MILL_SANDBOX_READONLY` | `sandbox.readonly` | `true` | |
| `MILL_DATA_VOLUME` | `sandbox.data_volume` | `mill_data` | Named volume name |
| `MILL_SANDBOX_DATA_MOUNT` | `sandbox.data_mount` | `null` | Host path for bind mount |
| `MILL_COMMAND_TIMEOUT` | `sandbox.command_timeout` | `900` | seconds |
| `MILL_TEST_COMMAND` | `sandbox.test_command` | `pytest -q` | |
| `MILL_SKILLS_DIR` | `sandbox.skills_dir` | `skills` | |

#### web

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_WEB_SEARCH` | `web.search_enabled` | `true` | |
| `MILL_WEB_RESEARCH_MODEL` | `web.research_model` | `deepseek/deepseek-v4-pro` | Also in `core.models` |
| `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `web.research_request_limit` | `8` | Also in `core.limits` |
| `MILL_FETCH_IMAGE` | `web.fetch_image` | `curlimages/curl:8.17.0` | |
| `MILL_WEB_FETCH_MAX_BYTES` | `web.fetch_max_bytes` | `2000000` | |
| `MILL_WEB_FETCH_TIMEOUT` | `web.fetch_timeout` | `30` | seconds |

> **Note on deduplication:** `web.research_model` and
> `web.research_request_limit` duplicate `core.models.web_research` and
> `core.limits.web_research_requests` respectively.  The loader resolves
> these to the same underlying value; the YAML uses YAML anchors to keep
> them in sync (see §11).

#### gates

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_REQUIRE_APPROVAL` | `gates.require_approval` | `true` | |
| `MILL_AUTO_APPROVE_ENABLED` | `gates.auto_approve_enabled` | `false` | |
| `MILL_AUTO_APPROVE_MODEL` | `gates.auto_approve_model` | `openai/gpt-4o-mini` | Also in `core.models` |
| `MILL_REVIEW_ENABLED` | `gates.review_enabled` | `false` | |
| `MILL_REVIEW_MODEL` | `gates.review_model` | `deepseek/deepseek-v4-pro` | Also in `core.models` |
| `MILL_REVIEW_MAX_ROUNDS` | `gates.review_max_rounds` | `3` | |
| `MILL_AUTO_MERGE_ENABLED` | `gates.auto_merge_enabled` | `false` | |
| `MILL_REFINE_TRIAGE_ENABLED` | `gates.refine_triage_enabled` | `true` | |
| `MILL_SPEC_REVIEW_ENABLED` | `gates.spec_review_enabled` | `false` | |

> `gates.auto_approve_model` and `gates.review_model` duplicate model
> fields in `core.models`.  The YAML uses anchors, and the loader resolves
> to a single value.  This is intentional: the model *defaults* are
> declared in `core.models`, while `gates` references them and can override
> independently if needed.

#### pipeline

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_BRANCH_PREFIX` | `pipeline.branch_prefix` | `mill/` | |
| `MILL_MERGE_POLL_SECONDS` | `pipeline.merge_poll_seconds` | `120` | |
| `MILL_REBASE_MAX_ATTEMPTS` | `pipeline.rebase_max_attempts` | `5` | |
| `MILL_CI_FIX_MAX_ATTEMPTS` | `pipeline.ci_fix_max_attempts` | `2` | |
| `MILL_RETROSPECT_SPAWN_DRAFTS` | `pipeline.retrospect_spawn_drafts` | `true` | |
| `MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY` | `pipeline.retrospect_deep_analysis_frequency` | `10` | |
| `MILL_PRUNE_CLONE_ON_CLOSE` | `pipeline.prune_clone_on_close` | `true` | |
| `MILL_MAX_ARCHIVED_TICKETS` | `pipeline.max_archived_tickets` | `100` | |
| `MILL_RETROSPECT_MEMORY_PATH` | `pipeline.retrospect_memory_path` | `null` | Override; defaults derived |
| `MILL_TRACE_INSPECTOR_MEMORY_PATH` | `pipeline.trace_inspector_memory_path` | `null` | Override |
| `MILL_IMPLEMENT_MEMORY_PATH` | `pipeline.implement_memory_path` | `null` | Override |
| `MILL_REFINE_MEMORY_PATH` | `pipeline.refine_memory_path` | `null` | Override |
| `MILL_CI_FIX_MEMORY_PATH` | `pipeline.ci_fix_memory_path` | `null` | Override |
| `MILL_REBASE_MEMORY_PATH` | `pipeline.rebase_memory_path` | `null` | Override |

#### periodic.audit

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_AUDIT_MODEL` | `periodic.audit.model` | `deepseek/deepseek-v4-pro` | Also in `core.models` |
| `MILL_AUDIT_PERIODIC` | `periodic.audit.enabled` | `false` | |
| `MILL_AUDIT_INTERVAL_SECONDS` | `periodic.audit.interval_seconds` | `86400` | |
| `MILL_AUDIT_MEMORY_PATH` | `periodic.audit.memory_path` | `null` | |

#### periodic.trace_health

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_TRACE_HEALTH_PERIODIC` | `periodic.trace_health.enabled` | `false` | |
| `MILL_TRACE_HEALTH_INTERVAL_SECONDS` | `periodic.trace_health.interval_seconds` | `86400` | min 3600 enforced in worker |

#### periodic.health

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_HEALTH_MODEL` | `periodic.health.model` | `deepseek/deepseek-v4-pro` | Also in `core.models` |
| `MILL_HEALTH_PERIODIC` | `periodic.health.enabled` | `false` | |
| `MILL_HEALTH_INTERVAL_SECONDS` | `periodic.health.interval_seconds` | `86400` | |
| `MILL_HEALTH_MEMORY_PATH` | `periodic.health.memory_path` | `null` | |

#### periodic.test_gap

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_TEST_GAP_MODEL` | `periodic.test_gap.model` | `deepseek/deepseek-v4-pro` | Also in `core.models` |
| `MILL_TEST_GAP_PERIODIC` | `periodic.test_gap.enabled` | `false` | |
| `MILL_TEST_GAP_INTERVAL_SECONDS` | `periodic.test_gap.interval_seconds` | `86400` | |
| `MILL_TEST_GAP_MEMORY_PATH` | `periodic.test_gap.memory_path` | `null` | |

#### periodic.agent_check

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_AGENT_CHECK_MODEL` | `periodic.agent_check.model` | `deepseek/deepseek-v4-pro` | Also in `core.models` |
| `MILL_AGENT_CHECK_PERIODIC` | `periodic.agent_check.enabled` | `false` | |
| `MILL_AGENT_CHECK_INTERVAL_SECONDS` | `periodic.agent_check.interval_seconds` | `86400` | min 60 enforced in worker |
| `MILL_AGENT_CHECK_MEMORY_PATH` | `periodic.agent_check.memory_path` | `null` | |

#### periodic.survey

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_SURVEY_MODEL` | `periodic.survey.model` | `deepseek/deepseek-v4-pro` | Also in `core.models` |
| `MILL_SURVEY_PERIODIC` | `periodic.survey.enabled` | `true` | On by default (unusual) |
| `MILL_SURVEY_INTERVAL_SECONDS` | `periodic.survey.interval_seconds` | `86400` | min 60 enforced in worker |
| `MILL_SURVEY_MEMORY_PATH` | `periodic.survey.memory_path` | `null` | |

#### periodic.ci_monitor

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_CI_LOG_MAX_BYTES` | `periodic.ci_monitor.log_max_bytes` | `65536` | global operational cap |
| — | (per-repo in `repos.yaml`) | `True` / `900` | `ci_monitor.enabled` and `ci_monitor.interval_seconds` are RepoConfig fields |

#### service

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `MILL_DATA_DIR` | `service.data_dir` | `.mill-data` | Container overrides to `/data` |
| `MILL_API_HOST` | `service.api_host` | `127.0.0.1` | Container overrides to `0.0.0.0` |
| `MILL_API_PORT` | `service.api_port` | `8077` | |
| `MILL_API_URL` | `service.api_url` | `http://127.0.0.1:8077` | |

#### secrets (separate file → separate model)

| Env var | YAML key | Default | Note |
|---|---|---|---|
| `OPENROUTER_API_KEY` | `secrets.openrouter_api_key` | `null` | **→ secrets.yaml** |
| `FORGE_TOKEN` | `secrets.forge_token` | `null` | **→ secrets.yaml** |
| `GITHUB_APP_ID` | `secrets.github_app_id` | `null` | **→ secrets.yaml** |
| `GITHUB_APP_PRIVATE_KEY` | `secrets.github_app_private_key` | `null` | **→ secrets.yaml** (inline PEM) |
| `LANGFUSE_PUBLIC_KEY` | `secrets.langfuse_public_key` | `null` | **→ secrets.yaml** |
| `LANGFUSE_SECRET_KEY` | `secrets.langfuse_secret_key` | `null` | **→ secrets.yaml** |
| `LANGFUSE_BASE_URL` | `secrets.langfuse_base_url` | `null` | **→ secrets.yaml** |
| `LANGFUSE_PROJECT_ID` | `secrets.langfuse_project_id` | `null` | **→ secrets.yaml** |
| `NTFY_URL` | `secrets.ntfy_url` | `null` | **→ secrets.yaml** |
| `NTFY_TOKEN` | `secrets.ntfy_token` | `null` | **→ secrets.yaml** |

> `GITHUB_APP_PRIVATE_KEY_PATH` (a *path*, not a secret) lives in `forge`
> above.  `GITHUB_APP_PRIVATE_KEY` (the inline PEM) lives in `secrets`.
> These are two separate values; the forge auth module already handles both.

---

## 5. Secrets handling

### 5.1 Separate model

Secrets live in `config/secrets.yaml` and are loaded into a **separate
`Secrets` Pydantic model**, not merged into `Settings`:

```python
# src/robotsix_mill/config.py (post-migration)

class Secrets(BaseModel):
    """Secrets loaded from config/secrets.yaml.  Never merged into Settings."""
    openrouter_api_key: str | None = None
    forge_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str | None = None
    langfuse_project_id: str | None = None
    ntfy_url: str | None = None
    ntfy_token: str | None = None
```

### 5.2 Access rules

- `Settings` **never** holds a secret attribute.  `settings.openrouter_api_key`
  does not exist.
- Code that needs a secret imports `Secrets` explicitly and receives it via
  dependency injection (function parameter, not global).
- The `Secrets` model logs every attribute access at `DEBUG` level via a
  custom `__getattribute__` or Pydantic validator, providing an audit trail
  of which module accessed which secret.
- Secrets are never serialized in logs, traces, or error messages.  The
  `Secrets` model's `__repr__` and `model_dump` redact all values.

### 5.3 Migration from secrets.env

`config/secrets.example.yaml` mirrors `secrets.env.example` — every key
with an empty/null value.  The migration script (§9) reads `secrets.env`
and emits `config/secrets.yaml` via key-for-key translation:

```bash
# secrets.env                          # config/secrets.yaml
OPENROUTER_API_KEY=sk-or-...    →      openrouter_api_key: "sk-or-..."
FORGE_TOKEN=ghp_...             →      forge_token: "ghp_..."
```

### 5.4 Resolving the tracing dual-source

Currently `runtime/tracing.py` reads `LANGFUSE_*` from `os.environ` directly
(not from `Settings`).  The new design routes all tracing config through
`Secrets`:

1. The `tracing` module exports an `init_tracing(secrets: Secrets)` function
   that receives the `Secrets` object explicitly.
2. The caller (FastAPI lifespan or worker startup) constructs `Secrets` from
   `config/secrets.yaml`, then calls `init_tracing(secrets)`.
3. `tracing.py` no longer reads `os.environ` for any config value.
4. The `_tracing_enabled()` check becomes `bool(secrets.langfuse_public_key
   and secrets.langfuse_secret_key and secrets.langfuse_base_url)`.

---

## 6. Load order and precedence

At startup, the config loader merges sources in this order (first = lowest
priority, last = highest):

```
1. config/mill.defaults.yaml     ← committed canonical defaults
2. config/mill.local.yaml        ← if present (dev only, gitignored)
3. config/mill.production.yaml   ← if MILL_CONFIG_FILE points to it
4. Environment variables          ← MILL_*, FORGE_*, etc.
5. config/secrets.yaml           ← loaded into SEPARATE Secrets object
```

### 6.1 How env vars override YAML

pydantic-settings reads `os.environ` natively — if an env var matching a
field alias is set, it takes precedence over whatever the YAML layers
provided.  This preserves the Docker/compose workflow where
`docker-compose.yml` `environment:` sets overrides.

The loader works as follows:

1. Parse YAML files in order (1→3), deep-merging each into a single dict.
2. Pass the merged dict as **field defaults** to a pydantic-settings
   `BaseSettings` subclass.
3. pydantic-settings then overlays `os.environ` on top — env vars win when
   set.

This means `docker-compose.yml` can still use `environment:` to override any
value without touching the YAML files.

### 6.2 The MILL_CONFIG_FILE and MILL_SECRETS_FILE env vars

Two new env vars control which override files are loaded:

| Env var | Purpose | Default |
|---|---|---|
| `MILL_CONFIG_FILE` | Path to a production override YAML file | unset → no production overlay |
| `MILL_SECRETS_FILE` | Path to a secrets YAML file | `config/secrets.yaml` |

In Docker, `docker-compose.yml` sets:

```yaml
environment:
  - MILL_CONFIG_FILE=/etc/mill/config.yaml
  - MILL_SECRETS_FILE=/run/secrets/mill_secrets.yaml
```

The production config and secrets files are bind-mounted from the host with
restricted permissions (`0600` for secrets).

---

## 7. Validation

The new system adds Pydantic validators for semantic checks beyond type
coercion.  These run at `Settings` / `Secrets` construction time, failing
fast at startup rather than deep in agent logic.

### 7.1 Example validators

**Range checks:**

```python
@field_validator("max_concurrency")
@classmethod
def max_concurrency_positive(cls, v: int) -> int:
    if v < 1:
        raise ValueError("max_concurrency must be ≥ 1")
    return v

@field_validator("model_request_timeout")
@classmethod
def timeout_positive(cls, v: float) -> float:
    if v <= 0:
        raise ValueError("model_request_timeout must be > 0")
    return v
```

**Cross-field checks:**

```python
@model_validator(mode="after")
def forge_auth_requires_credentials(self) -> "Settings":
    if self.forge_auth == "app":
        if not self.forge_github_app_id and not self.forge_github_app_private_key_path:
            raise ValueError(
                "FORGE_AUTH=app requires GITHUB_APP_ID and "
                "GITHUB_APP_PRIVATE_KEY_PATH (or _PRIVATE_KEY in secrets)"
            )
    return self

@model_validator(mode="after")
def forge_remote_required(self) -> "Settings":
    if self.forge_kind in ("github", "gitlab") and not self.forge_remote_url:
        raise ValueError(
            f"FORGE_KIND={self.forge_kind} requires FORGE_REMOTE_URL"
        )
    return self
```

**Format checks:**

```python
@field_validator("api_url")
@classmethod
def api_url_valid(cls, v: str) -> str:
    if not v.startswith(("http://", "https://")):
        raise ValueError(f"api_url must be an HTTP(S) URL, got {v!r}")
    return v
```

**Interval minimums:**

```python
@field_validator("trace_health_interval_seconds")
@classmethod
def trace_health_interval_min(cls, v: int) -> int:
    if v < 3600:
        raise ValueError(
            "trace_health_interval_seconds must be ≥ 3600 (1 hour) "
            "to avoid hammering Langfuse"
        )
    return v
```

**Rate-limit fallback consistency:**

```python
@model_validator(mode="after")
def fallback_model_consistency(self) -> "Settings":
    if self.rate_limit_fallback_model and self.rate_limit_fallback_retries < 1:
        raise ValueError(
            "rate_limit_fallback_retries must be ≥ 1 when "
            "rate_limit_fallback_model is set"
        )
    return self
```

### 7.2 Validation scope

Validation covers:
- **All `int` fields that have a meaningful range** (concurrency, retries,
  timeouts, intervals, limits).
- **All cross-field dependencies** (forge auth mode → credentials, forge kind
  → remote URL, rate-limit fallback model → retry count).
- **All URL-format fields** (`api_url`, `github_api_url`, `gitlab_api_url`,
  `langfuse_base_url`, `ntfy_url`).
- **File/directory existence** for paths that must exist at startup
  (`skills_dir`; `data_dir` is created if missing, not validated).

Fields that intentionally accept "zero means disabled" (`max_spend_usd_per_ticket`,
`rate_limit_fallback_model`) are exempt from positive-value checks.

---

## 8. Environment strategy

### 8.1 Dev (local workstation)

**Files loaded:**
- `config/mill.defaults.yaml` (committed)
- `config/mill.local.yaml` (gitignored, optional)
- `config/secrets.yaml` (gitignored, required for LLM calls)

**No `.env` file needed.**  A developer who needs different models or limits
creates `config/mill.local.yaml` with only the overrides they need:

```yaml
# config/mill.local.yaml — example
core:
  models:
    coordinator: openai/gpt-4o  # cheaper for local dev
```

The `.env` file is **retired** after migration.  During the migration window,
both sources are loaded (see §9) to avoid breaking existing setups.

### 8.2 CI (GitHub Actions)

**Files loaded:**
- `config/mill.defaults.yaml` only

Secrets are never needed — tests mock the model/HTTP seams via the existing
`_no_real_http` and `fake_sandbox` fixtures.  The `_no_dotenv` fixture in
`tests/conftest.py` is updated to:
1. Block the new YAML config files from loading (monkeypatch
   `MILL_CONFIG_FILE` and `MILL_SECRETS_FILE` to empty).
2. Continue clearing ambient credential env vars (same list as today).

CI never touches `config/secrets.yaml`, `config/mill.local.yaml`, or any
environment-specific overlay.

### 8.3 Docker / Production

**Files loaded:**
- `config/mill.defaults.yaml` (baked into the Docker image at build time)
- `config/mill.production.yaml` (bind-mounted from host, path set via
  `MILL_CONFIG_FILE` env var in `docker-compose.yml`)
- `config/secrets.yaml` (bind-mounted from host with `0600` permissions,
  path set via `MILL_SECRETS_FILE` env var)

**Changes from current state:**

| Today | v2 |
|---|---|
| `Dockerfile` sets `ENV MILL_DATA_DIR=/data` | `config/mill.production.yaml` sets `service.data_dir: /data` |
| `Dockerfile` sets `ENV MILL_API_HOST=0.0.0.0` | `config/mill.production.yaml` sets `service.api_host: "0.0.0.0"` |
| `docker-compose.yml` sets `MILL_SANDBOX_DATA_MOUNT=${PWD}/.data` | `config/mill.production.yaml` sets `sandbox.data_mount: /data` (container path) |
| CI monitor enabled in compose via env vars | per-repo `ci_monitor.enabled` / `ci_monitor.interval_seconds` in `config/repos.yaml` |

The `Dockerfile` no longer contains any `ENV` directives for application
config.  The only `ENV` directives are for system-level concerns (PATH,
Python unbuffered, etc.).

### 8.4 DOCKER_GID

`DOCKER_GID` is **removed from `.env`** and never appears in any YAML config
file.  It remains in `docker-compose.yml` as a compose variable substitution:

```yaml
group_add:
  - "${DOCKER_GID:-999}"
```

The `dev/mill-autoupdate.sh` script continues to `export DOCKER_GID` before
invoking compose — that is an orchestration concern, not application config.

---

## 9. Migration path from `.env`

The migration is designed to be incremental and lossless, executed across
multiple PRs without a flag-day cutover.

### Phase 1: Ship the new loader (alongside old)

1. Add `config/mill.defaults.yaml` and `config/secrets.example.yaml` (this
   ticket's PoC deliverables).
2. Add the YAML config loader (`src/robotsix_mill/config_loader.py`) that
   reads YAML files and produces a dict.
3. Modify `Settings.model_config` to load YAML layers **in addition to**
   `.env`/`secrets.env`.  The YAML layers populate Pydantic field defaults;
   `.env` still works as before.
4. Add the `Secrets` model and load it from `config/secrets.yaml` (if
   present) **alongside** the existing `secrets.env` path.  If both exist,
   YAML wins.
5. Ship `dev/migrate-env-to-yaml.py` (see Phase 2).  CI gains a
   `scripts/validate-config-docs` check (see §10).

**At this point:** existing `.env` files work unchanged.  Developers can opt
in to YAML by creating `config/mill.local.yaml`.  Production can test the
`config/mill.production.yaml` path without disrupting the `.env` flow.

### Phase 2: Run migration, commit defaults

1. Run `dev/migrate-env-to-yaml.py` — it reads `.env` + `secrets.env` and emits
   `config/mill.local.yaml` + `config/secrets.yaml` with only the values
   that differ from the committed defaults.
2. Review the generated production config for correctness.
3. Rename `.env` → `.env.deprecated` (kept for reference, not loaded).
4. Commit `config/mill.defaults.yaml` (already done in Phase 1).

### Phase 3: Refactor consumers

1. Update `forge/auth.py` to read from `Secrets` instead of `Settings`.
2. Update `runtime/tracing.py` to accept `Secrets` parameter.
3. Update all 46 consumer files that import `Settings` — most are
   transparent (they access the same attribute names).  Only files that
   accessed secrets through `Settings` need changes.
4. Update `tests/conftest.py::_no_dotenv` to block YAML config files instead
   of `.env`.

### Phase 4: Retire old path

1. Remove `.env` and `secrets.env` loading from `Settings.model_config`.
2. Delete `.env.deprecated` and `secrets.env.example`.
3. Update `docs/configuration.md` to reflect the YAML schema (or replace it
   with a generated reference — see §10).

### Migration script

`dev/migrate-env-to-yaml.py` performs a lossless translation:

```
Input:  .env + secrets.env (optional)
Output: config/mill.local.yaml + config/secrets.yaml
```

It reads every active (uncommented, non-empty) line from `.env` and
`secrets.env`, maps each `MILL_*` / `FORGE_*` / non-prefixed var to its
YAML dotted path using `_YAML_PATH_TO_ALIAS`, and writes the
corresponding nested YAML.  Vars that match the committed default in
`config/mill.defaults.yaml` are omitted from the local overlay (the
defaults file already covers them).  Secrets always go to
`config/secrets.yaml` with keys lowercased to match the `Secrets` model
field names.

The script is idempotent — running it twice produces the same output.
It is non-destructive (original `.env` and `secrets.env` are left
untouched).

---

## 10. Documentation strategy

### 10.1 RFC → implementation handoff

- `docs/rfc-config-v2.md` is the design RFC (this document).  It is stable
  after peer review approval.
- The implementation ticket references this RFC for architecture decisions.
  Deviations are documented as amendments to the RFC.

### 10.2 Generated reference

Once implemented, `docs/configuration.md` is replaced by (or supplemented
with) a generated reference.  Two options:

1. **Script-driven**: a `scripts/validate-config-docs` script that parses
   `config/mill.defaults.yaml` and `docs/configuration.md`, comparing every
   field.  Fails CI if they drift.  This is the lightweight option — the
   Markdown doc is still manually maintained but mechanically verified.

2. **Fully generated**: a script that reads `config/mill.defaults.yaml` and
   the Pydantic model field metadata (descriptions, types, constraints) and
   emits a complete `docs/configuration.md`.  This eliminates drift entirely
   but requires the generated Markdown to match the repo's documentation
   conventions.

**Recommendation:** Start with option 1 (validation script) and graduate to
option 2 once the schema stabilizes.  The validation script is trivial to
implement and provides immediate drift protection.

### 10.3 CI enforcement

The `scripts/validate-config-docs` check runs in CI on every PR.  If a
field is added to `config/mill.defaults.yaml` without a corresponding entry
in `docs/configuration.md` (or vice versa), CI fails with a diff showing
exactly what's missing.

This check replaces the current manual documentation maintenance, which has
already drifted by 12 fields (§2.1 of the audit).

---

## 11. Open questions / trade-offs

### 11.1 YAML anchors for shared defaults

The PoC template (`config/mill.defaults.yaml`) uses YAML anchors to DRY the
repeated `deepseek/deepseek-v4-pro` default:

```yaml
core:
  models:
    coordinator: &default_model deepseek/deepseek-v4-pro
    explore: deepseek/deepseek-v4-flash
    test: *default_model
    refine: *default_model
    # ...
```

**Trade-off:** pydantic-settings does not resolve YAML anchors natively —
the YAML parser resolves them during `yaml.safe_load()`, producing a plain
dict.  So anchors work transparently as long as the loader uses a YAML
parser that supports them (which `pyyaml` does).  The trade-off is that
anchor resolution happens once at parse time — if a consumer needs to know
"this field used the default model alias," that information is lost.

**Decision:** Use anchors.  The benefits (DRY, single point of change for
the default model) outweigh the lost metadata.  If a consumer needs to
distinguish "explicitly set" from "inherited default," that's a different
feature (explicit vs. default tracking) and not a YAML concern.

### 11.2 Secrets: singleton vs. explicit passing

**Option A:** `Secrets` is a module-level singleton, initialized once at
startup.  Consumers import it: `from robotsix_mill.config import secrets`.

**Option B:** `Secrets` is passed explicitly as a parameter to every
function that needs it.

**Recommendation:** Option B (explicit passing).  This is already the
pattern in the codebase — `build_agent(settings, ...)` takes a `Settings`
parameter.  Explicit passing makes dependencies visible, aids testing (no
global state to reset), and is consistent with the existing architecture.
The `Secrets` object is constructed once at startup and threaded through
the call stack.

### 11.3 Duplicate fields across groups

Several fields appear in multiple groups:
- Model names: `core.models.web_research` ≈ `web.research_model`
- Model names: `core.models.audit` ≈ `periodic.audit.model`
- Request limits: `core.limits.web_research_requests` ≈ `web.research_request_limit`

The PoC YAML uses anchors to keep these in sync — the value is defined once
and referenced everywhere it appears.  The loader resolves aliases during
parse, so there is exactly one value.  If a user overrides one occurrence
in their local/production YAML, the override applies only to that key path
(deep-merge semantics) — they are not magically linked.

**Recommendation:** Accept the duplication as intentional.  The groups
represent different *concerns* (model selection vs. web config, model
selection vs. periodic agent config), and the fact that they share defaults
today is an implementation detail.  A future change might give the audit
agent a different model than the default without touching `core.models`.

### 11.4 File watching / hot reload

Not in scope for v2.  The current system has no hot reload — config is read
once at startup.  The proposed system preserves this: YAML files are loaded
once when the `Settings` and `Secrets` objects are constructed.  Hot reload
(add a file watcher, re-construct models, propagate changes) is a separate
feature and not addressed here.

### 11.5 Environment variable naming convention

The current `MILL_` / `FORGE_` / unprefixed split is preserved for
backward compatibility.  The YAML keys use dot-notation paths that mirror
the group structure (e.g., `core.models.coordinator`), but the pydantic
field aliases still map to the old env var names.  New code uses the
structured accessors; old env vars continue to work for overrides.

### 11.6 Pydantic model structure

This RFC intentionally does not prescribe exact Pydantic model field
definitions.  The implementation ticket will decide:
- Whether to use nested Pydantic models (e.g., `CoreModels`, `CoreLimits`)
  for type-safe group access, or a flat `Settings` with dotted aliases.
- Whether `Settings` fields use `alias=` to map YAML paths to Python
  attribute names.
- How `model_validator(mode="before")` transforms the nested YAML dict into
  a flat structure pydantic-settings can overlay with env vars.

These are implementation details that don't affect the architecture
decisions in this RFC.

---

## Appendix A: Field count by group

| Group | Env vars | YAML keys | Notes |
|---|---|---|---|
| `core.models` | 19 | 19 | 15 share `*default_model` anchor |
| `core.limits` | 16 | 16 | |
| `core.memory` | 5 | 5 | |
| `forge` | 7 | 7 | Includes `GITHUB_APP_PRIVATE_KEY_PATH` |
| `sandbox` | 9 | 9 | |
| `web` | 6 | 6 | 2 duplicates of `core.*` via anchors |
| `gates` | 9 | 9 | 2 model duplicates via anchors |
| `pipeline` | 14 | 14 | Includes all memory-path overrides |
| `periodic.audit` | 4 | 4 | 1 model duplicate via anchor |
| `periodic.trace_health` | 2 | 2 | |
| `periodic.health` | 4 | 4 | 1 model duplicate via anchor |
| `periodic.test_gap` | 4 | 4 | 1 model duplicate via anchor |
| `periodic.agent_check` | 4 | 4 | 1 model duplicate via anchor |
| `periodic.survey` | 4 | 4 | 1 model duplicate via anchor |
| `periodic.ci_monitor` | 3 | 3 | |
| `service` | 4 | 4 | |
| `secrets` | 10 | 10 | Separate file + model |
| **Total** | **124** | **124** | 9 duplicates across groups = 115 unique values |

> The 124 YAML keys include 9 intentional duplicates (model names and
> request limits referenced in multiple groups via anchors).  The
> underlying field count is 115 unique values.

---

## Appendix B: Comparison with current state

| Concern | Today | v2 |
|---|---|---|
| Structure | Flat 115-key namespace | 17 logical groups, nested YAML |
| Secrets | Mixed into `Settings` attributes | Separate `Secrets` model + file |
| Validation | Type coercion only | Range, cross-field, format validators |
| Dev overrides | Edit committed `.env` | `config/mill.local.yaml` (gitignored) |
| CI config | Block `.env`, use `Settings()` defaults | `config/mill.defaults.yaml` only |
| Docker overrides | 3 `Dockerfile` ENV + 3 compose `environment:` | `config/mill.production.yaml` (single file) |
| Docs sync | Manual, 12 fields missing | Mechanical cross-check in CI |
| Tracing config | `os.environ` bypass | `Secrets` parameter |
| `DOCKER_GID` | Pollutes `.env` | Compose-only variable substitution |
