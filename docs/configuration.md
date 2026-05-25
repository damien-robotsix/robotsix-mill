# Configuration reference

robotsix-mill uses a **YAML-first configuration pipeline**. Settings
are loaded from committed defaults, optional local/production overlay
files, and environment variables (highest priority). Secrets (API keys,
tokens) live in a **separate** YAML file loaded by a dedicated
`Secrets` model — they are never logged and their values are redacted
in diagnostics.

---

## Configuration loading order

Settings are resolved from five layers (highest priority first):

| Priority | Source | Description |
|----------|--------|-------------|
| 1 (highest) | Explicit `Settings(k=v)` kwargs | Programmatic overrides from callers |
| 2 | `os.environ` | Any `MILL_*` or unprefixed variable set in the environment |
| 3 | YAML overlays | `config/mill.production.yaml` then `config/mill.local.yaml` (optional) |
| 4 | YAML defaults | `config/mill.defaults.yaml` (always loaded, committed) |
| 5 (lowest) | `Field(default=...)` | Static Python defaults in the Pydantic model |

YAML files are merged recursively: later layers overlay deeper keys
without replacing entire sections. The effective configuration is the
deep-merge of defaults → local → production, with environment variables
winning over any YAML value.

**Secrets** are loaded **separately** from `config/secrets.yaml`
(path overridable via `MILL_SECRETS_FILE` env var). They never
participate in the Settings merge; access them via `get_secrets()`.

---

## File structure

```
config/
  mill.defaults.yaml       # committed: canonical defaults (~115 fields)
  mill.local.yaml          # gitignored: your per-developer overrides
  mill.production.yaml     # gitignored: deployment overrides
  secrets.yaml             # gitignored: credentials (API keys, tokens)
  secrets.example.yaml     # committed: template for secrets.yaml
  repos.yaml               # per-repo board & Langfuse config (create from example)
  repos.example.yaml       # committed: template for repos.yaml
```

### Migration from `.env`

If you are upgrading from an older version that used `.env` and
`secrets.env` files, run the one-shot migration script:

```sh
python dev/migrate-env-to-yaml.py
```

This reads your existing `.env` (and optional `secrets.env`), diffs
against the committed defaults in `config/mill.defaults.yaml`, and
writes `config/mill.local.yaml` and `config/secrets.yaml` with only
the values that differ.  The original `.env` files are left untouched
— you can remove them after verifying the migration.

---

## Common tasks

### Run with a custom model

Create `config/mill.local.yaml`:

```yaml
core:
  models:
    coordinator: anthropic/claude-sonnet-4
```

Or set an environment variable (overrides YAML):

```sh
export MILL_MODEL=anthropic/claude-sonnet-4
make dev
```

### Use a different database URL / data directory

```yaml
# config/mill.local.yaml
service:
  data_dir: /data/mill-prod
```

### Enable periodic audit and trace-health checks

```yaml
# config/mill.local.yaml
periodic:
  audit:
    enabled: true
    interval_seconds: 43200
  trace_health:
    enabled: true
    interval_seconds: 86400
```

### Deploy to production with overrides

Point the `MILL_CONFIG_FILE` env var at your production overlay:

```yaml
# config/mill.production.yaml
core:
  models:
    coordinator: anthropic/claude-sonnet-4
  limits:
    max_concurrency: 2
    max_spend_usd_per_ticket: 5.0
forge:
  kind: github
  remote_url: https://github.com/your-org/your-repo
  target_branch: main
sandbox:
  test_command: pytest -q --timeout=300
```

Then run:

```sh
MILL_CONFIG_FILE=config/mill.production.yaml docker compose up -d
```

### Set up secrets

```sh
cp config/secrets.example.yaml config/secrets.yaml
# Edit config/secrets.yaml — fill in your credentials:
```

```yaml
# config/secrets.yaml
openrouter_api_key: "sk-or-..."
forge_token: "ghp_..."
# langfuse tracing (optional, leave blank to disable)
langfuse_public_key: "pk-..."
langfuse_secret_key: "sk-..."
```

File permissions should be `0600` (the YAML loader enforces a warning
if the file is group/other-readable).

### Add a new setting

1. Add the field to the Pydantic model in `src/robotsix_mill/config.py`
   (in the appropriate group class if grouped, or on `Settings` directly).
