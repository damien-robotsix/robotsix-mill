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
python scripts/migrate-config
```

This reads your existing `.env` and `secrets.env` (if present), maps
each variable to its YAML dotted path per the configuration reference,
and writes:

- `config/mill.production.yaml` — non-secret overrides (only values that
  differ from the committed defaults in `config/mill.defaults.yaml`)
- `config/secrets.yaml` — all secret values (API keys, tokens, etc.)

The original `.env` and `secrets.env` files are left untouched — you
can remove them after verifying the migration. Use `--dry-run` to see
what would be written without modifying disk:

```sh
python scripts/migrate-config --dry-run
```

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

This `sandbox.test_command` is the global fallback. A managed repo can
override it by committing a `test_command` to its own
`.robotsix-mill/config.yaml`, and the operator can override it per repo
in `repos.yaml`. The precedence is: per-repo `.robotsix-mill/config.yaml`
`test_command` > `repos.yaml` per-repo `test_command` > this global
`sandbox.test_command`; empty everywhere makes the gate pass.

### Per-language instructions

A repo declares the language(s) it uses in the same
`.robotsix-mill/config.yaml`:

```yaml
languages: [python, rust]   # or singular: language: python
```

When set, the **implement** and **refine** agents receive a
`## Language conventions` block for each declared language, appended to
their system prompt. Each snippet is resolved per language with this
precedence: the repo's own
`.robotsix-mill/language_instructions/<lang>.md` (house override) if
present, otherwise the mill's built-in
`agent_definitions/language_instructions/<lang>.md`. If neither exists
the language is silently skipped. The language source itself falls back
to `repos.yaml`'s per-repo `language` when the repo file declares none.

### Extra sandbox packages

A repo can declare extra OS/pip packages that the sandbox should install
before running any command (test gate, implement `run_command`, etc.):

```yaml
# .robotsix-mill/config.yaml
extra_sandbox_packages:
  - colcon              # ROS2 build tool (defaults to apt)
  - pip:my-test-lib     # Python-only dep via pip
  - apt:tree            # explicit apt for clarity
```

**Entry formats.** Each string in the list is parsed with this
prefix convention:

| Format | Install method | Example |
|--------|---------------|---------|
| `apt:<name>` | `apt-get install -y` | `apt:colcon` |
| `pip:<name>` | `pip install --user` | `pip:my-test-lib` |
| bare `<name>` | defaults to **apt** (the sandbox is Debian-based) | `colcon` |

**Trade-offs.**

* **Apt packages** cause the sandbox to drop `--read-only` mode and add
  tmpfs mounts for apt state directories (`/var/cache/apt`,
  `/var/lib/apt/lists`, `/var/lib/dpkg`). The container is slightly
  larger and the first-run setup is slower (`apt-get update` +
  `apt-get install`).
* **Pip-only packages** are lighter: they keep `--read-only` and install
  into the user site (`~/.local` via `--user`), so only a writable
  `/tmp` tmpfs is needed.
* Each extra package adds to the per-ticket sandbox startup time — prefer
  baking common dependencies into the sandbox image when latency matters.

