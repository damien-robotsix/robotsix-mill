"""Settings field mixin: core API/model, LLM backend, service, forge, sandbox.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field


class _CoreSettings(BaseModel):
    # --- core ---
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    # Per-agent model selection is driven by each agent definition's
    # ``level: 1|2|3`` field, resolved to a (transport, model) by
    # ``build_agent`` via llmio's tier defaults. Transient 429/5xx/timeouts
    # are absorbed by the bounded retry+backoff (see transient_* below).
    #
    # --- Capability levels (llmio tier defaults) -------------------------
    # Per-agent model selection lives entirely in the agent definitions'
    # ``level: 1|2|3`` field (resolved to a (transport, model) by
    # ``build_agent`` via llmio's baked tier defaults — L1 DeepSeek flash,
    # L2 DeepSeek pro, L3 Claude Opus). There is no global backend toggle.
    #
    # Process-wide cap on how many Claude Agent SDK runs may execute at once.
    # Each run spawns a ``claude`` CLI subprocess; spawning many simultaneously
    # (worker startup contention) can stall a run. A global semaphore (see
    # ``agents.claude_concurrency``) bounds concurrent runs to smooth the spawn
    # storm. Applies to level-3 (Claude SDK) agents. Must be ≥ 1.
    claude_max_concurrency: int = Field(
        default=4, alias="MILL_CLAUDE_MAX_CONCURRENCY", ge=1
    )
    # Host-level cap on total concurrently-running stages across ALL boards,
    # applied on top of each board's own ``max_concurrency``.  Default 12 sits
    # modestly below the ~18 slots a typical multi-board setup would open with
    # per-board caps summed (2+1+...+1), providing a genuine backstop without
    # throttling normal operation.
    max_global_concurrency: int = Field(
        default=12, alias="MILL_MAX_GLOBAL_CONCURRENCY", ge=1
    )
    # Capability gate for inline-image (vision) input on the Claude SDK
    # transport. Default False: the installed robotsix-llmio claude_sdk
    # bridge silently mishandles ``BinaryContent`` image parts (it
    # stringifies them into a useless repr that hangs the ``claude`` CLI
    # until the 1200s per-call cap fires), so mill must NOT emit inline
    # images on that path. The refine/review screenshot paths degrade to
    # a text note while this is False. Flip to True (a one-line change)
    # once the bridge gains real image-input support (which also needs a
    # robotsix-llmio pin bump) to re-enable inline vision.
    claude_sdk_vision_enabled: bool = Field(default=False)
    # Hard cap on explore/parallel_explore sub-agent calls per refine run.
    # Calls beyond this cap are rejected with a clear message. Default 4
    # mirrors the existing parallel_explore concurrency limit and bounds
    # per-run sub-agent cost. Set to 0 to disable exploration entirely.
    max_refine_explore_calls: int = Field(default=4, ge=0)
    # Hard cap on read_file calls per refine/triage agent run. Calls
    # beyond this cap are rejected with a clear message. Default 10
    # matches the documented prompt budget instruction. Set to 0 to
    # disable the cap entirely (unbounded reads).  None-typed callers
    # that don't pass read_file_max_calls are unaffected — this cap
    # is opt-in per build_fs_tools invocation.
    max_refine_read_file_calls: int = Field(default=10, ge=0)
    # How long a cached web_knowledge .md file is considered fresh
    # (days). A consultation that hits a stale file is allowed to
    # web_search and update the file.
    web_knowledge_stale_days: int = Field(
        default=30,
    )
    # Bound on the web_knowledge sub-agent's tool requests per
    # consultation. Each request is one Markdown read, one web_search,
    # or one Markdown write.
    web_knowledge_request_limit: int = Field(
        default=8,
    )
    # Web-knowledge gateway sub-agent model. Defaults to the llmio
    # tier-1 flash model; override to route this agent to a different
    # model without changing the global tier defaults.
    web_knowledge_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    # Per-pass request budget for the implement (coordinator) agent.
    # Default 500 — high enough that normal-sized tickets finish in a
    # single pass (a medium ticket used ~49 calls; 500 provides ~10×
    # headroom) while still bounded.  The hard upper bound (5000)
    # prevents runaway cost from a misconfigured value; the budget
    # resets each pass so resumed tickets get a fresh allocation.
    # Set via MILL_PER_PASS_REQUEST_BUDGET env var or
    # core.limits.coordinator_requests in YAML config.
    coordinator_request_limit: int = Field(
        default=500,
        ge=1,
        le=5000,
        alias="MILL_PER_PASS_REQUEST_BUDGET",
    )
    # Hard cap on total tool calls per coordinator (implement) trace.
    # The request cap defaults to 500; this ceiling sits generously
    # above any legitimate implement run while still terminating the
    # 1000+-read runaway loops that produced incomplete_trace +
    # cost_outlier flags.
    coordinator_max_tool_calls: int = Field(default=300, ge=1)
    # Per-subtask request budget when the coordinator delegates via
    # ``spawn_subtask``. The parent's ``coordinator_request_limit``
    # still bounds the outer loop; this cap bounds each individual
    # sub-agent so one stuck subtask can't drain the parent's budget.
    subtask_request_limit: int = Field(default=30)
    # The test agent inspects failing output, reads the relevant
    # sources, and distills the cause — exploration-heavy work that
    # easily exceeds 8 calls on a non-trivial failure (live case: the
    # a74b baseline distill burned 2 of its 8 requests on a wrong
    # tool-arg and a wrong-cwd guess, then died mid-diagnosis with
    # "exceed the request_limit of 8"). 30 gives a real diagnosis budget
    # — the baseline-distill agent must inspect failing output, read
    # sources, and name the failing test; 16 was observed to run out
    # before producing a usable diagnosis on multi-test failures.
    # Cost-bounded by the ticket-level cap. Aligned with
    # config/config.example.yaml's core.limits.test_requests (30). The
    # yaml value wins at runtime via _YAML_PATH_TO_ALIAS; this just stops
    # the dry-Settings() default from contradicting it on machines without
    # a yaml override.
    test_request_limit: int = Field(default=30, ge=1)
    # Max implement→test fix iterations before BLOCKing. Complex
    # tickets may need several correction rounds.
    max_fix_iterations: int = Field(default=8, ge=0)
    # Bounded retry for TRANSIENT model/network failures (HTTP 429,
    # HTTP 5xx, connection/read timeouts) — used by every model call
    # and the ntfy POST. Non-transient errors (other 4xx, budget caps)
    # are never retried. Backoff is exponential, jittered, and capped
    # so a worker can't be stalled long.
    # Hard per-request timeout on EVERY model call — catches a truly
    # hung connection, but must sit ABOVE the model's tail latency or
    # it aborts legitimate long generations. deepseek-v4-pro routinely
    # runs 60-130s and was observed up to ~190s per generation; complex
    # tickets push higher. 900s comfortably clears that while still
    # bounding a real hang. On timeout the call raises -> transient ->
    # retry/backoff rides it out (or it BLOCKs visibly).
    # NOTE: the per-request HTTP timeout is now owned by llmio
    # (``MODEL_REQUEST_TIMEOUT`` = 900s); the mill no longer overrides it.

    # --- OpenRouter credit-balance warning ---
    # Board-level low-credit banner: when the OpenRouter balance drops
    # below this threshold the board shows an amber warning with a
    # top-up link.  Also triggered reactively by 402 insufficient-credit
    # errors from the stage error handlers.
    low_credit_threshold_usd: float = Field(default=5.0, ge=0.0)
    # Background poll toggle.  Set false to disable the proactive
    # GET /api/v1/credits poll; the reactive 402 path still fires.
    low_credit_poll_enabled: bool = Field(default=True)
    # Seconds between proactive credit-balance polls (default 1 hour).
    low_credit_poll_interval_seconds: int = Field(default=3600, ge=60)

    # --- startup re-queue & periodic first-tick jitter ---
    # Tickets enqueued per batch in the startup re-queue drip feed.
    requeue_batch_size: int = Field(default=5, ge=1)
    # Pause (seconds) between batches in the startup re-queue drip feed.
    requeue_batch_pause_seconds: float = Field(default=2.0, ge=0.0)
    # Max random spread (seconds) added to the per-repo periodic pass
    # first-tick delay, spreading the initial fire across a window so
    # the post-boot thundering herd is diluted.
    startup_jitter_seconds: int = Field(default=30, ge=0)

    # Short-TTL cache for the board-poll GET /tickets endpoint (seconds).
    # The board UI + board-manager poll it every few seconds; each call is a
    # full all-board query + enrichment that, under load, stalls the shared
    # event loop. Repeated identical polls within this window return a cached
    # snapshot (≤ this many seconds stale). 0.0 disables the cache.
    # Field default is 0.0 (disabled) so unit tests that construct Settings()
    # directly see immediate list consistency (create-then-list); the live
    # mill enables it via config/config.example.yaml (3.0s).
    board_list_cache_ttl_seconds: float = Field(default=0.0, ge=0.0)

    transient_retries: int = Field(default=4, ge=0)
    transient_backoff_base: float = Field(default=2.0, gt=0)
    transient_backoff_cap: float = Field(default=30.0, gt=0)
    # Retry policy for stage-level transient errors (httpx.ConnectError,
    # etc.).  These control how many times a stage is re-attempted and
    # the exponential-backoff delay between attempts inside the worker
    # loop.  Test-friendly: keep the defaults small enough for tests to
    # override without needing long sleeps.
    stage_retry_max_attempts: int = Field(default=5)
    stage_retry_base_delay: float = Field(default=2.0)
    stage_retry_max_delay: float = Field(default=60.0)
    # Global-network-outage parking. When a stage fails with a
    # host-resolution error AND this probe host doesn't resolve either,
    # the worker re-schedules the ticket WITHOUT consuming a retry
    # attempt — an outage longer than the bounded stage-retry envelope
    # must not mass-block the board. The ticket re-polls every
    # network_outage_retry_seconds until connectivity returns.
    network_probe_host: str = Field(default="github.com")
    network_outage_retry_seconds: int = Field(default=120, ge=1)
    # Backoff for UsageLimitExceeded (pydantic-ai budget cap).  These
    # are longer than transient backoff because OpenRouter/provider
    # rate-limit windows are typically ~60s.
    rate_limit_backoff_base: float = Field(default=30.0, gt=0)
    rate_limit_backoff_cap: float = Field(default=120.0, gt=0)
    # Per-call cap for the read-only exploration sub-agent the
    # coordinator uses instead of reading the repo into its own context.
    # Per-call cap for the domain-expert consultation sub-agent the
    # coordinator uses when it needs domain-specific advice.
    consult_request_limit: int = Field(default=15, ge=1)
    explore_request_limit: int = Field(default=100, ge=1)
    explore_max_tokens: int = Field(default=4096, ge=1)
    # Per-call cap for the refine agent's tool loop. The refine agent
    # delegates deep search to the cheap ``explore`` sub-agent (which
    # has its own 100-call budget), so the top-level refine loop should
    # rarely exceed a few dozen tool calls.  80 sits above the old
    # implicit pydantic-ai default of 50 — intentionally, because broad
    # scaffolding and maintenance tickets (forge integration,
    # agent-definition build-out) empirically need more top-level calls
    # even with good delegation (refine runs saturated 40, then 60 —
    # ticket 5353 — despite the delegate-to-explore prompt bias);
    # per-run cost is negligible (~$0.03–0.09), and the ticket-level
    # spend cap is the real backstop.
    # Note: ``review_request_limit`` is also 80 (bumped from 40 — it has
    # no explore sub-agent and was saturating on test-heavy diffs; see its
    # field comment and ticket bc6d).
    refine_request_limit: int = Field(default=80, ge=1)
    # Per-call cap for non-escalated (simple/sonnet) refine runs.
    # Lower than the main cap (80) because simple tickets need fewer
    # tool calls — the explore/parallel_explore sub-agents are gated off.
    refine_request_limit_simple: int = Field(default=40, ge=1)
    # Per-call cap for the maintenance agent's tool loop. Maintenance
    # tickets are operational one-offs (clone + inspect + post
    # findings); like refine, deep search is delegated to explore. An
    # EXPLICIT cap so exhaustion is a documented knob, not the implicit
    # pydantic-ai default of 50 that blocked the data-dir audit tickets
    # with an opaque "Fatal: UsageLimitExceeded: … request_limit of 50".
    # Bumped from 60 → 100 to give headroom for investigation-heavy
    # tickets so the agent doesn't exhaust its budget on git history
    # searches when read_file + explore would be faster.
    maintenance_request_limit: int = Field(default=100, ge=1)
    # Per-call cap for the dedup check — the agent reads candidate
    # ticket bodies to verify matches, so allow a slightly larger
    # budget than a naive single-call (bumped from 4 after the agent
    # exhausted its budget on narrow read_file slices).
    dedup_request_limit: int = Field(default=12, ge=1)
    # Per-call cap for the obsolescence gate — the agent reads a few
    # cited files to verify the gap, so allow a slightly larger budget
    # than the dedup check.
    obsolescence_request_limit: int = Field(default=6, ge=1)
    # Per-call cap for the periodic audit agent's tool loop. The audit
    # agent does broad work (license scan, pip-audit, coverage
    # introspection) and can saturate 50 calls on a genuine run —
    # 80 gives headroom; per-run cost ~$0.29 stays well under the
    # per-ticket $ backstop.
    audit_request_limit: int = Field(default=80, ge=1)
    # Per-call cap for the test-gap agent's tool loop. The test-gap
    # agent does broad work (explore storms scanning the full repo for
    # test-coverage gaps) and can saturate the pydantic-ai default of
    # 50 calls on a genuine run — 80 gives headroom matching the audit
    # agent's budget for a similar broad-scan workload.
    test_gap_request_limit: int = Field(default=80, ge=1)
    # Hard cap on total tool calls per test_gap trace. 100 tool calls
    # is far beyond what any legitimate test-coverage scan requires —
    # only clearly broken runs are terminated.
    test_gap_max_tool_calls: int = Field(default=100, ge=1)
    # Hard cap on tool-call errors before auto-termination. A healthy
    # inspection should have near-zero errors; 20 indicates a broken
    # execution loop.
    test_gap_max_errors: int = Field(default=20, ge=0)
    # Hard cap on total tool calls per refine trace. A hard ceiling above
    # any legitimate refine run (the request cap is 80; 120 tool calls is a
    # generous headroom that still terminates the 100+-call broken loops).
    refine_max_tool_calls: int = Field(default=120, ge=1)
    # Hard cap on tool-call errors before auto-termination. Matches the
    # test_gap/trace_inspector default; a healthy refine has near-zero
    # tool errors.
    refine_max_errors: int = Field(default=20, ge=0)
    # Dynamic request-limit multiplier for large/complex specs.
    # When the draft exceeds refine_dynamic_limit_spec_chars (default
    # 3000) or the scope-triage agent's own budget was over 60% of
    # the refine limit, the effective request_limit is multiplied by
    # this factor (with a floor of refine_dynamic_limit_min).
    refine_dynamic_limit_multiplier: float = Field(default=1.5, gt=1.0)
    refine_dynamic_limit_min: int = Field(default=12, ge=1)
    refine_dynamic_limit_spec_chars: int = Field(default=3000, ge=1)
    # Emit a warning when the refine agent consumes more than this
    # fraction (0.0–1.0) of its request_limit, so near-exhaustion
    # patterns are observable even when the run doesn't crash.
    refine_usage_warning_threshold: float = Field(default=0.8, gt=0.0, le=1.0)
    doc_request_limit: int = Field(default=32)
    doc_classifier_request_limit: int = Field(default=3)
    # Caps the git diff fed to the cheap doc-classifier gate. Truncation
    # is safe here: the classifier is conservatively biased toward
    # user_facing=True, so a truncated diff at worst loses signal and
    # routes to the full doc agent — the harmless direction. The full
    # doc agent still receives the untruncated diff.
    doc_classifier_diff_max_chars: int = Field(default=6000)
    # Maximum characters of the memory ledger to load per agent pass.
    # When the file exceeds this, the oldest entries are dropped from the
    # loaded view; persist_memory also applies the cap on write when
    # max_chars is passed. Applies to all memory ledgers (refine, audit,
    # health, agent-check, etc.).
    max_memory_chars: int = Field(default=8000, ge=0)
    # Maximum characters of the retrospect stage's history + comments
    # logs fed to the agent. These are chronological, so the most-recent
    # tail is kept and older lines dropped. 0 disables capping.
    retrospect_log_max_chars: int = Field(default=12000, ge=0)
    # Max number of entries retained in AGENT_CANDIDATES.md (the per-board
    # append-only queue of proposed AGENT.md rule additions). Pending
    # entries are always kept; resolved (validated/rejected) entries are
    # pruned oldest-first to honor this cap. 0 disables pruning.
    retrospect_candidates_max_entries: int = Field(default=100, ge=0)
    # Maximum number of files whose full content the refine stage stores
    # as reference_files.json for the implement coordinator to pre-load.
    reference_files_max_count: int = Field(default=5)
    # Maximum total lines across all selected reference files. When the
    # cumulative line count would exceed this, files beyond the limit are
    # dropped (top-N priority order preserved).
    reference_files_max_total_lines: int = Field(default=3000)
    # How many days back closed tickets are considered as duplicate
    # candidates by the pre-refine dedup check.
    dedup_lookback_days: int = Field(default=7)
    # Maximum number of candidates to pass to the dedup LLM after
    # similarity-based pre-filtering.  Caps the token budget regardless
    # of repo size.  ≥ 1 enforced by validator.
    dedup_max_candidates: int = Field(default=8, ge=1)
    # When True (default), the pre-refine dedup LLM call is skipped
    # entirely when the draft shares zero meaningful token overlap with
    # every candidate (title+body) — the common "clearly unrelated"
    # case.  Saves 100% of the call cost for genuine non-duplicates.
    dedup_skip_on_no_overlap: bool = Field(default=True)
    # Caps each candidate body fed to the dedup prompt (mirrors
    # doc_classifier_diff_max_chars). Generous by default so it only
    # clips pathologically long specs; ≤ 0 disables truncation.
    dedup_candidate_body_max_chars: int = Field(default=4000)
    # Local-dev default: ``.data`` — the same path the docker-compose
    # volume mounts at /data, so host CLI invocations and the container
    # share state instead of leaking a separate sibling tree. The
    # Dockerfile sets MILL_DATA_DIR=/data explicitly so the container
    # always uses the absolute path. Tests override via tmp_path.
    data_dir: Path = Field(default=Path(".data"))

    # Path to a directory containing clones of registered repos for
    # cross-repo investigation by the maintenance agent.  When set, the
    # agent's read-only tools (read_file, list_dir, run_command, explore,
    # parallel_explore) are scoped to this directory.  When None, the
    # agent falls back to the ticket's own workspace repo_dir.
    # Configurable via MILL_INVESTIGATION_WORKSPACE env var or
    # config/config.yaml.
    investigation_workspace: Path | None = Field(
        default=None, alias="MILL_INVESTIGATION_WORKSPACE"
    )

    # Default repo ID for legacy tickets that lack a board_id.
    # Set in config/config.yaml.  When empty (default), accessing
    # a legacy ticket without a board_id raises an error telling the
    # operator to configure this.
    default_repo_id: str = Field(default="")

    # --- management-plane service ---
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8077)
    # Base URL the CLI client talks to.
    api_url: str = Field(default="http://127.0.0.1:8077", pattern=r"^https?://")

    # --- forge delivery (only used by the deliver stage) ---
    forge_kind: Literal["github", "gitlab", "none", "auto"] = Field(
        default="none", alias="FORGE_KIND"
    )
    forge_remote_url: str | None = Field(default=None, alias="FORGE_REMOTE_URL")
    forge_token: str | None = Field(default=None, alias="FORGE_TOKEN")
    forge_target_branch: str = Field(default="main", alias="FORGE_TARGET_BRANCH")
    # token  = use FORGE_TOKEN (PAT) directly.
    # app    = mint a short-lived GitHub App installation token so the
    #          bot identity (<app-slug>[bot]) authors the PR.
    forge_auth: Literal["token", "app"] = Field(default="token", alias="FORGE_AUTH")
    github_app_id: str | None = Field(default=None, alias="GITHUB_APP_ID")
    github_app_private_key: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY"
    )
    github_app_private_key_path: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )
    # GitHub API base (override for GitHub Enterprise).
    github_api_url: str = Field(default="https://api.github.com", pattern=r"^https?://")
    # GitLab API base (override for self-hosted GitLab instances).
    gitlab_api_url: str = Field(
        default="https://gitlab.com/api/v4", pattern=r"^https?://"
    )

    # --- implement stage ---
    # Command run to verify the implementation; empty string skips the
    # test gate. Failures feed back into the bounded fix loop.
    # Global fallback for the test gate command. Empty by default —
    # per-repo `test_command` in repos.yaml is the authoritative source.
    # When both are empty, the test gate short-circuits to PASS
    # ("no test gate configured"). MILL_TEST_COMMAND can override for
    # single-repo / legacy setups.
    test_command: str = Field(default="")
    # Global fallback for the path-scoped smoke gate command (run after
    # unit tests pass). Empty by default — the per-repo
    # `.robotsix-mill/config.yaml` `smoke_command` wins when set, this is
    # the fleet-wide fallback, and empty everywhere means no smoke gate
    # (short-circuits to PASS). MILL_SMOKE_COMMAND can override.
    # Path-scoping (`smoke_paths`) is inherently per-repo and lives only
    # in `.robotsix-mill/config.yaml`; there is no global counterpart.
    smoke_command: str = Field(default="")
    branch_prefix: str = Field(default="mill/")
    # Wall-clock cap (seconds) for the agent's shell tool and the test
    # command, so a hung command can't stall a worker forever.
    command_timeout: int = Field(default=1800, gt=0)
    # Safety net: if a ticket re-enters the *same* model-driven stage
    # this many times without ever progressing (e.g. its run keeps being
    # interrupted, or a stage churns), the worker escalates it to BLOCKED
    # + notifies instead of silently re-billing the LLM forever. Poll
    # stages (merge/deliver) are exempt — human_mr_approval legitimately waits.
    max_stuck_cycles: int = Field(default=3, ge=0)
    # Dollar-cap safety net: if a ticket's cumulative Langfuse-traced
    # LLM spend exceeds this value (across all stages), the worker
    # escalates it to BLOCKED. 0.0 disables the cap entirely.
    #
    # ON BY DEFAULT ($20) — this is the universal backstop against runaway
    # loops (e.g. a ci_fix or refine loop that oscillates between two states,
    # which the state-change-based no-progress net cannot see). A normal
    # ticket costs ~$1–4, so $20 gives ample headroom while killing a runaway
    # before it burns a fortune. The block is RESUMABLE — a genuinely
    # expensive ticket can be resumed with resume-blocked to continue.
    max_spend_usd_per_ticket: float = Field(default=20.0)
    max_traces_per_ticket: int = Field(default=15, ge=0)
    max_openrouter_marginal_usd_per_ticket: float = Field(default=3.0, ge=0.0)
    # Per-stage wall-clock timeout (seconds).  A stage that exceeds this
    # limit is escalated to BLOCKED, freeing the worker slot.  ≤ 0
    # disables the timeout entirely.  2400 s (40 min) comfortably
    # exceeds worst-case LLM latency (~190 s per call) and multiple
    # shell-command runs while still catching a true hang.
    stage_timeout_seconds: int = Field(default=2400)
    # Per-stage timeout overrides (JSON dict via env var, e.g.
    # MILL_STAGE_TIMEOUT_OVERRIDES='{"merge":0,"refine":1200}').
    # Keys are stage names; values are seconds.  Falls back to
    # stage_timeout_seconds when a stage isn't listed.  A value of 0
    # disables the timeout for that stage.
    #
    # Built-in default: refine caps at 900 s (15 min).  A sampled
    # legitimate refine run on model_level 3 (Claude SDK / Opus)
    # clocked 736 s (~12 min); 900 s leaves headroom while still
    # catching multi-hour runaway refine traces.  Operators can
    # override or disable (value 0) via the env var / YAML key.
    # Supplying your own dict REPLACES the built-in — re-include
    # a "refine" entry if you still want a cap.
    stage_timeout_overrides: dict[str, int] = Field(
        default_factory=lambda: {"refine": 900}
    )
    # Maximum seconds to wait for in-flight periodic-agent passes
    # (survey, audit, health, …) to finish before tearing the worker
    # down on container shutdown. The mill's docker-compose ships a
    # matching ``stop_grace_period`` so docker won't SIGKILL before
    # the wait completes; if you change one, change the other.
    # 0 → wait forever; set <= the docker grace period to bound the
    # final wait.
    shutdown_grace_seconds: int = Field(default=1800)

    # --- command sandbox (always a disposable container; no local mode) ---
    # Image the sandbox runs commands in — must contain the toolchain
    # MILL_TEST_COMMAND needs.
    sandbox_image: str = Field(default="python:3.14-slim")
    sandbox_memory: str = Field(default="2g")
    sandbox_pids_limit: int = Field(default=512)
    sandbox_readonly: bool = Field(default=True)
    # Docker network sandbox containers connect to. The network must be
    # internal (no direct internet) with a filtering proxy attached —
    # sandbox commands reach PyPI/GitHub ONLY through the proxy.
    sandbox_network: str = Field(default="mill-sandbox-net")
    # URL of the egress proxy. Sandbox containers receive HTTP_PROXY,
    # HTTPS_PROXY, http_proxy, and https_proxy set to this value.
    # Set to empty string to disable (restores --network none behavior).
    sandbox_proxy_url: str = Field(default="http://sandbox-proxy:8888")
    # What the sandbox sibling containers mount at MILL_DATA_DIR. The
    # daemon resolves -v on the host, so this must be a named volume OR
    # the host path of a bind mount. data_volume is the fallback name;
    # sandbox_data_mount (host path) overrides it for bind-mounted ./.data.
    data_volume: str = Field(default="mill_data")
    sandbox_data_mount: str | None = Field(default=None)