2. Add the default value to `config/mill.defaults.yaml` under the
   correct YAML key path.
3. Add the dotted-path → env-var alias mapping to
   `_YAML_PATH_TO_ALIAS` in `src/robotsix_mill/config_loader.py`.
   Without this, the setting will be silently ignored when read from YAML.
4. If it's a secret, add it to the `Secrets` model and to
   `config/secrets.example.yaml` instead.
5. Access it in code: `settings.my_new_field` for settings,
   `get_secrets().my_new_secret` for secrets.

Environment variable naming convention: use `Field(alias=...)` on the
Pydantic model with a `MILL_` prefix + uppercase with underscores
(e.g. `Field(alias="MILL_MY_NEW_FIELD")`).  The `_YAML_PATH_TO_ALIAS`
dict maps the dotted YAML path to this alias — there is no automatic
double-underscore convention.

---

## Full setting reference

Every setting below shows:
- **YAML path** — the key in `config/mill.defaults.yaml`
- **Env var** — the environment variable override
- **Default** — the committed default value
- **Description** — what it controls

### 1. Core models

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.models.coordinator` | `MILL_MODEL` | `deepseek/deepseek-v4-pro` | Coordinator model — reads/edits the repo, delegates to sub-agents |
| `core.models.explore` | `MILL_EXPLORE_MODEL` | `deepseek/deepseek-v4-flash` | Scout sub-agent — returns concise pointers, never whole files |
| `core.models.test` | `MILL_TEST_MODEL` | `deepseek/deepseek-v4-pro` | Test sub-agent — distills suite failures into diagnosis |
| `core.models.refine` | `MILL_REFINE_MODEL` | `deepseek/deepseek-v4-pro` | Refine agent — authors engineering specs from drafts |
| `core.models.answer` | `MILL_ANSWER_MODEL` | `deepseek/deepseek-v4-pro` | Answer agent — investigative Q&A via repo + web + traces |
| `core.models.retrospect` | `MILL_RETROSPECT_MODEL` | `deepseek/deepseek-v4-pro` | Retrospect agent — audits finished tickets; proposes improvements |
| `core.models.audit` | `MILL_AUDIT_MODEL` | `deepseek/deepseek-v4-pro` | Audit agent — meta-audit for quality/security coverage gaps |
| `core.models.dedup` | `MILL_DEDUP_MODEL` | `deepseek/deepseek-v4-pro` | Dedup agent — pre-refine duplicate/already-done check |
| `core.models.web_research` | `MILL_WEB_RESEARCH_MODEL` | `deepseek/deepseek-v4-pro` | Web-research sub-agent — web lookups, conclusion only |
| `core.models.review` | `MILL_REVIEW_MODEL` | `deepseek/deepseek-v4-pro` | Review agent — blind dual-model diff audit (opt-in) |
| `core.models.trace_inspector` | `MILL_TRACE_INSPECTOR_MODEL` | `deepseek/deepseek-v4-pro` | Trace-inspector sub-agent — inspects full Langfuse observation tree |
| `core.models.test_gap` | `MILL_TEST_GAP_MODEL` | `deepseek/deepseek-v4-pro` | Test-gap agent — identifies modules with zero dedicated tests |
| `core.models.agent_check` | `MILL_AGENT_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | Agent-check agent — audits agent definitions for coherence |
| `core.models.health` | `MILL_HEALTH_MODEL` | `deepseek/deepseek-v4-pro` | Health agent — codebase-health across 6 dimensions |
| `core.models.survey` | `MILL_SURVEY_MODEL` | `deepseek/deepseek-v4-pro` | Survey agent — discovers OSS projects; proposes improvements |
| `core.models.bc_check` | `MILL_BC_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | BC-check agent — backward-compatibility scanner |
| `core.models.completeness_check` | `MILL_COMPLETENESS_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | Completeness-check agent — feature-wiring completeness scanner |
| `core.models.rate_limit_fallback` | `MILL_RATE_LIMIT_FALLBACK_MODEL` | `""` (disabled) | Fallback model when rate-limit retries exhausted |
| `core.models.doc` | `MILL_DOC_MODEL` | `deepseek/deepseek-v4-pro` | Documentation agent |
| `core.models.triage` | `MILL_TRIAGE_MODEL` | `openai/gpt-4o-mini` | Pre-refine triage — fast/cheap classification |
| `core.models.auto_approve` | `MILL_AUTO_APPROVE_MODEL` | `openai/gpt-4o-mini` | Model for the auto-approve triage call (must be fast and cheap) |