**Resilience.** Installation failures are soft-warnings: the sandbox
still starts and the command still runs. Malformed values (not a list,
or non-string items) silently yield an empty package list — a managed
repo cannot break mill by committing a broken config file.

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
| `core.models.ask_to_ticket` | `MILL_ASK_TO_TICKET_MODEL` | `deepseek/deepseek-v4-pro` | Ask-to-ticket agent — drafts task tickets from answered inquiries' Q&A |
| `core.models.retrospect` | `MILL_RETROSPECT_MODEL` | `deepseek/deepseek-v4-pro` | Retrospect agent — audits finished tickets; proposes improvements |
| `core.models.audit` | `MILL_AUDIT_MODEL` | `deepseek/deepseek-v4-pro` | Audit agent — meta-audit for quality/security coverage gaps |
| `core.models.dedup` | `MILL_DEDUP_MODEL` | `deepseek/deepseek-v4-pro` | Dedup agent — pre-refine duplicate/already-done check |
| `core.models.obsolescence` | `MILL_OBSOLESCENCE_MODEL` | `deepseek/deepseek-v4-flash` | Obsolescence agent — pre-refine gap re-validation check |
| `core.models.web_research` | `MILL_WEB_RESEARCH_MODEL` | `deepseek/deepseek-v4-pro` | Web-research sub-agent — web lookups, conclusion only |
| `core.models.review` | `MILL_REVIEW_MODEL` | `deepseek/deepseek-v4-pro` | Review agent — blind dual-model diff audit (opt-in) |
| `core.models.review_revision` | `MILL_REVIEW_REVISION_MODEL` | `deepseek/deepseek-v4-pro` | Review-revision agent — autonomously implements changes requested by human reviewers (opt-in) |
| `core.models.trace_inspector` | `MILL_TRACE_INSPECTOR_MODEL` | `deepseek/deepseek-v4-pro` | Trace-inspector sub-agent — inspects full Langfuse observation tree |
| `core.models.test_gap` | `MILL_TEST_GAP_MODEL` | `deepseek/deepseek-v4-pro` | Test-gap agent — identifies modules with zero dedicated tests |
| `core.models.agent_check` | `MILL_AGENT_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | Agent-check agent — audits agent definitions for coherence |
| `core.models.health` | `MILL_HEALTH_MODEL` | `deepseek/deepseek-v4-pro` | Health agent — codebase-health across 6 dimensions |
| `core.models.survey` | `MILL_SURVEY_MODEL` | `deepseek/deepseek-v4-pro` | Survey agent — discovers OSS projects; proposes improvements |
| `core.models.bc_check` | `MILL_BC_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | BC-check agent — backward-compatibility scanner |
| `core.models.completeness_check` | `MILL_COMPLETENESS_CHECK_MODEL` | `deepseek/deepseek-v4-pro` | Completeness-check agent — feature-wiring completeness scanner |
| `core.models.rate_limit_fallback` | `MILL_RATE_LIMIT_FALLBACK_MODEL` | `""` (disabled) | Fallback model when rate-limit retries exhausted |
| `core.models.doc` | `MILL_DOC_MODEL` | `deepseek/deepseek-v4-pro` | Documentation agent |
| `core.models.doc_classifier` | `MILL_DOC_CLASSIFIER_MODEL` | `deepseek/deepseek-v4-flash` | Doc-diff classifier gate — cheap pre-check before full doc agent |
| `core.models.triage` | `MILL_TRIAGE_MODEL` | `deepseek/deepseek-v4-flash` | Pre-refine triage — fast/cheap classification |
| `core.models.auto_approve` | `MILL_AUTO_APPROVE_MODEL` | `deepseek/deepseek-v4-flash` | Model for the auto-approve triage call (must be fast and cheap) |
| `core.models.scope_triage` | `MILL_SCOPE_TRIAGE_MODEL` | `deepseek/deepseek-v4-flash` | Scope-triage model — classifies out-of-scope changes as EXPAND/REJECT/ESCALATE |

### 2. Request limits

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.coordinator_requests` | `MILL_COORDINATOR_REQUEST_LIMIT` | `200` | Per-ticket request cap for the implement (coordinator) agent |
| `core.limits.explore_requests` | `MILL_EXPLORE_REQUEST_LIMIT` | `100` | Per-call request cap for the explore sub-agent |
| `core.limits.consult_requests` | `MILL_CONSULT_REQUEST_LIMIT` | `15` | Per-call request cap for the domain-expert consultation sub-agent |
| `core.limits.test_requests` | `MILL_TEST_REQUEST_LIMIT` | `8` | Per-call request cap for the test sub-agent |
| `core.limits.web_research_requests` | `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `8` | Per-call request cap for the web-research sub-agent |
| `core.limits.dedup_requests` | `MILL_DEDUP_REQUEST_LIMIT` | `4` | Per-call request cap for the dedup check |
| `core.limits.obsolescence_requests` | `MILL_OBSOLESCENCE_REQUEST_LIMIT` | `6` | Per-call request cap for the obsolescence gate |
| `core.limits.scope_triage_requests` | `MILL_SCOPE_TRIAGE_REQUEST_LIMIT` | `4` | Per-call request cap for the scope-triage agent |
| — (env-var only) | `MILL_DOC_REQUEST_LIMIT` | `4` | Per-run request cap for the document agent |
| `core.limits.doc_classifier_requests` | `MILL_DOC_CLASSIFIER_REQUEST_LIMIT` | `3` | Per-call request cap for the doc-classifier gate |
| — (env-var only) | `MILL_REVIEW_REQUEST_LIMIT` | `20` | Per-run request cap for the review agent |

