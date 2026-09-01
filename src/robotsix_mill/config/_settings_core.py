"""Settings field mixin: core API/model, LLM backend, service, forge, sandbox.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr


class _CoreSettings(BaseModel):
    # Per-agent model selection is driven by each agent definition's
    # ``level: 1|2|3|4`` field, resolved to a (transport, model) by
    # ``build_agent`` via llmio's tier defaults. Transient 429/5xx/timeouts
    # are absorbed by the bounded retry+backoff (see transient_* below).
    #
    # --- Capability levels (llmio tier defaults) -------------------------
    # Per-agent model selection lives entirely in the agent definitions'
    # ``level: 1|2|3|4`` field (resolved to a (transport, model) by
    # ``build_agent`` via llmio's baked tier defaults (see llmio tier config
    # for current mapping). There is no
    # global backend toggle.
    #
    # Deprecated, inert. Used to size a process-wide semaphore around every
    # Claude SDK ``run_sync`` (``agents.claude_concurrency``, removed): a
    # 30-minute implement run held a slot for its whole duration and every
    # short Claude call (gates, classifiers, ingest) queued behind it. The real
    # limits are the subscription rate cap (park logic) and host resources
    # (``max_global_concurrency`` + sandbox caps). Kept only so configs that
    # pin it still load and ``PUT /config`` still accepts it.
    claude_max_concurrency: int = Field(
        default=4,
        ge=1,
        description=(
            "Deprecated, inert since the Claude run semaphore was removed: "
            "no longer bounds anything. Kept so existing pinned configs load."
        ),
        json_schema_extra={"advanced": True},
    )
    # Host-level cap on total concurrently-running stages across ALL boards,
    # applied on top of each board's own ``max_concurrency``.  Default 12 sits
    # modestly below the ~18 slots a typical multi-board setup would open with
    # per-board caps summed (2+1+...+1), providing a genuine backstop without
    # throttling normal operation.
    #
    # Also the hard ceiling on live ``mill-sbx-*`` sandbox containers — see
    # ``sandbox._sandbox_slot``.  The board-consumer semaphore alone never
    # bounded them: periodic passes and the meta-agent spawn sandboxes outside
    # it, so the live count ran well above the cap.
    max_global_concurrency: int = Field(
        default=12,
        ge=1,
        description=(
            "Host-level cap on total concurrently-running stages across ALL "
            "boards, and on live sandbox containers."
        ),
        json_schema_extra={"advanced": True},
    )
    # Maximum stagger window (seconds) for cross-repo fan-out ticket
    # creation. When a single programmatic source (periodic pass,
    # audit, survey) enqueues tickets across N repos, the N
    # invocations are spread evenly across this window so the
    # resulting pipeline activation is smoothed instead of hitting
    # the LLM provider as one concentrated spike.  Set to 0 to
    # disable staggering entirely (fan-out creates all tickets at
    # once, the historical behaviour).
    # Override with MILL_FAN_OUT_STAGGER_SECONDS.
    fan_out_stagger_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "Maximum stagger window (seconds) for cross-repo fan-out "
            "ticket creation. 0 disables staggering."
        ),
        json_schema_extra={"advanced": True},
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
    claude_sdk_vision_enabled: bool = Field(
        default=False,
        description="Enable inline-image (vision) input on the Claude SDK transport. Requires a robotsix-llmio pin bump with real image-input support.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on explore/parallel_explore sub-agent calls per refine run.
    # Calls beyond this cap are rejected with a clear message. Default 4
    # mirrors the existing parallel_explore concurrency limit and bounds
    # per-run sub-agent cost. Set to 0 to disable exploration entirely.
    max_refine_explore_calls: int = Field(
        default=4,
        ge=0,
        description="Hard cap on explore/parallel_explore sub-agent calls per refine run. Set to 0 to disable exploration entirely.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on read_file calls per refine/triage agent run. Calls
    # beyond this cap are rejected with a clear message. Default 10
    # matches the documented prompt budget instruction. Set to 0 to
    # disable the cap entirely (unbounded reads).  None-typed callers
    # that don't pass read_file_max_calls are unaffected — this cap
    # is opt-in per build_fs_tools invocation.
    max_refine_read_file_calls: int = Field(
        default=10,
        ge=0,
        description="Hard cap on read_file calls per refine/triage agent run. Set to 0 to disable the cap.",
        json_schema_extra={"advanced": True},
    )
    # When true, the POST /repos API endpoint is allowed to hot-register
    # repos at runtime. When false (default), only repos listed in the
    # operator's shipped config (config/config.json) are accepted — runtime
    # registration via the API is refused. Also controls whether tickets
    # for auto-registered repos (source="auto") are accepted by
    # POST /tickets and POST /tickets/ingest.
    allow_runtime_repo_registration: bool = Field(
        default=False,
        description="When true, allow POST /repos to hot-register repos at runtime. When false, only operator-configured repos are accepted.",
    )
    # How long a cached web_knowledge .md file is considered fresh
    # (days). A consultation that hits a stale file is allowed to
    # web_search and update the file.
    web_knowledge_stale_days: int = Field(
        default=30,
        description="Days before a cached web_knowledge .md file is considered stale and eligible for refresh.",
        json_schema_extra={"advanced": True},
    )
    # How long since the last ``last_verified`` touch before a cached
    # knowledge file is flagged as stale in the index (hours). When a
    # file is stale the web_knowledge agent's system prompt warns it
    # to cross-check claims with web_search before trusting the cache.
    web_knowledge_cache_ttl_hours: int = Field(
        default=72,
        description="Hours since last_verified before a cached knowledge file is flagged as stale in the index.",
        json_schema_extra={"advanced": True},
    )
    # Bound on the web_knowledge sub-agent's tool requests per
    # consultation. Each request is one Markdown read, one web_search,
    # or one Markdown write.
    web_knowledge_request_limit: int = Field(
        default=16,
        description="Request budget for the web_knowledge sub-agent per consultation.",
        json_schema_extra={"advanced": True},
    )
    # Web-knowledge gateway sub-agent model. Defaults to the llmio
    # tier-1 flash model; override to route this agent to a different
    # model without changing the global tier defaults.
    web_knowledge_model: str = Field(
        default="",
        description="Model alias for the web-knowledge gateway sub-agent. "
        "When empty, resolves to the llmio tier-1 model at use time.",
        json_schema_extra={"advanced": True},
    )
    # Per-pass request budget for the implement (coordinator) agent.
    # Default 500 — high enough that normal-sized tickets finish in a
    # single pass (a medium ticket used ~49 calls; 500 provides ~10×
    # headroom) while still bounded.  The hard upper bound (5000)
    # prevents runaway cost from a misconfigured value; the budget
    # resets each pass so resumed tickets get a fresh allocation.
    # Set via MILL_PER_PASS_REQUEST_BUDGET env var or
    # core.limits.coordinator_requests in JSON config.
    coordinator_request_limit: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="Per-pass request budget for the implement (coordinator) agent. Default 500.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on total tool calls per coordinator (implement) trace.
    # The request cap defaults to 500; this ceiling sits generously
    # above any legitimate implement run while still terminating the
    # 1000+-read runaway loops that produced incomplete_trace +
    # cost_outlier flags.
    coordinator_max_tool_calls: int = Field(
        default=300,
        ge=1,
        description="Hard cap on total tool calls per coordinator (implement) trace.",
        json_schema_extra={"advanced": True},
    )
    # Wall-clock timeout (seconds) for a single implement agent pass.
    # When the agent exceeds this duration the pass is terminated and
    # the stage can retry (with a fresh budget) or escalate.  Default
    # 600 s (10 min) — aligned with config/config.example.json (600).
    # Set via MILL_COORDINATOR_TIMEOUT_SECONDS env var or
    # core.limits.coordinator_timeout_seconds in JSON config.
    coordinator_timeout_seconds: int = Field(
        default=600,
        ge=60,
        description="Wall-clock timeout (seconds) for a single implement agent pass.",
        json_schema_extra={"advanced": True},
    )
    # Per-stage overrides for the coordinator (implement) timeout.
    # Keys are stage names; values are seconds.  Falls back to
    # coordinator_timeout_seconds when a stage isn't listed.  A value
    # of 0 disables the timeout for that stage.
    coordinator_timeout_overrides: dict[str, int] = Field(
        default_factory=dict,
        description="Per-stage coordinator timeout overrides (dict). Keys are stage names, values are seconds.",
        json_schema_extra={"advanced": True},
    )
    # Per-subtask request budget when the coordinator delegates via
    # ``spawn_subtask``. The parent's ``coordinator_request_limit``
    # still bounds the outer loop; this cap bounds each individual
    # sub-agent so one stuck subtask can't drain the parent's budget.
    subtask_request_limit: int = Field(
        default=30,
        description="Per-subtask request budget when the coordinator delegates via spawn_subtask.",
        json_schema_extra={"advanced": True},
    )
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
    # config/config.example.json's core.limits.test_requests (30). The
    # json value wins at runtime via JsonSettingsSource; this just stops
    # the dry-Settings() default from contradicting it on machines without
    # a json override.
    test_request_limit: int = Field(
        default=30,
        ge=1,
        description="Request budget for the test agent when diagnosing failures.",
        json_schema_extra={"advanced": True},
    )
    # Max implement→test fix iterations before BLOCKing. Complex
    # tickets may need several correction rounds.
    max_fix_iterations: int = Field(
        default=8,
        ge=0,
        description="Maximum implement→test fix iterations before the ticket is BLOCKED.",
        json_schema_extra={"advanced": True},
    )
    # Bounded retry for TRANSIENT model/network failures (HTTP 429,
    # HTTP 5xx, connection/read timeouts) — used by every model call
    # and the ntfy POST. Non-transient errors (other 4xx, budget caps)
    # are never retried. Backoff is exponential, jittered, and capped
    # so a worker can't be stalled long.
    # Hard per-request timeout on EVERY model call — catches a truly
    # hung connection, but must sit ABOVE the model's tail latency or
    # it aborts legitimate long generations. Some models routinely
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
    low_credit_threshold_usd: float = Field(
        default=5.0,
        ge=0.0,
        description="OpenRouter balance below this threshold triggers an amber warning banner on the board.",
    )
    # Background poll toggle.  Set false to disable the proactive
    # GET /api/v1/credits poll; the reactive 402 path still fires.
    low_credit_poll_enabled: bool = Field(
        default=True,
        description="Enable proactive OpenRouter credit-balance polling. The reactive 402 path still fires when disabled.",
    )
    # Seconds between proactive credit-balance polls (default 1 hour).
    low_credit_poll_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="Seconds between proactive OpenRouter credit-balance polls.",
        json_schema_extra={"advanced": True},
    )

    # --- startup re-queue & periodic first-tick jitter ---
    # Tickets enqueued per batch in the startup re-queue drip feed.
    requeue_batch_size: int = Field(
        default=5,
        ge=1,
        description="Tickets enqueued per batch in the startup re-queue drip feed.",
        json_schema_extra={"advanced": True},
    )
    # Pause (seconds) between batches in the startup re-queue drip feed.
    requeue_batch_pause_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="Pause (seconds) between batches in the startup re-queue drip feed.",
        json_schema_extra={"advanced": True},
    )
    # Max random spread (seconds) added to the per-repo periodic pass
    # first-tick delay, spreading the initial fire across a window so
    # the post-boot thundering herd is diluted.
    startup_jitter_seconds: int = Field(
        default=30,
        ge=0,
        description="Max random jitter (seconds) added to per-repo periodic pass first-tick delay.",
        json_schema_extra={"advanced": True},
    )

    # Short-TTL cache for the board-poll GET /tickets endpoint (seconds).
    # The board UI + board-manager poll it every few seconds; each call is a
    # full all-board query + enrichment that, under load, stalls the shared
    # event loop. Repeated identical polls within this window return a cached
    # snapshot (≤ this many seconds stale). 0.0 disables the cache.
    board_list_cache_ttl_seconds: float = Field(
        default=3.0,
        ge=0.0,
        description="Short-TTL cache for the board-poll GET /tickets endpoint (seconds). 0.0 disables.",
        json_schema_extra={"advanced": True},
    )

    # Retry policy for stage-level transient errors (httpx.ConnectError,
    # etc.).  These control how many times a stage is re-attempted and
    # the exponential-backoff delay between attempts inside the worker
    # loop.  Test-friendly: keep the defaults small enough for tests to
    # override without needing long sleeps.
    stage_retry_max_attempts: int = Field(
        default=5,
        description="Maximum stage-level retry attempts for transient errors before escalating.",
        json_schema_extra={"advanced": True},
    )
    stage_retry_base_delay: float = Field(
        default=2.0,
        description="Base delay (seconds) for exponential backoff between stage retries.",
        json_schema_extra={"advanced": True},
    )
    stage_retry_max_delay: float = Field(
        default=60.0,
        description="Maximum delay (seconds) for exponential backoff between stage retries.",
        json_schema_extra={"advanced": True},
    )
    # Global-network-outage parking. When a stage fails with a
    # host-resolution error AND this probe host doesn't resolve either,
    # the worker re-schedules the ticket WITHOUT consuming a retry
    # attempt — an outage longer than the bounded stage-retry envelope
    # must not mass-block the board. The ticket re-polls every
    # network_outage_retry_seconds until connectivity returns.
    network_probe_host: str = Field(
        default="github.com",
        description="Hostname probed to distinguish network outages from upstream errors.",
    )
    network_outage_retry_seconds: int = Field(
        default=120,
        ge=1,
        description="Seconds between re-poll attempts during a detected network outage.",
        json_schema_extra={"advanced": True},
    )
    # Disk-exhaustion parking — the ENOSPC analogue of the network-outage
    # parking above, and for the same reason. A full data volume fails
    # every clone and every write on every board identically; bounded
    # retries just burn the budget in seconds and then block the ticket
    # FATALLY, converting one infrastructure fault into one manual resume
    # per ticket. On 2026-08-06 that arithmetic produced 146 blocked
    # tickets from a single full volume — tickets which then pinned 34 GB
    # of .venv that only they could release. Park instead: the ticket
    # re-polls every disk_full_retry_seconds, retry budget untouched,
    # and resumes by itself once the GC or an operator frees space.
    disk_full_retry_seconds: int = Field(
        default=600,
        ge=1,
        description="Seconds between re-poll attempts while the data volume is full.",
        json_schema_extra={"advanced": True},
    )
    # Free-space floor for the pre-stage admission check. Below this,
    # a ticket parks BEFORE running rather than failing partway through
    # and leaving a half-written workspace behind. Sized well above a
    # single clone+sync so the check fires before the volume is actually
    # at zero. Set to 0 to disable the preflight entirely.
    disk_min_free_mb: int = Field(
        default=5_120,
        ge=0,
        description="Minimum free MB on the data volume before a stage is allowed to start.",
    )
    # Filesystems the disk gate must check BESIDES ``data_dir``.
    #
    # ``/`` is the container root, which is the Docker overlay — the same
    # storage the sandbox containers write their package installs to, and a
    # different device from the workspace volume. Checking only ``data_dir``
    # made the gate blind to the failure it exists to prevent: on 2026-08-07 a
    # rebase failed three times with ENOSPC on every ``run_command`` while the
    # data volume reported 146 GB free, because root was at 80%.
    #
    # Paths that cannot be stat'ed are skipped, so listing one that does not
    # exist in a given deployment is harmless.
    disk_check_extra_paths: list[str] = Field(
        default_factory=lambda: ["/"],
        description=(
            "Additional filesystems the disk gate checks alongside data_dir "
            "(the container root backs the sandbox overlay)."
        ),
    )
    # LLM-provider model-outage parking — the "model unavailable" / 503 /
    # overloaded analogue of the network/disk parks. A transient model
    # outage (provider-side 503, model unavailable, overloaded) fails
    # every stage that touches that model identically; bounded retries
    # burn the budget and then block FATALLY, mixing an infrastructure
    # anomaly with genuine content-level failures. Park instead: the
    # ticket re-polls every model_outage_retry_seconds, retry budget
    # untouched, and resumes by itself once the model recovers. A
    # model_outage_max_parks ceiling prevents infinite parking when the
    # "outage" is actually permanent (bad model id, decommissioned model).
    model_outage_retry_seconds: int = Field(
        default=120,
        ge=1,
        description="Seconds between re-poll attempts during a detected LLM model outage.",
        json_schema_extra={"advanced": True},
    )
    model_outage_max_parks: int = Field(
        default=20,
        ge=1,
        description="Maximum consecutive model-outage parks before escalating to BLOCKED.",
        json_schema_extra={"advanced": True},
    )
    # Claude subscription quota exhaustion ("You've hit your session limit ·
    # resets 9:20am (UTC)"). The quota comes back by itself, so the ticket
    # PARKS (model-outage shaped, retry budget untouched) until the stated
    # reset — or claude_usage_exhausted_retry_seconds when the message has
    # no reset hint. Failing over to the keyed OpenRouter provider slot
    # instead is real money per token; it is opt-in via
    # provider_failover_enabled.
    claude_usage_exhausted_retry_seconds: int = Field(
        default=900,
        ge=60,
        description=(
            "Seconds to park a ticket after a Claude usage-exhaustion error "
            "whose message carries no reset time."
        ),
        json_schema_extra={"advanced": True},
    )
    # The agent's run_command refuses a pytest invocation with no target
    # (bare `pytest`, `pytest tests/`, `python -m pytest .`): the stage gate
    # runs the whole suite once the agent stops, and in-loop full runs were
    # 91 h/week live plus the trigger for provider prompt-cache expiry.
    run_command_refuse_full_suite: bool = Field(
        default=True,
        description=(
            "Refuse agent run_command pytest invocations that would run the "
            "whole suite (no path below the suite root, no -k/-m/--lf); the "
            "stage gate owns the full run."
        ),
        json_schema_extra={"advanced": True},
    )
    provider_failover_enabled: bool = Field(
        default=False,
        description=(
            "Enable automatic provider failover: when the default (Anthropic) "
            "provider fails or is exhausted, rerun the SAME capability level "
            "on the paid OpenRouter fallback slot for llmio's failover window "
            "instead of parking the ticket until the quota resets."
        ),
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the read-only exploration sub-agent the
    # coordinator uses instead of reading the repo into its own context.
    # Per-call cap for the domain-expert consultation sub-agent the
    # coordinator uses when it needs domain-specific advice.
    consult_request_limit: int = Field(
        default=15,
        ge=1,
        description="Per-call request cap for the domain-expert consultation sub-agent.",
        json_schema_extra={"advanced": True},
    )
    explore_request_limit: int = Field(
        default=100,
        ge=1,
        description="Per-call request cap for the exploration sub-agent.",
        json_schema_extra={"advanced": True},
    )
    explore_max_tokens: int = Field(
        default=4096,
        ge=1,
        description="Maximum output tokens for the exploration sub-agent.",
        json_schema_extra={"advanced": True},
    )
    explore_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        description="Wall-clock timeout (seconds) for a single explore sub-agent call.",
        json_schema_extra={"advanced": True},
    )
    # The scout runs at the cheap level (haiku on the Claude subscription,
    # ~6.6 s median): it spends quota, not cash.  Claude-backed levels run
    # the scout through the SDK tool loop, so ``explore_request_limit``
    # only bounds the OpenRouter fallback slot there.
    explore_model_level: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Capability level for the exploration sub-agent "
            "(1 = haiku on the Claude subscription)."
        ),
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the refine agent's tool loop. The refine agent
    # delegates deep search to the cheap ``explore`` sub-agent (which
    # has its own 100-call budget), so the top-level refine loop should
    # rarely exceed a few dozen tool calls.  80 sits above the old
    # implicit pydantic-ai default of 50 — intentionally, because broad
    # scaffolding tickets (forge integration,
    # agent-definition build-out) empirically need more top-level calls
    # even with good delegation (refine runs saturated 40, then 60 —
    # ticket 5353 — despite the delegate-to-explore prompt bias);
    # per-run cost is negligible (~$0.03–0.09), and the ticket-level
    # spend cap is the real backstop.
    # Note: ``review_request_limit`` is also 80 (bumped from 40 — it has
    # no explore sub-agent and was saturating on test-heavy diffs; see its
    # field comment and ticket bc6d).
    refine_request_limit: int = Field(
        default=80,
        ge=1,
        description="Per-call request cap for the refine agent's tool loop.",
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for non-escalated (simple/sonnet) refine runs.
    # Lower than the main cap (80) because simple tickets need fewer
    # tool calls — the explore/parallel_explore sub-agents are gated off.
    refine_request_limit_simple: int = Field(
        default=40,
        ge=1,
        description="Per-call request cap for non-escalated (simple/sonnet) refine runs.",
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the dedup check — the agent reads candidate
    # ticket bodies to verify matches, so allow a slightly larger
    # budget than a naive single-call (bumped from 4 after the agent
    # exhausted its budget on narrow read_file slices).
    dedup_request_limit: int = Field(
        default=12,
        ge=1,
        description="Per-call request cap for the pre-refine dedup check.",
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the obsolescence gate — the agent reads a few
    # cited files to verify the gap, so allow a slightly larger budget
    # than the dedup check.
    obsolescence_request_limit: int = Field(
        default=6,
        ge=1,
        description="Per-call request cap for the obsolescence gate.",
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the periodic audit agent's tool loop. The audit
    # agent does broad work (license scan, pip-audit, coverage
    # introspection) and can saturate 50 calls on a genuine run —
    # 80 gives headroom; per-run cost ~$0.29 stays well under the
    # per-ticket $ backstop.
    audit_request_limit: int = Field(
        default=80,
        ge=1,
        description="Per-call request cap for the periodic audit agent's tool loop.",
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the docstring-coverage agent's tool loop. The
    # docstring-coverage agent does broad work (explore storms scanning
    # the full repo for docstring gaps) and can saturate the pydantic-ai
    # default.  80 matches the test-gap agent's budget for a similar
    # broad-scan workload.
    docstring_coverage_request_limit: int = Field(
        default=80,
        ge=1,
        description="Per-call request cap for the docstring-coverage agent's tool loop.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on total tool calls per docstring_coverage trace. 100 tool
    # calls is far beyond what any legitimate docstring scan requires —
    # only clearly broken runs are terminated.
    docstring_coverage_max_tool_calls: int = Field(
        default=100,
        ge=1,
        description="Hard cap on total tool calls per docstring_coverage trace.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on tool-call errors before auto-termination. A healthy
    # inspection should have near-zero errors; 20 indicates a broken
    # execution loop.
    docstring_coverage_max_errors: int = Field(
        default=20,
        ge=0,
        description="Hard cap on tool-call errors before auto-termination of docstring_coverage agent.",
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the test-gap agent's tool loop. The test-gap
    # agent does broad work (explore storms scanning the full repo for
    # test-coverage gaps) and can saturate the pydantic-ai default of
    # 50 calls on a genuine run — 80 gives headroom matching the audit
    # agent's budget for a similar broad-scan workload.
    test_gap_request_limit: int = Field(
        default=80,
        ge=1,
        description="Per-call request cap for the test-gap agent's tool loop.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on total tool calls per test_gap trace. 100 tool calls
    # is far beyond what any legitimate test-coverage scan requires —
    # only clearly broken runs are terminated.
    test_gap_max_tool_calls: int = Field(
        default=100,
        ge=1,
        description="Hard cap on total tool calls per test_gap trace.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on tool-call errors before auto-termination. A healthy
    # inspection should have near-zero errors; 20 indicates a broken
    # execution loop.
    test_gap_max_errors: int = Field(
        default=20,
        ge=0,
        description="Hard cap on tool-call errors before auto-termination of test_gap agent.",
        json_schema_extra={"advanced": True},
    )
    # Per-call cap for the module-size agent's tool loop.  The module-size
    # agent does a bounded file-count scan and is lighter than audit/health;
    # 60 requests gives comfortable headroom for a typical repo.
    module_size_request_limit: int = Field(
        default=60,
        ge=1,
        description="Per-call request cap for the module-size agent's tool loop.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on total tool calls per module_size trace.
    module_size_max_tool_calls: int = Field(
        default=80,
        ge=1,
        description="Hard cap on total tool calls per module_size trace.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on tool-call errors before auto-termination.
    module_size_max_errors: int = Field(
        default=20,
        ge=0,
        description="Hard cap on tool-call errors before auto-termination of module_size agent.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on total tool calls per refine trace. A hard ceiling above
    # any legitimate refine run (the request cap is 80; 120 tool calls is a
    # generous headroom that still terminates the 100+-call broken loops).
    refine_max_tool_calls: int = Field(
        default=120,
        ge=1,
        description="Hard cap on total tool calls per refine trace.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on tool-call errors before auto-termination. Matches the
    # test_gap/trace_inspector default; a healthy refine has near-zero
    # tool errors.
    refine_max_errors: int = Field(
        default=20,
        ge=0,
        description="Hard cap on tool-call errors before auto-termination of refine agent.",
        json_schema_extra={"advanced": True},
    )
    # Dynamic request-limit multiplier for large/complex specs.
    # When the draft exceeds refine_dynamic_limit_spec_chars (default
    # 3000) or the scope-triage agent's own budget was over 60% of
    # the refine limit, the effective request_limit is multiplied by
    # this factor (with a floor of refine_dynamic_limit_min).
    refine_dynamic_limit_multiplier: float = Field(
        default=1.5,
        gt=1.0,
        description="Multiplier for dynamic request-limit on large/complex specs.",
        json_schema_extra={"advanced": True},
    )
    refine_dynamic_limit_min: int = Field(
        default=12,
        ge=1,
        description="Floor for dynamic request-limit on large/complex specs.",
        json_schema_extra={"advanced": True},
    )
    refine_dynamic_limit_spec_chars: int = Field(
        default=3000,
        ge=1,
        description="Character threshold above which the dynamic request-limit multiplier activates.",
        json_schema_extra={"advanced": True},
    )
    # Emit a warning when the refine agent consumes more than this
    # fraction (0.0–1.0) of its request_limit, so near-exhaustion
    # patterns are observable even when the run doesn't crash.
    refine_usage_warning_threshold: float = Field(
        default=0.8,
        gt=0.0,
        le=1.0,
        description="Fraction (0.0–1.0) of request_limit at which a near-exhaustion warning is emitted.",
        json_schema_extra={"advanced": True},
    )
    doc_request_limit: int = Field(
        default=32,
        description="Per-call request cap for the document agent.",
        json_schema_extra={"advanced": True},
    )
    doc_classifier_request_limit: int = Field(
        default=3,
        description="Per-call request cap for the cheap doc-classifier gate.",
        json_schema_extra={"advanced": True},
    )
    # Caps the git diff fed to the cheap doc-classifier gate. Truncation
    # is safe here: the classifier is conservatively biased toward
    # user_facing=True, so a truncated diff at worst loses signal and
    # routes to the full doc agent — the harmless direction. The full
    # doc agent still receives the untruncated diff.
    doc_classifier_diff_max_chars: int = Field(
        default=6000,
        description="Character cap on the git diff fed to the doc-classifier gate.",
        json_schema_extra={"advanced": True},
    )
    # Maximum characters of the memory ledger to load per agent pass.
    # When the file exceeds this, the oldest entries are dropped from the
    # loaded view; persist_memory also applies the cap on write when
    # max_chars is passed. Applies to all memory ledgers (refine, audit,
    # health, agent-check, etc.).
    max_memory_chars: int = Field(
        default=8000,
        ge=0,
        description="Maximum characters of the memory ledger to load per agent pass. 0 disables capping.",
        json_schema_extra={"advanced": True},
    )
    # Maximum characters of the retrospect stage's history + comments
    # logs fed to the agent. These are chronological, so the most-recent
    # tail is kept and older lines dropped. 0 disables capping.
    retrospect_log_max_chars: int = Field(
        default=12000,
        ge=0,
        description="Maximum characters of history+comments logs fed to the retrospect agent. 0 disables.",
        json_schema_extra={"advanced": True},
    )
    # Max number of entries retained in AGENT_CANDIDATES.md (the per-board
    # append-only queue of proposed AGENT.md rule additions). Pending
    # entries are always kept; resolved (validated/rejected) entries are
    # pruned oldest-first to honor this cap. 0 disables pruning.
    retrospect_candidates_max_entries: int = Field(
        default=100,
        ge=0,
        description="Max entries retained in AGENT_CANDIDATES.md. 0 disables pruning.",
        json_schema_extra={"advanced": True},
    )
    # How many days back closed tickets are considered as duplicate
    # candidates by the pre-refine dedup check.
    dedup_lookback_days: int = Field(
        default=7,
        description="Days back closed tickets are considered as duplicate candidates.",
        json_schema_extra={"advanced": True},
    )
    # Maximum number of candidates to pass to the dedup LLM after
    # similarity-based pre-filtering.  Caps the token budget regardless
    # of repo size.  ≥ 1 enforced by validator.
    dedup_max_candidates: int = Field(
        default=8,
        ge=1,
        description="Maximum candidate tickets passed to the dedup LLM after similarity pre-filtering.",
        json_schema_extra={"advanced": True},
    )
    # When True (default), the pre-refine dedup LLM call is skipped
    # entirely when the draft shares zero meaningful token overlap with
    # every candidate (title+body) — the common "clearly unrelated"
    # case.  Saves 100% of the call cost for genuine non-duplicates.
    dedup_skip_on_no_overlap: bool = Field(
        default=True,
        description="When true, skip the dedup LLM call when the draft shares zero token overlap with all candidates.",
        json_schema_extra={"advanced": True},
    )
    # Caps each candidate body fed to the dedup prompt (mirrors
    # doc_classifier_diff_max_chars). Generous by default so it only
    # clips pathologically long specs; ≤ 0 disables truncation.
    dedup_candidate_body_max_chars: int = Field(
        default=4000,
        description="Character cap on each candidate body fed to the dedup prompt.",
        json_schema_extra={"advanced": True},
    )
    # When True, the ingest path runs a cheap scope classifier on every
    # genuinely-new (non-duplicate) report and, when it clearly bundles
    # several independent deliverables, promotes it to an epic and
    # invokes epic-breakdown to spawn dependency-ordered child tickets.
    auto_epic_enabled: bool = Field(
        default=True,
        description="When true, ingest promotes clearly multi-concern reports to an auto-decomposed epic.",
        json_schema_extra={"advanced": True},
    )
    # Minimum classifier confidence required to promote an ingested
    # report to an epic. Borderline reports (below this threshold) stay
    # single tasks — the conservative default avoids fragmenting
    # genuinely small tickets.
    auto_epic_min_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum scope-classifier confidence required to auto-promote an ingested report to an epic.",
        json_schema_extra={"advanced": True},
    )
    # Local-dev default: ``.data`` — the same path the docker-compose
    # volume mounts at /data, so host CLI invocations and the container
    # share state instead of leaking a separate sibling tree. The
    # Dockerfile sets MILL_DATA_DIR=/data explicitly so the container
    # always uses the absolute path. Tests override via tmp_path.
    data_dir: Path = Field(
        default=Path(".data"),
        description="Local data directory. In container, always /data.",
    )

    # Default repo ID for legacy tickets that lack a board_id.
    # Set in config/config.json.  When empty (default), accessing
    # a legacy ticket without a board_id raises an error telling the
    # operator to configure this.
    default_repo_id: str = Field(
        default="",
        description="Default repo ID for legacy tickets lacking a board_id.",
    )

    # --- management-plane service ---
    api_host: str = Field(
        default="0.0.0.0",  # nosec B104 — config default, not a bind call; management API is localhost-restricted
        description="Management API listen host.",
    )
    api_port: int = Field(
        default=8077,
        description="Management API listen port.",
    )
    # Base URL the CLI client talks to.
    api_url: str = Field(
        default="http://127.0.0.1:8077",
        pattern=r"^https?://",
        description="Base URL the CLI client uses to reach the management API.",
    )

    # --- forge delivery (only used by the deliver stage) ---
    forge_kind: Literal["github", "gitlab", "none", "auto"] = Field(
        default="none",
        validation_alias=AliasChoices("forge_kind", "FORGE_KIND"),
        description="Forge backend: github, gitlab, none, or auto (detected from remote URL).",
    )
    forge_remote_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("forge_remote_url", "FORGE_REMOTE_URL"),
        description="Git remote URL of the forge repository.",
    )
    forge_target_branch: str = Field(
        default="main",
        validation_alias=AliasChoices("forge_target_branch", "FORGE_TARGET_BRANCH"),
        description="Default target branch for PRs.",
    )
    # token  = use forge_token from secrets block (PAT) directly.
    # app    = mint a short-lived GitHub App installation token so the
    #          bot identity (<app-slug>[bot]) authors the PR.
    forge_auth: Literal["token", "app"] = Field(
        default="token",
        validation_alias=AliasChoices("forge_auth", "FORGE_AUTH"),
        description="Forge authentication method: token (PAT) or app (GitHub App installation token).",
    )
    # GitHub API base (override for GitHub Enterprise).
    github_api_url: str = Field(
        default="https://api.github.com",
        pattern=r"^https?://",
        description="GitHub API base URL. Override for GitHub Enterprise.",
    )
    # GitLab API base (override for self-hosted GitLab instances).
    gitlab_api_url: str = Field(
        default="https://gitlab.com/api/v4",
        pattern=r"^https?://",
        description="GitLab API base URL. Override for self-hosted GitLab instances.",
    )

    # --- implement stage ---
    # Command run to verify the implementation; empty string skips the
    # test gate. Failures feed back into the bounded fix loop.
    # Global fallback for the test gate command. Empty by default —
    # per-repo `test_command` in repos config is the authoritative source.
    # When both are empty, the test gate short-circuits to PASS
    # ("no test gate configured"). MILL_TEST_COMMAND can override for
    # single-repo / legacy setups.
    test_command: str = Field(
        default="",
        description="Global fallback test command. Per-repo test_command in repos config takes precedence.",
    )
    # Global fallback for the path-scoped smoke gate command (run after
    # unit tests pass). Empty by default — the per-repo
    # `.robotsix-mill/config.json` `smoke_command` wins when set, this is
    # the fleet-wide fallback, and empty everywhere means no smoke gate
    # (short-circuits to PASS). MILL_SMOKE_COMMAND can override.
    # Path-scoping (`smoke_paths`) is inherently per-repo and lives only
    # in `.robotsix-mill/config.json`; there is no global counterpart.
    smoke_command: str = Field(
        default="",
        description="Global fallback smoke test command. Per-repo smoke_command in .robotsix-mill/config.json takes precedence.",
    )
    branch_prefix: str = Field(
        default="mill/",
        description="Prefix for per-ticket branch names.",
    )
    # Wall-clock cap (seconds) for the agent's shell tool and the test
    # command, so a hung command can't stall a worker forever.
    command_timeout: int = Field(
        default=1800,
        gt=0,
        description="Wall-clock cap (seconds) for shell tool and test command execution.",
        json_schema_extra={"advanced": True},
    )
    # Safety net: if a ticket re-enters the *same* model-driven stage
    # this many times without ever progressing (e.g. its run keeps being
    # interrupted, or a stage churns), the worker escalates it to BLOCKED
    # + notifies instead of silently re-billing the LLM forever. Poll
    # stages (merge/deliver) are exempt — human_mr_approval legitimately waits.
    max_stuck_cycles: int = Field(
        default=3,
        ge=0,
        description="Maximum re-entries to the same stage without progress before escalating to BLOCKED.",
        json_schema_extra={"advanced": True},
    )
    # --- per-ticket runaway budgets: OFF by default -------------------------
    #
    # These three cap a single ticket's spend, trace count, and marginal
    # OpenRouter cost, blocking it when it goes over. All default to disabled.
    #
    # They were on by default as a hedge against a model that burns tokens
    # unpredictably. Measured against real fleet behaviour that hedge cost far
    # more than it saved: the models actually in use consume predictably, so
    # the caps almost never fired on a genuine runaway — they fired on ordinary
    # long work. On 2026-08-06 the trace cap alone had 20 tickets BLOCKED at
    # **$0.00** of recorded OpenRouter spend, because a full pipeline pass with
    # retries legitimately exceeds a per-ticket trace count.
    #
    # A per-TICKET budget is also the wrong unit for the thing being guarded
    # against. A model that starts consuming erratically is a property of the
    # model, not of whichever ticket happened to be running — so a per-ticket
    # cap punishes the unlucky ticket while the real problem continues on the
    # next one. Cost is better watched fleet-wide (robotsix-cost-monitor), where
    # a drift shows up as a trend rather than as an arbitrary per-ticket cliff.
    #
    # The mechanism is kept, not deleted: if a future model does start using
    # tokens erratically, set a non-zero value here and the cap is live again.
    # `max_turns` and the per-stage wall-clock timeout remain on as the real
    # runaway backstops — both bound work without pretending to price it.
    max_spend_usd_per_ticket: float = Field(
        default=0.0,
        description="Dollar-cap safety net: cumulative LLM spend per ticket before blocking. 0.0 (default) disables.",
    )
    max_traces_per_ticket: int = Field(
        default=0,
        ge=0,
        description="Maximum Langfuse traces per ticket. 0 (default) disables.",
        json_schema_extra={"advanced": True},
    )
    max_openrouter_marginal_usd_per_ticket: float = Field(
        default=0.0,
        ge=0.0,
        description="Maximum OpenRouter marginal cost per ticket. 0.0 (default) disables.",
    )
    # Per-stage wall-clock timeout (seconds).  A stage that exceeds this
    # limit is escalated to BLOCKED, freeing the worker slot.  ≤ 0
    # disables the timeout entirely.  2400 s (40 min) comfortably
    # exceeds worst-case LLM latency (~190 s per call) and multiple
    # shell-command runs while still catching a true hang.
    stage_timeout_seconds: int = Field(
        default=2400,
        description="Per-stage wall-clock timeout (seconds). 0 disables.",
        json_schema_extra={"advanced": True},
    )
    # Per-stage timeout overrides (JSON dict via env var, e.g.
    # MILL_STAGE_TIMEOUT_OVERRIDES='{"merge":0,"refine":1200}').
    # Keys are stage names; values are seconds.  Falls back to
    # stage_timeout_seconds when a stage isn't listed.  A value of 0
    # disables the timeout for that stage.
    #
    # Built-in default: refine caps at 900 s (15 min).  A sampled
    # legitimate refine run on model_level 2 (Claude SDK / Opus)
    # clocked 736 s (~12 min); 900 s leaves headroom while still
    # catching multi-hour runaway refine traces.  Operators can
    # override or disable (value 0) via the env var / JSON key.
    # Supplying your own dict REPLACES the built-in — re-include
    # a "refine" entry if you still want a cap.
    stage_timeout_overrides: dict[str, int] = Field(
        default_factory=lambda: {"refine": 900},
        description="Per-stage timeout overrides (dict). Keys are stage names, values are seconds.",
        json_schema_extra={"advanced": True},
    )
    # Maximum seconds to wait for in-flight periodic-agent passes
    # (survey, audit, health, …) to finish before tearing the worker
    # down on container shutdown. The mill's docker-compose ships a
    # matching ``stop_grace_period`` so docker won't SIGKILL before
    # the wait completes; if you change one, change the other.
    # 0 → wait forever; set <= the docker grace period to bound the
    # final wait.
    shutdown_grace_seconds: int = Field(
        default=1800,
        description="Maximum seconds to wait for in-flight periodic passes before teardown.",
        json_schema_extra={"advanced": True},
    )

    # --- command sandbox (always a disposable container; no local mode) ---
    # Image the sandbox runs commands in — must contain the toolchain
    # MILL_TEST_COMMAND needs.
    # The code default (python:3.14-slim) is a lightweight image for
    # local development. Production JSON config overrides to
    # robotsix/mill-sandbox:latest, which includes uv and toolchain.
    sandbox_image: str = Field(
        default="python:3.14-slim",
        description="Docker image for the command sandbox.",
    )
    sandbox_memory: str = Field(
        default="2g",
        description="Memory limit for sandbox containers.",
        json_schema_extra={"advanced": True},
    )
    sandbox_pids_limit: int = Field(
        default=512,
        description="PID limit for sandbox containers.",
        json_schema_extra={"advanced": True},
    )
    # Sandboxes cap memory and PIDs but historically nothing bounded CPU, so
    # ``max_global_concurrency`` sandboxes could each take as many cores as
    # their test command's parallelism allowed — the cap bounded the container
    # COUNT while host load stayed unbounded. A quota here makes the two
    # proportional (N sandboxes ≤ N × this), which is what makes raising the
    # concurrency cap safe rather than merely optimistic.
    #
    # 0 disables the limit — the previous behaviour, and the right default
    # since the useful value depends on the host's core count. Set it when
    # raising max_global_concurrency past roughly half your cores.
    sandbox_cpus: float = Field(
        default=0.0,
        ge=0,
        description=(
            "CPU quota per sandbox container, in cores (e.g. 0.7); "
            "0 disables the limit."
        ),
        json_schema_extra={"advanced": True},
    )
    # How long a caller waits for a free sandbox slot before giving up.
    # Generous by design: the cap is a memory guard, and a queued periodic
    # pass should wait behind a long test run rather than fail. Bounded
    # anyway so a leaked slot surfaces as an error instead of a hang.
    sandbox_slot_timeout: int = Field(
        default=1800,
        ge=1,
        description=(
            "Seconds to wait for a free sandbox slot before failing "
            "(ceiling = max_global_concurrency)."
        ),
        json_schema_extra={"advanced": True},
    )
    sandbox_readonly: bool = Field(
        default=True,
        description="When true, sandbox containers run with read-only root filesystem.",
        json_schema_extra={"advanced": True},
    )
    # The sandbox's /tmp is a tmpfs — RAM charged to sandbox_memory. Bound it
    # so a runaway write fails with ENOSPC instead of OOM-killing the command;
    # an unsized Docker tmpfs defaults to half the HOST's RAM.
    sandbox_tmpfs_size: str = Field(
        default="512m",
        description="Size limit for the sandbox's /tmp tmpfs (RAM-backed).",
        json_schema_extra={"advanced": True},
    )
    # Share one disk-backed uv/pip cache across sandboxes instead of letting
    # each one fill its RAM-backed /tmp with the project's dependency tree.
    # See sandbox._cache_mount for the cross-sandbox visibility trade-off.
    sandbox_package_cache: bool = Field(
        default=True,
        description=(
            "Mount a shared disk-backed uv/pip cache into sandboxes "
            "(keeps package downloads out of the RAM-backed /tmp)."
        ),
        json_schema_extra={"advanced": True},
    )
    # The cache lives on the data volume, which has run out of space before,
    # so the sandbox-reaper pass drops it once it exceeds this. Pure cache —
    # the next sandbox refills it.
    sandbox_package_cache_max_mb: int = Field(
        default=4096,
        ge=0,
        description=(
            "Size budget for the shared sandbox package cache, in MiB; "
            "0 disables pruning."
        ),
        json_schema_extra={"advanced": True},
    )
    # Docker network sandbox containers connect to. The network must be
    # internal (no direct internet) with a filtering proxy attached —
    # sandbox commands reach PyPI/GitHub ONLY through the proxy.
    sandbox_network: str = Field(
        default="mill-sandbox-net",
        description="Docker network sandbox containers connect to.",
        json_schema_extra={"advanced": True},
    )
    # URL of the egress proxy. Sandbox containers receive HTTP_PROXY,
    # HTTPS_PROXY, http_proxy, and https_proxy set to this value.
    # Set to empty string to disable (restores --network none behavior).
    sandbox_proxy_url: str = Field(
        default="http://sandbox-proxy:8888",
        description="Egress proxy URL for sandbox containers. Set to empty string to disable.",
    )
    # What the sandbox sibling containers mount at MILL_DATA_DIR. The
    # daemon resolves -v on the host, so this must be a named volume OR
    # the host path of a bind mount. data_volume is the fallback name;
    # sandbox_data_mount (host path) overrides it for bind-mounted ./.data.
    data_volume: str = Field(
        default="mill_data",
        description="Named Docker volume for mill data.",
    )
    sandbox_data_mount: str | None = Field(
        default=None,
        description="Host path for bind-mounted .data directory. Overrides data_volume.",
    )
    # URL of the deploy server's management API (e.g.
    # ``http://deploy-server:8080``).  When set, the implement stage
    # checks ``GET /services/mill`` for ``running_digest`` vs
    # ``latest_digest`` before burning an attempt on a ticket that may
    # have been blocked by a code bug already fixed in a newer image.
    # ``None`` → freshness gate disabled (safe default when no deploy
    # server is available).
    deploy_api_url: str | None = Field(
        default=None,
        pattern=r"^https?://",
        description="Deploy server management API URL. When set, used to check worker image freshness before resuming blocked tickets.",
    )

    # --- outbound event subscribers ---
    # List of HTTP endpoints that receive a JSON POST on every ticket
    # state transition.  Delivery is best-effort and asynchronous (a
    # dead subscriber does not block or slow down state transitions).
    subscriber_urls: list[str] = Field(
        default_factory=list,
        description="List of subscriber endpoint URLs that receive ticket state-change events via HTTP POST.",
    )
    # Optional shared secret sent as an ``X-Mill-Event-Secret`` header
    # on every outbound event POST.  Empty → header omitted.
    subscriber_shared_secret: SecretStr | None = Field(
        default=None,
        description="Optional shared secret for the X-Mill-Event-Secret header on outbound events.",
    )
