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

### Change which model an agent uses

Models are not configured per-agent in YAML. Each agent definition
(`agent_definitions/<name>.yaml`) declares a capability `level: 1|2|3`,
which resolves to a `(transport, model)` via robotsix-llmio's tier
defaults (see [§1 Capability levels](#1-capability-levels-model-selection)).
To change an agent's model, change its `level` in the definition; to change
what a level maps to, change the defaults in robotsix-llmio.

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

#### Test gate environment-error circuit breaker

The test gate has a **circuit breaker** that detects when a test suite
failure is due to a **missing or inaccessible binary** in the sandbox —
not a code problem the implement agent can fix by editing the repo. When
detected, the gate short-circuits with a stable ENV-ERROR diagnosis
instead of forwarding the failure to the distill agent for analysis.

The ENV-ERROR circuit breaker fires on:
- **rc=127** — a binary was not found on PATH (shell standard for "command not found")
- **rc=126 + Permission denied** on a `$HOME/.local/bin` path — a
  `pip install --user` console script exists but cannot execute because
  the sandbox's `/tmp` tmpfs was not mounted with the `exec` flag (by
  default Docker mounts tmpfs as `noexec`). The sandbox has been updated
  to mount `/tmp` as `exec` to allow pip console scripts to run; if a
  script still fails with rc=126 on a HOME path, the gate reports ENV-ERROR.

This prevents the implement fix-loop from burning iterations on unfixable
sandbox issues. The diagnosis is **byte-identical across runs** for the
same failure (e.g. same missing binary) so the circuit breaker recognizes
repeated failures and escal​ates instead of retrying forever.

**Sandbox requirements for console scripts:** If your repo uses
`extra_sandbox_packages` to install pip packages with CLI entry points
(e.g. `pip:yamllint`, `pip:vcs`), those console scripts are installed
under `$HOME/.local/bin` (which maps to `/tmp/.local/bin` in the sandbox)
and must be executable. The sandbox's `/tmp` tmpfs is mounted with the
`exec` flag to support this. If a console script cannot execute even
with `exec` mounted, the ENV-ERROR circuit breaker will catch it and
report it as a sandbox regression rather than treating it as a code bug.

### Smoke gate (`smoke_command` / `smoke_paths`)

A repo can declare an optional **path-scoped smoke gate** that runs
*after* the unit-test gate passes — a lightweight end-to-end check (e.g.
booting the server and hitting key routes) that catches breakages a unit
suite misses:

```yaml
# .robotsix-mill/config.yaml
smoke_command: scripts/smoke_board.sh
smoke_paths:
  - src/robotsix_mill/runtime/**
```

- `smoke_command` — the shell command the gate runs in the sandbox. The
  per-repo value wins over the global `sandbox.smoke_command` (env
  `MILL_SMOKE_COMMAND`); empty everywhere means **no smoke gate** (the
  gate short-circuits to PASS). The gate is strictly opt-in — no command
  set anywhere is a no-op.
- `smoke_paths` — a glob list scoping *when* the gate runs. When
  empty/absent the smoke command runs **unconditionally** (whenever it is
  set); otherwise the gate runs only when the ticket's introduced files
  match a glob. A pure backend change that touches no listed path skips
  the gate. `smoke_paths` is inherently per-repo and has no global
  counterpart.

The smoke gate runs **only after unit tests pass** (no point smoking a
red build), and a smoke failure routes exactly like a unit-test failure
(retry while iterations remain, escalate on the last, BLOCKED on
sandbox-unavailable).

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

### Deployed log folder (`deployed_log_folder`)

The operator can point the refine agent at a repo's live deployment log
directory by setting a single per-repo field in mill's central,
gitignored `config/repos.yaml` (alongside `board_id` / `forge_remote_url`
/ `langfuse:`):

```yaml
# config/repos.yaml
repos:
  robotsix-auto-mail:
    board_id: "..."
    deployed_log_folder: /var/log/robotsix-auto-mail
```

- `deployed_log_folder` — a path (string) to the live deployment's log
  directory, either **absolute** or **relative to the repo root**
  (relative paths are resolved against the repo dir, and a warning is
  logged for relative paths). It is **opt-in**: when absent — or when it
  does not resolve to an existing directory — the log tooling is
  silently skipped. When it resolves, it drives the refine agent's
  `query_app_logs` tool plus an injected log summary. Because the value
  is a deployment-specific host path, it lives in the operator's central
  config — **not** the managed repo's committed
  `.robotsix-mill/config.yaml` (the repo-owned key is deprecated and
  ignored). See [observability.md](observability.md) for the full story.

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

1. Add the field to the Pydantic model in `src/robotsix_mill/config/`
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

Steps 2–4 are enforced deterministically by
`scripts/check_config_sync.py`, which runs as a blocking CI step
("Validate config sync") and as the `validate-config-sync` pre-commit
hook. It fails if a `_YAML_PATH_TO_ALIAS` key has no matching
`mill.defaults.yaml` leaf (or vice-versa), if a mapping value names a
non-existent `Settings` field/alias, or if `secrets.example.yaml`
drifts from the `Secrets` model. Intentional gaps live in the script's
inline-commented exception sets. (Doc-table drift is not gated here —
the heuristic `config_sync` agent still covers that.)

Environment variable naming convention: use `Field(alias=...)` on the
Pydantic model with a `MILL_` prefix + uppercase with underscores
(e.g. `Field(alias="MILL_MY_NEW_FIELD")`).  The `_YAML_PATH_TO_ALIAS`
dict maps the dotted YAML path to this alias — there is no automatic
double-underscore convention.

## Config drift prevention

**Rule:** Every new Pydantic settings field added to
`_settings_periodic.py` or `settings.py` MUST have a corresponding
entry in BOTH `config/mill.defaults.yaml` (under the appropriate
agent/feature block) AND `_YAML_PATH_TO_ALIAS` in
`src/robotsix_mill/config/loader.py` in the same commit. Fields
omitted from both surfaces are invisible to `check_config_sync.py` —
the suite only cross-references alias-map ↔ YAML leaves, not
Settings-model ↔ surfaces.

**Rationale:** PR #1546 and the still-unfiled prune_orphans gap
(PR #1533): two instances where Pydantic fields were added to the
model but never wired to config surfaces. The drift checker could
not catch either because the gap was symmetric. This rule encodes
the same convention as the `docs/modules.yaml` "add the path in
the same commit" discipline.

---

## Full setting reference

Every setting below shows:
- **YAML path** — the key in `config/mill.defaults.yaml`
- **Env var** — the environment variable override
- **Default** — the committed default value
- **Description** — what it controls

### 1. Capability levels (model selection)

Per-agent model selection is declared in each **agent definition's**
`level: 1|2|3` field. `build_agent` resolves a level to a
`(transport, model)` pair via robotsix-llmio's baked tier defaults — there
is no per-agent model config in YAML and no global backend toggle. The
level *is* the backend choice.

| Level | Transport | Model | Intent |
|-------|-----------|-------|--------|
| 1 | `openrouter[deepseek]` | `deepseek/deepseek-v4-flash` | cheap, repetitive (triage, audit, dedup, periodic scanners, …) |
| 2 | `openrouter[deepseek]` | `deepseek/deepseek-v4-pro` | intermediate — implement, ci_fix, review, test, … |
| 3 | `claude-sdk` | `opus` | high-level planning — refine, meta_triage, epic_breakdown |

Level-3 agents run on the Claude Agent SDK (subscription auth; needs Node +
the `claude` CLI in the container). These knobs govern that path:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.claude_max_concurrency` | `MILL_CLAUDE_MAX_CONCURRENCY` | `4` | Process-wide cap on concurrent Claude SDK runs (each spawns a `claude` CLI subprocess) |
| `core.claude_sdk_vision_enabled` | — | `false` | Allow inline image (screenshot/vision) input on the Claude SDK path. **Default off**: the installed llmio bridge cannot consume `BinaryContent` image parts — it stringifies them into a useless repr that hangs the `claude` CLI until the 1200s per-call cap fires. While off, the refine/review screenshot paths degrade to a text note. Flip to `true` (a one-line change) once the bridge gains real image-input support |
| `core.investigation_workspace` | `MILL_INVESTIGATION_WORKSPACE` | `None` | Path to a directory containing clones of registered repos for cross-repo investigation by the maintenance agent. When set, the agent's read-only tools are scoped to this directory. When None, falls back to the ticket's own workspace repo_dir. |

### 2. Request limits

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.coordinator_requests` | `MILL_PER_PASS_REQUEST_BUDGET` | `500` | Per-pass request budget for the implement (coordinator) agent. Resets each pass; normal tickets fit in one pass. Hard upper bound 5000 |
| `core.limits.subtask_request_limit` | — | `30` | Per-subtask request cap for `spawn_subtask` sub-agents delegated by the coordinator |
| `core.limits.explore_requests` | `MILL_EXPLORE_REQUEST_LIMIT` | `100` | Per-call request cap for the explore sub-agent |
| `core.limits.explore_max_tokens` | `MILL_EXPLORE_MAX_TOKENS` | `4096` | Output token cap for explore sub-agent responses |
| `core.limits.consult_requests` | `MILL_CONSULT_REQUEST_LIMIT` | `15` | Per-call request cap for the domain-expert consultation sub-agent |
| `core.limits.test_requests` | `MILL_TEST_REQUEST_LIMIT` | `30` | Per-call request cap for the test sub-agent |
| `core.limits.web_research_requests` | `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `8` | Per-call request cap for the web-research sub-agent |
| `core.limits.dedup_requests` | `MILL_DEDUP_REQUEST_LIMIT` | `6` | Per-call request cap for the dedup check |
| `core.limits.obsolescence_requests` | `MILL_OBSOLESCENCE_REQUEST_LIMIT` | `6` | Per-call request cap for the obsolescence gate |
| `core.limits.scope_triage_requests` | `MILL_SCOPE_TRIAGE_REQUEST_LIMIT` | `8` | Per-call request cap for the scope-triage agent |
| `core.limits.scope_triage_max_files` | `MILL_SCOPE_TRIAGE_MAX_FILES` | `50` | Max out-of-scope text files before the scope-triage flood guard blocks (0 disables) |
| `core.limits.refine_requests` | `MILL_REFINE_REQUEST_LIMIT` | `80` | Per-call request cap for the refine agent |
| `core.limits.refine_requests_simple` | `MILL_REFINE_REQUEST_LIMIT_SIMPLE` | `40` | Per-call request cap for simple/sonnet refine runs (lower because explore tools are gated off) |
| `core.limits.refine_max_tool_calls` | — | `120` | (YAML-only) Hard cap on total tool calls per refine trace (runaway-loop backstop) |
| `core.limits.refine_max_errors` | — | `20` | (YAML-only) Max tool-call errors per refine trace before auto-termination |
| `core.limits.refine_web_fetch_max_calls` | — | `5` | (YAML-only) Max real (cache-miss) `web_fetch` calls across one whole refine trace (cross-consult) |
| `core.limits.refine_web_fetch_max_total_bytes` | — | `500000` | (YAML-only) Cumulative fetch-bytes ceiling across one refine trace; `0` disables |
| `core.limits.refine_web_search_max_calls` | — | `5` | (YAML-only) Max `web_search` calls across one whole refine trace (cross-consult) |
| `core.limits.maintenance_requests` | `MILL_MAINTENANCE_REQUEST_LIMIT` | `100` | Per-call request cap for the maintenance agent |
| `core.limits.doc_requests` | `MILL_DOC_REQUEST_LIMIT` | `16` | Per-run request cap for the document agent |
| `core.limits.doc_classifier_requests` | `MILL_DOC_CLASSIFIER_REQUEST_LIMIT` | `3` | Per-call request cap for the doc-classifier gate |
| `core.limits.triage_requests` | `MILL_TRIAGE_REQUEST_LIMIT` | `8` | Per-call cap for the pre-refine triage agent (main call + tool calls). Distinct from `scope_triage_requests` (which caps the scope-triage agent) |
| `core.limits.already_done_requests` | `MILL_ALREADY_DONE_REQUEST_LIMIT` | `8` | Per-call cap for the already-done verifier sub-agent (short-circuits when a prior no-change-needed memory entry matches the draft) |
| `core.limits.dedup_max_candidates` | `MILL_DEDUP_MAX_CANDIDATES` | `8` | Maximum candidates passed to the dedup LLM after similarity pre-filtering. Caps token budget regardless of repo size |
| `core.limits.coordinator_max_tool_calls` | — | `300` | Hard cap on total tool calls per implement (coordinator) trace — runaway-loop backstop above the request budget |
| `core.limits.max_refine_explore_calls` | — | `4` | Hard cap on explore/parallel_explore sub-agent calls per refine run. 0 disables exploration entirely |
| `core.limits.max_refine_read_file_calls` | — | `10` | Hard cap on read_file calls per refine/triage agent run. 0 disables the cap (unbounded reads) |
| `core.limits.review_requests` | `MILL_REVIEW_REQUEST_LIMIT` | `80` | Per-run request cap for the review agent |

### 3. Worker pool & retry

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.max_fix_iterations` | `MILL_MAX_FIX_ITERATIONS` | `8` | Max implement→test fix loop iterations before BLOCK |
| `core.limits.max_stuck_cycles` | `MILL_MAX_STUCK_CYCLES` | `3` | Re-entries to same stage without progress before BLOCK |
| `core.limits.max_spend_usd_per_ticket` | `MILL_MAX_SPEND_USD_PER_TICKET` | `20.0` | Dollar cap per ticket (0.0 = disabled) |
| `core.limits.max_traces_per_ticket` | `MILL_MAX_TRACES_PER_TICKET` | `15` | Trace-count circuit-breaker (0 = disabled) |
| `core.limits.max_openrouter_marginal_usd_per_ticket` | `MILL_MAX_OPENROUTER_MARGINAL_USD_PER_TICKET` | `3.0` | OpenRouter marginal-spend breaker (0.0 = disabled) |
| `core.limits.stage_timeout_seconds` | `MILL_STAGE_TIMEOUT_SECONDS` | `2400` | Per-stage wall-clock timeout in seconds; stage that exceeds it is escalated to BLOCKED (≤ 0 disables) |
| `core.limits.stage_timeout_overrides` | `MILL_STAGE_TIMEOUT_OVERRIDES` | `{"refine": 900}` | Per-stage overrides as a JSON dict (e.g. `{"merge":0,"deliver":0}`); keys are stage names, values are seconds; 0 disables timeout for that stage. The built-in default caps the **refine** stage at 900 seconds — add `"refine": 0` to disable this cap, or override it with a different value. |
| `core.limits.max_global_concurrency` | `MILL_MAX_GLOBAL_CONCURRENCY` | `12` | Host-level cap on total concurrently-running stages across ALL boards, applied on top of each board's own `max_concurrency`. Default 12 provides a genuine backstop without throttling normal operation |
| `core.limits.transient_retries` | `MILL_TRANSIENT_RETRIES` | `4` | Max retries for transient LLM-call failures (429, 5xx, timeouts) |
| `core.limits.transient_backoff_base` | `MILL_TRANSIENT_BACKOFF_BASE` | `2.0` | Base seconds for exponential backoff at LLM-call level (jittered) |
| `core.limits.transient_backoff_cap` | `MILL_TRANSIENT_BACKOFF_CAP` | `30.0` | Max seconds between LLM-call retries |
| `core.limits.stage_retry_max_attempts` | `MILL_STAGE_RETRY_MAX_ATTEMPTS` | `5` | Max automatic retries for transient stage-level failures (git outage, provider 5xx, connection refused) |
| `core.limits.stage_retry_base_delay` | `MILL_STAGE_RETRY_BASE_DELAY` | `2.0` | Base seconds for stage-level exponential backoff |
| `core.limits.stage_retry_max_delay` | `MILL_STAGE_RETRY_MAX_DELAY` | `60.0` | Max seconds between stage-level retries |
| `core.limits.rate_limit_backoff_base` | `MILL_RATE_LIMIT_BACKOFF_BASE` | `30.0` | Base seconds for rate-limit backoff (longer window) |
| `core.limits.rate_limit_backoff_cap` | `MILL_RATE_LIMIT_BACKOFF_CAP` | `120.0` | Max seconds between rate-limit retries |
| `core.low_credit_threshold_usd` | — | `5.0` | OpenRouter credit balance below this value triggers the board warning banner |
| `core.low_credit_poll_enabled` | — | `true` | Enable the proactive OpenRouter credit-balance poll (hourly via `GET /api/v1/credits`) |
| `core.low_credit_poll_interval_seconds` | — | `3600` | Seconds between proactive credit-balance checks |
| `core.requeue_batch_size` | `MILL_REQUEUE_BATCH_SIZE` | `5` | Tickets enqueued per batch in the startup re-queue drip feed |
| `core.requeue_batch_pause_seconds` | `MILL_REQUEUE_BATCH_PAUSE_SECONDS` | `2.0` | Pause (seconds) between startup re-queue batches |
| `core.startup_jitter_seconds` | `MILL_STARTUP_JITTER_SECONDS` | `30` | Max random jitter (seconds) added to the per-repo periodic pass first-tick delay |
| `core.board_list_cache_ttl_seconds` | `MILL_BOARD_LIST_CACHE_TTL_SECONDS` | `3.0` | Short-TTL cache for board-poll GET /tickets endpoint (seconds). Repeated polls within this window return a cached snapshot to avoid stalling the event loop under load. 0.0 disables the cache. |

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
| `pipeline.doc_memory_path` | `MILL_DOC_MEMORY_PATH` | `None` | Override path for the document agent's Markdown memory ledger; defaults to `<data_dir>/doc_memory.md` |

### 5. Dedup

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.memory.dedup_lookback_days` | `MILL_DEDUP_LOOKBACK_DAYS` | `7` | Days back to consider closed tickets as dup candidates |
| `epic_dedup_lookback_days` | `MILL_EPIC_DEDUP_LOOKBACK_DAYS` | `7` | Recency window (days) for the epic-decomposition pre-filing dedup recent-ticket check (see [epic-dedup.md](epic-dedup.md)) |
| `core.limits.dedup_skip_on_no_overlap` | `MILL_DEDUP_SKIP_ON_NO_OVERLAP` | `true` | Skip dedup LLM call when draft shares no token overlap with any candidate — saves cost in the "clearly unrelated" case |
| `core.limits.dedup_candidate_body_max_chars` | `MILL_DEDUP_CANDIDATE_BODY_MAX_CHARS` | `4000` | Cap each candidate body fed to dedup prompt; ≤0 disables truncation |

### 6. Service (management plane)

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `service.data_dir` | `MILL_DATA_DIR` | `.data` | Data directory for DB, workspaces, and memory ledgers |
| `service.default_repo_id` | `MILL_DEFAULT_REPO_ID` | `""` | Backward-compatibility fallback: board_id assigned to tickets created before the mandatory-board_id migration. Not a substitute for configuring repos.yaml. |
| `service.api_host` | `MILL_API_HOST` | `127.0.0.1` | FastAPI listen address |
| `service.api_port` | `MILL_API_PORT` | `8077` | FastAPI listen port |
| `service.api_url` | `MILL_API_URL` | `http://127.0.0.1:8077` | Base URL the CLI client uses to reach the API |
| `service.shutdown_grace_seconds` | `MILL_SHUTDOWN_GRACE_SECONDS` | `1800` | Maximum seconds to wait for in-flight periodic-agent passes to finish before tearing the worker down on container shutdown. 0 = wait forever. |
### 7. Approval & review

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `gates.require_approval` | `MILL_REQUIRE_APPROVAL` | `true` | Pause after refine for human approval (`human_issue_approval` state) |
| `gates.auto_approve_enabled` | `MILL_AUTO_APPROVE_ENABLED` | `false` | Enable conservative auto-approve triage |
| `gates.review_enabled` | `MILL_REVIEW_ENABLED` | `false` | Enable dual-model code review stage before deliver |
| `gates.review_max_rounds` | `MILL_REVIEW_MAX_ROUNDS` | `3` | Max CODE_REVIEW round-trips before escalate |
| `gates.refine_triage_enabled` | `MILL_REFINE_TRIAGE_ENABLED` | `true` | Cheap triage before full refine (skip if precise) |
| `gates.maintenance_triage_enabled` | `MILL_MAINTENANCE_TRIAGE_ENABLED` | `true` | Cheap triage before a full maintenance pass |
| `gates.refine_advisory_dedup_enabled` | `MILL_REFINE_ADVISORY_DEDUP_ENABLED` | `true` | Cheap advisory-dedup-verification gate: resolves carried `Possible duplicate of <id>` advisory with a single cheapest-tier `run_dedup_check` |
| `gates.freshness_gate_enabled` | `MILL_FRESHNESS_GATE_ENABLED` | `false` | Pre-refine freshness check: verify cited evidence paths exist on HEAD |
| `gates.obsolescence_gate_enabled` | `MILL_OBSOLESCENCE_GATE_ENABLED` | `false` | Pre-refine obsolescence check: re-validate spawned-draft gaps (opt-in) |
| `gates.spec_review_enabled` | `MILL_SPEC_REVIEW_ENABLED` | `true` | Post-refinement spec narrative stripping |
| `gates.scope_triage_enabled` | `MILL_SCOPE_TRIAGE_ENABLED` | `true` | Cheap scope-violation triage before blocking (EXPAND/REJECT/ESCALATE) |
| `gates.prerequisite_gate_enabled` | `MILL_PREREQUISITE_GATE_ENABLED` | `true` | Pre-implement gate: when enabled, verify that external symbols/imports declared in the spec's `## Prerequisites` block are importable in the cloned repo before invoking the implement agent. When a declared prerequisite is unmet (e.g. an unmerged external port), the ticket is short-circuited to BLOCKED without the expensive coordinator LLM run. This is a no-op for specs without a `## Prerequisites` block and degrades gracefully on checker errors (always proceeds, never blocks on internal errors). |
| `gates.auto_merge_enabled` | `MILL_AUTO_MERGE_ENABLED` | `false` | Auto-merge PR when CI passes |
| `gates.auto_merge_main_debt_detection_enabled` | `MILL_AUTO_MERGE_MAIN_DEBT_DETECTION_ENABLED` | `true` | When enabled, the single-repo auto-merge decision detects pre-existing main-branch CI debt: if every workflow failing on the PR head is ALSO failing on the merge target, the failure was not introduced by this PR and the ticket is routed to BLOCKED instead of cycling rebase/ci-fix retries. Safe-by-default — only fires when main is demonstrably red on the same workflow(s); the flag exists so an operator can disable it if needed. |
| `gates.review_feedback_enabled` | `MILL_REVIEW_FEEDBACK_ENABLED` | `false` | Enable autonomous review-revision agent (opt-in — implements changes requested by human reviewers) |
| `gates.pr_summary_enabled` | `MILL_PR_SUMMARY_ENABLED` | `false` | Generate structured PR body from diff via cheap LLM (opt-in) |
| `gates.comments_after_body` | `MILL_COMMENTS_AFTER_BODY` | `false` | Render description.md before comments in ticket detail drawer |
| `gates.reviewer_agreement_gate_enabled` | `MILL_REVIEWER_AGREEMENT_GATE_ENABLED` | `true` | Pre-Opus guard: when a reviewer's sendback feedback already agrees with the draft's no-change-needed conclusion, the pipeline short-circuits to DONE, skipping the expensive Opus refine agent. Requires `refine_triage_enabled=true`. |
| `gates.refine_mill_misroute_gate_enabled` | `MILL_REFINE_MILL_MISROUTE_GATE_ENABLED` | `true` | Deterministic pre-refine gate: detects drafts referencing mill-specific source paths absent from the current checkout and redirects them to the mill maintenance board before any LLM budget is spent. |
| `ci.codeql_fp_triage_enabled` | `MILL_CODEQL_FP_TRIAGE_ENABLED` | `true` | When enabled, ci_fix may invoke a conservative sub-agent at the hard cycle ceiling to dismiss high-conviction CodeQL false positives, unblocking the ticket |

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
| `core.enable_repo_creation` | `MILL_ENABLE_REPO_CREATION` | `false` | Allow the new-repo meta flow to create repositories via the forge API |
| `core.repo_visibility_default` | `MILL_REPO_VISIBILITY_DEFAULT` | `public` | Default visibility for newly created repositories. `public` — repos are public unless the caller specifies private=True. `private` — repos are private unless the caller specifies private=False. |

### 9. Sandbox

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `sandbox.image` | `MILL_SANDBOX_IMAGE` | `robotsix/mill-sandbox:latest` | Docker image for disposable sandbox containers. Includes the `uv` binary and Python toolchain. Defaults to `robotsix/mill-sandbox:latest`; customize this to a pre-built image that includes any additional tooling (e.g. formatters, linters) your test command needs. |
| `sandbox.memory` | `MILL_SANDBOX_MEMORY` | `2g` | Memory limit for sandbox containers |
| `sandbox.pids_limit` | `MILL_SANDBOX_PIDS_LIMIT` | `512` | PID limit for sandbox containers |
| `sandbox.readonly` | `MILL_SANDBOX_READONLY` | `true` | Mount sandbox rootfs read-only (except tmpfs `/tmp`) |
| `sandbox.command_timeout` | `MILL_COMMAND_TIMEOUT` | `1800` | Wall-clock cap (seconds) for sandbox shell/test commands |
| `sandbox.data_volume` | `MILL_DATA_VOLUME` | `mill_data` | Named Docker volume for data (fallback when not bind-mounted) |
| `sandbox.data_mount` | `MILL_SANDBOX_DATA_MOUNT` | `None` | Host path for bind-mounted data directory (overrides `data_volume`) |
| `sandbox.network` | `MILL_SANDBOX_NETWORK` | `mill-sandbox-net` | Docker network sandbox containers connect to (internal, filtered through proxy) |
| `sandbox.proxy_url` | `MILL_SANDBOX_PROXY_URL` | `http://sandbox-proxy:8888` | Egress proxy URL (empty = no proxy, `--network none`) |
| `sandbox.test_command` | `MILL_TEST_COMMAND` | `""` | Command run to verify the implementation (empty = skip). Global fallback only: a managed repo's own `.robotsix-mill/config.yaml` `test_command` takes precedence, then `repos.yaml` per-repo `test_command`, then this value (precedence: per-repo file > repos.yaml > global). |

### 10. Web research

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `web.search_enabled` | `MILL_WEB_SEARCH` | `true` | Enable web-search capability (delegated to sub-agent) |
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
| `pipeline.rebase_max_attempts` | `MILL_REBASE_MAX_ATTEMPTS` | `3` | Max rebase LLM invocations before BLOCK |
| `pipeline.ci_fix_max_iterations` | `MILL_CI_FIX_MAX_ITERATIONS` | `5` | Single-repo ci-fix: max `wait_for_ci` push-and-recheck iterations the agent may run before BLOCK. The agent owns its fix→push→verify loop; this is its iteration budget. Set to 0 to disable the verify loop. |
| `pipeline.ci_fix_max_attempts` | `MILL_CI_FIX_MAX_ATTEMPTS` | `2` | Multi-repo merge ci-fix only: max CI-fix LLM invocations before BLOCK |
| `pipeline.ci_fix_max_cycles` | `MILL_CI_FIX_MAX_CYCLES` | `3` | Multi-repo merge ci-fix only: hard ceiling on total ci-fix cycles per repo (reset only when CI turns green). Set to 0 to disable. |
| `pipeline.ci_fix_max_identical_failures` | `MILL_CI_FIX_MAX_IDENTICAL_FAILURES` | `2` | Max consecutive identical CI failure cycles before escalating to BLOCKED. When the same failure fingerprint repeats this many times without progress, the stage short-circuits. Set to 0 to disable. |
| `pipeline.ci_fix_request_limit` | `MILL_CI_FIX_REQUEST_LIMIT` | `120` | Per-run request budget for the ci-fix agent (must cover ALL fix→push→verify iterations). When exhausted, pydantic-ai raises `UsageLimitExceeded`, which the retry layer catches and triggers the fallback model (if configured). Set to 0 to disable. |
| `pipeline.review_revision_max_attempts` | `MILL_REVIEW_REVISION_MAX_ATTEMPTS` | `2` | Max review-revision LLM invocations before BLOCK |
| `pipeline.branch_prefix` | `MILL_BRANCH_PREFIX` | `mill/` | Prefix for deliver-stage branch names |
| `pipeline.delete_branch_on_merge` | `MILL_DELETE_BRANCH_ON_MERGE` | `true` | Delete the per-ticket head branch on the forge after merge to DONE |
| `pipeline.prune_clone_on_close` | `MILL_PRUNE_CLONE_ON_CLOSE` | `true` | Delete workspace repo clone on ticket close |
| `pipeline.max_archived_tickets` | `MILL_MAX_ARCHIVED_TICKETS` | `40` | Max terminal-state tickets retained (0 = no purge) |
| `pipeline.max_events_per_ticket` | `MILL_MAX_EVENTS_PER_TICKET` | `200` | Max TicketEvent rows retained per non-terminal ticket; events beyond this cap are pruned (oldest first). 0 disables per-ticket event capping. |
| `pipeline.max_comments_per_ticket` | `MILL_MAX_COMMENTS_PER_TICKET` | `500` | Max Comment rows retained per non-terminal ticket; OPEN threads are never pruned. 0 disables comment capping. |
| `pipeline.auto_fix_max_cycles` | `MILL_AUTO_FIX_MAX_CYCLES` | `6` | Cross-stage ceiling on combined REBASING+FIXING_CI dispatches without CI turning green. Reset only when CI is observed green. Set to 0 to disable. |
| `pipeline.ping_pong_max_alternations` | `MILL_PING_PONG_MAX_ALTERNATIONS` | `3` | Ceiling on REBASING↔FIXING_CI alternations before escalating to BLOCKED. Reset when CI is observed green. Set to 0 to disable. |
| — | `MILL_TICKET_STATE_CYCLE_LIMIT` | `3` | Ceiling on re-dispatches of the same LLM-bearing stage within a single pass before BLOCKED. Set to 0 to disable. |

### 11.2 Stages tuning

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `stages.review.prior_context_max_chars` | `MILL_REVIEW_PRIOR_CONTEXT_MAX_CHARS` | `8000` | Max characters of the re-review prior-context block (prior review comments + the implement rebuttal) fed to the review agent. Each component is tail-kept (most-recent content survives) so multi-round reviews don't re-pay for the entire accumulated history. Set to `0` to disable the cap. |
| `stages.review.diff_max_chars` | `MILL_REVIEW_DIFF_MAX_CHARS` | `200_000` | Max characters of the combined git diff injected into the review prompt. The raw `git diff origin/<target>...HEAD` can balloon to megabytes (divergent base, generated/lockfile churn, branch history) regardless of how few lines the intended change touches, overflowing even a 1M-token model context. When the diff exceeds this limit it is **middle-truncated** (head + tail kept, middle dropped, with a marker stating how many characters were omitted) so both early and late files get representation. ~200K chars ≈ 50K tokens, leaving room for spec + prior context + preseed + tools + the output reservation. Set to `0` to disable the cap (unbounded diffs). |
| `stages.review.output_token_budget` | `MILL_REVIEW_OUTPUT_TOKEN_BUDGET` | `65536` | Output token budget for the review agent retry when the primary attempt exhausts its `max_tokens` before generating a response (the reasoning model burns output tokens on internal reasoning). This is the **retry** budget; the primary attempt uses the YAML `max_tokens`. Set higher than the YAML `max_tokens`. Set to `0` to disable the output-exhaustion retry (falls straight to `NEEDS_DISCUSSION`). |
| `core.lint_on_edit` | `MILL_LINT_ON_EDIT` | `true` | Pre-write Python syntax check on `write_file`/`edit_file`. When True, a SyntaxError aborts the edit before writing broken code. Configured via `core.lint_on_edit` in YAML config. |
| `core.read_file_max_chars` | `MILL_READ_FILE_MAX_CHARS` | `50000` | (YAML-only) Character cap on an *implicit full* `read_file` (`offset=1`, `limit=None`) payload returned to any `build_fs_tools` agent (implement, review, document). Over the cap the tool returns a head + tail slice plus an elision marker stating the file's total line count and steering the agent to re-read the omitted region with `offset`/`limit`; explicit ranged reads are **never** truncated. ~50K chars ≈ 12.5K tokens — above ordinary source modules (returned in full), so only large generated/lock/baseline files are trimmed before they bloat the re-billed prefix. Set to `0` to disable the cap. |

**Graceful token-exhaustion handling.** If a token-limit error is hit on
the first review pass, the review is retried once with no preseed and a
hard-truncated diff (~40K chars). If that retry also overflows, the
stage returns a `NEEDS_DISCUSSION` verdict with an explanatory comment
rather than crashing — a human can review the PR directly or split the
change into smaller diffs.

### 11.3 Refine routing

These knobs control how the refine agent selects a model and when it
routes to cheaper tiers. All values are applied at the start of each
refinement pass.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `gates.refine_trivial_routing_enabled` | `MILL_REFINE_TRIVIAL_ROUTING_ENABLED` | `true` | Route trivial-scope tickets to a cheaper model instead of the full refinement model |
| `gates.refine_trivial_model_level` | `MILL_REFINE_TRIVIAL_MODEL_LEVEL` | `3` | Model level for trivial-scope refines (3 = flat-cost Claude subscription; 1/2 = pay-per-token DeepSeek rollback) |
| `gates.refine_trivial_subscription_model` | `MILL_REFINE_TRIVIAL_SUBSCRIPTION_MODEL` | `sonnet` | Claude alias for trivial/forced-cheap refines routed to the level-3 subscription |
| `gates.refine_subscription_tier_routing_enabled` | `MILL_REFINE_SUBSCRIPTION_TIER_ROUTING_ENABLED` | `true` | Complexity-gated Claude alias routing for level-3 refines (set `false` for Opus-always rollback) |
| `gates.refine_subscription_model_default` | `MILL_REFINE_SUBSCRIPTION_MODEL_DEFAULT` | `sonnet` | Claude alias for non-escalated (simple) level-3 refines |
| `gates.refine_subscription_model_complex` | `MILL_REFINE_SUBSCRIPTION_MODEL_COMPLEX` | `opus` | Claude alias for escalated (needs-exploration) level-3 refines |
| `gates.refine_findings_downgrade_enabled` | `MILL_REFINE_FINDINGS_DOWNGRADE_ENABLED` | `true` | Downgrade Opus → cheaper Claude alias when triage findings are substantial (root cause already known) |
| `gates.refine_findings_downgrade_min_chars` | `MILL_REFINE_FINDINGS_DOWNGRADE_MIN_CHARS` | `200` | Minimum stripped-character length of triage findings for the Opus downgrade to fire |
| `gates.refine_subscription_model_findings` | `MILL_REFINE_SUBSCRIPTION_MODEL_FINDINGS` | `sonnet` | Claude alias used when the findings-present downgrade fires |
| `gates.max_re_refine_cycles_before_cheap` | `MILL_MAX_RE_REFINE_CYCLES_BEFORE_CHEAP` | `2` | Force cheap model after this many "changes requested" sendbacks; `0` disables |
| — | `MILL_REFINE_DELTA_REUSE_ENABLED` | `true` | When re-entering refine after an operator sendback, reuse the prior refined description.md as the starting point instead of refining from scratch |

### 12. Periodic agents

Each periodic agent shares this pattern:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.<name>.enabled` | `MILL_<NAME>_PERIODIC` | `true`¹ | Enable periodic passes |
| `periodic.<name>.interval_seconds` | `MILL_<NAME>_INTERVAL_SECONDS` | `86400` | Seconds between automatic passes |
| `periodic.<name>.memory_path` | `MILL_<NAME>_MEMORY_PATH` | `None` | Override path for memory ledger ² ³ |

Periodic agents: `audit`, `trace_health`, `trace_review`, `health`, `test_gap`,
`agent_check`, `survey`, `ci_monitor`, `config_sync`, `member_sync`, `bc_check`,
`completeness_check`, `diagnostic`, `forge_parity`, `module_curator`,
`copy_paste`, `timeout_escalation`, `langfuse_cleanup`, `data_dir_audit`, `dependabot_ingest`, `run_health`, `stale_branch_cleanup`,
`state_sync`, `env_doc_sync`, `db_maintenance`, `sandbox_reaper`.

> ¹ Most agents default to `enabled: true`. Exceptions: `diagnostic`, `stale_branch_cleanup`, and `meta_periodic` default to `false`.
>
> ² `trace_health`, `ci_monitor`, `member_sync`, and `diagnostic` do **not** have a
> `memory_path` field — they write no per-agent memory ledger
> (`member_sync` and `diagnostic` are deterministic passes with no LLM agent).
>
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
| `periodic.bespoke_periodic` | `MILL_BESPOKE_PERIODIC` | `true` | Master toggle for the per-repo bespoke periodic agent supervisor (default `true` — enabled) |
| `periodic.bespoke_discovery_interval_seconds` | `MILL_BESPOKE_DISCOVERY_INTERVAL_SECONDS` | `600` | Seconds between bespoke supervisor clone-refresh and agent-reconciliation cycles. A new YAML committed to a managed repo's `.robotsix-mill/agents/` lands within this window. |
| `periodic.ci_monitor.log_max_bytes` | `MILL_CI_LOG_MAX_BYTES` | `65536` | Max bytes fetched per CI job log |
| `periodic.diagnostic.target_repo_id` | `MILL_DIAGNOSTIC_TARGET_REPO_ID` | `robotsix-mill` | Board the diagnostic agent routes activity to; single-repo fallback when the monitored list is empty |
| `periodic.diagnostic.monitored_repo_ids` | `MILL_DIAGNOSTIC_MONITORED_REPO_IDS` | `[]` | Repos the diagnostic agent monitors each pass (JSON list); empty → falls back to `target_repo_id`. Add/remove repos here — no code change. See [diagnostic-agent.md](diagnostic-agent.md) |
| `periodic.langfuse_cleanup.max_traces` | `MILL_LANGFUSE_CLEANUP_MAX_TRACES` | `5000` | Max traces retained in the shared workspace Langfuse project when `langfuse_cleanup_periodic` is enabled; oldest traces are deleted to stay under this cap. Centralized (global-only) — one pass per interval, not per-repo. |
| `pipeline.retrospect_spawn_drafts` | `MILL_RETROSPECT_SPAWN_DRAFTS` | `true` | Allow retrospect to file improvement draft tickets |
| `pipeline.retrospect_spawn_agented_proposals` | `MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS` | `true` | When True, retrospect may append AGENT.md proposals to AGENT_CANDIDATES.md for human review. |
| `pipeline.retrospect_memory_path` | `MILL_RETROSPECT_MEMORY_PATH` | `None` | Override path for retrospect memory |
| `pipeline.trace_inspector_memory_path` | `MILL_TRACE_INSPECTOR_MEMORY_PATH` | `None` | Override path for trace-inspector memory |

#### trace_review

The trace-review periodic agent inspects Langfuse traces for anomalies
(cost spikes, tool-call errors, repeated-tool storms, explore loops,
ask_user stalls) and files draft tickets with proposed fixes. Every
field below is settable via its `MILL_TRACE_REVIEW_*` environment
variable and its dotted YAML path.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.trace_review.enabled` | `MILL_TRACE_REVIEW_PERIODIC` | `true` | Enable periodic trace-review passes |
| `periodic.trace_review.interval_seconds` | `MILL_TRACE_REVIEW_INTERVAL_SECONDS` | `86400` | Seconds between trace-review passes (minimum 3600) |
| `periodic.trace_review.cost_multiplier` | `MILL_TRACE_REVIEW_COST_MULTIPLIER` | `3.0` | Outlier threshold: cost > batch median × N → flagged |
| `periodic.trace_review.per_obs_cost_threshold` | `MILL_TRACE_REVIEW_PER_OBS_COST_THRESHOLD` | `0.001` | Per-observation cost threshold for flagging |
| `periodic.trace_review.obs_multiplier` | `MILL_TRACE_REVIEW_OBS_MULTIPLIER` | `3.0` | Outlier threshold: observation count > batch median × N → flagged |
| `periodic.trace_review.max_repeated_tool` | `MILL_TRACE_REVIEW_MAX_REPEATED_TOOL` | `50` | Absolute cap on repeated tool calls before flagging |
| `periodic.trace_review.max_tool_calls` | `MILL_TRACE_REVIEW_MAX_TOOL_CALLS` | `100` | Hard cap on total tool calls per trace inspection |
| `periodic.trace_review.max_errors` | `MILL_TRACE_REVIEW_MAX_ERRORS` | `20` | Hard cap on tool-call errors before auto-termination |
| `periodic.trace_review.model_level` | `MILL_TRACE_REVIEW_MODEL_LEVEL` | `1` | Model tier for the trace inspector (1–3) |
| `periodic.trace_review.inspector_min_requests` | `MILL_TRACE_REVIEW_INSPECTOR_MIN_REQUESTS` | `20` | Floor for the tools-on request budget |
| `periodic.trace_review.inspector_max_requests` | `MILL_TRACE_REVIEW_INSPECTOR_MAX_REQUESTS` | `80` | Ceiling for the tools-on request budget |
| `periodic.trace_review.inspector_requests_per_obs` | `MILL_TRACE_REVIEW_INSPECTOR_REQUESTS_PER_OBS` | `0.1` | Requests granted per observation before clamping |
| `periodic.trace_review.inspector_max_obs_for_tools` | `MILL_TRACE_REVIEW_INSPECTOR_MAX_OBS_FOR_TOOLS` | `200` | Observation count above which code-access tools are dropped |
| `periodic.trace_review.inspector_toolless_requests` | `MILL_TRACE_REVIEW_INSPECTOR_TOOLLESS_REQUESTS` | `3` | Request budget for the tool-less summary-only path |
| `periodic.trace_review.tool_request_limit` | `MILL_TRACE_REVIEW_TOOL_REQUEST_LIMIT` | `15` | Request budget for the interactive `langfuse_inspect_trace` tool |
| `periodic.trace_review.max_drafts_per_run` | `MILL_TRACE_REVIEW_MAX_DRAFTS_PER_RUN` | `5` | Cap on drafted findings per trace-review pass |
| `periodic.trace_review.max_inspections_per_run` | `MILL_TRACE_REVIEW_MAX_INSPECTIONS_PER_RUN` | `5` | Hard cap on LLM inspector calls per trace-review run |
| `periodic.trace_review.max_traces_per_run` | `MILL_TRACE_REVIEW_MAX_TRACES_PER_RUN` | `300` | Hard cap on traces pulled for full detail per run |
| `periodic.trace_review.initial_lookback_hours` | `MILL_TRACE_REVIEW_INITIAL_LOOKBACK_HOURS` | `24` | First-run lookback window when no watermark exists (hours) |
| `periodic.trace_review.restart_correlation_window_seconds` | `MILL_TRACE_REVIEW_RESTART_CORRELATION_WINDOW_SECONDS` | `60` | Window for correlating incomplete traces with process restarts (seconds) |
| `periodic.trace_review.dedup_lookback_days` | `MILL_TRACE_REVIEW_DEDUP_LOOKBACK_DAYS` | `7` | Recency window (days) for pre-filing duplicate check |
| `pipeline.trace_review_target_repo_id` | `MILL_TRACE_REVIEW_TARGET_REPO_ID` | `""` | Target repo for trace-review drafts; empty → source-repo routing |

#### data_dir_audit

The `data_dir_audit` periodic agent surveys the mill's data directory for
monotonic growth and files findings when storage crosses configured
thresholds. In addition to the standard `periodic` fields above, these
agent-specific settings are available:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.data_dir_audit.enabled` | `MILL_DATA_DIR_AUDIT_PERIODIC` | `true` | Master switch for the periodic data-dir audit pass. Default `true` — the agent is harmless when idle (no findings). |
| `periodic.data_dir_audit.interval_seconds` | `MILL_DATA_DIR_AUDIT_INTERVAL_SECONDS` | `86400` | Seconds between periodic data-dir audit passes. Minimum enforced at 60 s in the worker loop. |
| `periodic.data_dir_audit.memory_path` | `MILL_DATA_DIR_AUDIT_MEMORY_PATH` | `None` | Override path for the data-dir audit memory ledger; defaults to `<data_dir>/data_dir_audit_memory.md`. |
| `periodic.data_dir_audit.size_threshold_bytes` | `MILL_DATA_DIR_AUDIT_SIZE_THRESHOLD_BYTES` | `104857600` | Threshold (bytes) for the top-N largest-items check. Files and directories whose cumulative size reaches this threshold are reported as oversized. Default 100 MiB. |
| `periodic.data_dir_audit.growth_delta_bytes` | `MILL_DATA_DIR_AUDIT_GROWTH_DELTA_BYTES` | `10485760` | If a file or directory grew by at least this many bytes since the last audit pass, flag it for growth. Default 10 MiB. |
| `periodic.data_dir_audit.growth_delta_pct` | `MILL_DATA_DIR_AUDIT_GROWTH_DELTA_PCT` | `20` | If a file or directory grew by at least this percentage since the last audit pass, flag it for growth. |
| `periodic.data_dir_audit.growth_delta_pct_min_bytes` | `MILL_DATA_DIR_AUDIT_GROWTH_DELTA_PCT_MIN_BYTES` | `1048576` | Minimum absolute growth (bytes) required for the percentage threshold to fire. Suppresses tiny-baseline false positives. Gates only the pct contributor; the bytes contributor is unaffected. Default 1 MiB. |
| `periodic.data_dir_audit.max_drafts_per_pass` | `MILL_DATA_DIR_AUDIT_MAX_DRAFTS_PER_PASS` | `5` | Maximum number of drafts created per data-dir audit pass. Findings beyond this cap are dropped and re-considered on the next scheduled pass. |
| `periodic.data_dir_audit.prune_closed` | `MILL_DATA_DIR_AUDIT_PRUNE_CLOSED` | `false` | Opt-in GC: prune workspace directories of tickets in a terminal state (CLOSED / EPIC_CLOSED / ANSWERED) during the data-dir audit pass, before size measurement. Default `false`. |
| `periodic.data_dir_audit.prune_closed_age_seconds` | `MILL_DATA_DIR_AUDIT_PRUNE_CLOSED_AGE_SECONDS` | `604800` | Minimum age (seconds since the ticket entered its terminal state) before its workspace becomes eligible for prune_closed GC. Recent closures are kept for post-mortems. Default 7 days. |
| `periodic.data_dir_audit.prune_terminal_clones` | `MILL_DATA_DIR_AUDIT_PRUNE_TERMINAL_CLONES` | `true` | Default-on GC: prune the reproducible git clones (`repo/` and `repos/`) inside workspaces of terminal-state tickets at the start of each data-dir audit pass, before size measurement. |
| `periodic.data_dir_audit.prune_terminal_clones_age_seconds` | `MILL_DATA_DIR_AUDIT_PRUNE_TERMINAL_CLONES_AGE_SECONDS` | `86400` | Minimum age (seconds since the ticket entered its terminal state) before its clones are pruned. Clones are cheap to recreate, so the guard is short. Default 1 day. |
| `periodic.data_dir_audit.prune_db_rows` | `MILL_DATA_DIR_AUDIT_PRUNE_DB_ROWS` | `true` | Default-on DB row GC: purge oldest terminal-ticket rows (and their associated events, comments, and proposed actions) when the count of terminal tickets exceeds `max_archived_tickets`. |
| `periodic.data_dir_audit.prune_memory_ledgers` | `MILL_DATA_DIR_AUDIT_PRUNE_MEMORY_LEDGERS` | `true` | Default-on GC: truncate over-cap `*_memory.md` files on disk before size measurement, using the same tail_keep primitive the agent already uses at read/write time. |
| `periodic.data_dir_audit.prune_orphans` | `MILL_DATA_DIR_AUDIT_PRUNE_ORPHANS` | `true` | Default-on GC: prune orphan workspace directories (ticket absent from the board DB) older than the configured age at the start of each data-dir audit pass, before size measurement. |
| `periodic.data_dir_audit.prune_orphans_age_seconds` | `MILL_DATA_DIR_AUDIT_PRUNE_ORPHANS_AGE_SECONDS` | `86400` | Minimum age (seconds since the ticket-ID timestamp) before an orphan workspace becomes eligible for GC. Default 1 day. |

#### run_health

The run-health periodic agent reads every board's run registry over a
lookback window, flags failed/degraded runs deterministically, runs one
LLM pass to separate real failures from legitimate empties, and files
high-confidence draft tickets to the mill board. Every field below is
settable via its `MILL_RUN_HEALTH_*` environment variable and its dotted
YAML path.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.run_health.enabled` | `MILL_RUN_HEALTH_PERIODIC` | `true` | Enable periodic run-health passes |
| `periodic.run_health.interval_seconds` | `MILL_RUN_HEALTH_INTERVAL_SECONDS` | `86400` | Seconds between run-health passes |
| `periodic.run_health.window_hours` | `MILL_RUN_HEALTH_WINDOW_HOURS` | `168` | Lookback window (hours) over which run registries are scanned |
| `periodic.run_health.target_repo_id` | `MILL_RUN_HEALTH_TARGET_REPO_ID` | `robotsix-mill` | Board the run-health agent files its drafts to |
| `periodic.run_health.memory_path` | `MILL_RUN_HEALTH_MEMORY_PATH` | `None` | Override path for the run-health memory ledger; defaults to `<data_dir>/<board>/run_health_memory.md` |

#### db_maintenance

The `db_maintenance` periodic agent runs SQLite maintenance (`VACUUM`,
`ANALYZE`, WAL checkpoint) to keep the ticket database healthy.  It has
**no YAML path** — configure it via environment variables only:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_DB_MAINTENANCE_PERIODIC` | `true` | Enable periodic database maintenance passes |
| `MILL_DB_MAINTENANCE_INTERVAL_SECONDS` | `86400` | Seconds between database maintenance passes |

#### sandbox_reaper

The `sandbox_reaper` periodic agent prunes stopped Docker sandbox
containers left behind by the implement stage.  YAML paths
(`periodic.sandbox_reaper.enabled`, `periodic.sandbox_reaper.interval_seconds`)
and environment variables are both available:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_SANDBOX_REAPER_PERIODIC` | `true` | Enable periodic sandbox-container reaping |
| `MILL_SANDBOX_REAPER_INTERVAL_SECONDS` | `3600` | Seconds between sandbox-reaper passes |

#### survey

The `survey` periodic agent searches for library/ecosystem news and files
draft tickets with findings. Four extra fields beyond the generic periodic
pattern control its tool-call and web-fetch budgets:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_SURVEY_PERIODIC` | `true` | Enable periodic survey passes |
| `MILL_SURVEY_INTERVAL_SECONDS` | `86400` | Seconds between survey passes |
| `MILL_SURVEY_MEMORY_PATH` | `None` | Override path for survey memory; defaults to `<data_dir>/survey_memory.md` |
| `MILL_SURVEY_REQUEST_LIMIT` | `40` | Per-call request cap for the survey agent |
| `MILL_SURVEY_WEB_FETCH_MAX_CALLS` | `5` | Max real (cache-miss) web_fetch calls per survey run |
| `MILL_SURVEY_WEB_FETCH_MAX_TOTAL_BYTES` | `500000` | Cumulative ceiling on returned fetch bytes per survey run |
| `MILL_SURVEY_WEB_SEARCH_MAX_CALLS` | `5` | Max web_search invocations per survey run |

#### Env-var-only periodic agents

`bc_check` and `completeness_check` enabled, interval, and memory_path
fields are available as YAML paths (`periodic.bc_check.*`, `periodic.completeness_check.*`)
and as environment variables:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_BC_CHECK_PERIODIC` | `true` | Enable periodic backward-compatibility inspection |
| `MILL_BC_CHECK_INTERVAL_SECONDS` | `86400` | Seconds between bc-check passes |
| `MILL_BC_CHECK_MEMORY_PATH` | `None` | Override path for bc-check memory; defaults to `<data_dir>/bc_check_memory.md` |
| `MILL_COMPLETENESS_CHECK_PERIODIC` | `true` | Enable periodic feature-wiring completeness inspection |
| `MILL_COMPLETENESS_CHECK_INTERVAL_SECONDS` | `86400` | Seconds between completeness-check passes |
| `MILL_COMPLETENESS_CHECK_MEMORY_PATH` | `None` | Override path for completeness-check memory; defaults to `<data_dir>/completeness_check_memory.md` |
| `MILL_COMPLETENESS_CHECK_REQUEST_LIMIT` | `80` | Per-call request cap for the completeness-check agent |
| `MILL_STATE_SYNC_MODEL` | `deepseek/deepseek-v4-flash` | Model for the state-sync agent (cross-surface `State` enum consistency check) |
| `MILL_STATE_SYNC_PERIODIC` | `true` | Enable periodic state-sync passes |
| `MILL_STATE_SYNC_INTERVAL_SECONDS` | `86400` | Seconds between state-sync passes |
| `MILL_STATE_SYNC_MEMORY_PATH` | `None` | Override path for state-sync memory; defaults to `<data_dir>/state_sync_memory.md` |
| `MILL_ENV_DOC_SYNC_MODEL` | `deepseek/deepseek-v4-flash` | Model for the env-doc-sync agent (env-var documentation consistency check) |
| `MILL_ENV_DOC_SYNC_PERIODIC` | `true` | Enable periodic env-doc-sync passes |
| `MILL_ENV_DOC_SYNC_INTERVAL_SECONDS` | `86400` | Seconds between env-doc-sync passes |
| `MILL_ENV_DOC_SYNC_MEMORY_PATH` | `None` | Override path for env-doc-sync memory; defaults to `<data_dir>/env_doc_sync_memory.md` |

#### Stale branch cleanup, timeout escalation, dependabot ingest, module curator

These four periodic agents each carry one or two extra fields beyond the generic periodic pattern (periodic, interval, memory path). The following env vars configure those agent-specific extras:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_STALE_BRANCH_MAX_AGE_DAYS` | `30` | A branch is eligible for cleanup only if its last commit is older than this many days |
| `MILL_STALE_BRANCH_CLEANUP_PREFIX_ONLY` | `true` | When `true`, only delete branches whose name starts with `branch_prefix` ("old mill" branches); when `false`, also reap any other stale branch ("stale dev") |
| `MILL_TIMEOUT_ESCALATION_THRESHOLD_SECONDS` | `259200` | Tickets in `AWAITING_USER_REPLY` with `updated_at` older than this many seconds are escalated to `BLOCKED`; set ≤ 0 to disable escalation |
| `MILL_DEPENDABOT_INGEST_MAX_DRAFTS_PER_PASS` | `5` | Maximum number of Dependabot drafts created per ingest pass (across all repos) |
| `MILL_MODULE_CURATOR_REQUEST_LIMIT` | `120` | Per-call request budget for the module-curator agent |

### 13. Skills & language instructions

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `sandbox.skills_dir` | `MILL_SKILLS_DIR` | `skills` | Directory of skill docs injected into agent system prompts |
| `core.language_instructions_dir` | `MILL_LANGUAGE_INSTRUCTIONS_DIR` | `agent_definitions/language_instructions` | Directory of per-language instruction Markdown snippets injected into the implement agent's system prompt |

### 14. Board agent & board manager

The board agent is an opt-in agent-comm service that connects the mill
to its own board API, allowing agent-to-agent communication through a
central broker. The board manager is a conversational, natural-language
board management agent — a level-3 LLM agent that acts on the board
(with a level-1 recall pass over its capped question→answer memory).
Both are off by default.

#### Board agent

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `board_agent.enabled` | `MILL_BOARD_AGENT_ENABLED` | `false` | Master switch for the board agent |
| `board_agent.api_url` | `MILL_BOARD_AGENT_API_URL` | `http://127.0.0.1:8077` | Board REST API the agent calls (this mill) |
| `board_agent.api_token` | `MILL_BOARD_AGENT_API_TOKEN` | `""` | Bearer token for the board API (`""` = none, e.g. loopback) |
| `board_agent.repo_id` | `MILL_BOARD_AGENT_REPO_ID` | `""` | Board repo ID (`""` → the worker's lead repo) |
| `board_agent.write_ops` | `MILL_BOARD_AGENT_WRITE_OPS` | `true` | Allow write ops (create_ticket, transition, …) |
| `board_agent.broker_host` | `MILL_BOARD_AGENT_BROKER_HOST` | `""` | Agent-comm broker host (pull/mailbox mode — NAT-safe, outbound-only). When set the board agent runs as a BrokeredBoardResponder reachable from off-host clients (e.g. the cost-analyst); `broker_token` authenticates this agent to the broker. `""` → disabled |
| `board_agent.broker_port` | `MILL_BOARD_AGENT_BROKER_PORT` | `443` | Broker port |
| `board_agent.broker_scheme` | `MILL_BOARD_AGENT_BROKER_SCHEME` | `https` | Broker scheme (`http`/`https`) |
| `board_agent.broker_token` | `MILL_BOARD_AGENT_BROKER_TOKEN` | `""` | This agent's bearer token for the broker |

#### Board manager

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `board_manager.enabled` | `MILL_BOARD_MANAGER_ENABLED` | `false` | Master switch for the board manager |
| `board_manager.broker_token` | `MILL_BOARD_MANAGER_BROKER_TOKEN` | `""` | The manager's bearer token for the broker |
| `board_manager.model` | `MILL_BOARD_MANAGER_MODEL` | `""` | Level-3 model (`""` → tier default) |
| `board_manager.recall_model` | `MILL_BOARD_MANAGER_RECALL_MODEL` | `""` | Level-1 recall model (`""` → tier default) |
| `board_manager.max_conversations` | `MILL_BOARD_MANAGER_MAX_CONVERSATIONS` | `200` | Cap on retained question→answer turns |

#### Component agent

The component agent is a generic monitor/config responder on the agent-comm broker. Off by default.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `component_agent.enabled` | `MILL_COMPONENT_AGENT_ENABLED` | `false` | Master switch for the component agent |
| `component_agent.agent_id` | `MILL_COMPONENT_AGENT_AGENT_ID` | `"component-robotsix-mill"` | Agent id registered on the broker |
| `component_agent.broker_host` | `MILL_COMPONENT_AGENT_BROKER_HOST` | `""` | Broker host; empty string disables |
| `component_agent.broker_port` | `MILL_COMPONENT_AGENT_BROKER_PORT` | `443` | Broker port |
| `component_agent.broker_scheme` | `MILL_COMPONENT_AGENT_BROKER_SCHEME` | `"https"` | Broker scheme (http/https) |
| `component_agent.broker_token` | `MILL_COMPONENT_AGENT_BROKER_TOKEN` | `""` | Bearer token for the broker |

---

## Secrets reference

Secrets are loaded from `config/secrets.yaml` by a separate `Secrets`
Pydantic model. They are **not** merged into `Settings` — access them
via `get_secrets()`.

| YAML key | Env var override | Description |
|----------|-----------------|-------------|
| `openrouter_api_key` | `OPENROUTER_API_KEY` | OpenRouter API key (required for any LLM call) |
| `openrouter_management_key` | — | OpenRouter management API key for credit balance checks (`GET /api/v1/activity`). Separate from the inference key; leave blank to skip OpenRouter-side fetching. |
| `forge_token` | `FORGE_TOKEN` | PAT for forge authentication |
| `forge_repo_create_token` | — | Fine-grained PAT used ONLY for repo creation. Falls back to `forge_token` if unset. |
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
| `repos.<id>.working_branch` | no | — | Per-repo target branch for clone/baseline/deliver operations. When set, overrides the global `forge_target_branch`. Use this for repos whose default branch is not `main` (e.g. `rolling`, `lyrical`, `develop`). Automatically populated by member-sync from the manifest `version` field. |
| `repos.<id>.langfuse.project_name` | yes | — | Langfuse project name for this repo's traces |
| `repos.<id>.langfuse.public_key` | yes | — | Langfuse public key for this repo's project |
| `repos.<id>.langfuse.secret_key` | yes | — | Langfuse secret key for this repo's project |
| `repos.<id>.langfuse.base_url` | no | `https://cloud.langfuse.com` | Langfuse base URL |

Each repo ID must be unique and non-empty. The `board_id` must also be
non-empty. The registry validates that every entry's `repo_id` matches
its YAML key.

### Per-repo branch configuration

Every stage that clones, bases PRs, or rebases work (refine, implement,
deliver, merge, CI monitor, etc.) resolves the **effective target branch**
for each repo using this rule:

1. If `repos.<id>.working_branch` is set in `config/repos.yaml`, **use that**.
2. Otherwise, use the global `forge_target_branch` setting (default `main`).

This allows repos with non-main default branches to be fully onboarded:

```yaml
# config/repos.yaml
repos:
  ros2-example-interfaces:
    board_id: "example-interfaces"
    forge_remote_url: "https://github.com/damien-robotsix/example_interfaces.git"
    working_branch: lyrical  # This repo's default branch is 'lyrical', not 'main'
    langfuse:
      project_name: "example-interfaces"
      public_key: "pk-lf-..."
      secret_key: "sk-lf-..."
```

With this configuration, the mill will:
- Clone against `origin/lyrical` instead of `origin/main`
- Run baseline tests on the `lyrical` branch
- Open PRs into `lyrical` (not `main`)
- Rebase work onto `lyrical`

When `working_branch` is absent, every repo uses the global default,
preserving backward compatibility with existing deployments.

#### Common use cases

- **Cross-repo contributions**: when a managed repo forks or contributes to an upstream repo that uses a different default branch (e.g. ROS 2 repos use `rolling` or `lyrical` instead of `main`)
- **Workspace member auto-registration**: member-sync automatically populates `working_branch` from each member's vcs2l manifest `version` field
- **Development branches**: when a repo is in active development on a non-default branch and tickets should target that branch until release

### Workspace member auto-registration

A master repository that uses vcs2l manifests to declare workspace members
can opt into **automatic registration** of those members as RepoConfig
entries. When enabled, the mill detects members from the manifest and
automatically upserts them into `config/repos.yaml`, creating boards and
filing build-out tickets on their behalf.

#### How it works

The workspace-member sync agent:

1. **Detects** vcs2l manifest members from the master repo's manifest file
   (typically `.rosinstall`).
2. **Derives** a `repo_id` from each member's path key (e.g. `src/zeta/pkg`
   → `src-zeta-pkg`), slugifying special characters to ASCII.
3. **Inherits** Langfuse configuration from the master repo so all members
   share observability projects.
4. **Upserts** entries into `config/repos.yaml` with the member's:
   - `forge_remote_url` from the manifest `url` field
   - `working_branch` from the manifest `version` field (if present)
   - `cross_repo_target` upstream policy (if present)
   - `member_of: <master_repo_id>` provenance marker
5. **Flags** members that vanish from the manifest with `pending_removal: true`
   instead of auto-deleting — boards + history stay intact for operator review.
6. **Files** a build-out ticket on each newly registered member's board so the
   pipeline populates the member's `.robotsix-mill/config.yaml` and enables it.

#### Fields added by auto-registration

When a member is auto-registered, its entry carries additional fields:

| YAML key | Description |
|----------|-------------|
| `member_of` | Master repo ID; presence indicates this entry was synced from a manifest. Used to scope disappearance detection — only this master's members are affected by subsequent sync passes. |
| `pending_removal` | Set to `true` when the member vanishes from the manifest but the entry is retained for operator review. Cleared when the member reappears. |

Manual entries (not synced) omit both fields, so sync passes never modify
them — collision with a non-member entry is logged and skipped.

#### Integration with repo provisioning

Auto-registered members follow the same onboarding path as manually
configured repos:

- **Board creation** happens automatically on first ticket write (no explicit
  board provisioning needed).
- **Build-out ticket** is filed on the member's board with instructions to add
  `.robotsix-mill/config.yaml` (test command + languages).
- **Langfuse project** is inherited from the master repo and wired
  automatically.
- **Cross-repo targeting** is configured if the manifest declares an upstream
  policy for the member.

This integration ensures members are fully onboarded into the mill pipeline
in a single pass without additional operator steps.

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
- [observability.md](observability.md) — per-repo Langfuse + deployed-log config the refine agent consults
- [deployment.md](deployment.md) — continuous deployment guide
- [config-audit.md](config-audit.md) — complete inventory of every config value and its source
- [`config/mill.defaults.yaml`](../config/mill.defaults.yaml) — committed canonical defaults
- [`config/secrets.example.yaml`](../config/secrets.example.yaml) — secrets template