### 3. Worker pool & retry

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.max_concurrency` | `MILL_MAX_CONCURRENCY` | `4` | Max parallel tickets in the worker pool |
| `core.limits.max_fix_iterations` | `MILL_MAX_FIX_ITERATIONS` | `8` | Max implement→test fix loop iterations before BLOCK |
| `core.limits.max_stuck_cycles` | `MILL_MAX_STUCK_CYCLES` | `3` | Re-entries to same stage without progress before BLOCK |
| `core.limits.max_spend_usd_per_ticket` | `MILL_MAX_SPEND_USD_PER_TICKET` | `0.0` | Dollar cap per ticket (0.0 = disabled) |
| `core.limits.stage_timeout_seconds` | `MILL_STAGE_TIMEOUT_SECONDS` | `1800` | Per-stage wall-clock timeout in seconds; stage that exceeds it is escalated to BLOCKED (≤ 0 disables) |
| `core.limits.stage_timeout_overrides` | `MILL_STAGE_TIMEOUT_OVERRIDES` | `{}` | Per-stage overrides as a JSON dict (e.g. `{"merge":0,"deliver":0}`); keys are stage names, values are seconds; 0 disables timeout for that stage |
| `core.limits.transient_retries` | `MILL_TRANSIENT_RETRIES` | `4` | Max retries for transient LLM-call failures (429, 5xx, timeouts) |
| `core.limits.transient_backoff_base` | `MILL_TRANSIENT_BACKOFF_BASE` | `2.0` | Base seconds for exponential backoff at LLM-call level (jittered) |
| `core.limits.transient_backoff_cap` | `MILL_TRANSIENT_BACKOFF_CAP` | `30.0` | Max seconds between LLM-call retries |
| `core.limits.stage_retry_max_attempts` | `MILL_STAGE_RETRY_MAX_ATTEMPTS` | `5` | Max automatic retries for transient stage-level failures (git outage, provider 5xx, connection refused) |
| `core.limits.stage_retry_base_delay` | `MILL_STAGE_RETRY_BASE_DELAY` | `30.0` | Base seconds for stage-level exponential backoff |
| `core.limits.stage_retry_max_delay` | `MILL_STAGE_RETRY_MAX_DELAY` | `300.0` | Max seconds between stage-level retries |
| `core.limits.rate_limit_backoff_base` | `MILL_RATE_LIMIT_BACKOFF_BASE` | `30.0` | Base seconds for rate-limit backoff (longer window) |
| `core.limits.rate_limit_backoff_cap` | `MILL_RATE_LIMIT_BACKOFF_CAP` | `120.0` | Max seconds between rate-limit retries |
| `core.limits.rate_limit_fallback_retries` | `MILL_RATE_LIMIT_FALLBACK_RETRIES` | `3` | Consecutive rate-limit failures before switching to fallback model |
| `core.limits.model_request_timeout` | `MILL_MODEL_REQUEST_TIMEOUT` | `900.0` | Hard per-call timeout in seconds for every model request |

### 4. Memory

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.memory.max_memory_chars` | `MILL_MAX_MEMORY_CHARS` | `8000` | Max characters loaded from any memory ledger per agent pass |
| `core.memory.retrospect_log_max_chars` | `MILL_RETROSPECT_LOG_MAX_CHARS` | `12000` | Max characters of the retrospect stage's history + comments logs (keeps most-recent, drops oldest; `0` disables) |
| `core.memory.reference_files_max_count` | `MILL_REFERENCE_FILES_MAX_COUNT` | `5` | Max files whose full content refine stores |
| `core.memory.reference_files_max_total_lines` | `MILL_REFERENCE_FILES_MAX_TOTAL_LINES` | `3000` | Max total lines across selected reference files |
| `pipeline.implement_memory_path` | `MILL_IMPLEMENT_MEMORY_PATH` | `None` | Override path for implement memory; defaults to `<data_dir>/implement_memory.md` |
| `pipeline.refine_memory_path` | `MILL_REFINE_MEMORY_PATH` | `None` | Override path for refine memory; defaults to `<data_dir>/refine_memory.md` |
| `pipeline.ci_fix_memory_path` | `MILL_CI_FIX_MEMORY_PATH` | `None` | Override path for CI-fix memory; defaults to `<data_dir>/ci_fix_memory.md` |
| `pipeline.rebase_memory_path` | `MILL_REBASE_MEMORY_PATH` | `None` | Override path for rebase memory; defaults to `<data_dir>/rebase_memory.md` |
| `pipeline.review_revision_memory_path` | `MILL_REVIEW_REVISION_MEMORY_PATH` | `None` | Override path for review-revision memory; defaults to `<data_dir>/review_revision_memory.md` |
| `pipeline.ci_patterns_path` | `MILL_CI_PATTERNS_PATH` | `None` | Override path for the ci-fix agent's structured pattern memory; defaults to `<data_dir>/ci_patterns.json` |