### 2. Request limits

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.coordinator_requests` | `MILL_COORDINATOR_REQUEST_LIMIT` | `200` | Per-ticket request cap for the implement (coordinator) agent |
| `core.limits.explore_requests` | `MILL_EXPLORE_REQUEST_LIMIT` | `20` | Per-call request cap for the explore sub-agent |
| `core.limits.test_requests` | `MILL_TEST_REQUEST_LIMIT` | `8` | Per-call request cap for the test sub-agent |
| `core.limits.web_research_requests` | `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `8` | Per-call request cap for the web-research sub-agent |
| `core.limits.dedup_requests` | `MILL_DEDUP_REQUEST_LIMIT` | `4` | Per-call request cap for the dedup check |
| — (env-var only) | `MILL_DOC_REQUEST_LIMIT` | `4` | Per-run request cap for the document agent |
| — (env-var only) | `MILL_REVIEW_REQUEST_LIMIT` | `20` | Per-run request cap for the review agent |

### 3. Worker pool & retry

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.max_concurrency` | `MILL_MAX_CONCURRENCY` | `4` | Max parallel tickets in the worker pool |
| `core.limits.max_fix_iterations` | `MILL_MAX_FIX_ITERATIONS` | `8` | Max implement→test fix loop iterations before BLOCK |
| `core.limits.max_stuck_cycles` | `MILL_MAX_STUCK_CYCLES` | `3` | Re-entries to same stage without progress before BLOCK |
| `core.limits.max_spend_usd_per_ticket` | `MILL_MAX_SPEND_USD_PER_TICKET` | `0.0` | Dollar cap per ticket (0.0 = disabled) |
| `core.limits.transient_retries` | `MILL_TRANSIENT_RETRIES` | `4` | Max retries for transient network/model failures (429, 5xx, timeouts) |
| `core.limits.transient_backoff_base` | `MILL_TRANSIENT_BACKOFF_BASE` | `2.0` | Base seconds for exponential backoff (jittered) |
| `core.limits.transient_backoff_cap` | `MILL_TRANSIENT_BACKOFF_CAP` | `30.0` | Max seconds between transient retries |
| `core.limits.rate_limit_backoff_base` | `MILL_RATE_LIMIT_BACKOFF_BASE` | `30.0` | Base seconds for rate-limit backoff (longer window) |
| `core.limits.rate_limit_backoff_cap` | `MILL_RATE_LIMIT_BACKOFF_CAP` | `120.0` | Max seconds between rate-limit retries |
| `core.limits.rate_limit_fallback_retries` | `MILL_RATE_LIMIT_FALLBACK_RETRIES` | `3` | Consecutive rate-limit failures before switching to fallback model |
| `core.limits.model_request_timeout` | `MILL_MODEL_REQUEST_TIMEOUT` | `900.0` | Hard per-call timeout in seconds for every model request |

### 4. Memory

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.memory.max_memory_chars` | `MILL_MAX_MEMORY_CHARS` | `8000` | Max characters loaded from any memory ledger per agent pass |
| `core.memory.reference_files_max_count` | `MILL_REFERENCE_FILES_MAX_COUNT` | `5` | Max files whose full content refine stores |
| `core.memory.reference_files_max_total_lines` | `MILL_REFERENCE_FILES_MAX_TOTAL_LINES` | `3000` | Max total lines across selected reference files |
| `pipeline.implement_memory_path` | `MILL_IMPLEMENT_MEMORY_PATH` | `None` | Override path for implement memory; defaults to `<data_dir>/implement_memory.md` |
| `pipeline.refine_memory_path` | `MILL_REFINE_MEMORY_PATH` | `None` | Override path for refine memory; defaults to `<data_dir>/refine_memory.md` |
| `pipeline.ci_fix_memory_path` | `MILL_CI_FIX_MEMORY_PATH` | `None` | Override path for CI-fix memory; defaults to `<data_dir>/ci_fix_memory.md` |
| `pipeline.rebase_memory_path` | `MILL_REBASE_MEMORY_PATH` | `None` | Override path for rebase memory; defaults to `<data_dir>/rebase_memory.md` |

