"""Runtime configuration, sourced from environment, .env, and secrets.env.

Conventional keys (``OPENROUTER_API_KEY``, ``LANGFUSE_*``) are
unprefixed to match the reference projects; mill-specific knobs use the
``MILL_`` / ``FORGE_`` prefixes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[".env", "secrets.env"], env_file_encoding="utf-8", extra="ignore"
    )

    # --- core ---
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    # Per-agent models. Each role gets its own model (env-overridable):
    #  - `model`        : the COORDINATOR (capable). Explores via the
    #                     cheap explore sub-agent, drafts a plan,
    #                     delegates coding to the implement sub-agent
    #                     with precise instructions, gets distilled test
    #                     feedback, and loops. Keeps a short history by
    #                     never holding raw files/logs itself.
    #    (it reads + edits the repo itself; uses MILL_MODEL.)
    #  - explore_model  : the scout sub-agent — returns concise
    #                     pointers, never whole files (cheap).
    #  - web_research_model : web lookups (cheap).
    #  - test_model     : distills test failures into actionable
    #                     feedback (cheap).
    #  - refine_model   : spec authoring (capable; may web_research).
    #  - answer_model   : investigative analyst (capable; web + repo +
    #                     Langfuse tools).
    #  - retrospect_model / audit_model : structured analysis (capable).
    # Transient 429/5xx/timeouts on any of these are absorbed by the
    # bounded retry+backoff (see transient_* below).
    model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_MODEL"
    )
    # NOTE: cheap candidates (deepseek-v4-flash) for explore/test/
    # web_research are deferred — all default to the capable model for
    # now (best performance); switch per-agent later for cost leverage.
    explore_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_EXPLORE_MODEL"
    )
    test_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_TEST_MODEL"
    )
    refine_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_REFINE_MODEL"
    )
    answer_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_ANSWER_MODEL"
    )
    retrospect_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_RETROSPECT_MODEL"
    )
    audit_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_AUDIT_MODEL"
    )
    # Model for the pre-refine dedup/already-done check — a cheap call
    # that short-circuits duplicate drafts before the expensive refiner.
    dedup_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_DEDUP_MODEL"
    )
    # Model for the pre-refine triage pass — a single cheap call that
    # decides whether the draft needs refinement at all.  Must be a
    # fast, inexpensive model; classification is the only task.
    triage_model: str = Field(
        default="openai/gpt-4o-mini", alias="MILL_TRIAGE_MODEL"
    )
    # Per-call request caps (bound each role's loop). Sized for slow
    # deepseek-v4-pro + complex tickets: a medium ticket (53de) used
    # ~49 implement calls, so 200 leaves generous headroom; raising it
    # only matters if a ticket genuinely needs more steps.
    coordinator_request_limit: int = Field(
        default=200, alias="MILL_COORDINATOR_REQUEST_LIMIT"
    )
    test_request_limit: int = Field(
        default=8, alias="MILL_TEST_REQUEST_LIMIT"
    )
    # Max implement→test fix iterations before BLOCKing. Complex
    # tickets may need several correction rounds.
    max_fix_iterations: int = Field(
        default=8, alias="MILL_MAX_FIX_ITERATIONS"
    )
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
    model_request_timeout: float = Field(
        default=900.0, alias="MILL_MODEL_REQUEST_TIMEOUT"
    )
    # How many tickets the worker pool processes in parallel. One
    # ticket's stages still run sequentially within its consumer; this
    # is cross-ticket concurrency. Each in-flight implement may spawn a
    # sandbox container and hit the model API, so keep it modest.
    max_concurrency: int = Field(
        default=4, alias="MILL_MAX_CONCURRENCY"
    )
    transient_retries: int = Field(
        default=4, alias="MILL_TRANSIENT_RETRIES"
    )
    transient_backoff_base: float = Field(
        default=2.0, alias="MILL_TRANSIENT_BACKOFF_BASE"
    )
    transient_backoff_cap: float = Field(
        default=30.0, alias="MILL_TRANSIENT_BACKOFF_CAP"
    )
    # Backoff for UsageLimitExceeded (pydantic-ai budget cap).  These
    # are longer than transient backoff because OpenRouter/provider
    # rate-limit windows are typically ~60s.  When
    # rate_limit_fallback_model is set, call_with_retry switches to
    # that model after rate_limit_fallback_retries consecutive
    # UsageLimitExceeded failures.
    rate_limit_backoff_base: float = Field(
        default=30.0, alias="MILL_RATE_LIMIT_BACKOFF_BASE"
    )
    rate_limit_backoff_cap: float = Field(
        default=120.0, alias="MILL_RATE_LIMIT_BACKOFF_CAP"
    )
    rate_limit_fallback_retries: int = Field(
        default=3, alias="MILL_RATE_LIMIT_FALLBACK_RETRIES"
    )
    rate_limit_fallback_model: str = Field(
        default="", alias="MILL_RATE_LIMIT_FALLBACK_MODEL"
    )
    # Per-call cap for the read-only exploration sub-agent the
    # coordinator uses instead of reading the repo into its own context.
    explore_request_limit: int = Field(
        default=20, alias="MILL_EXPLORE_REQUEST_LIMIT"
    )
    # Per-call cap for the dedup check — one cheap call, so keep it tight.
    dedup_request_limit: int = Field(
        default=4, alias="MILL_DEDUP_REQUEST_LIMIT"
    )
    # Maximum characters of the memory ledger to load per agent pass.
    # When the file exceeds this, the oldest entries are dropped (read-side
    # only — persist_memory is unchanged).  Applies to all memory ledgers
    # (refine, audit, health, agent-check, etc.).
    max_memory_chars: int = Field(
        default=8000, alias="MILL_MAX_MEMORY_CHARS"
    )
    # How many days back closed tickets are considered as duplicate
    # candidates by the pre-refine dedup check.
    dedup_lookback_days: int = Field(
        default=30, alias="MILL_DEDUP_LOOKBACK_DAYS"
    )
    # How many recent commits on the forge target branch to inspect for
    # "already implemented" by the pre-refine dedup check.
    dedup_lookback_commits: int = Field(
        default=20, alias="MILL_DEDUP_LOOKBACK_COMMITS"
    )
    # Local-dev default: a repo-local, gitignored dir. The Dockerfile
    # sets MILL_DATA_DIR=/data explicitly, so the container is unaffected.
    data_dir: Path = Field(default=Path(".mill-data"), alias="MILL_DATA_DIR")

    # --- management-plane service ---
    api_host: str = Field(default="127.0.0.1", alias="MILL_API_HOST")
    api_port: int = Field(default=8077, alias="MILL_API_PORT")
    # Base URL the CLI client talks to.
    api_url: str = Field(default="http://127.0.0.1:8077", alias="MILL_API_URL")

    # --- forge delivery (only used by the deliver stage) ---
    forge_kind: Literal["github", "gitlab", "none"] = Field(
        default="none", alias="FORGE_KIND"
    )
    forge_remote_url: str | None = Field(default=None, alias="FORGE_REMOTE_URL")
    forge_token: str | None = Field(default=None, alias="FORGE_TOKEN")
    forge_target_branch: str = Field(default="main", alias="FORGE_TARGET_BRANCH")
    # token  = use FORGE_TOKEN (PAT) directly.
    # app    = mint a short-lived GitHub App installation token so the
    #          bot identity (<app-slug>[bot]) authors the PR.
    forge_auth: Literal["token", "app"] = Field(
        default="token", alias="FORGE_AUTH"
    )
    github_app_id: str | None = Field(default=None, alias="GITHUB_APP_ID")
    github_app_private_key: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY"
    )
    github_app_private_key_path: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )
    # GitHub API base (override for GitHub Enterprise).
    github_api_url: str = Field(
        default="https://api.github.com", alias="MILL_GITHUB_API_URL"
    )

    # --- implement stage ---
    # Command run to verify the implementation; empty string skips the
    # test gate. Failures feed back into the bounded fix loop.
    test_command: str = Field(default="pytest -q", alias="MILL_TEST_COMMAND")
    branch_prefix: str = Field(default="mill/", alias="MILL_BRANCH_PREFIX")
    # Wall-clock cap (seconds) for the agent's shell tool and the test
    # command, so a hung command can't stall a worker forever.
    command_timeout: int = Field(default=900, alias="MILL_COMMAND_TIMEOUT")
    # Safety net: if a ticket re-enters the *same* model-driven stage
    # this many times without ever progressing (e.g. its run keeps being
    # interrupted, or a stage churns), the worker escalates it to BLOCKED
    # + notifies instead of silently re-billing the LLM forever. Poll
    # stages (merge/deliver) are exempt — human_mr_approval legitimately waits.
    max_stuck_cycles: int = Field(default=3, alias="MILL_MAX_STUCK_CYCLES")
    # Dollar-cap safety net: if a ticket's cumulative Langfuse-traced
    # LLM spend exceeds this value (across all stages), the worker
    # escalates it to BLOCKED. 0.0 disables the cap entirely.
    max_spend_usd_per_ticket: float = Field(
        default=0.0, alias="MILL_MAX_SPEND_USD_PER_TICKET"
    )

    # --- command sandbox (always a disposable container; no local mode) ---
    # Image the sandbox runs commands in — must contain the toolchain
    # MILL_TEST_COMMAND needs.
    sandbox_image: str = Field(
        default="python:3.14-slim", alias="MILL_SANDBOX_IMAGE"
    )
    sandbox_memory: str = Field(default="2g", alias="MILL_SANDBOX_MEMORY")
    sandbox_pids_limit: int = Field(
        default=512, alias="MILL_SANDBOX_PIDS_LIMIT"
    )
    sandbox_readonly: bool = Field(
        default=True, alias="MILL_SANDBOX_READONLY"
    )
    # What the sandbox sibling containers mount at MILL_DATA_DIR. The
    # daemon resolves -v on the host, so this must be a named volume OR
    # the host path of a bind mount. data_volume is the fallback name;
    # sandbox_data_mount (host path) overrides it for bind-mounted ./.data.
    data_volume: str = Field(default="mill_data", alias="MILL_DATA_VOLUME")
    sandbox_data_mount: str | None = Field(
        default=None, alias="MILL_SANDBOX_DATA_MOUNT"
    )

    # --- agent web access (refine + implement) ---
    # Web search is delegated to a cheap, bounded SUB-agent: the main
    # (expensive) agent never carries OpenRouter's ":online" suffix, it
    # only gets a `web_research(query)` tool whose body runs this small
    # model — with ":online" + web_fetch — and returns just a concise
    # conclusion. This kills the per-request web-search surcharge on the
    # pricey model and keeps its context lean (conclusions, not pages).
    web_search: bool = Field(default=True, alias="MILL_WEB_SEARCH")
    web_research_model: str = Field(
        default="deepseek/deepseek-v4-pro",
        alias="MILL_WEB_RESEARCH_MODEL",
    )
    web_research_request_limit: int = Field(
        default=8, alias="MILL_WEB_RESEARCH_REQUEST_LIMIT"
    )
    # web_fetch runs in its OWN container: network ON, but NO repo/data
    # mount, non-root, read-only, fixed curl. Trade-off accepted: an
    # agent could encode data into a fetched URL. http(s) only.
    fetch_image: str = Field(
        default="curlimages/curl:8.17.0", alias="MILL_FETCH_IMAGE"
    )
    web_fetch_max_bytes: int = Field(
        default=2_000_000, alias="MILL_WEB_FETCH_MAX_BYTES"
    )
    web_fetch_timeout: int = Field(
        default=30, alias="MILL_WEB_FETCH_TIMEOUT"
    )
    # Directory of skill docs (skills/<name>/SKILL.md) injected into the
    # refine + implement agents' system prompt. Relative to CWD (/app in
    # the container, repo root locally).
    skills_dir: Path = Field(default=Path("skills"), alias="MILL_SKILLS_DIR")

    # --- human approval gate (refine -> implement) ---
    # When true (default), the refine stage transitions to
    # human_issue_approval instead of ready — a human must approve before
    # the implement stage kicks in. Set false for fully-autonomous mode.
    require_approval: bool = Field(
        default=True, alias="MILL_REQUIRE_APPROVAL"
    )

    # When true, a cheap conservative LLM call inspects the refined spec
    # after refinement.  If the change is "obviously safe" (cosmetic,
    # doc-only, single-file, no logic changes) the ticket skips the
    # human approval gate and goes straight to READY.  When false
    # (default), every gated ticket waits for a human click.
    auto_approve_enabled: bool = Field(
        default=False, alias="MILL_AUTO_APPROVE_ENABLED"
    )
    # Model for the auto-approve triage call — must be fast and cheap.
    auto_approve_model: str = Field(
        default="openai/gpt-4o-mini", alias="MILL_AUTO_APPROVE_MODEL"
    )

    # --- dual-model review gate (implement → deliver) ---
    # When true, the implement stage transitions to code_review instead of
    # deliverable. A dedicated review agent audits the diff blind before the
    # deliver stage pushes + opens the PR. Default False (opt-in).
    review_enabled: bool = Field(
        default=False, alias="MILL_REVIEW_ENABLED"
    )
    # When True (default), a cheap triage LLM call runs before the full
    # refine agent.  Drafts that are already precise, single-scoped, and
    # implementation-ready skip the full refine — saving cost & latency.
    # Set False to force full refine for all tickets without a deploy.
    refine_triage_enabled: bool = Field(
        default=True, alias="MILL_REFINE_TRIAGE_ENABLED"
    )
    # Model for the review agent. Defaults to the capable coordinator model.
    # Override to use a *different* model for a genuinely independent review
    # perspective (the dual-model benefit).
    review_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_REVIEW_MODEL"
    )

    # Model for the documentation agent. Defaults to the capable
    # coordinator model.
    doc_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_DOC_MODEL"
    )

    # --- retrospect stage (done -> reviewed) ---
    # When True, retrospect may file an improvement DRAFT. Until the
    # human-gate-after-refine exists, that draft auto-flows to done and
    # is retrospected again — set False to analyse without spawning.
    retrospect_spawn_drafts: bool = Field(
        default=True, alias="MILL_RETROSPECT_SPAWN_DRAFTS"
    )
    # How many retrospect runs between deep analyses. The deep analysis
    # gates a sub-agent per trace (`trace_inspect`) to inspect the full
    # observation tree for systematic issues the summary misses.
    retrospect_deep_analysis_frequency: int = Field(
        default=10, alias="MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY"
    )
    # Model for the trace inspector sub-agent — a dedicated cheap model
    # that inspects a single trace's full observation tree.
    trace_inspector_model: str = Field(
        default="deepseek/deepseek-v4-pro",
        alias="MILL_TRACE_INSPECTOR_MODEL",
    )
    # Memory ledger for the trace inspector. Used only by the manual
    # Deep Review surface (the route path) — retrospect's deep-analysis
    # `trace_inspect` tool calls run_trace_inspector without a memory
    # arg. Unset (default) derives <data_dir>/trace_inspector_memory.md.
    trace_inspector_memory_path: Path | None = Field(
        default=None, alias="MILL_TRACE_INSPECTOR_MEMORY_PATH"
    )
    # Path to the agent-maintained Markdown memory ledger.  Override to
    # pin a specific path; unset (default) derives <data_dir>/retrospect_memory.md.
    retrospect_memory_path: Path | None = Field(
        default=None, alias="MILL_RETROSPECT_MEMORY_PATH"
    )
    # human_mr_approval (PR open) re-check cadence. mill has no scheduler; this
    # timer exists only to observe the external merge event.
    merge_poll_seconds: int = Field(
        default=120, alias="MILL_MERGE_POLL_SECONDS"
    )
    # When true (default), the workspace's clone (repo/) is removed on
    # close to save disk space.
    prune_clone_on_close: bool = Field(
        default=True, alias="MILL_PRUNE_CLONE_ON_CLOSE"
    )

    # --- merge stage: auto-rebase of stale PRs ---
    # When a PR in human_mr_approval becomes conflicting (other PRs merged to
    # the target branch), the merge stage invokes the rebase agent to
    # resolve conflicts automatically.  This is the max number of
    # rebase attempts per ticket before escalating to BLOCKED.
    rebase_max_attempts: int = Field(
        default=5, alias="MILL_REBASE_MAX_ATTEMPTS"
    )

    # --- merge stage: auto-fix of failing remote CI ---
    # When a PR in human_mr_approval has failing CI checks, the merge stage
    # transitions to fixing_ci and invokes the ci-fix agent to resolve
    # the failures automatically.  This is the max number of ci-fix
    # attempts per ticket before escalating to BLOCKED.
    ci_fix_max_attempts: int = Field(
        default=2, alias="MILL_CI_FIX_MAX_ATTEMPTS"
    )

    # --- target-branch CI monitor ---
    # When True, the worker runs a periodic poll that watches the forge
    # target branch for completed workflow-run failures and files a
    # source="ci" draft for each new one. Default False (opt-in).
    ci_monitor_periodic: bool = Field(
        default=False, alias="MILL_CI_MONITOR_PERIODIC"
    )
    # Interval between CI monitor polls (seconds). Only used when
    # MILL_CI_MONITOR_PERIODIC=true.
    ci_monitor_interval_seconds: int = Field(
        default=3600, alias="MILL_CI_MONITOR_INTERVAL_SECONDS"
    )
    # Per-job log tail cap (bytes) when fetching workflow job logs for
    # CI-fix context and the CI monitor draft body.
    ci_log_max_bytes: int = Field(
        default=65536, alias="MILL_CI_LOG_MAX_BYTES"
    )

    @property
    def ci_monitor_memory_path(self) -> Path:
        """Resolved path to the CI monitor dedup state file."""
        return self.data_dir / "ci_monitor_state.json"

    # --- audit agent (meta-audit for quality/security coverage) ---
    # When True, the worker runs periodic audit passes at the configured
    # interval. Default False (opt-in).
    audit_periodic: bool = Field(
        default=False, alias="MILL_AUDIT_PERIODIC"
    )
    # Interval between periodic audit passes (seconds). Only used when
    # MILL_AUDIT_PERIODIC=true.
    audit_interval_seconds: int = Field(
        default=3600, alias="MILL_AUDIT_INTERVAL_SECONDS"
    )
    # Path to the audit agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/audit_memory.md.
    audit_memory_path: Path | None = Field(
        default=None, alias="MILL_AUDIT_MEMORY_PATH"
    )

    # --- trace-health check ---
    # When True, the worker runs periodic trace-health checks at the
    # configured interval. Default False (opt-in).
    trace_health_periodic: bool = Field(
        default=False, alias="MILL_TRACE_HEALTH_PERIODIC"
    )
    # Interval between automatic trace-health checks (seconds). Only
    # used when MILL_TRACE_HEALTH_PERIODIC=true. Enforced minimum 3600s
    # (1h) in the worker to avoid hammering Langfuse.
    trace_health_interval_seconds: int = Field(
        default=86400, alias="MILL_TRACE_HEALTH_INTERVAL_SECONDS"
    )

    # --- test-gap agent (dedicated test-coverage oversight) ---
    # Model for the test-gap agent. Defaults to the same capable model
    # as audit/health. Override with MILL_TEST_GAP_MODEL.
    test_gap_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_TEST_GAP_MODEL"
    )
    # When True, the worker runs periodic test-gap passes at the
    # configured interval. Default False (opt-in).
    test_gap_periodic: bool = Field(
        default=False, alias="MILL_TEST_GAP_PERIODIC"
    )
    # Interval between periodic test-gap passes (seconds). Only used
    # when MILL_TEST_GAP_PERIODIC=true.
    test_gap_interval_seconds: int = Field(
        default=86400, alias="MILL_TEST_GAP_INTERVAL_SECONDS"
    )
    # Path to the test-gap agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives
    # <data_dir>/test_gap_memory.md.
    test_gap_memory_path: Path | None = Field(
        default=None, alias="MILL_TEST_GAP_MEMORY_PATH"
    )

    # --- agent-check agent (agent-definition coherence) ---
    # Model for the agent-check meta-agent. Defaults to the same cheap
    # model as other read-only periodic agents. Override with
    # MILL_AGENT_CHECK_MODEL.
    agent_check_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_AGENT_CHECK_MODEL"
    )
    # Path to the agent-check agent's Markdown memory ledger. Override
    # to pin a specific path; unset (default) derives
    # <data_dir>/agent_check_memory.md.
    agent_check_memory_path: Path | None = Field(
        default=None, alias="MILL_AGENT_CHECK_MEMORY_PATH"
    )
    # Opt-in periodic agent-check pass. Defaults to False (off); flip
    # to true to schedule the pass every ``agent_check_interval_seconds``
    # in addition to the on-demand POST /agent-check and CLI.
    agent_check_periodic: bool = Field(
        default=False, alias="MILL_AGENT_CHECK_PERIODIC"
    )
    # Seconds between periodic agent-check passes when
    # MILL_AGENT_CHECK_PERIODIC=true. Minimum enforced at 60s in the
    # worker loop.
    agent_check_interval_seconds: int = Field(
        default=86400, alias="MILL_AGENT_CHECK_INTERVAL_SECONDS"
    )

    # --- health agent (codebase-health inspection) ---
    # Model for the health agent. Defaults to the same capable model as
    # audit. Override with MILL_HEALTH_MODEL.
    health_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_HEALTH_MODEL"
    )
    # When True, the worker runs periodic health passes at the
    # configured interval. Default False (opt-in).
    health_periodic: bool = Field(
        default=False, alias="MILL_HEALTH_PERIODIC"
    )
    # Interval between periodic health passes (seconds). Only used when
    # MILL_HEALTH_PERIODIC=true.
    health_interval_seconds: int = Field(
        default=86400, alias="MILL_HEALTH_INTERVAL_SECONDS"
    )
    # Path to the health agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/health_memory.md.
    health_memory_path: Path | None = Field(
        default=None, alias="MILL_HEALTH_MEMORY_PATH"
    )

    # --- survey agent (OSS project discovery) ---
    # Model for the survey agent. Defaults to the same capable model as
    # audit. Override with MILL_SURVEY_MODEL.
    survey_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_SURVEY_MODEL"
    )
    # Path to the survey agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/survey_memory.md.
    survey_memory_path: Path | None = Field(
        default=None, alias="MILL_SURVEY_MEMORY_PATH"
    )

    # --- action-agent memory paths ---
    # Path to the implement agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/implement_memory.md.
    implement_memory_path: Path | None = Field(
        default=None, alias="MILL_IMPLEMENT_MEMORY_PATH"
    )
    # Path to the refine agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/refine_memory.md.
    refine_memory_path: Path | None = Field(
        default=None, alias="MILL_REFINE_MEMORY_PATH"
    )
    # Path to the ci-fix agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/ci_fix_memory.md.
    ci_fix_memory_path: Path | None = Field(
        default=None, alias="MILL_CI_FIX_MEMORY_PATH"
    )
    # Path to the rebase agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/rebase_memory.md.
    rebase_memory_path: Path | None = Field(
        default=None, alias="MILL_REBASE_MEMORY_PATH"
    )

    # --- tracing (optional) ---
    langfuse_base_url: str | None = Field(default=None, alias="LANGFUSE_BASE_URL")
    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_project_id: str | None = Field(default=None, alias="LANGFUSE_PROJECT_ID")

    # --- notifications (optional) ---
    ntfy_url: str | None = Field(default=None, alias="NTFY_URL")
    ntfy_token: str | None = Field(default=None, alias="NTFY_TOKEN")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mill.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def tracing_enabled(self) -> bool:
        return bool(
            self.langfuse_base_url
            and self.langfuse_public_key
            and self.langfuse_secret_key
        )

    @property
    def retrospect_memory_file(self) -> Path:
        """Resolved path to the agent-maintained retrospect memory ledger."""
        if self.retrospect_memory_path is not None:
            return self.retrospect_memory_path
        return self.data_dir / "retrospect_memory.md"

    @property
    def trace_inspector_memory_file(self) -> Path:
        """Resolved path to the trace inspector's memory ledger."""
        if self.trace_inspector_memory_path is not None:
            return self.trace_inspector_memory_path
        return self.data_dir / "trace_inspector_memory.md"

    @property
    def audit_memory_file(self) -> Path:
        """Resolved path to the agent-maintained audit memory ledger."""
        if self.audit_memory_path is not None:
            return self.audit_memory_path
        return self.data_dir / "audit_memory.md"

    @property
    def agent_check_memory_file(self) -> Path:
        """Resolved path to the agent-maintained agent-check memory ledger."""
        if self.agent_check_memory_path is not None:
            return self.agent_check_memory_path
        return self.data_dir / "agent_check_memory.md"

    @property
    def health_memory_file(self) -> Path:
        """Resolved path to the agent-maintained health memory ledger."""
        if self.health_memory_path is not None:
            return self.health_memory_path
        return self.data_dir / "health_memory.md"

    @property
    def test_gap_memory_file(self) -> Path:
        """Resolved path to the test-gap agent's Markdown memory ledger."""
        if self.test_gap_memory_path is not None:
            return self.test_gap_memory_path
        return self.data_dir / "test_gap_memory.md"

    @property
    def survey_memory_file(self) -> Path:
        """Resolved path to the agent-maintained survey memory ledger."""
        if self.survey_memory_path is not None:
            return self.survey_memory_path
        return self.data_dir / "survey_memory.md"

    @property
    def implement_memory_file(self) -> Path:
        """Resolved path to the agent-maintained implement memory ledger."""
        if self.implement_memory_path is not None:
            return self.implement_memory_path
        return self.data_dir / "implement_memory.md"

    @property
    def refine_memory_file(self) -> Path:
        """Resolved path to the agent-maintained refine memory ledger."""
        if self.refine_memory_path is not None:
            return self.refine_memory_path
        return self.data_dir / "refine_memory.md"

    @property
    def ci_fix_memory_file(self) -> Path:
        """Resolved path to the agent-maintained ci-fix memory ledger."""
        if self.ci_fix_memory_path is not None:
            return self.ci_fix_memory_path
        return self.data_dir / "ci_fix_memory.md"

    @property
    def rebase_memory_file(self) -> Path:
        """Resolved path to the agent-maintained rebase memory ledger."""
        if self.rebase_memory_path is not None:
            return self.rebase_memory_path
        return self.data_dir / "rebase_memory.md"


def load_settings() -> Settings:
    return Settings()