### 5. Dedup

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.memory.dedup_lookback_days` | `MILL_DEDUP_LOOKBACK_DAYS` | `30` | Days back to consider closed tickets as dup candidates |
| `epic_dedup_lookback_days` | `MILL_EPIC_DEDUP_LOOKBACK_DAYS` | `7` | Recency window (days) for the epic-decomposition pre-filing dedup recent-ticket check (see [epic-dedup.md](epic-dedup.md)) |
| `core.limits.dedup_skip_on_no_overlap` | `MILL_DEDUP_SKIP_ON_NO_OVERLAP` | `true` | Skip dedup LLM call when draft shares no token overlap with any candidate — saves cost in the "clearly unrelated" case |
| `core.limits.dedup_candidate_body_max_chars` | `MILL_DEDUP_CANDIDATE_BODY_MAX_CHARS` | `4000` | Cap each candidate body fed to dedup prompt; ≤0 disables truncation |

### 6. Service (management plane)

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `service.data_dir` | `MILL_DATA_DIR` | `.mill-data` | Data directory for DB, workspaces, and memory ledgers |
| `service.default_repo_id` | `MILL_DEFAULT_REPO_ID` | `""` | Backward-compatibility fallback: board_id assigned to tickets created before the mandatory-board_id migration. Not a substitute for configuring repos.yaml. |
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
| `gates.review_model` | `MILL_REVIEW_MODEL` | `deepseek/deepseek-v4-flash` | Review agent model |
| `gates.review_max_rounds` | `MILL_REVIEW_MAX_ROUNDS` | `3` | Max CODE_REVIEW round-trips before escalate |
| `gates.refine_triage_enabled` | `MILL_REFINE_TRIAGE_ENABLED` | `true` | Cheap triage before full refine (skip if precise) |
| `gates.freshness_gate_enabled` | `MILL_FRESHNESS_GATE_ENABLED` | `false` | Pre-refine freshness check: verify cited evidence paths exist on HEAD |
| `gates.obsolescence_gate_enabled` | `MILL_OBSOLESCENCE_GATE_ENABLED` | `false` | Pre-refine obsolescence check: re-validate spawned-draft gaps (opt-in) |
| `gates.spec_review_enabled` | `MILL_SPEC_REVIEW_ENABLED` | `false` | Post-refinement spec narrative stripping |
| `gates.scope_triage_enabled` | `MILL_SCOPE_TRIAGE_ENABLED` | `true` | Cheap scope-violation triage before blocking (EXPAND/REJECT/ESCALATE) |
| `gates.auto_merge_enabled` | `MILL_AUTO_MERGE_ENABLED` | `false` | Auto-merge PR when CI passes |
| `gates.review_feedback_enabled` | `MILL_REVIEW_FEEDBACK_ENABLED` | `false` | Enable autonomous review-revision agent (opt-in — implements changes requested by human reviewers) |
| `gates.review_revision_model` | `MILL_REVIEW_REVISION_MODEL` | `deepseek/deepseek-v4-pro` | Review-revision agent model |
| `gates.comments_after_body` | `MILL_COMMENTS_AFTER_BODY` | `false` | Render description.md before comments in ticket detail drawer |
### 8. Forge

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `forge.kind` | `FORGE_KIND` | `none` | Forge platform: `github`, `gitlab`, `auto`, or `none`. `auto` detects the kind from the remote URL hostname (`github.com` → GitHub, `gitlab.com` → GitLab); custom domains raise an error and require an explicit setting. |
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
| `sandbox.command_timeout` | `MILL_COMMAND_TIMEOUT` | `1800` | Wall-clock cap (seconds) for sandbox shell/test commands |
| `sandbox.data_volume` | `MILL_DATA_VOLUME` | `mill_data` | Named Docker volume for data (fallback when not bind-mounted) |
| `sandbox.data_mount` | `MILL_SANDBOX_DATA_MOUNT` | `None` | Host path for bind-mounted data directory (overrides `data_volume`) |
| `sandbox.test_command` | `MILL_TEST_COMMAND` | `pytest -q` | Command run to verify the implementation (empty = skip). Global fallback only: a managed repo's own `.robotsix-mill/config.yaml` `test_command` takes precedence, then `repos.yaml` per-repo `test_command`, then this value (precedence: per-repo file > repos.yaml > global). |

### 10. Web research

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `web.search_enabled` | `MILL_WEB_SEARCH` | `true` | Enable web-search capability (delegated to sub-agent) |
| `web.research_model` | `MILL_WEB_RESEARCH_MODEL` | `deepseek/deepseek-v4-pro` | Web-research sub-agent model (also reachable via `core.models.web_research`) |
| `web.research_request_limit` | `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `8` | Per-call request cap for web research (also reachable via `core.limits.web_research_requests`) |
| `web.fetch_image` | `MILL_FETCH_IMAGE` | `curlimages/curl:8.17.0` | Docker image for isolated `web_fetch` container |
| `web.fetch_max_bytes` | `MILL_WEB_FETCH_MAX_BYTES` | `2000000` | Max bytes fetched per URL |
| `web.fetch_timeout` | `MILL_WEB_FETCH_TIMEOUT` | `30` | Timeout (seconds) per web fetch |
| `web.fetch_max_calls` | — | `15` | (YAML-only) Max real (cache-miss) fetches per web-knowledge consult; cache hits and `web.fetch_raw` returns do NOT count |
| `web.fetch_max_total_bytes` | — | `2000000` | (YAML-only) Cumulative ceiling on returned (post-extraction, post-cap) text bytes per consult; `0` disables the byte ceiling |