### 5. Dedup

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.memory.dedup_lookback_days` | `MILL_DEDUP_LOOKBACK_DAYS` | `30` | Days back to consider closed tickets as dup candidates |
| `core.memory.dedup_lookback_commits` | `MILL_DEDUP_LOOKBACK_COMMITS` | `20` | Recent commits to inspect for "already done" |

### 6. Service (management plane)

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `service.data_dir` | `MILL_DATA_DIR` | `.mill-data` | Data directory for DB, workspaces, and memory ledgers |
| `service.api_host` | `MILL_API_HOST` | `127.0.0.1` | FastAPI listen address |
| `service.api_port` | `MILL_API_PORT` | `8077` | FastAPI listen port |
| `service.api_url` | `MILL_API_URL` | `http://127.0.0.1:8077` | Base URL the CLI client uses to reach the API |

### 7. Approval & review

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `gates.require_approval` | `MILL_REQUIRE_APPROVAL` | `true` | Pause after refine for human approval (`awaiting_approval` state) |
| `gates.auto_approve_enabled` | `MILL_AUTO_APPROVE_ENABLED` | `false` | Enable conservative auto-approve triage |
| `gates.auto_approve_model` | `MILL_AUTO_APPROVE_MODEL` | `openai/gpt-4o-mini` | Model for auto-approve triage (fast + cheap) |
| `gates.review_enabled` | `MILL_REVIEW_ENABLED` | `false` | Enable dual-model code review stage before deliver |
| `gates.review_model` | `MILL_REVIEW_MODEL` | `deepseek/deepseek-v4-pro` | Review agent model |
| `gates.review_max_rounds` | `MILL_REVIEW_MAX_ROUNDS` | `3` | Max CODE_REVIEW round-trips before escalate |
| `gates.refine_triage_enabled` | `MILL_REFINE_TRIAGE_ENABLED` | `true` | Cheap triage before full refine (skip if precise) |
| `gates.spec_review_enabled` | `MILL_SPEC_REVIEW_ENABLED` | `false` | Post-refinement spec narrative stripping |
| `gates.auto_merge_enabled` | `MILL_AUTO_MERGE_ENABLED` | `false` | Auto-merge PR when CI passes |

### 8. Forge

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `forge.kind` | `FORGE_KIND` | `none` | Forge platform: `github`, `gitlab`, or `none` |
| `forge.remote_url` | `FORGE_REMOTE_URL` | `None` | Remote URL for clone + push |
| `forge.target_branch` | `FORGE_TARGET_BRANCH` | `main` | Target branch for PRs |
| `forge.auth_mode` | `FORGE_AUTH` | `token` | Auth mode: `token` (PAT) or `app` (GitHub App) |
| `forge.github_api_url` | `MILL_GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL (override for GitHub Enterprise) |
| `forge.gitlab_api_url` | `MILL_GITLAB_API_URL` | `https://gitlab.com/api/v4` | GitLab API base URL (override for self-hosted GitLab) |
| `forge.github_app_private_key_path` | `GITHUB_APP_PRIVATE_KEY_PATH` | `None` | Host path to GitHub App private-key `.pem` file |

### 9. Sandbox

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `sandbox.image` | `MILL_SANDBOX_IMAGE` | `python:3.14-slim` | Docker image for disposable sandbox containers |
| `sandbox.memory` | `MILL_SANDBOX_MEMORY` | `2g` | Memory limit for sandbox containers |
| `sandbox.pids_limit` | `MILL_SANDBOX_PIDS_LIMIT` | `512` | PID limit for sandbox containers |
| `sandbox.readonly` | `MILL_SANDBOX_READONLY` | `true` | Mount sandbox rootfs read-only (except tmpfs `/tmp`) |
| `sandbox.command_timeout` | `MILL_COMMAND_TIMEOUT` | `900` | Wall-clock cap (seconds) for sandbox shell/test commands |
| `sandbox.data_volume` | `MILL_DATA_VOLUME` | `mill_data` | Named Docker volume for data (fallback when not bind-mounted) |
| `sandbox.data_mount` | `MILL_SANDBOX_DATA_MOUNT` | `None` | Host path for bind-mounted data directory (overrides `data_volume`) |
| `sandbox.test_command` | `MILL_TEST_COMMAND` | `pytest -q` | Command run to verify the implementation (empty = skip) |

