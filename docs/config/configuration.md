# Configuration reference

robotsix-mill uses a **single JSON config file**. Every non-secret knob
plus a top-level `"secrets"` block (API keys, tokens) live in one
`config/config.json`. The committed `config/config.example.json`
documents the full structure with safe defaults and `"SECRET"` sentinel
placeholders. Secrets are loaded by a dedicated `Secrets` model — they
are never logged and their values are redacted in diagnostics.

> **Note:** The config file is flat alias-keyed JSON (e.g. `"MILL_DATA_DIR"`,
> `"data_dir"`), not nested YAML. The "YAML path" column in the setting
> tables below shows the **conceptual** dotted path for readability — the
> actual JSON keys are the flat aliases from the "Env var" column (or their
> unprefixed equivalents). See [Add a new setting](#add-a-new-setting) for
> how to wire a new field.

---

## Configuration loading order

Settings are resolved from these layers (highest priority first):

| Priority | Source | Description |
|----------|--------|-------------|
| 1 (highest) | Explicit `Settings(k=v)` kwargs | Programmatic overrides from callers |
| 2 | `os.environ` | Any `MILL_*` or unprefixed variable set in the environment |
| 3 | The config file | `config/config.json` if present, else the committed `config/config.example.json` |
| 4 (lowest) | `Field(default=...)` | Static Python defaults in the Pydantic model |

The non-secret part of the file feeds the `Settings` model; environment
variables win over any JSON value. Point `ROBOTSIX_CONFIG_FILE` at a
specific file to override the default resolution (an empty string forces
the committed example, the hermetic choice used by the test suite).

**Secrets** are loaded from the `"secrets"` block of the same file
(located via `ROBOTSIX_CONFIG_FILE`). They never participate in
the Settings merge; access them via `get_secrets()`. A secret whose
value is the literal `"SECRET"` sentinel (as in `config.example.json`) is
treated as unset.

---

## File structure

```
config/
  config.json              # gitignored: THE single config — all knobs + a "secrets" block
  config.example.json      # committed: template (safe defaults + "SECRET" sentinels)
  config.schema.json       # committed: JSON Schema for config.json
  #  (example repo entries live under the "repos" key of config/config.example.json)
```

### Getting started

Copy the committed template and fill in your real values:

```sh
cp config/config.example.json config/config.json
# edit config/config.json — set the "secrets" block and any host overrides
chmod 600 config/config.json          # it now holds real credentials
```

`config/config.json` is gitignored and never committed. If it is absent
(CI, a fresh checkout), the loader falls back to the committed
`config/config.example.json`, whose all-`"SECRET"` secrets resolve to
"unset".

---

## Common tasks

### Change which model an agent uses

Models are not configured per-agent in the JSON config file. Each agent definition
(`agent_definitions/<name>.yaml`) declares a capability `level: 1|2|3`,
which resolves to a `(transport, model)` via robotsix-llmio's tier
defaults (see [§1 Capability levels](#1-capability-levels-model-selection)).
To change an agent's model, change its `level` in the definition; to change
what a level maps to, change the defaults in robotsix-llmio.

### Use a different database URL / data directory

```json
// config/config.json
{
  "settings": {
    "MILL_DATA_DIR": "/data/mill-prod"
  }
}
```

### Enable periodic audit and trace-health checks

```json
// config/config.json
{
  "settings": {
    "audit_periodic": true,
    "audit_interval_seconds": 43200,
    "trace_health_periodic": true,
    "trace_health_interval_seconds": 86400
  }
}
```

### Deploy to production with overrides

Put deployment values directly in `config/config.json` (or point
`ROBOTSIX_CONFIG_FILE` at an alternative file):

```json
// config/config.json
{
  "settings": {
    "MILL_MAX_GLOBAL_CONCURRENCY": 2,
    "MILL_MAX_SPEND_USD_PER_TICKET": 5.0,
    "forge_kind": "github",
    "forge_remote_url": "https://github.com/your-org/your-repo",
    "forge_target_branch": "main",
    "MILL_TEST_COMMAND": "pytest -q --timeout=300"
  }
}
```

This `MILL_TEST_COMMAND` is the global fallback. A managed repo can
override it by committing a `test_command` to its own
`.robotsix-mill/config.yaml`, and the operator can override it per repo
in `repos.yaml`. The precedence is: per-repo `.robotsix-mill/config.yaml`
`test_command` > `repos.yaml` per-repo `test_command` > this global
`MILL_TEST_COMMAND`; empty everywhere makes the gate pass.

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
docker compose up -d   # reads config/config.json (or set ROBOTSIX_CONFIG_FILE)
```

### Deployed log folder (`deployed_log_folder`)

The operator can point the refine agent at a repo's live deployment log
directory by setting a single per-repo field in mill's central,
`config/config.json` (the `"repos"` key), alongside `board_id` /
`forge_remote_url`:

```json
// Inside the "repos" block of config/config.json
"robotsix-auto-mail": {
  "board_id": "...",
  "deployed_log_folder": "/var/log/robotsix-auto-mail"
}
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
  ignored). See [observability.md](../langfuse/observability.md) for the full story.

### Set up secrets

Fill in the `"secrets"` block of `config/config.json` (replace each
`"SECRET"` sentinel with a real value):

```json
// config/config.json
{
  "secrets": {
    "openrouter_api_key": "sk-or-...",
    "forge_token": "ghp_..."
  }
}
```

File permissions should be `0600` (it holds real credentials).

### Add a new setting

1. Add the field to the Pydantic model in `src/robotsix_mill/config/`
   (in the appropriate group class if grouped, or on `Settings` directly).
   Use `Field(alias=...)` with a `MILL_` prefix + uppercase underscore
   naming convention (e.g. `Field(alias="MILL_MY_NEW_FIELD")`). The alias
   is the flat JSON key — there is no nested YAML path.
2. Add the alias key and its default value to the `"settings"` block of
   `config/config.example.json`.
3. If it's a secret, add it to the `Secrets` model and to the `"secrets"`
   block of `config/config.example.json` (as a `"SECRET"` sentinel) instead.
4. Access it in code: `settings.my_new_field` for settings,
   `get_secrets().my_new_secret` for secrets.

Steps 1–3 are enforced deterministically by
`scripts/check_config_sync.py`, which runs as a blocking CI step
("Validate config sync") and as the `validate-config-sync` pre-commit
hook. It fails if a `"settings"` key in `config.example.json` names a
non-existent `Settings` field/alias (or vice-versa), or if the
`"secrets"` block drifts from the `Secrets` model. Intentional gaps
live in the script's inline-commented exception sets. (Doc-table drift
is not gated here — the heuristic `config_sync` agent still covers that.)

## Config drift prevention

**Rule:** Every new Pydantic settings field added to
`_settings_periodic.py` or `settings.py` MUST have a corresponding
entry in BOTH `config/config.example.json` (under the `"settings"`
block) AND a `Field(alias=...)` on the model in the same commit.
Fields missing from both surfaces are invisible to
`check_config_sync.py` — the suite only cross-references JSON keys ↔
model fields, not Settings-model ↔ surfaces.

**Rationale:** PR #1546 and the still-unfiled prune_orphans gap
(PR #1533): two instances where Pydantic fields were added to the
model but never wired to config surfaces. The drift checker could
not catch either because the gap was symmetric. This rule encodes
the same convention as the `docs/modules.yaml` "add the path in
the same commit" discipline.

---

## Full setting reference

Every setting below shows:
- **YAML path** — the **conceptual** dotted path (for readability). The
  actual `config/config.example.json` uses flat alias keys — the "Env var"
  column shows the real JSON key (the Pydantic field `alias`).
- **Env var** — the environment variable override (also the flat JSON key)
- **Default** — the committed default value
- **Description** — what it controls

### 1. Capability levels (model selection)

Per-agent model selection is declared in each **agent definition's**
`level: 1|2|3` field — a pure capability axis. `build_agent` resolves a
level via robotsix-llmio's two provider slots: the **default** slot
(Anthropic via the Claude SDK: haiku / opus / claude-fable-5) serves
normal operation, and the **fallback** slot (DeepSeek via OpenRouter:
flash / flash-with-reasoning / pro) serves the same levels while llmio's
automatic provider failover is active. Levels never fall back to one
another; providers do (opt-in via `provider_failover_enabled`, visible in
the board UI banner and `GET /provider-status`).

| Level | Intent |
|-------|--------|
| 1 | cheap, frequent — triage, dedup, classifiers, periodic scanners, … |
| 2 | workhorse — implement, ci_fix, review, refine, test, … |
| 3 | frontier — epic_breakdown, hardest reasoning |

> **Note:** The concrete model behind each level is owned by robotsix-llmio's
> tier defaults and may change without notice in mill.  To see or override the
> current binding, consult llmio's tier configuration.

Default-slot agents run on the Claude Agent SDK (subscription auth; needs
Node + the `claude` CLI in the container). These knobs govern that path:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.claude_max_concurrency` | `MILL_CLAUDE_MAX_CONCURRENCY` | `4` | Deprecated, inert since the Claude run semaphore was removed: no longer bounds anything. Kept so existing pinned configs load |
| `core.claude_sdk_vision_enabled` | `MILL_CLAUDE_SDK_VISION_ENABLED` | `false` | **Legacy Claude-only vision gate — being removed.** Image feeding is now provider-agnostic through llmio's `images=` parameter on `build_agent`: the Claude SDK reads images natively, and OpenRouter (DeepSeek) gets an `ask_image` tool answered by the `TierConfig.vision` binding (see [Image handling guide](../guides/image-handling.md)). The prompt always stays text-only; never embed `BinaryContent`. New configs should omit this flag — screenshots reach the model via `images=` regardless of its value. |
### 2. Request limits

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.coordinator_requests` | `MILL_COORDINATOR_REQUEST_LIMIT` | `500` | Per-pass request budget for the implement (coordinator) agent. Resets each pass; normal tickets fit in one pass. Hard upper bound 5000 |
| `core.limits.subtask_request_limit` | `MILL_SUBTASK_REQUEST_LIMIT` | `30` | Per-subtask request cap for `spawn_subtask` sub-agents delegated by the coordinator |
| `core.limits.explore_requests` | `MILL_EXPLORE_REQUEST_LIMIT` | `100` | Per-call request cap for the explore sub-agent |
| `core.limits.explore_max_tokens` | `MILL_EXPLORE_MAX_TOKENS` | `4096` | Output token cap for explore sub-agent responses |
| `core.limits.explore_timeout_seconds` | `MILL_EXPLORE_TIMEOUT_SECONDS` | `30.0` | Wall-clock timeout (seconds) for a single explore sub-agent call. Default 30 s. Minimum 1 s |
| `core.limits.explore_model_level` | `MILL_EXPLORE_MODEL_LEVEL` | `1` | Capability level for the exploration sub-agent (1 = haiku on the Claude subscription). Claude-backed levels run the scout through the SDK tool loop, so `explore_request_limit` only bounds the OpenRouter fallback slot; the wall-clock timeout applies to both. Range 1–3 |
| `core.limits.consult_requests` | `MILL_CONSULT_REQUEST_LIMIT` | `15` | Per-call request cap for the domain-expert consultation sub-agent |
| `core.limits.test_requests` | `MILL_TEST_REQUEST_LIMIT` | `30` | Per-call request cap for the test sub-agent |
| `core.limits.web_research_requests` | `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `16` | Per-call request cap for the web-research sub-agent |
| `core.limits.dedup_requests` | `MILL_DEDUP_REQUEST_LIMIT` | `12` | Per-call request cap for the dedup check |
| `core.limits.obsolescence_requests` | `MILL_OBSOLESCENCE_REQUEST_LIMIT` | `6` | Per-call request cap for the obsolescence gate |
| `core.limits.scope_triage_max_files` | `MILL_SCOPE_TRIAGE_MAX_FILES` | `50` | Max NEWLY ADDED out-of-scope text files before the scope-triage flood guard blocks. Edits to files that already exist on the target branch are not counted — a wide refactor is not an artifact flood (0 disables) |
| `core.limits.scope_triage_hard_max_files` | `MILL_SCOPE_TRIAGE_HARD_MAX_FILES` | `500` | Absolute cap on out-of-scope text files, counted regardless of whether they are newly added. Protects the scope-triage prompt from overflowing (0 disables) |
| `core.limits.refine_requests` | `MILL_REFINE_REQUEST_LIMIT` | `80` | Per-call request cap for the refine agent |
| `core.limits.refine_requests_simple` | `MILL_REFINE_REQUEST_LIMIT_SIMPLE` | `40` | Per-call request cap for simple/sonnet refine runs (lower because explore tools are gated off) |
| `core.limits.refine_max_tool_calls` | `MILL_REFINE_MAX_TOOL_CALLS` | `120` | (config-file-only) Hard cap on total tool calls per refine trace (runaway-loop backstop) |
| `core.limits.refine_max_errors` | `MILL_REFINE_MAX_ERRORS` | `20` | (config-file-only) Max tool-call errors per refine trace before auto-termination |
| `core.limits.refine_web_fetch_max_calls` | — | `5` | (config-file-only) Max real (cache-miss) `web_fetch` calls across one whole refine trace (cross-consult) |
| `core.limits.refine_web_fetch_max_total_bytes` | — | `500000` | (config-file-only) Cumulative fetch-bytes ceiling across one refine trace; `0` disables |
| `core.limits.refine_web_search_max_calls` | — | `5` | (config-file-only) Max `web_search` calls across one whole refine trace (cross-consult) |
| `core.limits.audit_requests` | `MILL_AUDIT_REQUEST_LIMIT` | `80` | Per-call request cap for the periodic audit agent |
| `core.limits.doc_requests` | `MILL_DOC_REQUEST_LIMIT` | `32` | Per-run request cap for the document agent |
| `core.limits.doc_classifier_requests` | `MILL_DOC_CLASSIFIER_REQUEST_LIMIT` | `3` | Per-call request cap for the doc-classifier gate |
| `core.limits.doc_classifier_diff_max_chars` | `MILL_DOC_CLASSIFIER_DIFF_MAX_CHARS` | `6000` | Caps the git diff (characters) fed to the doc-classifier gate; truncation is safe as the classifier is biased toward `user_facing=True`, and the full doc agent still receives the untruncated diff |
| `core.limits.triage_requests` | `MILL_TRIAGE_REQUEST_LIMIT` | `8` | Per-call cap for the pre-refine triage agent (main call + tool calls) |
| `core.limits.dedup_max_candidates` | `MILL_DEDUP_MAX_CANDIDATES` | `8` | Maximum candidates passed to the dedup LLM after similarity pre-filtering. Caps token budget regardless of repo size |
| `core.limits.coordinator_max_tool_calls` | `MILL_COORDINATOR_MAX_TOOL_CALLS` | `300` | Hard cap on total tool calls per implement (coordinator) trace — runaway-loop backstop above the request budget |
| `core.limits.coordinator_timeout_seconds` | `MILL_COORDINATOR_TIMEOUT_SECONDS` | `600` | Wall-clock timeout (seconds) for a single implement agent pass. When the agent exceeds this duration the pass is terminated and the stage can retry (with a fresh budget) or escalate. Default 600 s (10 min) caps worst-case stuck-loop burn. Minimum 60 s |
| `core.limits.implement_pass_timeout` | `MILL_IMPLEMENT_PASS_TIMEOUT` | `300` | Progress-reset watchdog (seconds) for the implement agent. The timer resets on every tool call — the agent is killed only after this many seconds of NO progress. `0` disables the watchdog and falls back to the flat `coordinator_timeout_seconds` cap |
| `core.limits.max_refine_explore_calls` | `MILL_MAX_REFINE_EXPLORE_CALLS` | `4` | Hard cap on explore/parallel_explore sub-agent calls per refine run. 0 disables exploration entirely |
| `core.limits.max_refine_read_file_calls` | `MILL_MAX_REFINE_READ_FILE_CALLS` | `10` | Hard cap on read_file calls per refine/triage agent run. 0 disables the cap (unbounded reads) |
| `core.limits.review_requests` | `MILL_REVIEW_REQUEST_LIMIT` | `80` | Per-run request cap for the review agent |

### 3. Worker pool & retry

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.limits.max_fix_iterations` | `MILL_MAX_FIX_ITERATIONS` | `8` | Max implement→test fix loop iterations before BLOCK |
| `core.limits.max_stuck_cycles` | `MILL_MAX_STUCK_CYCLES` | `3` | Re-entries to same stage without progress before BLOCK |
| `pipeline.implement_stall_threshold` | `MILL_IMPLEMENT_STALL_THRESHOLD` | `2` | Consecutive no-progress BLOCKED implement cycles before stall guard fires. `0` disables |
| `pipeline.implement_zero_diff_abort_threshold` | `MILL_IMPLEMENT_ZERO_DIFF_ABORT_THRESHOLD` | `2` | Consecutive zero-diff implement passes before pausing with an `ask_user` prompt instead of consuming spawn attempts. `0` disables |
| `core.limits.max_spend_usd_per_ticket` | `MILL_MAX_SPEND_USD_PER_TICKET` | `0.0` | Dollar cap per ticket. **Disabled by default** — a per-ticket budget is the wrong unit for guarding against a model that consumes erratically (that is a property of the model, not of whichever ticket was running). Watch cost fleet-wide instead; set non-zero to re-arm. |
| `core.limits.max_traces_per_ticket` | `MILL_MAX_TRACES_PER_TICKET` | `0` | Trace-count circuit-breaker. **Disabled by default** — a full pipeline pass with retries legitimately exceeds any small per-ticket trace count, so this fired on ordinary long work rather than on runaways (20 tickets blocked at $0.00 spend on 2026-08-06). |
| `core.limits.max_openrouter_marginal_usd_per_ticket` | `MILL_MAX_OPENROUTER_MARGINAL_USD_PER_TICKET` | `0.0` | OpenRouter marginal-spend breaker. **Disabled by default**, same reasoning as the two above. |
| `core.limits.stage_timeout_seconds` | `MILL_STAGE_TIMEOUT_SECONDS` | `2400` | Per-stage wall-clock timeout in seconds; stage that exceeds it is escalated to BLOCKED (≤ 0 disables) |
| `core.limits.stage_timeout_overrides` | `MILL_STAGE_TIMEOUT_OVERRIDES` | `{"refine": 900}` | Per-stage overrides as a JSON dict (e.g. `{"merge":0,"deliver":0}`); keys are stage names, values are seconds; 0 disables timeout for that stage. The built-in default caps the **refine** stage at 900 seconds — add `"refine": 0` to disable this cap, or override it with a different value. |
| `core.limits.max_global_concurrency` | `MILL_MAX_GLOBAL_CONCURRENCY` | `12` | Host-level cap on total concurrently-running stages across ALL boards, applied on top of each board's own `max_concurrency`. Default 12 provides a genuine backstop without throttling normal operation |
| `core.limits.classify_reserved_slots` | `MILL_CLASSIFY_RESERVED_SLOTS` | `1` | Slots within `max_global_concurrency` held back for the cheap classify stage so freshly-ingested tickets classify promptly even when every other slot is held by hour-scale implement/ci_fix runs. Comes OUT of the cap, not on top of it — the total ceiling is unchanged. Clamped to at most `max_global_concurrency - 1` so heavy stages always keep at least one slot. `0` disables the reservation. |
| `core.limits.stage_retry_max_attempts` | `MILL_STAGE_RETRY_MAX_ATTEMPTS` | `5` | Max automatic retries for transient stage-level failures (git outage, provider 5xx, connection refused) |
| `core.limits.stage_retry_base_delay` | `MILL_STAGE_RETRY_BASE_DELAY` | `2.0` | Base seconds for stage-level exponential backoff |
| `core.limits.stage_retry_max_delay` | `MILL_STAGE_RETRY_MAX_DELAY` | `60.0` | Max seconds between stage-level retries |
| `core.low_credit_threshold_usd` | — | `5.0` | OpenRouter credit balance below this value triggers the board warning banner |
| `core.low_credit_poll_enabled` | — | `true` | Enable the proactive OpenRouter credit-balance poll (hourly via `GET /api/v1/credits`) |
| `core.low_credit_poll_interval_seconds` | — | `3600` | Seconds between proactive credit-balance checks |
| `core.requeue_batch_size` | `MILL_REQUEUE_BATCH_SIZE` | `5` | Tickets enqueued per batch in the startup re-queue drip feed |
| `core.requeue_batch_pause_seconds` | `MILL_REQUEUE_BATCH_PAUSE_SECONDS` | `2.0` | Pause (seconds) between startup re-queue batches |
| `core.startup_jitter_seconds` | `MILL_STARTUP_JITTER_SECONDS` | `30` | Max random jitter (seconds) added to the per-repo periodic pass first-tick delay |
| `core.fan_out_stagger_seconds` | `MILL_FAN_OUT_STAGGER_SECONDS` | `300` | Max stagger window (seconds) for cross-repo fan-out ticket creation; 0 disables |
| `core.board_list_cache_ttl_seconds` | `MILL_BOARD_LIST_CACHE_TTL_SECONDS` | `3.0` | Short-TTL cache for board-poll GET /tickets endpoint (seconds). Repeated polls within this window return a cached snapshot to avoid stalling the event loop under load. 0.0 disables the cache. |
| `core.limits.network_probe_host` | `MILL_NETWORK_PROBE_HOST` | `"github.com"` | Host probed to detect global network outage; when unreachable, stage failures don't consume retry attempts |
| `core.limits.network_outage_retry_seconds` | `MILL_NETWORK_OUTAGE_RETRY_SECONDS` | `120` | Seconds between re-polls during a detected network outage |
| `core.limits.disk_min_free_mb` | `MILL_DISK_MIN_FREE_MB` | `5120` | Free-space floor on the data volume. Below it a stage is parked BEFORE dispatch rather than dying partway through and leaving a half-written workspace consuming the space it ran out of. 0 disables the check. |
| `core.limits.disk_full_retry_seconds` | `MILL_DISK_FULL_RETRY_SECONDS` | `600` | Seconds between re-polls while the data volume is full. Like the network-outage park, an ENOSPC failure re-polls without consuming a retry attempt. |
| `core.limits.model_outage_retry_seconds` | `MILL_MODEL_OUTAGE_RETRY_SECONDS` | `120` | Seconds between re-polls during a detected LLM model outage ("model unavailable" / overloaded / 503). Like the network/disk parks, a model outage re-polls without consuming a retry attempt. |
| `core.limits.model_outage_max_parks` | `MILL_MODEL_OUTAGE_MAX_PARKS` | `20` | Maximum consecutive model-outage parks before escalating to BLOCKED (guards against a permanently bad model id). |
| `core.limits.claude_usage_exhausted_retry_seconds` | `MILL_CLAUDE_USAGE_EXHAUSTED_RETRY_SECONDS` | `900` | Seconds to park a ticket after a Claude usage-exhaustion error whose message carries no reset time. When the message says `resets 9:20am (UTC)` the park runs straight through to that reset instead. |
| `core.limits.provider_failover_enabled` | `MILL_PROVIDER_FAILOVER_ENABLED` | `false` | Enable automatic provider failover: when the default (Anthropic) provider fails or is exhausted, rerun the SAME capability level on the paid OpenRouter fallback slot for llmio's failover window instead of parking the ticket until the quota resets. Off by default: the subscription quota comes back by itself, paid tokens do not. |
| `core.limits.run_command_refuse_full_suite` | `MILL_RUN_COMMAND_REFUSE_FULL_SUITE` | `true` | Refuse agent `run_command` pytest invocations that would run the whole suite (no path below the suite root, no `-k`/`-m`/`--lf`); the stage-owned gate runs the full suite once the agent stops. |
| `core.limits.refine_dynamic_limit_multiplier` | `MILL_REFINE_DYNAMIC_LIMIT_MULTIPLIER` | `1.5` | Dynamic request_limit multiplier applied when draft exceeds `refine_dynamic_limit_spec_chars` chars; must be > 1.0 |
| `core.limits.refine_dynamic_limit_min` | `MILL_REFINE_DYNAMIC_LIMIT_MIN` | `12` | Floor for dynamic request_limit (never lower than this even if base × multiplier is lower) |
| `core.limits.refine_dynamic_limit_spec_chars` | `MILL_REFINE_DYNAMIC_LIMIT_SPEC_CHARS` | `3000` | Draft character threshold above which the dynamic limit fires |
| `core.limits.refine_usage_warning_threshold` | `MILL_REFINE_USAGE_WARNING_THRESHOLD` | `0.8` | Log a warning when more than this fraction of request_limit is consumed during a refine pass |

### 4. Memory

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.memory.max_memory_chars` | `MILL_MAX_MEMORY_CHARS` | `8000` | Max characters loaded from any memory ledger per agent pass |
| `core.memory.retrospect_log_max_chars` | `MILL_RETROSPECT_LOG_MAX_CHARS` | `12000` | Max characters of the retrospect stage's history + comments logs (keeps most-recent, drops oldest; `0` disables) |
| `pipeline.implement_memory_path` | `MILL_IMPLEMENT_MEMORY_PATH` | `None` | Override path for implement memory; defaults to `<data_dir>/implement_memory.md` |
| `pipeline.refine_memory_path` | `MILL_REFINE_MEMORY_PATH` | `None` | Override path for refine memory; defaults to `<data_dir>/refine_memory.md` |
| `pipeline.ci_fix_memory_path` | `MILL_CI_FIX_MEMORY_PATH` | `None` | Override path for CI-fix memory; defaults to `<data_dir>/ci_fix_memory.md` |
| `pipeline.rebase_memory_path` | `MILL_REBASE_MEMORY_PATH` | `None` | Override path for rebase memory; defaults to `<data_dir>/rebase_memory.md` |
| `pipeline.review_revision_memory_path` | `MILL_REVIEW_REVISION_MEMORY_PATH` | `None` | Override path for review-revision memory; defaults to `<data_dir>/review_revision_memory.md` |
| `pipeline.ci_patterns_path` | `MILL_CI_PATTERNS_PATH` | `None` | Override path for the ci-fix agent's structured pattern memory; defaults to `<data_dir>/ci_patterns.json` |
| `pipeline.doc_memory_path` | `MILL_DOC_MEMORY_PATH` | `None` | Override path for the document agent's Markdown memory ledger; defaults to `<data_dir>/doc_memory.md` |
| `core.memory.retrospect_candidates_max_entries` | `MILL_RETROSPECT_CANDIDATES_MAX_ENTRIES` | `100` | Max entries retained in `AGENT_CANDIDATES.md`; pending entries are always kept; resolved entries are pruned oldest-first. `0` disables pruning |

### 5. Dedup

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `core.memory.dedup_lookback_days` | `MILL_DEDUP_LOOKBACK_DAYS` | `7` | Days back to consider closed tickets as dup candidates |
| `epic_dedup_lookback_days` | `MILL_EPIC_DEDUP_LOOKBACK_DAYS` | `7` | Recency window (days) for the epic-decomposition pre-filing dedup recent-ticket check (see [epic-dedup.md](../epic-dedup.md)) |
| `core.limits.dedup_skip_on_no_overlap` | `MILL_DEDUP_SKIP_ON_NO_OVERLAP` | `true` | Skip dedup LLM call when draft shares no token overlap with any candidate — saves cost in the "clearly unrelated" case |
| `core.limits.dedup_candidate_body_max_chars` | `MILL_DEDUP_CANDIDATE_BODY_MAX_CHARS` | `4000` | Cap each candidate body fed to dedup prompt; ≤0 disables truncation |

### 6. Service (management plane)

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `service.data_dir` | `MILL_DATA_DIR` | `.data` | Data directory for DB, workspaces, and memory ledgers |
| `service.default_repo_id` | `MILL_DEFAULT_REPO_ID` | `""` | Backward-compatibility fallback: board_id assigned to tickets created before the mandatory-board_id migration. Not a substitute for configuring repos.yaml. |
| `service.api_host` | `MILL_API_HOST` | `0.0.0.0` | FastAPI listen address |
| `service.api_port` | `MILL_API_PORT` | `8077` | FastAPI listen port |
| `service.api_url` | `MILL_API_URL` | `http://127.0.0.1:8077` | Base URL the CLI client uses to reach the API |
| `service.deploy_api_url` | `MILL_DEPLOY_API_URL` | `None` | Deploy server management API URL. When set, the implement stage checks worker image freshness before resuming blocked tickets. |
| `service.shutdown_grace_seconds` | `MILL_SHUTDOWN_GRACE_SECONDS` | `1800` | Maximum seconds to wait for in-flight periodic-agent passes to finish before tearing the worker down on container shutdown. 0 = wait forever. |
### 7. Approval & review

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `gates.require_approval` | `MILL_REQUIRE_APPROVAL` | `true` | Pause after refine for human approval (`human_issue_approval` state). A cheap conservative LLM auto-approval check runs before the gate: obviously-safe changes (cosmetic, doc-only, single-file, no logic changes) skip the human step automatically. |
| `gates.review_enabled` | `MILL_REVIEW_ENABLED` | `false` | Enable dual-model code review stage before deliver |
| `gates.review_max_rounds` | `MILL_REVIEW_MAX_ROUNDS` | `3` | Max CODE_REVIEW round-trips before escalate |
| `gates.max_implement_review_cycles` | `MILL_MAX_IMPLEMENT_REVIEW_CYCLES` | `10` | Backstop ceiling on total implement passes per ticket across all review rounds; `0` disables |
| `gates.refine_triage_enabled` | `MILL_REFINE_TRIAGE_ENABLED` | `true` | Cheap triage before full refine (skip if precise) |
| `gates.refine_advisory_dedup_enabled` | `MILL_REFINE_ADVISORY_DEDUP_ENABLED` | `true` | Cheap advisory-dedup-verification gate: resolves carried `Possible duplicate of <id>` advisory with a single cheapest-tier `run_dedup_check` |
| `gates.freshness_gate_enabled` | `MILL_FRESHNESS_GATE_ENABLED` | `false` | Pre-refine freshness check: verify cited evidence paths exist on HEAD |
| `gates.obsolescence_gate_enabled` | `MILL_OBSOLESCENCE_GATE_ENABLED` | `false` | Pre-refine obsolescence check: re-validate spawned-draft gaps (opt-in) |
| `gates.standards_gate_enabled` | `MILL_STANDARDS_GATE_ENABLED` | `true` | Pre-refine standards gate: discard agent-spawned drafts whose goal violates an explicit robotsix-standards prohibition. Only repos that follow the fleet standards are gated (auto-detected from the `robotsix-` repo-id prefix; override per repo with `follows_standards` in repos.yaml); user-authored drafts are never auto-closed |
| `gates.spec_review_enabled` | `MILL_SPEC_REVIEW_ENABLED` | `true` | Post-refinement spec narrative stripping |
| `gates.scope_triage_enabled` | `MILL_SCOPE_TRIAGE_ENABLED` | `true` | Cheap scope-violation triage before blocking (EXPAND/REJECT/ESCALATE) |
| `gates.prerequisite_gate_enabled` | `MILL_PREREQUISITE_GATE_ENABLED` | `true` | Pre-implement gate: when enabled, verify that external symbols/imports declared in the spec's `## Prerequisites` block are importable in the cloned repo before invoking the implement agent. When a declared prerequisite is unmet (e.g. an unmerged external port), the ticket is short-circuited to BLOCKED without the expensive coordinator LLM run. This is a no-op for specs without a `## Prerequisites` block and degrades gracefully on checker errors (always proceeds, never blocks on internal errors). |
| `gates.auto_merge_enabled` | `MILL_AUTO_MERGE_ENABLED` | `true` | Auto-merge PR when CI passes |
| `gates.auto_merge_kill_switch` | `MILL_AUTO_MERGE_KILL_SWITCH` | `false` | Global kill-switch for auto-merge: when True, disables auto-merge for ALL repos, regardless of per-repo settings |
| `gates.auto_merge_main_debt_detection_enabled` | `MILL_AUTO_MERGE_MAIN_DEBT_DETECTION_ENABLED` | `true` | When enabled, the single-repo auto-merge decision detects pre-existing main-branch CI debt: if every workflow failing on the PR head is ALSO failing on the merge target, the failure was not introduced by this PR and the ticket is routed to BLOCKED instead of cycling rebase/ci-fix retries. Safe-by-default — only fires when main is demonstrably red on the same workflow(s); the flag exists so an operator can disable it if needed. |
| `gates.review_feedback_enabled` | `MILL_REVIEW_FEEDBACK_ENABLED` | `false` | Enable autonomous review-revision agent (opt-in — implements changes requested by human reviewers) |
| `gates.pr_summary_enabled` | `MILL_PR_SUMMARY_ENABLED` | `false` | Generate structured PR body from diff via cheap LLM (opt-in) |
| `gates.comments_after_body` | `MILL_COMMENTS_AFTER_BODY` | `false` | Render description.md before comments in ticket detail drawer |
| `gates.reviewer_agreement_gate_enabled` | `MILL_REVIEWER_AGREEMENT_GATE_ENABLED` | `true` | Pre-Opus guard: when a reviewer's sendback feedback already agrees with the draft's no-change-needed conclusion, the pipeline short-circuits to DONE, skipping the expensive Opus refine agent. Requires `refine_triage_enabled=true`. |
| `ci.codeql_fp_triage_enabled` | `MILL_CODEQL_FP_TRIAGE_ENABLED` | `true` | When enabled, ci_fix may invoke a conservative sub-agent at the hard cycle ceiling to dismiss high-conviction CodeQL false positives, unblocking the ticket |

### 8. Forge

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `forge.kind` | `MILL_FORGE_KIND` | `none` | Forge platform: `github`, `gitlab`, `auto`, or `none`. `auto` detects the kind from the remote URL hostname (`github.com` → GitHub, `gitlab.com` → GitLab); custom domains raise an error and require an explicit setting. Legacy env var `FORGE_KIND` is still accepted. |
| `forge.remote_url` | `MILL_FORGE_REMOTE_URL` | `None` | Remote URL for clone + push. Legacy env var `FORGE_REMOTE_URL` is still accepted. |
| `forge.target_branch` | `MILL_FORGE_TARGET_BRANCH` | `main` | Target branch for PRs. Legacy env var `FORGE_TARGET_BRANCH` is still accepted. |
| `forge.auth_mode` | `MILL_FORGE_AUTH` | `token` | Auth mode: `token` (PAT) or `app` (GitHub App). Under `app` mode, `github_push_token()` mints a fresh `contents: write`-scoped installation token per push — no PAT is stored or reused for git push operations. Legacy env var `FORGE_AUTH` is still accepted. |
| `forge.github_api_url` | `MILL_GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL (override for GitHub Enterprise) |
| `forge.gitlab_api_url` | `MILL_GITLAB_API_URL` | `https://gitlab.com/api/v4` | GitLab API base URL (override for self-hosted GitLab) |
| `core.enable_repo_creation` | `MILL_ENABLE_REPO_CREATION` | `false` | Allow the new-repo meta flow to create repositories via the forge API |
| `core.repo_visibility_default` | `MILL_REPO_VISIBILITY_DEFAULT` | `public` | Default visibility for newly created repositories. `public` — repos are public unless the caller specifies private=True. `private` — repos are private unless the caller specifies private=False. |

### 9. Sandbox

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `sandbox.image` | `MILL_SANDBOX_IMAGE` | `python:3.14-slim` | Docker image for disposable sandbox containers. Code default is a lightweight `python:3.14-slim` image for local development; production JSON config overrides to `robotsix/mill-sandbox:latest`, which includes the `uv` binary and Python toolchain. Customize to a pre-built image that includes any additional tooling (e.g. formatters, linters) your test command needs. |
| `sandbox.memory` | `MILL_SANDBOX_MEMORY` | `2g` | Memory limit for sandbox containers |
| `sandbox.pids_limit` | `MILL_SANDBOX_PIDS_LIMIT` | `512` | PID limit for sandbox containers |
| `sandbox.cpus` | `MILL_SANDBOX_CPUS` | `0.0` | CPU quota per sandbox container, in cores (e.g. `0.7`). `0` disables the limit. Memory and PIDs were always capped but CPU was not, so `MILL_MAX_GLOBAL_CONCURRENCY` bounded the sandbox *count* while host load stayed unbounded. Set this when raising the concurrency cap past roughly half the host's cores. |
| `sandbox.readonly` | `MILL_SANDBOX_READONLY` | `true` | Mount sandbox rootfs read-only (except tmpfs `/tmp`) |
| `sandbox.tmpfs_size` | `MILL_SANDBOX_TMPFS_SIZE` | `512m` | Size limit for the sandbox's `/tmp` tmpfs. It is RAM charged to `sandbox.memory`; an unsized Docker tmpfs defaults to half the *host's* RAM, so bounding it turns an overflow into `ENOSPC` instead of an OOM kill. |
| `sandbox.package_cache` | `MILL_SANDBOX_PACKAGE_CACHE` | `true` | Mount a shared disk-backed `uv`/`pip` cache at `/sbxcache`, keeping package downloads out of the RAM-backed `/tmp`. Sandboxes share it, so a poisoned wheel written by one is visible to the next — set `false` to isolate. |
| `sandbox.package_cache_max_mb` | `MILL_SANDBOX_PACKAGE_CACHE_MAX_MB` | `4096` | Size budget (MiB) for the shared package cache. The sandbox-reaper pass drops the cache once it exceeds this; `0` disables pruning. |
| `sandbox.command_timeout` | `MILL_COMMAND_TIMEOUT` | `1800` | Wall-clock cap (seconds) for sandbox shell/test commands |
| `sandbox.op_timeout` | `MILL_SANDBOX_OP_TIMEOUT` | `300` | Per-docker-exec timeout (seconds) for individual sandbox operations. `0` disables. |
| `sandbox.slot_timeout` | `MILL_SANDBOX_SLOT_TIMEOUT` | `1800` | Seconds a caller waits for a free sandbox slot before failing. Live sandboxes are capped at `MILL_MAX_GLOBAL_CONCURRENCY`; this bounds the wait so a leaked slot surfaces as an error instead of hanging a worker. |
| `sandbox.data_volume` | `MILL_DATA_VOLUME` | `mill_data` | Named Docker volume for data (fallback when not bind-mounted) |
| `sandbox.data_mount` | `MILL_SANDBOX_DATA_MOUNT` | `None` | Host path for bind-mounted data directory (overrides `data_volume`) |
| `sandbox.network` | `MILL_SANDBOX_NETWORK` | `mill-sandbox-net` | Docker network sandbox containers connect to (internal, filtered through proxy) |
| `sandbox.proxy_url` | `MILL_SANDBOX_PROXY_URL` | `http://sandbox-proxy:8888` | Egress proxy URL (empty = no proxy, `--network none`) |
| `sandbox.test_command` | `MILL_TEST_COMMAND` | `""` | Command run to verify the implementation (empty = skip). Global fallback only: a managed repo's own `.robotsix-mill/config.yaml` `test_command` takes precedence, then `repos.yaml` per-repo `test_command`, then this value (precedence: per-repo file > repos.yaml > global). |

### 10. Web research

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `web.search_enabled` | `MILL_WEB_SEARCH` | `true` | Enable web-search capability (delegated to sub-agent) |
| `web.research_request_limit` | `MILL_WEB_RESEARCH_REQUEST_LIMIT` | `16` | Per-call request cap for web research (also reachable via `core.limits.web_research_requests`) |
| `web.research_fetch_max_calls` | `MILL_WEB_RESEARCH_FETCH_MAX_CALLS` | `4` | Maximum real (cache-miss) `web_fetch` calls per `web_research` sub-agent invocation |
| `web.fetch_image` | `MILL_FETCH_IMAGE` | `curlimages/curl:8.17.0` | Docker image for isolated `web_fetch` container |
| `web.fetch_max_bytes` | `MILL_WEB_FETCH_MAX_BYTES` | `2000000` | Max bytes fetched per URL |
| `web.fetch_timeout` | `MILL_WEB_FETCH_TIMEOUT` | `30` | Timeout (seconds) per web fetch |
| `web.fetch_max_calls` | — | `15` | (config-file-only) Max real (cache-miss) fetches per web-knowledge consult; cache hits and `web.fetch_raw` returns do NOT count |
| `web.fetch_max_total_bytes` | — | `2000000` | (config-file-only) Cumulative ceiling on returned (post-extraction, post-cap) text bytes per consult; `0` disables the byte ceiling |

### 10.1 Web knowledge agent

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| — | `MILL_WEB_KNOWLEDGE_MODEL` | `""` | Web-knowledge gateway sub-agent model — multi-turn flash agent that owns the per-library Markdown knowledge base and decides autonomously whether to answer from cache or web-search. Every agent's route to the internet flows through this gateway. When empty, resolves to the llmio tier-1 model at use time. |
| — | `MILL_WEB_KNOWLEDGE_STALE_DAYS` | `30` | Days before a cached web-knowledge .md file is considered stale. A consult that hits a stale file is allowed to web-search and update it. Users can tune this to match their tolerance for stale documentation. |
| `core.web_knowledge_cache_ttl_hours` | — | `72` | (config-file-only) Hours since the last `last_verified` touch before a cached knowledge file is flagged `[STALE]` in the agent's index. When flagged, the web_knowledge agent's system prompt warns it to cross-check claims with `web_search` before trusting cached data. |
| — | `MILL_WEB_KNOWLEDGE_REQUEST_LIMIT` | `16` | Per-consult request cap for the web-knowledge sub-agent. Each request is one Markdown read, one web-search, or one Markdown write. |

### 11. Pipeline tail (merge stage)

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `pipeline.merge_poll_seconds` | `MILL_MERGE_POLL_SECONDS` | `120` | Poll interval for PR merge/CI status |
| `pipeline.rebase_max_attempts` | `MILL_REBASE_MAX_ATTEMPTS` | `3` | Max rebase LLM invocations before BLOCK |
| `pipeline.parked_rebase_cooldown_hours` | `MILL_PARKED_REBASE_COOLDOWN_HOURS` | `4` | Hours to wait before re-rebasing a PR parked in `human_mr_approval`. Set to 0 to disable (always fall through, pre-existing behaviour). |
| `pipeline.autonomous_rebase_enabled` | `MILL_AUTONOMOUS_REBASE_ENABLED` | `true` | When true, the merge stage autonomously rebases parked PRs that become conflicting. |
| `pipeline.ci_fix_agent_timeout_seconds` | `MILL_CI_FIX_AGENT_TIMEOUT_SECONDS` | `1800` | Wall-clock timeout (seconds) for a single ci-fix agent pass. Wraps the LLM agent call inside the stage and fires **before** the worker's generic `stage_timeout_seconds` (default 2400 s). When the agent exceeds this budget the stage produces a diagnostic BLOCKED note naming which CI check(s) it was working on and the elapsed time, instead of a bare "timed out after 2400 s". Should be less than `stage_timeout_seconds` to leave headroom for clone, guard checks, and finalization. Set to `0` to disable (agent runs until the worker timeout or request limit). |
| `pipeline.ci_fix_max_iterations` | `MILL_CI_FIX_MAX_ITERATIONS` | `3` | Single-repo ci-fix: max `wait_for_ci` push-and-recheck iterations the agent may run before BLOCK. The agent owns its fix→push→verify loop; this is its iteration budget. Set to 0 to disable the verify loop. Multiplies into the ci_fix stage ceiling — see `Settings.stage_timeout_for`. |
| `pipeline.ci_fix_max_attempts` | `MILL_CI_FIX_MAX_ATTEMPTS` | `2` | Multi-repo merge ci-fix only: max CI-fix LLM invocations before BLOCK |
| `pipeline.ci_fix_max_cycles` | `MILL_CI_FIX_MAX_CYCLES` | `3` | Multi-repo merge ci-fix only: hard ceiling on total ci-fix cycles per repo (reset only when CI turns green). Set to 0 to disable. |
| `pipeline.ci_fix_max_identical_failures` | `MILL_CI_FIX_MAX_IDENTICAL_FAILURES` | `2` | Max consecutive identical CI failure cycles before escalating to BLOCKED. When the same failure fingerprint repeats this many times without progress, the stage short-circuits. Set to 0 to disable. |
| `pipeline.ci_fix_wait_poll_interval_s` | `MILL_CI_FIX_WAIT_POLL_INTERVAL_S` | `30.0` | How often `wait_for_ci` polls the forge for CI conclusion while a run is in progress |
| `pipeline.ci_fix_wait_timeout_s` | `MILL_CI_FIX_WAIT_TIMEOUT_S` | `900.0` | Max seconds a single `wait_for_ci` call blocks before returning a still-pending signal. A timeout here is not a failure — the agent may call the tool again — so this bounds one poll, not the whole wait. Multiplies into the ci_fix stage ceiling. |
| `pipeline.ci_fix_max_consecutive_pending` | `MILL_CI_FIX_MAX_CONSECUTIVE_PENDING` | `2` | Early bail-out threshold: when `wait_for_ci` returns `CI_STILL_PENDING` this many times in a row (each burning a full `ci_fix_wait_timeout_s` window), the next call returns `CI_STUCK` immediately without polling — the agent can then report FAILED rather than draining the remaining iteration budget on a CI run that is never going to report. Set to `0` to disable (never short-circuit on consecutive pending). |
| `pipeline.ci_transient_max_retries` | `MILL_CI_TRANSIENT_MAX_RETRIES` | `3` | Maximum automatic CI re-runs for transient/infrastructure failures (network flakes like ECONNRESET, buildkit boot timeouts, setup-uv fetch errors, runner shutdowns, HTTP 5xx) before escalating to a blocking `ci_fix_dependency` ticket. A transient-classified out-of-scope failure triggers workflow re-runs (via the forge's `rerun_workflow`) instead of immediately spawning a fix ticket; only when the failure persists across the retry budget does it escalate. Set to `0` to disable automatic transient re-runs entirely. |
| `pipeline.deliver_max_identical_blocks` | — | `2` | (config-file-only) Max consecutive identical merge-guard blocks before escalating the deliver stage's meta-triage-fallback guard to a stronger BLOCKED requiring human intervention. When the same brand-new top-level file fingerprint repeats this many times without progress (e.g. a deterministic resume→block loop), the stage escalates instead of burning cost. Set to 0 to disable. |
| `pipeline.ci_fix_request_limit` | `MILL_CI_FIX_REQUEST_LIMIT` | `120` | Per-run request budget for the ci-fix agent (must cover ALL fix→push→verify iterations). When exhausted, pydantic-ai raises `UsageLimitExceeded`, which the retry layer catches and triggers the fallback model (if configured). Set to 0 to disable. |
| `pipeline.ci_fix_log_context_max_chars` | `MILL_CI_FIX_LOG_CONTEXT_MAX_CHARS` | `16000` | Maximum characters of inline CI job-log context in the ci-fix failing summary — applied to both the initial prompt and each `wait_for_ci` iteration's fresh failure detail. The forge already windows each job log on the first failure marker at `ci_log_max_bytes`; this caps the TOTAL concatenated log by keeping each failing run's first-error window plus a head+tail slice of the whole concatenation, so a hard ticket's late fix→verify iterations stop re-sending unbounded log history while preserving earlier runs' first errors. The agent can still expand on demand via `fetch_ci_logs(full_log=True)`. Set to `0` to disable the cap. |
| `pipeline.ci_fix_iteration_summary_max_chars` | `MILL_CI_FIX_ITERATION_SUMMARY_MAX_CHARS` | `2000` | Maximum characters of the compact failure summary `wait_for_ci` returns on the 2nd and later iterations of one ci-fix run. The first iteration (and the initial dispatch prompt) keeps the full capped detail; later iterations get a bounded digest (failing check names + key error signatures + a short first-error window) so the pydantic-ai transcript stops growing with loop depth. The agent can still expand via `fetch_ci_logs`. Set to `0` to disable compacting (every iteration re-sends the full summary). |
| `pipeline.ci_fix_max_annotations` | `MILL_CI_FIX_MAX_ANNOTATIONS` | `40` | Maximum check annotations (path:line:message entries) rendered per ci-fix failing summary — bounds the non-log portion of the summary on annotation-heavy failures. Set to `0` to disable the cap. |
| `pipeline.ci_fix_max_alerts` | `MILL_CI_FIX_MAX_ALERTS` | `40` | Maximum code-scanning alert lines rendered per ci-fix failing summary — bounds the non-log portion of the summary on CodeQL-heavy failures. Set to `0` to disable the cap. |
| `pipeline.review_revision_max_attempts` | `MILL_REVIEW_REVISION_MAX_ATTEMPTS` | `2` | Max review-revision LLM invocations before BLOCK |
| `pipeline.branch_prefix` | `MILL_BRANCH_PREFIX` | `mill/` | Prefix for deliver-stage branch names |
| `pipeline.delete_branch_on_merge` | `MILL_DELETE_BRANCH_ON_MERGE` | `true` | Delete the per-ticket head branch on the forge after merge to DONE |
| `pipeline.prune_clone_on_close` | `MILL_PRUNE_CLONE_ON_CLOSE` | `true` | Delete workspace repo clone on ticket close |
| `pipeline.max_archived_tickets` | `MILL_MAX_ARCHIVED_TICKETS` | `40` | Max terminal-state tickets retained (0 = no purge) |
| `pipeline.max_events_per_ticket` | `MILL_MAX_EVENTS_PER_TICKET` | `200` | Max TicketEvent rows retained per non-terminal ticket; events beyond this cap are pruned (oldest first). 0 disables per-ticket event capping. |
| `pipeline.max_comments_per_ticket` | `MILL_MAX_COMMENTS_PER_TICKET` | `500` | Max Comment rows retained per non-terminal ticket; OPEN threads are never pruned. 0 disables comment capping. |
| `pipeline.auto_fix_max_cycles` | `MILL_AUTO_FIX_MAX_CYCLES` | `6` | Cross-stage ceiling on combined REBASING+FIXING_CI dispatches without CI turning green. Reset only when CI is observed green. Set to 0 to disable. |
| `pipeline.ping_pong_max_alternations` | `MILL_PING_PONG_MAX_ALTERNATIONS` | `3` | Ceiling on REBASING↔FIXING_CI alternations before escalating to BLOCKED. Reset when CI is observed green. Set to 0 to disable. |
| `pipeline.green_unpromotable_max_polls` | `MILL_GREEN_UNPROMOTABLE_MAX_POLLS` | `10` | Ceiling on consecutive merge polls where every reported check is green but the forge still refuses to promote the PR — permanent when branch protection requires a status context no workflow on the PR produces. Reset whenever a check is still pending. Set to 0 to disable. |
| `pipeline.empty_rollup_max_polls` | `MILL_EMPTY_ROLLUP_MAX_POLLS` | `3` | Ceiling on consecutive merge polls where CI reports success with zero check runs and `mergeable_state=blocked` — the signature of a PR whose `pull_request` event never fired. After this many polls the mill closes and reopens the PR once to trigger the event. Set to 0 to disable the self-heal. |
| `pipeline.merge_pr_missing_max_polls` | `MILL_MERGE_PR_MISSING_MAX_POLLS` | `20` | Ceiling on consecutive merge polls where the forge reports NO PR for the ticket's branch (single-repo) or for every repo in `pr_urls.json` (multi-repo). The PR may live in a repo other than this board's own repo (meta/cross-repo delivery), so a lookup that re-derives the repo from the board re-polls the same dead query forever — after this many polls the ticket is BLOCKED with a specific note. Reset whenever a PR is found. Set to 0 to disable. |
| — | `MILL_TICKET_STATE_CYCLE_LIMIT` | `3` | Ceiling on re-dispatches of the same LLM-bearing stage within a single pass before BLOCKED. Set to 0 to disable. |

### 11.2 Stages tuning

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `stages.review.prior_context_max_chars` | `MILL_REVIEW_PRIOR_CONTEXT_MAX_CHARS` | `8000` | Max characters of the re-review prior-context block (prior review comments + the implement rebuttal) fed to the review agent. Each component is tail-kept (most-recent content survives) so multi-round reviews don't re-pay for the entire accumulated history. Set to `0` to disable the cap. |
| `stages.review.diff_max_chars` | `MILL_REVIEW_DIFF_MAX_CHARS` | `200_000` | Max characters of the combined git diff injected into the review prompt. The raw `git diff origin/<target>...HEAD` can balloon to megabytes (divergent base, generated/lockfile churn, branch history) regardless of how few lines the intended change touches, overflowing even a 1M-token model context. When the diff exceeds this limit it is **middle-truncated** (head + tail kept, middle dropped, with a marker stating how many characters were omitted) so both early and late files get representation. ~200K chars ≈ 50K tokens, leaving room for spec + prior context + preseed + tools + the output reservation. Set to `0` to disable the cap (unbounded diffs). |
| `stages.review.output_token_budget` | `MILL_REVIEW_OUTPUT_TOKEN_BUDGET` | `65536` | Output token budget for the review agent retry when the primary attempt exhausts its `max_tokens` before generating a response (the reasoning model burns output tokens on internal reasoning). This is the **retry** budget; the primary attempt uses the agent definition's `max_tokens`. Set higher than the agent definition `max_tokens`. Set to `0` to disable the output-exhaustion retry (falls straight to `NEEDS_DISCUSSION`). |
| `stages.review.diff_context_lines` | `MILL_REVIEW_DIFF_CONTEXT_LINES` | `1` | Max surrounding context lines kept per hunk when the review diff exceeds `review_diff_max_chars`. Set to `0` to disable hunk thinning (keeps git's full 3-line context). |
| `stages.review.preseed_context_lines` | `MILL_REVIEW_PRESEED_CONTEXT_LINES` | `40` | Lines of file context around each changed region preloaded in the review preseed. Set to `0` to preload only the changed lines themselves. |
| `stages.scanner_rollup` | `MILL_SCANNER_ROLLUP` | `true` | When True (default), scanner periodic passes that produce multiple findings per run are rolled up into a single rollup ticket listing all findings, instead of filing one ticket per finding. Set to `false` to restore the legacy one-ticket-per-finding behaviour. |
| `stages.scanner_max_drafts_per_run` | `MILL_SCANNER_MAX_DRAFTS_PER_RUN` | `5` | Hard cap on the total number of drafts a single scanner pass may file. When `scanner_rollup` is `true` this is effectively 1 (the rollup itself). When rollup is disabled, this bounds the per-run ticket count. |
| `stages.retrospect_max_drafts_per_run` | `MILL_RETROSPECT_MAX_DRAFTS_PER_RUN` | `2` | Hard cap on the total number of drafts a single retrospect pass may file (systemic draft + concrete follow-up). Set lower when the board is overloaded. |
| `periodic.periodic_max_drafts_per_run` | `MILL_PERIODIC_MAX_DRAFTS_PER_RUN` | `3` | Default hard cap on drafts a single periodic pass run may file, applied to every pass that does not set its own (16 of the 17 built-in passes, plus all bespoke agents). Findings beyond the cap are dropped and resurface on the next run, so the pass keeps working — it just cannot flood the board in one go. Passes with their own cap (`scanner_max_drafts_per_run`, `retrospect_max_drafts_per_run`, `trace_review_max_drafts_per_run`, `dependabot_ingest_max_drafts_per_pass`) are unaffected. Set to `0` to stop uncapped passes filing drafts entirely; lower it when the board is backlogged. |
| `pipeline.implement_max_spawns_per_ticket` | `MILL_IMPLEMENT_MAX_SPAWNS_PER_TICKET` | `3` | Maximum times the implement stage may be entered per ticket. 0 disables. |
| `stages.implement.preseed_context_lines` | `MILL_IMPLEMENT_PRESEED_CONTEXT_LINES` | `40` | Lines of file context around each changed region preloaded in the implement retry preseed. On a retry pass the prior attempt's edits are already on disk, so reference_files are excerpt-preloaded around the changed lines plus this much surrounding context instead of re-sending whole files. Set to `0` to preload only the changed lines themselves. |
| `stages.implement.history_max_turns` | `MILL_IMPLEMENT_HISTORY_MAX_TURNS` | `8` | Last-N tool-call turns kept when compacting a resumed implement `message_history`; the older prefix is replaced by a rolling summary. Set to `0` to disable compaction (full history replayed). |
| `stages.implement.history_summary_max_chars` | `MILL_IMPLEMENT_HISTORY_SUMMARY_MAX_CHARS` | `3000` | Maximum characters of the rolling summary that replaces the dropped older turns in a compacted implement `message_history`. Set to `0` to drop the summary (kept turns only). |
| `gates.max_refine_passes_per_ticket` | `MILL_MAX_REFINE_PASSES_PER_TICKET` | `3` | Per-ticket ceiling on total refine passes before escalating to BLOCKED. 0 disables. |
| `core.lint_on_edit` | `MILL_LINT_ON_EDIT` | `true` | Pre-write Python syntax check on `write_file`/`edit_file`. When True, a SyntaxError aborts the edit before writing broken code. Configured via `core.lint_on_edit` in the JSON config file. |
| `core.read_file_max_chars` | `MILL_READ_FILE_MAX_CHARS` | `50000` | (config-file-only) Character cap on an *implicit full* `read_file` (`offset=1`, `limit=None`) payload returned to any `build_fs_tools` agent (implement, review, document). Over the cap the tool returns a head + tail slice plus an elision marker stating the file's total line count and steering the agent to re-read the omitted region with `offset`/`limit`; explicit ranged reads are **never** truncated. ~50K chars ≈ 12.5K tokens — above ordinary source modules (returned in full), so only large generated/lock/baseline files are trimmed before they bloat the re-billed prefix. Set to `0` to disable the cap. |

**Graceful token-exhaustion handling.** If a token-limit error is hit on
the first review pass, the review is retried once with no preseed and a
hard-truncated diff (~40K chars). If that retry also overflows, the
stage returns a `NEEDS_DISCUSSION` verdict with an explanatory comment
rather than crashing — a human can review the PR directly or split the
change into smaller diffs.

### 11.3 LLM context bounding

These keys control the amount of conversational context (diff excerpts, preseed excerpts, history turns, CI-failure details) fed to LLM agents per stage. Setting them lower saves context-window budget at the cost of agent awareness of prior work; raising them gives the agent more visibility but risks overflow on large diffs or long-running tickets.

| Key | Default | Stage | Description |
|-----|---------|-------|-------------|
| `review_diff_context_lines` | `1` | review | Max surrounding context lines kept per hunk when the review diff exceeds `review_diff_max_chars`. PR #2902. |
| `review_preseed_context_lines` | `40` | review | Lines of file context around each changed region preloaded in the review preseed. PR #2902. |
| `ci_fix_log_context_max_chars` | `16000` | ci_fix | Max characters of inline CI job-log context in the ci-fix failing summary (failing-log window). PR #2904. |
| `ci_fix_iteration_summary_max_chars` | `2000` | ci_fix | Max characters of the compact failure summary on 2nd+ iterations (prior-iteration summary bound). PR #2904. |
| `ci_fix_max_annotations` | `40` | ci_fix | Max check annotation lines rendered per ci-fix failing summary. PR #2904. |
| `ci_fix_max_alerts` | `40` | ci_fix | Max code-scanning alert lines rendered per ci-fix failing summary. PR #2904. |
| `implement_history_max_turns` | `8` | implement | Last-N tool-call turns kept when compacting a resumed implement `message_history`. PR #2906. |
| `implement_history_summary_max_chars` | `3000` | implement | Maximum characters of the rolling summary replacing dropped older turns in a compacted implement `message_history`. PR #2906. |
| `implement_preseed_context_lines` | `40` | implement | Lines of file context around each changed region preloaded in the implement retry preseed. PR #2906. |

### 11.4 Refine routing

These knobs control how the refine agent selects a model and when it
routes to cheaper tiers. All values are applied at the start of each
refinement pass.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `gates.refine_trivial_routing_enabled` | `MILL_REFINE_TRIVIAL_ROUTING_ENABLED` | `true` | Route trivial-scope tickets to a cheaper model instead of the full refinement model |
| `gates.refine_trivial_model_level` | `MILL_REFINE_TRIVIAL_MODEL_LEVEL` | `2` | Model level for trivial-scope refines (1 = haiku/flash cheap tier; 2 = opus/pro workhorse; 3 = fable frontier). |
| `gates.refine_trivial_subscription_model` | `MILL_REFINE_TRIVIAL_SUBSCRIPTION_MODEL` | `sonnet` | Claude alias for trivial/forced-cheap refines routed to the level-2 subscription workhorse |
| `gates.refine_subscription_tier_routing_enabled` | `MILL_REFINE_SUBSCRIPTION_TIER_ROUTING_ENABLED` | `true` | Complexity-gated Claude alias routing for level-3 refines (set `false` for Opus-always rollback) |
| `gates.refine_subscription_model_default` | `MILL_REFINE_SUBSCRIPTION_MODEL_DEFAULT` | `sonnet` | Claude alias for non-escalated (simple) level-3 refines |
| `gates.refine_subscription_model_complex` | `MILL_REFINE_SUBSCRIPTION_MODEL_COMPLEX` | `opus` | Claude alias for escalated (needs-exploration) level-3 refines |
| `gates.refine_findings_downgrade_enabled` | `MILL_REFINE_FINDINGS_DOWNGRADE_ENABLED` | `true` | Downgrade Opus → cheaper Claude alias when triage findings are substantial (root cause already known) |
| `gates.refine_findings_downgrade_min_chars` | `MILL_REFINE_FINDINGS_DOWNGRADE_MIN_CHARS` | `150` | Minimum stripped-character length of triage findings for the Opus downgrade to fire |
| `gates.refine_subscription_model_findings` | `MILL_REFINE_SUBSCRIPTION_MODEL_FINDINGS` | `sonnet` | Claude alias used when the findings-present downgrade fires |
| `gates.max_re_refine_cycles_before_cheap` | `MILL_MAX_RE_REFINE_CYCLES_BEFORE_CHEAP` | `2` | Force cheap model after this many "changes requested" sendbacks; `0` disables |
| `gates.delta_context_retry_enabled` | `MILL_DELTA_CONTEXT_RETRY_ENABLED` | `true` | When true, retry/audit/re-refine passes receive only the delta rather than full context |
| — | `MILL_REFINE_DELTA_REUSE_ENABLED` | `true` | When re-entering refine after an operator sendback, reuse the prior refined description.md as the starting point instead of refining from scratch |

### 12. Periodic agents

Each periodic agent shares this pattern:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.<name>.interval_seconds` | `MILL_<NAME>_INTERVAL_SECONDS` | `1209600`¹ | Seconds between automatic passes (`0` = disabled) |

Periodic agents: `audit`, `trace_health`, `trace_review`, `health`, `test_gap`,
`agent_check`, `survey`, `ci_debt_recheck`, `ci_monitor`, `config_sync`, `member_sync`, `meta`, `bc_check`,
`ci_prevention_rules`, `completeness_check`, `diagnostic`, `docstring_coverage`, `forge_parity`, `frontend_sync`, `module_curator`, `module_size`, `mypy_baseline`, `orphaned_pr_check`, `pin_bump`,
`copy_paste`, `timeout_escalation`, `triage_boilerplate`, `langfuse_cleanup`, `token_metrics_aggregation`, `data_dir_gc`, `dependabot_ingest`, `run_health`, `stale_branch_cleanup`,
`db_maintenance`, `roadmap_sync`, `sandbox_reaper`, `repo_description_sync`.

> ¹ The **primary** interval now lives in each agent's base YAML definition
> (``agent_definitions/periodic/<name>.yaml``), defaulting to **14 days**
> (``interval: 14d``, i.e. 1,209,600 seconds) for all built-in periodic
> agents.  Per-repo overlays (``.robotsix-mill/periodic/<name>.yaml``)
> can override it per repository.  The ``*_interval_seconds`` Settings
> keys below are a **fallback** — the scheduler only reads them when
> neither the base YAML nor an overlay sets an interval.  The code
> defaults for the four agents targeted by this cadence change
> (audit, trace_review, survey, completeness_check) are also set to
> 1209600 in ``_settings_periodic.py``; the remaining periodic agents'
> code defaults remain at 604800 (7 d) — their YAML definitions
> specifying ``interval: 14d`` take precedence over the code fallback.
>
> `trace_health`, `ci_monitor`, `member_sync`, and `diagnostic` write no
> per-agent memory ledger (`member_sync` and `diagnostic` are deterministic
> passes with no LLM agent).
>
> Periodic agents' memory ledger paths are **fixed and not overridable**:
> in multi-repo mode each repo gets its own isolated ledger at
> `<data_dir>/<repo_id>/<agent>_memory.md`; when no repos are registered
> (single-repo or `--repo-id` mode), the path falls back to
> `<data_dir>/<agent>_memory.md`.  The only exception is `run_health`,
> which keeps a `periodic.run_health.memory_path` override (see below).

Additional fields:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.bespoke_discovery_interval_seconds` | `MILL_BESPOKE_DISCOVERY_INTERVAL_SECONDS` | `600` | Seconds between bespoke supervisor clone-refresh and agent-reconciliation cycles. A new YAML committed to a managed repo's `.robotsix-mill/agents/` lands within this window. Set to `0` to disable. |
| `periodic.config_pin_drift_interval_seconds` | `MILL_CONFIG_PIN_DRIFT_INTERVAL_SECONDS` | `86400` | Seconds between config pin-drift passes. Set to `0` to disable. |
| `periodic.ci_auto_close_interval_seconds` | `MILL_CI_AUTO_CLOSE_INTERVAL_SECONDS` | `900` | Seconds between CI-failure auto-close passes: open `source=ci` tickets whose workflow has since turned green on the target branch are force-closed to DONE automatically (no human/monitor needed). Set to `0` to disable. |
| `periodic.upstream_ci_recovery_interval_seconds` | `MILL_UPSTREAM_CI_RECOVERY_INTERVAL_SECONDS` | `600` | Seconds between upstream-CI recovery passes: tickets that ci_fix parked BLOCKED because the target branch shared their failing checks are resumed automatically once that branch is green. Set to `0` to disable. |
| `periodic.blocked_auto_resume_interval_seconds` | `MILL_BLOCKED_AUTO_RESUME_INTERVAL_SECONDS` | `600` | Seconds between blocked auto-resume passes: BLOCKED tickets whose latest block note matches a resumable pattern are resumed automatically, bounded per ticket. 0 = disabled. |
| `periodic.blocked_auto_resume_cooldown_seconds` | `MILL_BLOCKED_AUTO_RESUME_COOLDOWN_SECONDS` | `1800` | Minimum seconds a ticket must have been BLOCKED before it is auto-resumed. |
| `periodic.blocked_auto_resume_max_per_ticket` | `MILL_BLOCKED_AUTO_RESUME_MAX_PER_TICKET` | `1` | Maximum automatic resumes per ticket (counted from its `[auto-resume` comments); after that a human is needed. |
| `periodic.blocked_auto_resume_patterns` | `MILL_BLOCKED_AUTO_RESUME_PATTERNS` | `["agent error \u2014 resumable", "\u2014 resumable", "timed out", "stage timeout", "clone (is )?missing", "pr_urls\\.json corrupted", "unknown repo_id", "could not turn CI green within its iteration budget", "tests still failing after", "review rounds exhausted", "Infrastructure: LLM model outage", "scope-triage agent error"]` | Case-insensitive regexes matched against the latest BLOCKED note; a match makes the block auto-resumable. Spec-fingerprint (`spec unchanged`) and upstream-CI parks are always excluded. |
| `periodic.config_pin_drift_baseline` | `MILL_CONFIG_PIN_DRIFT_BASELINE` | `[]` | Settings keys whose pinned value deliberately differs from the code default; excluded from pin-drift reporting (ratchet baseline, same idea as the mypy baseline). |
| `periodic.ci_prevention_rules.max_events` | `MILL_CI_PREVENTION_RULES_MAX_EVENTS` | `100` | Most recent `CI_FAILURE` events (per board) the `ci_prevention_rules` pass reads when deriving prevention rules for the implement memory ledger. |
| `periodic.ci_prevention_rules.max_rules` | `MILL_CI_PREVENTION_MAX_RULES` | `10` | Maximum prevention rules the `ci_prevention_rules` pass writes into the `## CI prevention rules (auto-maintained)` section of the implement memory ledger. |
| `periodic.ci_monitor.log_max_bytes` | `MILL_CI_LOG_MAX_BYTES` | `65536` | Max bytes fetched per CI job log |
| `periodic.diagnostic.target_repo_id` | `MILL_DIAGNOSTIC_TARGET_REPO_ID` | `robotsix-mill` | Board the diagnostic agent routes activity to; single-repo fallback when the monitored list is empty |
| `periodic.diagnostic.monitored_repo_ids` | `MILL_DIAGNOSTIC_MONITORED_REPO_IDS` | `[]` | Repos the diagnostic agent monitors each pass (JSON list); empty → falls back to `target_repo_id`. Add/remove repos here — no code change. See [diagnostic-agent.md](../agents/diagnostic-agent.md) |
| `periodic.langfuse_cleanup.max_traces` | `MILL_LANGFUSE_CLEANUP_MAX_TRACES` | `5000` | Max traces retained in the shared workspace Langfuse project when `langfuse_cleanup_periodic` is enabled; oldest traces are deleted to stay under this cap. Centralized (global-only) — one pass per interval, not per-repo. |
| `periodic.token_metrics_aggregation.interval_seconds` | `MILL_TOKEN_METRICS_AGGREGATION_INTERVAL_SECONDS` | `86400` | Seconds between token-metrics aggregation passes. Centralized (global-only) — one pass per interval, not per-repo. Set to `0` to disable. |
| `periodic.token_metrics_aggregation.window_seconds` | `MILL_TOKEN_METRICS_AGGREGATION_WINDOW_SECONDS` | `86400` | Lookback window of Langfuse traces aggregated each token-metrics pass; per-call stage×model input/output-token percentiles (p50/p95/max) are written to `<data_dir>/token_metrics/<YYYY-MM-DD>.json`. |
| `pipeline.retrospect_spawn_drafts` | `MILL_RETROSPECT_SPAWN_DRAFTS` | `true` | Allow retrospect to file improvement draft tickets |
| `pipeline.retrospect_skip_uneventful` | `MILL_RETROSPECT_SKIP_UNEVENTFUL` | `true` | Skip the retrospect LLM pass for tickets that went through the pipeline first time with no review round, a single implement pass, no block/CI-fix/rebase history and no comments; they close directly. |
| `pipeline.retrospect_spawn_agented_proposals` | `MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS` | `true` | When True, retrospect files a draft ticket per AGENT.md proposal on the originating repo's board. |
| `pipeline.retrospect_memory_path` | `MILL_RETROSPECT_MEMORY_PATH` | `None` | Override path for retrospect memory |

#### trace_review

The trace-review periodic agent inspects Langfuse traces for anomalies
(cost spikes, tool-call errors, repeated-tool storms, explore loops,
ask_user stalls) and files draft tickets with proposed fixes. Every
field below is settable via its `MILL_TRACE_REVIEW_*` environment
variable and its dotted YAML path.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.trace_review.interval_seconds` | `MILL_TRACE_REVIEW_INTERVAL_SECONDS` | `1209600` | Seconds between trace-review passes (minimum 3600). Set to `0` to disable. |
| `periodic.trace_review.cost_multiplier` | `MILL_TRACE_REVIEW_COST_MULTIPLIER` | `3.0` | Outlier threshold: cost > batch median × N → flagged |
| `periodic.trace_review.per_obs_cost_threshold` | `MILL_TRACE_REVIEW_PER_OBS_COST_THRESHOLD` | `0.001` | Per-observation cost threshold for flagging |
| `periodic.trace_review.obs_multiplier` | `MILL_TRACE_REVIEW_OBS_MULTIPLIER` | `3.0` | Outlier threshold: observation count > batch median × N → flagged |
| `periodic.trace_review.max_repeated_tool` | `MILL_TRACE_REVIEW_MAX_REPEATED_TOOL` | `50` | Absolute cap on repeated tool calls before flagging |
| `periodic.trace_review.max_tool_calls` | `MILL_TRACE_REVIEW_MAX_TOOL_CALLS` | `100` | Hard cap on total tool calls per trace inspection |
| `periodic.trace_review.max_errors` | `MILL_TRACE_REVIEW_MAX_ERRORS` | `20` | Hard cap on tool-call errors before auto-termination |
| `periodic.trace_review.model_level` | `MILL_TRACE_REVIEW_MODEL_LEVEL` | `1` | Model level for the trace inspector (1–3; 1 = haiku on the subscription) |
| `periodic.trace_review.inspector_min_requests` | `MILL_TRACE_REVIEW_INSPECTOR_MIN_REQUESTS` | `20` | Floor for the tools-on request budget |
| `periodic.trace_review.inspector_max_requests` | `MILL_TRACE_REVIEW_INSPECTOR_MAX_REQUESTS` | `80` | Ceiling for the tools-on request budget |
| `periodic.trace_review.inspector_requests_per_obs` | `MILL_TRACE_REVIEW_INSPECTOR_REQUESTS_PER_OBS` | `0.1` | Requests granted per observation before clamping |
| `periodic.trace_review.inspector_max_obs_for_tools` | `MILL_TRACE_REVIEW_INSPECTOR_MAX_OBS_FOR_TOOLS` | `200` | Observation count above which code-access tools are dropped |
| `periodic.trace_review.inspector_toolless_requests` | `MILL_TRACE_REVIEW_INSPECTOR_TOOLLESS_REQUESTS` | `3` | Request budget for the tool-less summary-only path |
| `periodic.trace_review.tool_request_limit` | `MILL_TRACE_REVIEW_TOOL_REQUEST_LIMIT` | `15` | Request budget for the interactive `langfuse_inspect_trace` tool |
| `periodic.trace_review.max_drafts_per_run` | `MILL_TRACE_REVIEW_MAX_DRAFTS_PER_RUN` | `5` | Cap on drafted findings per trace-review pass |
| `periodic.trace_review.min_confidence` | `MILL_TRACE_REVIEW_MIN_CONFIDENCE` | `"medium"` | Minimum inspector confidence for a finding to be filed as a draft |
| `periodic.trace_review.max_inspections_per_run` | `MILL_TRACE_REVIEW_MAX_INSPECTIONS_PER_RUN` | `5` | Hard cap on LLM inspector calls per trace-review run |
| `periodic.trace_review.max_traces_per_run` | `MILL_TRACE_REVIEW_MAX_TRACES_PER_RUN` | `300` | Hard cap on traces pulled for full detail per run |
| `periodic.trace_review.initial_lookback_hours` | `MILL_TRACE_REVIEW_INITIAL_LOOKBACK_HOURS` | `24` | First-run lookback window when no watermark exists (hours) |
| `periodic.trace_review.restart_correlation_window_seconds` | `MILL_TRACE_REVIEW_RESTART_CORRELATION_WINDOW_SECONDS` | `60` | Window for correlating incomplete traces with process restarts (seconds) |
| `periodic.trace_review.dedup_lookback_days` | `MILL_TRACE_REVIEW_DEDUP_LOOKBACK_DAYS` | `7` | Recency window (days) for pre-filing duplicate check |
| `pipeline.trace_review_target_repo_id` | `MILL_TRACE_REVIEW_TARGET_REPO_ID` | `""` | Target repo for trace-review drafts; empty → source-repo routing |

#### data_dir_gc

The `data_dir_gc` periodic agent reclaims disk space through deterministic GC steps (terminal clone pruning, parked-ticket venv pruning, orphan workspace pruning, closed workspace pruning, DB row purging, and memory ledger truncation). No size scanning or ticket filing — those concerns live in robotsix-central-deploy.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.data_dir_gc.interval_seconds` | `MILL_DATA_DIR_GC_INTERVAL_SECONDS` | `86400` | Seconds between periodic data-dir GC passes. Minimum enforced at 60 s in the worker loop. Set to `0` to disable. |
| `periodic.data_dir_gc.prune_closed` | `MILL_DATA_DIR_GC_PRUNE_CLOSED` | `false` | Opt-in GC: prune workspace directories of terminal-state tickets during the data-dir GC pass. Default `false`. |
| `periodic.data_dir_gc.prune_closed_age_seconds` | `MILL_DATA_DIR_GC_PRUNE_CLOSED_AGE_SECONDS` | `604800` | Minimum age (seconds since the ticket entered its terminal state) before its workspace becomes eligible for prune_closed GC. Recent closures are kept for post-mortems. Default 7 days. |
| `periodic.data_dir_gc.prune_terminal_clones` | `MILL_DATA_DIR_GC_PRUNE_TERMINAL_CLONES` | `true` | Default-on GC: prune the reproducible git clones (`repo/` and `repos/`) inside workspaces of terminal-state tickets at the start of each data-dir GC pass. |
| `periodic.data_dir_gc.prune_terminal_clones_age_seconds` | `MILL_DATA_DIR_GC_PRUNE_TERMINAL_CLONES_AGE_SECONDS` | `86400` | Minimum age (seconds since the ticket entered its terminal state) before its clones are pruned. Clones are cheap to recreate, so the guard is short. Default 1 day. |
| `periodic.data_dir_gc.prune_parked_venvs` | `MILL_DATA_DIR_GC_PRUNE_PARKED_VENVS` | `true` | Default-on GC: prune the `.venv` inside workspaces of PARKED tickets (`BLOCKED`, `HUMAN_MR_APPROVAL`, `HUMAN_ISSUE_APPROVAL`). These are not terminal, so no other GC step can reach them, and a parked ticket can sit for weeks. Only `.venv` is removed — the clone, its git history and any uncommitted work stay inspectable for the human the ticket is parked for. |
| `periodic.data_dir_gc.prune_parked_venvs_age_seconds` | `MILL_DATA_DIR_GC_PRUNE_PARKED_VENVS_AGE_SECONDS` | `3600` | Minimum age (seconds since the ticket entered its parked state) before its `.venv` is pruned. Short, because the venv is pure cache and a ticket resumed within the hour still re-syncs cheaply from the shared package cache. Default 1 hour. |
| `periodic.data_dir_gc.prune_db_rows` | `MILL_DATA_DIR_GC_PRUNE_DB_ROWS` | `true` | Default-on DB row GC: purge oldest terminal-ticket rows (and their associated events, comments, and proposed actions) when the count of terminal tickets exceeds `max_archived_tickets`. |
| `periodic.data_dir_gc.prune_memory_ledgers` | `MILL_DATA_DIR_GC_PRUNE_MEMORY_LEDGERS` | `true` | Default-on GC: truncate over-cap `*_memory.md` files on disk, using the same tail_keep primitive the agent already uses at read/write time. |
| `periodic.data_dir_gc.prune_orphans` | `MILL_DATA_DIR_GC_PRUNE_ORPHANS` | `true` | Default-on GC: prune orphan workspace directories (ticket absent from the board DB) older than the configured age at the start of each data-dir GC pass. |
| `periodic.data_dir_gc.prune_orphans_age_seconds` | `MILL_DATA_DIR_GC_PRUNE_ORPHANS_AGE_SECONDS` | `86400` | Minimum age (seconds since the ticket-ID timestamp) before an orphan workspace becomes eligible for GC. Default 1 day. |

User agent-config YAMLs that set `periodic.data_dir_audit.*` must be updated to `periodic.data_dir_gc.*`; unknown keys are silently ignored by the loader.

#### run_health

The run-health periodic agent reads every board's run registry over a
lookback window, flags failed/degraded runs deterministically, runs one
LLM pass to separate real failures from legitimate empties, and files
high-confidence draft tickets to the mill board. Every field below is
settable via its `MILL_RUN_HEALTH_*` environment variable and its dotted
YAML path.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.run_health.interval_seconds` | `MILL_RUN_HEALTH_INTERVAL_SECONDS` | `604800` | Seconds between run-health passes. Set to `0` to disable. |
| `periodic.run_health.window_hours` | `MILL_RUN_HEALTH_WINDOW_HOURS` | `168` | Lookback window (hours) over which run registries are scanned |
| `periodic.run_health.target_repo_id` | `MILL_RUN_HEALTH_TARGET_REPO_ID` | `robotsix-mill` | Board the run-health agent files its drafts to |
| `periodic.run_health.memory_path` | `MILL_RUN_HEALTH_MEMORY_PATH` | `None` | Override path for the run-health memory ledger; defaults to `<data_dir>/<board>/run_health_memory.md` |

#### db_maintenance

The `db_maintenance` periodic agent runs SQLite maintenance (`VACUUM`,
`ANALYZE`, WAL checkpoint) to keep the ticket database healthy.  It has
**no YAML path** — configure it via environment variables only:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_DB_MAINTENANCE_INTERVAL_SECONDS` | `86400` | Seconds between database maintenance passes. Set to `0` to disable. |

#### docstring_coverage

The `docstring_coverage` periodic agent scans the repository for public-API
documentation gaps (missing or incomplete docstrings) and files draft tickets
when coverage is insufficient. Five extra fields beyond the generic periodic
pattern control its request budget and tool-call guardrails:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_DOCSTRING_COVERAGE_INTERVAL_SECONDS` | `604800` | Seconds between docstring-coverage passes. Set to `0` to disable. |
| `MILL_DOCSTRING_COVERAGE_REQUEST_LIMIT` | `80` | Per-call request cap for the docstring-coverage agent |
| `MILL_DOCSTRING_COVERAGE_MAX_TOOL_CALLS` | `100` | Hard cap on total tool calls per docstring-coverage trace |
| `MILL_DOCSTRING_COVERAGE_MAX_ERRORS` | `20` | Hard cap on tool-call errors before auto-termination |

#### meta

The `meta` periodic agent is a cross-repo survey agent that clones all
registered repositories, compares their codebases, and files extraction
and alignment proposals.  It runs as a single global pass per interval
(not per-repo).  In addition to the standard `periodic` fields above,
these agent-specific settings are available:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_META_INTERVAL_SECONDS` | `604800` | Seconds between automatic meta-agent passes. Minimum enforced at 60 s in the worker loop. Set to `0` to disable. |

#### mypy_baseline

The `mypy_baseline` periodic agent manages the mypy type-check
baseline, tracking and ratcheting type errors over time.  It uses
only the standard two periodic-agent fields:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_MYPY_BASELINE_INTERVAL_SECONDS` | `604800` | Seconds between mypy-baseline passes. Set to `0` to disable. |

#### module_size

The `module_size` periodic agent scans the repository for oversized files
(modules exceeding a reasonable line-count threshold) and files draft tickets
when files warrant splitting. Five extra fields beyond the generic periodic
pattern control its request budget and tool-call guardrails:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_MODULE_SIZE_INTERVAL_SECONDS` | `604800` | Seconds between module-size passes. Set to `0` to disable. |
| `MILL_MODULE_SIZE_REQUEST_LIMIT` | `60` | Per-call request cap for the module-size agent |
| `MILL_MODULE_SIZE_MAX_TOOL_CALLS` | `80` | Hard cap on total tool calls per module-size trace |
| `MILL_MODULE_SIZE_MAX_ERRORS` | `20` | Hard cap on tool-call errors before auto-termination |

#### sandbox_reaper

The `sandbox_reaper` periodic agent prunes stopped Docker sandbox
containers left behind by the implement stage.  YAML paths
(`periodic.sandbox_reaper.enabled`, `periodic.sandbox_reaper.interval_seconds`)
and environment variables are both available:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_SANDBOX_REAPER_INTERVAL_SECONDS` | `3600` | Seconds between sandbox-reaper passes. Set to `0` to disable. |

#### survey

The `survey` periodic agent searches for library/ecosystem news and files
draft tickets with findings. Four extra fields beyond the generic periodic
pattern control its tool-call and web-fetch budgets:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_SURVEY_INTERVAL_SECONDS` | `1209600` | Seconds between survey passes. Set to `0` to disable. |
| `MILL_SURVEY_REQUEST_LIMIT` | `40` | Per-call request cap for the survey agent |
| `MILL_SURVEY_WEB_FETCH_MAX_CALLS` | `5` | Max real (cache-miss) web_fetch calls per survey run |
| `MILL_SURVEY_WEB_FETCH_MAX_TOTAL_BYTES` | `500000` | Cumulative ceiling on returned fetch bytes per survey run |
| `MILL_SURVEY_WEB_SEARCH_MAX_CALLS` | `5` | Max web_search invocations per survey run |

#### audit

The `audit` periodic agent performs broad repository audits (license
scanning, pip-audit, coverage introspection) and files draft tickets
with findings. One extra field beyond the generic periodic pattern
controls its request budget:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_AUDIT_INTERVAL_SECONDS` | `1209600` | Seconds between audit passes. Set to `0` to disable. |
| `MILL_AUDIT_REQUEST_LIMIT` | `80` | Per-call request cap for the audit agent |

#### test_gap

The `test_gap` periodic agent scans the repository for test-coverage
gaps and files draft tickets when coverage is insufficient. Three
extra fields beyond the generic periodic pattern control its request
budget and tool-call guardrails:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_TEST_GAP_INTERVAL_SECONDS` | `604800` | Seconds between test-gap passes. Set to `0` to disable. |
| `MILL_TEST_GAP_REQUEST_LIMIT` | `80` | Per-call request cap for the test-gap agent |
| `MILL_TEST_GAP_MAX_TOOL_CALLS` | `100` | Hard cap on total tool calls per test-gap trace |
| `MILL_TEST_GAP_MAX_ERRORS` | `20` | Hard cap on tool-call errors before auto-termination |

#### triage_boilerplate

The `triage_boilerplate` periodic agent determines boilerplate for new-ticket
triage, scanning the board for recurring triage patterns.  It uses only the
standard two periodic-agent fields:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.triage_boilerplate.interval_seconds` | `MILL_TRIAGE_BOILERPLATE_INTERVAL_SECONDS` | `604800` | Seconds between triage-boilerplate passes (1 week). Set to `0` to disable. |

#### orphaned_pr_check

The `orphaned_pr_check` periodic agent scans tickets for stale PRs whose
branch has been deleted or whose associated ticket has been resolved, and
either auto-closes the PR or files a tracking ticket. It defaults to `false`
(opt-in) because closing PRs and filing tickets are destructive actions.
It is a deterministic pass with no LLM agent and writes no memory ledger.
Configure via environment variables or YAML paths under
`periodic.orphaned_pr_check.<field>`:

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `periodic.orphaned_pr_check.interval_seconds` | `MILL_ORPHANED_PR_CHECK_INTERVAL_SECONDS` | `86400` | Seconds between orphaned-PR check passes. Minimum enforced at 3600 s (1 hour) in the worker loop. Set to `0` to disable. |
| `periodic.orphaned_pr_check.min_age_hours` | `MILL_ORPHANED_PR_MIN_AGE_HOURS` | `4` | Minimum age (hours) of a ticket before its PR is considered for orphan classification. Skips tickets younger than this to avoid racing the deliver stage. |
| `periodic.orphaned_pr_check.max_actions_per_pass` | `MILL_ORPHANED_PR_MAX_ACTIONS_PER_PASS` | `5` | Maximum number of combined close+file actions per pass run. Findings beyond this cap are deferred to the next scheduled pass. |
| `periodic.orphaned_pr_check.dry_run` | `MILL_ORPHANED_PR_DRY_RUN` | `true` | Dry-run mode: log intent only, make zero forge mutations. Default `true` for safety — flip to `false` to enable real actions. |
| `periodic.orphaned_pr_check.bot_logins` | `MILL_ORPHANED_PR_BOT_LOGINS` | `[]` | Bot author logins trusted for orphaned-PR actions. When non-empty, only PRs whose author is in this list are eligible. When empty, the runner resolves the bot login from the forge and uses that; if that also returns empty, the author guard is bypassed (fail-open). |
| `periodic.orphaned_pr_check.max_closes_per_pass` | `MILL_ORPHANED_PR_MAX_CLOSES_PER_PASS` | `10` | Per-pass cap on PR close actions. Applied in addition to the combined `max_actions_per_pass` cap. |
| `periodic.orphaned_pr_check.max_files_per_pass` | `MILL_ORPHANED_PR_MAX_FILES_PER_PASS` | `5` | Per-pass cap on tracking-ticket file actions. Applied in addition to the combined `max_actions_per_pass` cap. |
| `periodic.orphaned_pr_check.track_foreign_prs` | `MILL_ORPHANED_PR_TRACK_FOREIGN_PRS` | `false` | Also file tracking tickets for non-mill PRs (dependabot/human); never closes them |

#### Env-var-only periodic agents

`bc_check`, `completeness_check`, `frontend_sync`, `pin_bump`, `repo_description_sync`, and `roadmap_sync` interval
fields are available as YAML paths (`periodic.bc_check.*`, `periodic.completeness_check.*`, `periodic.frontend_sync.*`, `periodic.pin_bump.*`, `periodic.repo_description_sync.*`, `periodic.roadmap_sync.*`)
and as environment variables:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_BC_CHECK_INTERVAL_SECONDS` | `604800` | Seconds between bc-check passes. Set to `0` to disable. |
| `MILL_CI_DEBT_RECHECK_INTERVAL_SECONDS` | `3600` | Seconds between CI-debt recheck passes (1 hour). Set to `0` to disable. |
| `MILL_COMPLETENESS_CHECK_INTERVAL_SECONDS` | `1209600` | Seconds between completeness-check passes. Set to `0` to disable. |
| `MILL_COMPLETENESS_CHECK_REQUEST_LIMIT` | `80` | Per-call request cap for the completeness-check agent |
| `MILL_CONFIG_SYNC_INTERVAL_SECONDS` | `86400` | Seconds between config-sync passes (1 day). Set to `0` to disable. |
| `MILL_DIAGNOSTIC_EVENTS_PATH` | `None` | Explicit file path for the diagnostic event store JSONL file |
| `MILL_DIAGNOSTIC_EVENTS_MAX_AGE_DAYS` | `90` | Days after which diagnostic events are considered stale and excluded from recurring-failure counts and from the `ci_prevention_rules` digest. Set to `0` to disable aging (keep events indefinitely) |
| `MILL_DIAGNOSTIC_CI_FAILURE_THRESHOLD` | `3` | Legacy, inert. The recurring-CI diagnostic check no longer files report tickets (recurring failures feed the `ci_prevention_rules` pass instead); the field is kept only so configs that pin it still load. |
| `MILL_FRONTEND_SYNC_INTERVAL_SECONDS` | `604800` | Seconds between frontend-sync passes. Set to `0` to disable. |
| `MILL_MEMBER_SYNC_INTERVAL_SECONDS` | `86400` | Seconds between member-sync passes. Set to `0` to disable. |
| `MILL_PIN_BUMP_INTERVAL_SECONDS` | `86400` | Seconds between pin-bump passes. Set to `0` to disable. |
| `MILL_ROADMAP_SYNC_INTERVAL_SECONDS` | `604800` | Seconds between roadmap-sync passes. Set to `0` to disable. |
| `MILL_STATE_SYNC_INTERVAL_SECONDS` | `604800` | Seconds between state-sync passes. Set to `0` to disable. |
| `MILL_REPO_DESCRIPTION_SYNC_INTERVAL_SECONDS` | `604800` | Seconds between repo-description-sync passes. Set to `0` to disable. |

#### Stale branch cleanup, timeout escalation, dependabot ingest, module curator

These four periodic agents each carry one or two extra fields beyond the generic periodic pattern (periodic, interval). The following env vars configure those agent-specific extras:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_STALE_BRANCH_MAX_AGE_DAYS` | `30` | A branch is eligible for cleanup only if its last commit is older than this many days |
| `MILL_STALE_BRANCH_CLEANUP_PREFIX_ONLY` | `true` | When `true`, only delete branches whose name starts with `branch_prefix` ("old mill" branches); when `false`, also reap any other stale branch ("stale dev") |
| `MILL_TIMEOUT_ESCALATION_THRESHOLD_SECONDS` | `259200` | Tickets in `AWAITING_USER_REPLY` with `updated_at` older than this many seconds are escalated to `BLOCKED`; set ≤ 0 to disable escalation |
| `MILL_DEPENDABOT_INGEST_MAX_DRAFTS_PER_PASS` | `5` | Maximum number of Dependabot drafts created per ingest pass (across all repos) |
| `MILL_MODULE_CURATOR_REQUEST_LIMIT` | `120` | Per-call request budget for the module-curator agent |

#### Board hygiene (draft TTL auto-close + open-ticket cap)

The board-hygiene guards cover two faces of standing-stock hygiene: the
**draft TTL auto-close** runs during the `db_maintenance` periodic sweep
(no dedicated pass of its own), while the **open-ticket cap** is enforced
in real time on every `POST /tickets/ingest` request. Three settings
control the behaviour:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_BOARD_HYGIENE_DRAFT_TTL_DAYS` | `7` | Maximum age (days) an untouched draft can remain before auto-close. Only standalone drafts (no parent epic) are eligible; epics and their children are skipped. Set to `0` to disable (no drafts auto-closed regardless of age) |
| `MILL_BOARD_HYGIENE_MAX_OPEN_TICKETS` | `0` | Ceiling on total open (non-terminal) tickets per board. When reached, `POST /tickets/ingest` findings are appended to a rollup epic instead of creating standalone tickets. Human-created tickets are exempt. Set to `0` to disable the cap |

**Draft TTL auto-close.** During each `db_maintenance` sweep
(`db_maintenance_interval_seconds`, default 86400 s), all standalone DRAFT
tickets whose `updated_at` is older than `board_hygiene_draft_ttl_days`
are closed via `close_tracker` with a note explaining the TTL policy.
Epics and children of epics are skipped — their lifecycle is governed by
the parent.

**Open-ticket cap.** When the board reaches
`board_hygiene_max_open_tickets` (counting all non-terminal states),
each machine-ingest request (`POST /tickets/ingest`) appends its
finding as a history note to a `Rollup: <source_tag>` epic instead of
creating a new standalone ticket. The rollup epic is created once per
`source_tag` per board and reused on subsequent capped reports. This
guard is evaluated per request at ingest time — it does **not** wait
for a periodic sweep, and it applies to machine ingest regardless of
`board_hygiene_periodic`.
Human/operator-created tickets (`POST /tickets`) are exempt from the
cap.

**Config file keys.** The flat JSON keys in `config/config.json` match the
env-var names: `"board_hygiene_periodic"`, `"board_hygiene_draft_ttl_days"`,
`"board_hygiene_max_open_tickets"`.

**Ingest scope gate (auto-epic).** On every genuinely-new (non-duplicate)
`POST /tickets/ingest` report, a cheap small-tier scope classifier decides
whether the report is a single focused task or multi-concern work that
should become an epic. When it returns `EPIC` with confidence at or above
the threshold, the ticket is created as an epic, the decision + rationale
are recorded in its history, and the existing epic-breakdown machinery
spawns dependency-ordered child tickets. The gate runs past all dedup
checks, so a re-ingest of the same report is deduped upstream and never
re-classified. It is conservative by design: borderline reports stay
single tasks.

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_AUTO_EPIC_ENABLED` | `true` | When true, ingest promotes clearly multi-concern reports to an auto-decomposed epic. Set to `false` to disable the scope gate (all reports proceed as single tasks) |
| `MILL_AUTO_EPIC_MIN_CONFIDENCE` | `0.7` | Minimum scope-classifier confidence (`0.0`–`1.0`) required to auto-promote an ingested report to an epic. Below this, the report stays a single task |

**Machine-ingest source-tag block list.** robotsix-mill is a deployment-only board: `POST /tickets/ingest` callers whose `source_tag` matches an entry in `ingest_blocked_source_tags` are rejected with 400 and file no ticket. Investigation/diagnosis sources (caretaker, pin_bump, agent_limitation, recurring-diagnostic, recurring_error, robotsix-chat-feedback) can no longer auto-file tickets here — investigations are run as chat subsession agents instead. A source tag matches an entry when it equals it, is a `/`- or `-`-prefixed descendant (e.g. `caretaker` matches `caretaker/web`), or contains the entry as a delimited token (e.g. `caretaker` matches `pin_bump caretaker`). Override with a JSON array of source tags (`MILL_INGEST_BLOCKED_SOURCE_TAGS`), e.g. `["caretaker", "pin_bump"]`.

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `ingest_blocked_source_tags` | `MILL_INGEST_BLOCKED_SOURCE_TAGS` | `["caretaker", "pin_bump", "agent_limitation", "recurring-diagnostic", "recurring_error", "robotsix-chat-feedback"]` | Source tags whose machine ingests are rejected on this board (deployment-only). Override as a JSON array, e.g. `["caretaker", "my-investigation"]`. |

### 13. Skills & language instructions

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `sandbox.skills_dir` | `MILL_SKILLS_DIR` | `skills` | Directory of skill docs injected into agent system prompts |
| `core.language_instructions_dir` | `MILL_LANGUAGE_INSTRUCTIONS_DIR` | `agent_definitions/language_instructions` | Directory of per-language instruction Markdown snippets injected into the implement agent's system prompt |

---

## Secrets reference

Secrets are loaded from the `"secrets"` block of `config/config.json` by
a separate `Secrets` Pydantic model. They are **not** merged into
`Settings` — access them via `get_secrets()`. A value equal to the
literal `"SECRET"` sentinel (as in `config.example.json`) is treated as
unset.

| JSON key | Env var override | Description |
|----------|-----------------|-------------|
| `openrouter_api_key` | `OPENROUTER_API_KEY` | OpenRouter API key (required for any LLM call) |
| `openrouter_management_key` | — | OpenRouter management API key for credit balance checks (`GET /api/v1/activity`). Separate from the inference key; leave blank to skip OpenRouter-side fetching. |
| `forge_token` | `FORGE_TOKEN` | PAT for forge authentication |
| `forge_repo_create_token` | — | Fine-grained PAT used ONLY for repo creation. Falls back to `forge_token` if unset. |
| `github_app_id` | `GITHUB_APP_ID` | GitHub App ID (when `forge_auth=app`) |
| `github_app_private_key` | `GITHUB_APP_PRIVATE_KEY` | GitHub App private key (inline PEM, newlines as `\n`) |
| `github_app_private_key_path` | `GITHUB_APP_PRIVATE_KEY_PATH` | Alternative: host path to GitHub App private-key `.pem` file |
| `langfuse_public_key`¹ | — | Langfuse public key (configured via the `secrets:` block of `config/config.json`; read by `Secrets` model and stamped onto every `RepoConfig` at startup) |
| `langfuse_secret_key`¹ | — | Langfuse secret key (configured via the `secrets:` block of `config/config.json`; read by `Secrets` model and stamped onto every `RepoConfig` at startup) |
| `langfuse_base_url`¹ | — | Langfuse base URL (configured via the `secrets:` block of `config/config.json`; read by `Secrets` model and stamped onto every `RepoConfig` at startup) |
| `langfuse_project_id`¹ | — | Langfuse project ID (configured via the `secrets:` block of `config/config.json`; read by `Secrets` model and stamped onto every `RepoConfig` at startup) |
| `langfuse_project_name`¹ | — | Langfuse project name (configured via the `secrets:` block of `config/config.json`; read by `Secrets` model and stamped onto every `RepoConfig` at startup) |
| `fleet_notify_url` | — | Fleet notification endpoint URL for dispatching alerts (e.g. to robotsix-chat). Preferred over ntfy. |
| `fleet_notify_token` | — | Bearer token for the fleet notification endpoint. |
| `ntfy_url` | `NTFY_URL` | ntfy.sh topic URL for notifications. Legacy fallback; prefer `fleet_notify_url`. |
| `ntfy_token` | `NTFY_TOKEN` | ntfy.sh bearer token (optional). Legacy fallback; prefer `fleet_notify_token`. |
| `sandbox_push_token` | — | Optional dedicated token for the sandbox git-push bridge. When set, `github_push_token()` prefers this over `forge_token` (PAT mode only). Falls back to `forge_token` if unset. |

Secrets live in the `"secrets"` block of `config/config.json` (overridable
(located via `ROBOTSIX_CONFIG_FILE`). Template: the `"secrets"` block of
`config/config.example.json`.

> ¹ The `langfuse_*` fields on `Secrets` are configured via the
> `secrets:` block of `config/config.json`.  At startup, `Secrets` reads
> them from that block, and `_apply_global_langfuse` stamps them onto
> every `RepoConfig` so that each repo inherits the global Langfuse
> credentials.  Per-repo overrides are not supported — all repos share
> the same Langfuse configuration.

---

## Repos registry

The repos registry maps each repository to its own board identity and
Langfuse observability project. It is loaded **separately** from
`Settings` by a dedicated `ReposRegistry` Pydantic model — it never
participates in the Settings merge. Access it via `get_repos_config()`
or `get_repo_config("repo-id")`.

> **There is no longer a board-less default.** Every ticket must carry a
> `board_id` from `config/config.json`'s `"repos"` key. The legacy `<data_dir>/mill.db`
> that held tickets without a board_id has been removed. For single-repo
> deployments, configure exactly one repo entry.

Langfuse credentials are configured globally via the ``secrets:`` block of
``config/config.json`` — the ``Secrets`` model reads them from that block,
and ``_apply_global_langfuse`` stamps them onto every ``RepoConfig`` at
startup.  Per-repo overrides are not supported; all repos share the same
Langfuse configuration.

### Set up

Add a `"repos"` block to `config/config.json` — one entry per repository
(example entries under the `"repos"` key in `config/config.example.json`):

```yaml
# config/repos.yaml (or the "repos" key of config/config.json)
repos:
  my-repo:
    board_id: "my-board"
    # forge_remote_url: "https://github.com/your-org/your-repo.git"  # optional — defaults to FORGE_REMOTE_URL
    # Langfuse credentials are configured globally in the `secrets:` block —
    # there is no per-repo langfuse configuration.
```

After editing, verify the config is valid and uses real (non-placeholder)
keys:

```sh
python scripts/verify_repos_config.py
```

### Select a repo at startup

Once the `"repos"` key in `config/config.json` is configured, start the server.  By default
the server loads **all** repos from `config/config.json`'s `"repos"` key and serves them
together.  In this multi-repo mode the board UI includes a repo selector
dropdown — pick a repo to filter the kanban, runs list, and cost
dashboard, or select "All repos" to see everything at once.

```sh
# Multi-repo mode: serves every repo in config/config.json
robotsix-mill serve
```

To scope the process to a single repo (useful for tests/dev), pass
`--repo-id`:

```sh
# Single-repo override:
robotsix-mill serve --repo-id my-repo
```

When the `"repos"` key in `config/config.json` is empty, the server refuses to start (exit
code 2) with an error message.  An unknown `--repo-id` also causes an
error exit.

List the registered repos from the CLI:

```sh
robotsix-mill repos list
```

Source: the `"repos"` key of `config/config.json` (located via
`ROBOTSIX_CONFIG_FILE`). Example entries live under the `"repos"` key
in `config/config.example.json`.

### Field reference

| YAML key (in repos:) | Required | Default | Description |
|----------|----------|---------|-------------|
| `repos.<id>.board_id` | yes | — | Board identifier for per-repo board isolation |
| `repos.<id>.forge_remote_url` | no | `forge_remote_url` | Per-repo forge remote URL for push/PR/merge operations |
| `repos.<id>.meta_exclude` | no | `false` | When `true`, this repo is excluded from the periodic meta (fleet-consistency) pass — it is not cloned/studied, no META alignment/TODO drafts are filed against its board, and its board is not scanned for prior meta proposals. Operator-controlled (set in `config/repos.yaml`, not the managed repo's committed config). Use this for WIP or private repos that should not be graded against fleet standards. |
| `repos.<id>.working_branch` | no | — | Per-repo target branch for clone/baseline/deliver operations. When set, overrides the global `forge_target_branch`. Use this for repos whose default branch is not `main` (e.g. `rolling`, `lyrical`, `develop`). Automatically populated by member-sync from the manifest `version` field. |


Each repo ID must be unique and non-empty. The `board_id` must also be
non-empty. The registry validates that every entry's `repo_id` matches
its key in the repos map.

### Per-repo branch configuration

Every stage that clones, bases PRs, or rebases work (refine, implement,
deliver, merge, CI monitor, etc.) resolves the **effective target branch**
for each repo using this rule:

1. If `repos.<id>.working_branch` is set in `config/config.json`'s `"repos"` key, **use that**.
2. Otherwise, use the global `forge_target_branch` setting (default `main`).

This allows repos with non-main default branches to be fully onboarded:

```yaml
# config/config.json (repos key)
repos:
  ros2-example-interfaces:
    board_id: "example-interfaces"
    forge_remote_url: "https://github.com/damien-robotsix/example_interfaces.git"
    working_branch: lyrical  # This repo's default branch is 'lyrical', not 'main'
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
automatically upserts them into `config/config.json`'s `"repos"` key, creating boards and
filing build-out tickets on their behalf.

#### How it works

The workspace-member sync agent:

1. **Detects** vcs2l manifest members from the master repo's manifest file
   (typically `.rosinstall`).
2. **Derives** a `repo_id` from each member's path key (e.g. `src/zeta/pkg`
   → `src-zeta-pkg`), slugifying special characters to ASCII.
3. **Inherits** Langfuse configuration from the master repo so all members
   share observability projects.
4. **Upserts** entries into `config/config.json`'s `"repos"` key with the member's:
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

When multiple repos are registered (default when the `"repos"` key in `config/config.json`
has two or more entries), each periodic agent fans out across all repos
sequentially — one timer per agent type iterates every enabled repo in
turn. This means:

- **Memory files** are per-repo: `<data_dir>/<repo_id>/audit_memory.md`,
  `<data_dir>/<repo_id>/bc_check_memory.md`, etc.
- **Run registry** entries include a `repo_id` field. `GET /runs` accepts
  `?repo_id=X` to filter by repo.
- **CI monitor** dedup state is per-repo:
  `<data_dir>/<repo_id>/ci_monitor_state.json`.
- **Agent intervals** (e.g. `MILL_AUDIT_INTERVAL_SECONDS`) remain global — all
  repos share the same interval settings. Set to `0` to disable.

In single-repo mode (`--repo-id` on serve or one entry in
`config/config.json`'s `"repos"` key) periodic agents run only for that repo, and memory
files use the legacy flat path (`<data_dir>/audit_memory.md`).

---

## See also

- [index.md](../index.md) — documentation home
- [cli/usage.md](../cli/usage.md) — full CLI command reference
- [observability.md](../langfuse/observability.md) — per-repo Langfuse + deployed-log config the refine agent consults
- [deployment.md](../dev-tooling/deployment.md) — continuous deployment guide
- [config-audit.md](config-audit.md) — complete inventory of every config value and its source
- [`config/config.example.json`](../../config/config.example.json) — committed single-file config template (defaults + `"secrets"` block)