### 10.1 Web knowledge agent

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| — | `MILL_WEB_KNOWLEDGE_MODEL` | `deepseek/deepseek-v4-flash` | Web-knowledge gateway sub-agent model — multi-turn flash agent that owns the per-library Markdown knowledge base and decides autonomously whether to answer from cache or web-search. Every agent's route to the internet flows through this gateway. |
| — | `MILL_WEB_KNOWLEDGE_STALE_DAYS` | `30` | Days before a cached web-knowledge .md file is considered stale. A consult that hits a stale file is allowed to web-search and update it. Users can tune this to match their tolerance for stale documentation. |
| — | `MILL_WEB_KNOWLEDGE_REQUEST_LIMIT` | `8` | Per-consult request cap for the web-knowledge sub-agent. Each request is one Markdown read, one web-search, or one Markdown write. |

### 11. Pipeline tail (merge stage)

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `pipeline.merge_poll_seconds` | `MILL_MERGE_POLL_SECONDS` | `120` | Poll interval for PR merge/CI status |
| `pipeline.rebase_max_attempts` | `MILL_REBASE_MAX_ATTEMPTS` | `5` | Max rebase LLM invocations before BLOCK |
| `pipeline.ci_fix_max_attempts` | `MILL_CI_FIX_MAX_ATTEMPTS` | `2` | Max CI-fix LLM invocations before BLOCK |
| `pipeline.ci_max_auto_retries` | `MILL_CI_MAX_AUTO_RETRIES` | `3` | Max consecutive ci-fix cycles with no code changes before BLOCK |
| `pipeline.ci_fix_max_cycles` | `MILL_CI_FIX_MAX_CYCLES` | `8` | Hard ceiling on total ci-fix cycles per ticket (counts every agent-running cycle on failing CI; reset only when CI turns green). Set to 0 to disable. |
| `pipeline.review_revision_max_attempts` | `MILL_REVIEW_REVISION_MAX_ATTEMPTS` | `2` | Max review-revision LLM invocations before BLOCK |
| `pipeline.branch_prefix` | `MILL_BRANCH_PREFIX` | `mill/` | Prefix for deliver-stage branch names |
| `pipeline.delete_branch_on_merge` | `MILL_DELETE_BRANCH_ON_MERGE` | `true` | Delete the per-ticket head branch on the forge after merge to DONE |
| `pipeline.prune_clone_on_close` | `MILL_PRUNE_CLONE_ON_CLOSE` | `true` | Delete workspace repo clone on ticket close |
| `pipeline.max_archived_tickets` | `MILL_MAX_ARCHIVED_TICKETS` | `100` | Max terminal-state tickets retained (0 = no purge) |