### 10. Web research

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `web.search_enabled` | `MILL_WEB_SEARCH` | `true` | Enable web-search capability (delegated to sub-agent) |
| `web.research_model` | `MILL_WEB_RESEARCH_MODEL` | `deepseek/deepseek-v4-pro` | Web-research sub-agent model (also reachable via `core.models.web_research`) |
| `web.research_request_limit` | `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `8` | Per-call request cap for web research (also reachable via `core.limits.web_research_requests`) |
| `web.fetch_image` | `MILL_FETCH_IMAGE` | `curlimages/curl:8.17.0` | Docker image for isolated `web_fetch` container |
| `web.fetch_max_bytes` | `MILL_WEB_FETCH_MAX_BYTES` | `2000000` | Max bytes fetched per URL |
| `web.fetch_timeout` | `MILL_WEB_FETCH_TIMEOUT` | `30` | Timeout (seconds) per web fetch |

### 11. Pipeline tail (merge stage)

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `pipeline.merge_poll_seconds` | `MILL_MERGE_POLL_SECONDS` | `120` | Poll interval for PR merge/CI status |
| `pipeline.rebase_max_attempts` | `MILL_REBASE_MAX_ATTEMPTS` | `5` | Max rebase LLM invocations before BLOCK |
| `pipeline.ci_fix_max_attempts` | `MILL_CI_FIX_MAX_ATTEMPTS` | `2` | Max CI-fix LLM invocations before BLOCK |
| `pipeline.branch_prefix` | `MILL_BRANCH_PREFIX` | `mill/` | Prefix for deliver-stage branch names |
| `pipeline.prune_clone_on_close` | `MILL_PRUNE_CLONE_ON_CLOSE` | `true` | Delete workspace repo clone on ticket close |
| `pipeline.max_archived_tickets` | `MILL_MAX_ARCHIVED_TICKETS` | `100` | Max terminal-state tickets retained (0 = no purge) |

### 12. Periodic agents

Each periodic agent shares this pattern:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.<name>.enabled` | `MILL_<NAME>_PERIODIC` | `false`¹ | Enable periodic passes |
| `periodic.<name>.interval_seconds` | `MILL_<NAME>_INTERVAL_SECONDS` | `86400` | Seconds between automatic passes |
| `periodic.<name>.memory_path` | `MILL_<NAME>_MEMORY_PATH` | `None` | Override path for memory ledger ² |

Periodic agents: `audit`, `trace_health`, `health`, `test_gap`,
`agent_check`, `survey`, `ci_monitor`, `env_sync`, `completeness_check`.

> ¹ `survey` is the exception — its default is `enabled: true`.
>
> ² `trace_health` and `ci_monitor` do **not** have a `memory_path`
> field — they write no per-agent memory ledger.
>
> `env_sync`, `bc_check`, and `completeness_check` are **env-var-only** (no YAML mapping yet).
> Set `MILL_ENV_SYNC_PERIODIC=true`, `MILL_BC_CHECK_PERIODIC=true`, etc.

Additional fields:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.ci_monitor.log_max_bytes` | `MILL_CI_LOG_MAX_BYTES` | `65536` | Max bytes fetched per CI job log |
| `pipeline.retrospect_spawn_drafts` | `MILL_RETROSPECT_SPAWN_DRAFTS` | `true` | Allow retrospect to file improvement draft tickets |
| `pipeline.retrospect_deep_analysis_frequency` | `MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY` | `10` | How many retrospect runs between deep trace analyses |
| `pipeline.retrospect_memory_path` | `MILL_RETROSPECT_MEMORY_PATH` | `None` | Override path for retrospect memory |
| `pipeline.trace_inspector_memory_path` | `MILL_TRACE_INSPECTOR_MEMORY_PATH` | `None` | Override path for trace-inspector memory |

#### Env-var-only periodic agents

`env_sync`, `bc_check`, and `completeness_check` have no YAML mapping yet — set them via
environment variables only:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_ENV_SYNC_PERIODIC` | `false` | Enable periodic config/docs drift detection |
| `MILL_ENV_SYNC_INTERVAL_SECONDS` | `86400` | Seconds between env-sync passes |
| `MILL_ENV_SYNC_MODEL` | `openai/gpt-4o-mini` | Env-sync agent model |
| `MILL_BC_CHECK_PERIODIC` | `false` | Enable periodic backward-compatibility inspection |
| `MILL_BC_CHECK_INTERVAL_SECONDS` | `86400` | Seconds between bc-check passes |
| `MILL_BC_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | BC-check agent model |
| `MILL_COMPLETENESS_CHECK_PERIODIC` | `false` | Enable periodic feature-wiring completeness inspection |
| `MILL_COMPLETENESS_CHECK_INTERVAL_SECONDS` | `86400` | Seconds between completeness-check passes |
| `MILL_COMPLETENESS_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | Completeness-check agent model |

### 13. Skills

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `sandbox.skills_dir` | `MILL_SKILLS_DIR` | `skills` | Directory of skill docs injected into agent system prompts |

---

## Secrets reference

Secrets are loaded from `config/secrets.yaml` by a separate `Secrets`
Pydantic model. They are **not** merged into `Settings` — access them
via `get_secrets()`.

| YAML key | Env var override | Description |
|----------|-----------------|-------------|
| `openrouter_api_key` | `OPENROUTER_API_KEY` | OpenRouter API key (required for any LLM call) |
| `forge_token` | `FORGE_TOKEN` | PAT for forge authentication |
| `github_app_id` | `GITHUB_APP_ID` | GitHub App ID (when `FORGE_AUTH=app`) |
| `github_app_private_key` | `GITHUB_APP_PRIVATE_KEY` | GitHub App private key (inline PEM, newlines as `\n`) |
| `langfuse_public_key` | `LANGFUSE_PUBLIC_KEY` | Langfuse public key (tracing) |
| `langfuse_secret_key` | `LANGFUSE_SECRET_KEY` | Langfuse secret key |
| `langfuse_base_url` | `LANGFUSE_BASE_URL` | Langfuse base URL |
| `langfuse_project_id` | `LANGFUSE_PROJECT_ID` | Langfuse project ID (optional) |
| `ntfy_url` | `NTFY_URL` | ntfy.sh topic URL for notifications |
| `ntfy_token` | `NTFY_TOKEN` | ntfy.sh bearer token (optional) |

Secrets file path: `config/secrets.yaml` (overridable via
`MILL_SECRETS_FILE` env var). Template: `config/secrets.example.yaml`.

---

## Repos registry

The repos registry maps each repository to its own board identity and
Langfuse observability project. It is loaded **separately** from
`Settings` by a dedicated `ReposRegistry` Pydantic model — it never
participates in the Settings merge. Access it via `get_repos_config()`
or `get_repo_config("repo-id")`.

### Set up

```sh
cp config/repos.example.yaml config/repos.yaml
# Edit config/repos.yaml — add one entry per repository:
```

```yaml
# config/repos.yaml
repos:
  my-repo:
    board_id: "my-board"
    langfuse:
      project_name: "my-repo"
      public_key: "pk-lf-..."
      secret_key: "sk-lf-..."
      base_url: "https://cloud.langfuse.com"  # optional — defaults to cloud
```

File path: `config/repos.yaml` (overridable via `MILL_REPOS_FILE` env var).
Set `MILL_REPOS_FILE=""` to disable repos config entirely. Template:
`config/repos.example.yaml`.

### Field reference

| YAML key | Required | Default | Description |
|----------|----------|---------|-------------|
| `repos.<id>.board_id` | yes | — | Board identifier for per-repo board isolation |
| `repos.<id>.langfuse.project_name` | yes | — | Langfuse project name for this repo's traces |
| `repos.<id>.langfuse.public_key` | yes | — | Langfuse public key for this repo's project |
| `repos.<id>.langfuse.secret_key` | yes | — | Langfuse secret key for this repo's project |
| `repos.<id>.langfuse.base_url` | no | `https://cloud.langfuse.com` | Langfuse base URL |

Each repo ID must be unique and non-empty. The `board_id` must also be
non-empty. The registry validates that every entry's `repo_id` matches
its YAML key.

---

## See also

- [index.md](index.md) — documentation home
- [deployment.md](deployment.md) — continuous deployment guide
- [config-audit.md](config-audit.md) — complete inventory of every config value and its source
- [`config/mill.defaults.yaml`](../config/mill.defaults.yaml) — committed canonical defaults
- [`config/secrets.example.yaml`](../config/secrets.example.yaml) — secrets template