### 11.2 Stages tuning

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `stages.review.prior_context_max_chars` | `MILL_REVIEW_PRIOR_CONTEXT_MAX_CHARS` | `8000` | Max characters of the re-review prior-context block (prior review comments + the implement rebuttal) fed to the review agent. Each component is tail-kept (most-recent content survives) so multi-round reviews don't re-pay for the entire accumulated history. Set to `0` to disable the cap. |

### 12. Periodic agents

Each periodic agent shares this pattern:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.<name>.enabled` | `MILL_<NAME>_PERIODIC` | `false`¹ | Enable periodic passes |
| `periodic.<name>.interval_seconds` | `MILL_<NAME>_INTERVAL_SECONDS` | `86400` | Seconds between automatic passes |
| `periodic.<name>.memory_path` | `MILL_<NAME>_MEMORY_PATH` | `None` | Override path for memory ledger ² ³ |

Periodic agents: `audit`, `board_cleanup`, `trace_health`, `health`, `test_gap`,
`agent_check`, `survey`, `ci_monitor`, `config_sync`, `bc_check`,
`completeness_check`, `cost_reconciliation`, `module_curator`.

> ¹ `survey` is the exception — its default is `enabled: true`.
>
> ² `trace_health` and `ci_monitor` do **not** have a `memory_path`
> field — they write no per-agent memory ledger.
>
> `bc_check` and `completeness_check` are **env-var-only** (no YAML mapping yet).
> Set `MILL_BC_CHECK_PERIODIC=true`, `MILL_COMPLETENESS_CHECK_PERIODIC=true`, etc.
>
> ³ In multi-repo mode, the default memory file path is
> `<data_dir>/<repo_id>/<agent>_memory.md` — each repo gets its own
> isolated memory ledger.  The `memory_path` override (when set) takes
> precedence over this default, but is shared across repos (use with
> caution in multi-repo deployments).  When no repos are registered
> (single-repo or `--repo-id` mode), the path falls back to the
> original `<data_dir>/<agent>_memory.md`.

Additional fields:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.board_cleanup.model` | `MILL_BOARD_CLEANUP_MODEL` | `deepseek/deepseek-v4-flash` | Board-cleanup agent model (read-only board hygiene proposer, flash sufficient) |
| `periodic.board_cleanup.enabled` | `MILL_BOARD_CLEANUP_PERIODIC` | `true` | Enable periodic board-cleanup passes |
| `periodic.board_cleanup.interval_seconds` | `MILL_BOARD_CLEANUP_INTERVAL_SECONDS` | `86400` | Seconds between board-cleanup passes |
| `periodic.board_cleanup.memory_path` | `MILL_BOARD_CLEANUP_MEMORY_PATH` | `None` | Override path for board-cleanup memory; defaults to `<data_dir>/<repo_id>/board_cleanup_memory.md` |
| `periodic.ci_monitor.log_max_bytes` | `MILL_CI_LOG_MAX_BYTES` | `65536` | Max bytes fetched per CI job log |
| `pipeline.retrospect_spawn_drafts` | `MILL_RETROSPECT_SPAWN_DRAFTS` | `true` | Allow retrospect to file improvement draft tickets |
| `pipeline.retrospect_memory_path` | `MILL_RETROSPECT_MEMORY_PATH` | `None` | Override path for retrospect memory |
| `pipeline.trace_inspector_memory_path` | `MILL_TRACE_INSPECTOR_MEMORY_PATH` | `None` | Override path for trace-inspector memory |

#### Env-var-only periodic agents

`bc_check` and `completeness_check` have no YAML mapping yet — set them via
environment variables only:

| Env var | Default | Description |
|---------|---------|-------------|
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
| `openrouter_management_key` | — | OpenRouter management API key for cost-reconciliation (`GET /api/v1/activity`). Separate from the inference key; leave blank to skip OpenRouter-side cost fetching. |
| `forge_token` | `FORGE_TOKEN` | PAT for forge authentication |
| `github_app_id` | `GITHUB_APP_ID` | GitHub App ID (when `FORGE_AUTH=app`) |
| `github_app_private_key` | `GITHUB_APP_PRIVATE_KEY` | GitHub App private key (inline PEM, newlines as `\n`) |
| `langfuse_public_key`¹ | — | Langfuse public key (populated from `RepoConfig` at startup) |
| `langfuse_secret_key`¹ | — | Langfuse secret key (populated from `RepoConfig` at startup) |
| `langfuse_base_url`¹ | — | Langfuse base URL (populated from `RepoConfig` at startup) |
| `langfuse_project_id`¹ | — | Langfuse project ID (populated from `RepoConfig` at startup) |
| `ntfy_url` | `NTFY_URL` | ntfy.sh topic URL for notifications |
| `ntfy_token` | `NTFY_TOKEN` | ntfy.sh bearer token (optional) |

Secrets file path: `config/secrets.yaml` (overridable via
`MILL_SECRETS_FILE` env var). Template: `config/secrets.example.yaml`.

> ¹ The `langfuse_*` fields on `Secrets` are **not** user-configurable
> via `secrets.yaml` or environment variables.  They exist on the model
> for backward compatibility but are no longer populated at startup —
> per-repo Langfuse credentials are read directly from ``RepoConfig``
> at call time.  See [Repos registry](#repos-registry) above.

---

## Repos registry

The repos registry maps each repository to its own board identity and
Langfuse observability project. It is loaded **separately** from
`Settings` by a dedicated `ReposRegistry` Pydantic model — it never
participates in the Settings merge. Access it via `get_repos_config()`
or `get_repo_config("repo-id")`.

> **There is no longer a board-less default.** Every ticket must carry a
> `board_id` from `config/repos.yaml`. The legacy `<data_dir>/mill.db`
> that held tickets without a board_id has been removed. For single-repo
> deployments, configure exactly one repo entry.

Langfuse credentials are read from ``RepoConfig`` at call time (per
ticket, per operation) — they are **not** stamped onto the global
``Secrets`` singleton.  Each ticket's ``board_id`` determines which
repo entry (and thus which Langfuse project) is used for its traces.

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
    # forge_remote_url: "https://github.com/your-org/your-repo.git"  # optional — defaults to FORGE_REMOTE_URL
    langfuse:
      project_name: "my-repo"
      public_key: "pk-lf-..."
      secret_key: "sk-lf-..."
      base_url: "https://cloud.langfuse.com"  # optional — defaults to cloud
```

After editing, verify the config is valid and uses real (non-placeholder)
keys:

```sh
python scripts/verify_repos_config.py
```

### Select a repo at startup

Once `config/repos.yaml` is configured, start the server.  By default
the server loads **all** repos from `config/repos.yaml` and serves them
together.  In this multi-repo mode the board UI includes a repo selector
dropdown — pick a repo to filter the kanban, runs list, and cost
dashboard, or select "All repos" to see everything at once.

```sh
# Multi-repo mode: serves every repo in config/repos.yaml
robotsix-mill serve
```

To scope the process to a single repo (useful for tests/dev), pass
`--repo-id`:

```sh
# Single-repo override:
robotsix-mill serve --repo-id my-repo
```

When `config/repos.yaml` is empty, the server refuses to start (exit
code 2) with an error message.  An unknown `--repo-id` also causes an
error exit.

List the registered repos from the CLI:

```sh
robotsix-mill repos list
```

File path: `config/repos.yaml` (overridable via `MILL_REPOS_FILE` env var).
Set `MILL_REPOS_FILE=""` to disable repos config entirely. Template:
`config/repos.example.yaml`.

### Field reference

| YAML key | Required | Default | Description |
|----------|----------|---------|-------------|
| `repos.<id>.board_id` | yes | — | Board identifier for per-repo board isolation |
| `repos.<id>.forge_remote_url` | no | `FORGE_REMOTE_URL` | Per-repo forge remote URL for push/PR/merge operations |
| `repos.<id>.langfuse.project_name` | yes | — | Langfuse project name for this repo's traces |
| `repos.<id>.langfuse.public_key` | yes | — | Langfuse public key for this repo's project |
| `repos.<id>.langfuse.secret_key` | yes | — | Langfuse secret key for this repo's project |
| `repos.<id>.langfuse.base_url` | no | `https://cloud.langfuse.com` | Langfuse base URL |

Each repo ID must be unique and non-empty. The `board_id` must also be
non-empty. The registry validates that every entry's `repo_id` matches
its YAML key.

### Multi-repo behaviour

When multiple repos are registered (default when `config/repos.yaml`
has two or more entries), each periodic agent fans out across all repos
sequentially — one timer per agent type iterates every enabled repo in
turn. This means:

- **Memory files** are per-repo: `<data_dir>/<repo_id>/audit_memory.md`,
  `<data_dir>/<repo_id>/bc_check_memory.md`, etc.
- **Run registry** entries include a `repo_id` field. `GET /runs` accepts
  `?repo_id=X` to filter by repo.
- **CI monitor** dedup state is per-repo:
  `<data_dir>/<repo_id>/ci_monitor_state.json`.
- **Agent toggles** (e.g. `MILL_AUDIT_PERIODIC`) remain global — all
  repos share the same enabled/disabled flags.

In single-repo mode (`--repo-id` on serve or one entry in
`config/repos.yaml`) periodic agents run only for that repo, and memory
files use the legacy flat path (`<data_dir>/audit_memory.md`).

---

## See also

- [index.md](index.md) — documentation home
- [deployment.md](deployment.md) — continuous deployment guide
- [config-audit.md](config-audit.md) — complete inventory of every config value and its source
- [`config/mill.defaults.yaml`](../config/mill.defaults.yaml) — committed canonical defaults
- [`config/secrets.example.yaml`](../config/secrets.example.yaml) — secrets template
