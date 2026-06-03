"""Runtime configuration, sourced from environment, .env, and secrets.env.

Conventional keys (``OPENROUTER_API_KEY``, ``LANGFUSE_*``) are
unprefixed to match the reference projects; mill-specific knobs use the
``MILL_`` / ``FORGE_`` prefixes.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class YamlSettingsSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that loads YAML config via the existing
    ``load_yaml_config()`` + ``flatten_yaml_config()`` pipeline.

    Called at ``Settings()`` construction time (not import time), so
    test monkeypatching of ``_DEFAULTS_FILE`` / ``_LOCAL_FILE`` /
    ``MILL_CONFIG_FILE`` works reliably.

    Returns an alias-keyed ``{alias: value}`` dict (e.g.
    ``{"MILL_MAX_CONCURRENCY": 4}``), matching the convention used by
    ``EnvSettingsSource`` / ``DotEnvSettingsSource`` in
    pydantic-settings, so ``populate_by_name`` is not required.

    Only fields whose env-var alias appears in the flattened YAML output
    are included — all others fall through to subsequent (lower-priority)
    sources or Field defaults.
    """

    def get_field_value(self, field, field_name):
        # Not used — __call__ is overridden directly.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        from .config_loader import flatten_yaml_config, load_yaml_config

        yaml_config = load_yaml_config()
        flat: dict[str, object] = flatten_yaml_config(yaml_config)  # alias → value
        result: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            alias: str | None = field_info.alias
            key = alias if alias is not None else field_name
            if key in flat:
                # Return alias-keyed dict so pydantic-settings recognises the
                # values — the framework passes source dicts directly as
                # ``super().__init__(**state)``, and pydantic only accepts
                # alias names (not Python field names) when
                # ``populate_by_name`` is False (the default).
                result[key] = flat[key]
        return result


class Settings(BaseSettings):
    """Central Pydantic configuration model for robotsix-mill.

    All fields are sourced from ``os.environ`` and layered
    ``config/*.yaml`` files.  Conventional keys like
    ``OPENROUTER_API_KEY`` or ``LANGFUSE_*`` are unprefixed to remain
    compatible with the reference projects.  Mill-specific settings use
    the ``MILL_`` / ``FORGE_`` prefix convention and declare explicit
    ``Field(alias=...)`` values.
    """

    model_config = SettingsConfigDict(
        # ``extra="forbid"``: an unknown kwarg is a typo or a stale
        # MILL_*-style legacy alias from a feature branch written
        # before the YAML-only refactor. Silent drops let those
        # branches "pass" locally and explode in CI after rebase —
        # exactly the failure mode that BLOCKED ticket ad2f's PR.
        # Forbidding the unknown kwarg surfaces the typo at the call
        # site, where the implement agent can see and fix it.
        #
        # ``env_prefix="MILL_"``: fields without an explicit
        # ``Field(alias=...)`` derive their env-var name as
        # ``MILL_<field_name>`` (e.g. ``model`` → ``MILL_MODEL``).
        # Fields WITH an explicit alias (e.g. ``FORGE_KIND``,
        # ``OPENROUTER_API_KEY``) use that alias verbatim — the
        # prefix is NOT applied.
        env_prefix="MILL_",
        env_file_encoding="utf-8",
        extra="forbid",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert YAML source with second-lowest priority (above only
        Field defaults), so ``os.environ`` still overrides it.

        Precedence (highest to lowest):
        1. explicit ``Settings(k=v)`` kwargs
        2. ``os.environ``
        3. file secrets
        4. ``config/*.yaml`` layered YAML
        5. Field(default=…) static defaults
        """
        return (
            init_settings,
            env_settings,
            file_secret_settings,
            YamlSettingsSource(settings_cls),
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
    #
    # --- LLM backend toggle (DeepSeek ↔ Claude SDK) ----------------------
    # REVERSIBLE provider selection. Default "deepseek" keeps the proven
    # OpenRouter/DeepSeek path for every agent. Set ``llm_backend:
    # claude_sdk`` to route ALL agents through the robotsix-llmio Claude
    # Agent SDK transport (subscription auth, no API key; tier→model
    # opus/haiku), or list specific agent names in ``claude_sdk_agents``
    # to route only those while everything else stays on DeepSeek.
    # Reverting is a config flip + restart — no code change. The Claude SDK
    # path needs Node + the ``claude`` CLI (logged in) in the container;
    # the *_model fields below still drive DeepSeek and only pick the tier
    # (pro→DEFAULT/opus, flash→CHEAP/haiku) when Claude SDK is selected.
    llm_backend: str = Field(default="deepseek")
    claude_sdk_agents: list[str] = Field(default_factory=list)
    # Process-wide cap on how many Claude Agent SDK runs may execute at once.
    # Each run spawns a ``claude`` CLI subprocess; spawning many simultaneously
    # (worker startup contention) can stall a run. A global semaphore (see
    # ``agents.claude_concurrency``) bounds concurrent runs to smooth the spawn
    # storm. Only takes effect when ``llm_backend``/``claude_sdk_agents`` routes
    # work to the Claude SDK; the DeepSeek path is unaffected. Must be ≥ 1.
    claude_max_concurrency: int = Field(
        default=4, alias="MILL_CLAUDE_MAX_CONCURRENCY", ge=1
    )
    # Resilience: when a Claude-SDK agent run terminally fails (after its local
    # retries are exhausted), fall back to the equivalent DeepSeek/OpenRouter
    # build of the same agent. Only takes effect for Claude-routed agents AND
    # when an OpenRouter key is configured (no key → no fallback, unchanged).
    # The fallback uses the same tier-resolved model the agent would have run on
    # DeepSeek. Set False to make a Claude failure surface directly.
    claude_fallback_to_deepseek: bool = Field(
        default=True, alias="MILL_CLAUDE_FALLBACK_TO_DEEPSEEK"
    )
    model: str = Field(default="deepseek/deepseek-v4-pro")
    explore_model: str = Field(default="deepseek/deepseek-v4-flash")
    test_model: str = Field(default="deepseek/deepseek-v4-flash")
    refine_model: str = Field(default="deepseek/deepseek-v4-pro")
    # Model for implement agent runs where the task is likely a no-change
    # investigation (the previous pass returned ``no_change_needed=True``
    # or the feedback indicates no edits are needed). Flash-class keeps
    # these re-check / resume passes cheap.
    no_change_model: str = Field(default="deepseek/deepseek-v4-flash")
    answer_model: str = Field(default="deepseek/deepseek-v4-pro")
    retrospect_model: str = Field(default="deepseek/deepseek-v4-pro")
    audit_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Default model for bespoke per-repo periodic agents loaded from
    # ``<clone>/.robotsix-mill/agents/<name>.yaml``. Each bespoke YAML
    # may override via its own ``model:`` field. Flash-class is the
    # default — bespoke agents are typically narrow standing checkers,
    # not deep reasoners.
    bespoke_default_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    # Model for the web_knowledge gateway sub-agent — a multi-turn
    # flash agent that owns the per-repo Markdown knowledge base
    # (per-library .md files + a cross-library _general.md) AND a
    # web_search tool, and decides autonomously which to use. Every
    # parent-agent route to the internet now goes through it, so the
    # knowledge base accumulates instead of fragmenting and cost
    # attribution for web hits stays in one place.
    web_knowledge_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
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
    # Model for the pre-refine dedup/already-done check — a cheap call
    # that short-circuits duplicate drafts before the expensive refiner.
    dedup_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Model for the pre-refine triage pass — a single cheap call that
    # decides whether the draft needs refinement at all.  Must be a
    # fast, inexpensive model; classification is the only task.
    triage_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Model for the scope-violation triage agent — a cheap call that
    # decides whether changed-out-of-scope files are legitimate
    # expansions or scope creep. Must be fast and inexpensive.
    scope_triage_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Per-call request caps (bound each role's loop). Sized for slow
    # deepseek-v4-pro + complex tickets: a medium ticket (53de) used
    # ~49 implement calls, so 200 leaves generous headroom; raising it
    # only matters if a ticket genuinely needs more steps.
    coordinator_request_limit: int = Field(default=200, ge=1)
    # Per-subtask request budget when the coordinator delegates via
    # ``spawn_subtask``. The parent's ``coordinator_request_limit``
    # still bounds the outer loop; this cap bounds each individual
    # sub-agent so one stuck subtask can't drain the parent's budget.
    subtask_request_limit: int = Field(default=30)
    # The test agent inspects failing output, reads the relevant
    # sources, and distills the cause — exploration-heavy work that
    # easily exceeds 8 calls on a non-trivial failure. 50 leaves ample
    # headroom (flash is cheap; cost-bounded by ticket-level cap).
    # Aligned with config/mill.defaults.yaml's core.limits.test_requests (8).
    # Pre-alignment the field default was 50 (legacy from before the
    # test agent flipped to a cheap flash model that doesn't need
    # nearly as many tool calls). The yaml value wins at runtime via
    # _YAML_PATH_TO_ALIAS; this just stops the dry-Settings() default
    # from contradicting it on machines without a yaml override.
    test_request_limit: int = Field(default=8, ge=1)
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
    model_request_timeout: float = Field(default=900.0, gt=0)
    transient_retries: int = Field(default=4, ge=0)
    transient_backoff_base: float = Field(default=2.0, gt=0)
    transient_backoff_cap: float = Field(default=30.0, gt=0)
    # Retry policy for stage-level transient errors (httpx.ConnectError,
    # etc.).  These control how many times a stage is re-attempted and
    # the exponential-backoff delay between attempts inside the worker
    # loop.  Test-friendly: keep the defaults small enough for tests to
    # override without needing long sleeps.
    stage_retry_max_attempts: int = Field(default=3)
    stage_retry_base_delay: float = Field(default=2.0)
    stage_retry_max_delay: float = Field(default=30.0)
    # Backoff for UsageLimitExceeded (pydantic-ai budget cap).  These
    # are longer than transient backoff because OpenRouter/provider
    # rate-limit windows are typically ~60s.  When
    # rate_limit_fallback_model is set, call_with_retry switches to
    # that model after rate_limit_fallback_retries consecutive
    # UsageLimitExceeded failures.
    rate_limit_backoff_base: float = Field(default=30.0, gt=0)
    rate_limit_backoff_cap: float = Field(default=120.0, gt=0)
    rate_limit_fallback_retries: int = Field(default=3, ge=0)
    rate_limit_fallback_model: str = Field(default="")
    # Per-call cap for the read-only exploration sub-agent the
    # coordinator uses instead of reading the repo into its own context.
    # Per-call cap for the domain-expert consultation sub-agent the
    # coordinator uses when it needs domain-specific advice.
    consult_request_limit: int = Field(default=15, ge=1)
    explore_request_limit: int = Field(default=100, ge=1)
    explore_max_tokens: int = Field(default=600, ge=1)
    # Per-call cap for the dedup check — one cheap call, so keep it tight.
    dedup_request_limit: int = Field(default=4, ge=1)
    doc_request_limit: int = Field(default=8)
    # Cheap classifier gate that runs *before* the full doc agent.
    doc_classifier_model: str = Field(default="deepseek/deepseek-v4-flash")
    doc_classifier_request_limit: int = Field(default=3)
    # Maximum characters of the memory ledger to load per agent pass.
    # When the file exceeds this, the oldest entries are dropped (read-side
    # only — persist_memory is unchanged).  Applies to all memory ledgers
    # (refine, audit, health, agent-check, etc.).
    max_memory_chars: int = Field(default=8000, ge=0)
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
    # Local-dev default: ``.data`` — the same path the docker-compose
    # volume mounts at /data, so host CLI invocations and the container
    # share state instead of leaking a separate sibling tree. The
    # Dockerfile sets MILL_DATA_DIR=/data explicitly so the container
    # always uses the absolute path. Tests override via tmp_path.
    data_dir: Path = Field(default=Path(".data"))

    # Default repo ID for legacy tickets that lack a board_id.
    # Set in config/mill.local.yaml.  When empty (default), accessing
    # a legacy ticket without a board_id raises an error telling the
    # operator to configure this.
    default_repo_id: str = Field(default="")

    # --- management-plane service ---
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8077)
    # Base URL the CLI client talks to.
    api_url: str = Field(default="http://127.0.0.1:8077", pattern=r"^https?://")

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
    max_spend_usd_per_ticket: float = Field(default=0.0)
    # Per-stage wall-clock timeout (seconds).  A stage that exceeds this
    # limit is escalated to BLOCKED, freeing the worker slot.  ≤ 0
    # disables the timeout entirely.  2400 s (40 min) comfortably
    # exceeds worst-case LLM latency (~190 s per call) and multiple
    # shell-command runs while still catching a true hang.
    stage_timeout_seconds: int = Field(default=2400)
    # Per-stage timeout overrides (JSON dict via env var, e.g.
    # MILL_STAGE_TIMEOUT_OVERRIDES='{"merge":0,"deliver":0}').
    # Keys are stage names; values are seconds.  Falls back to
    # stage_timeout_seconds when a stage isn't listed.  A value of 0
    # disables the timeout for that stage.
    stage_timeout_overrides: dict[str, int] = Field(default_factory=dict)
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

    # --- agent web access (refine + implement) ---
    # Web search is delegated to a cheap, bounded SUB-agent: the main
    # (expensive) agent never carries OpenRouter's ":online" suffix, it
    # only gets a `web_research(query)` tool whose body runs this small
    # model — with ":online" + web_fetch — and returns just a concise
    # conclusion. This kills the per-request web-search surcharge on the
    # pricey model and keeps its context lean (conclusions, not pages).
    web_search: bool = Field(default=True)
    # web_research is a focused single-question summariser — it reads
    # a URL or two and returns one concise factual conclusion. Flash
    # is plenty for that distillation, and a v4-pro context bloated
    # by a 314KB raw web_fetch costs $0.25 per call (seen in trace
    # d40e3c9d4fa5add80b2fe313c1d821f2 — pipeline ticket f6e2 refine).
    # Flip the default to flash. Operators who want v4-pro back set
    # MILL_WEB_RESEARCH_MODEL.
    web_research_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    web_research_request_limit: int = Field(default=8, ge=1)
    # web_fetch runs in its OWN container: network ON, but NO repo/data
    # mount, non-root, read-only, fixed curl. Trade-off accepted: an
    # agent could encode data into a fetched URL. http(s) only.
    fetch_image: str = Field(default="curlimages/curl:8.17.0")
    web_fetch_max_bytes: int = Field(default=2_000_000, ge=0)
    web_fetch_timeout: int = Field(default=30, gt=0)
    # Post-extraction cap, applied AFTER HTML→text stripping. The
    # network-level ``web_fetch_max_bytes`` bounds raw bytes; this
    # bounds what the agent ACTUALLY sees in its context. Default
    # 200 KB ≈ 50K tokens — enough for one doc page worth of prose,
    # not enough to nuke a refine context with a 315 KB markup dump.
    # Configured via ``web.fetch_max_text_bytes`` in the YAML config.
    web_fetch_max_text_bytes: int = 200_000
    # When True, web_fetch returns the raw response body verbatim
    # (no HTML→text stripping, no per-run URL dedupe). Operator
    # escape hatch for the rare case the agent needs the markup
    # itself (parsing structure, inspecting attributes). Default
    # False — every agent we ship is a prose consumer.
    # Configured via ``web.fetch_raw`` in the YAML config.
    web_fetch_raw: bool = False
    # Pre-write Python syntax check on `write_file` / `edit_file`. When
    # True (default) a SyntaxError aborts the edit and the agent gets
    # an actionable error string instead of writing broken code that
    # would only be caught one expensive test cycle later.
    # Configured via ``core.lint_on_edit`` in the YAML config.
    lint_on_edit: bool = Field(default=True, alias="MILL_LINT_ON_EDIT")
    # Directory of skill docs (skills/<name>/SKILL.md) injected into the
    # refine + implement agents' system prompt. Relative to CWD (/app in
    # the container, repo root locally).
    skills_dir: Path = Field(default=Path("skills"))
    # Directory of per-language instruction Markdown snippets
    # (agent_definitions/language_instructions/<language>.md) injected
    # into the implement agent's system prompt. Relative to CWD (/app
    # in the container, repo root locally).
    language_instructions_dir: Path = Field(
        default=Path("agent_definitions/language_instructions"),
    )

    # --- human approval gate (refine -> implement) ---
    # When true (default), the refine stage transitions to
    # human_issue_approval instead of ready — a human must approve before
    # the implement stage kicks in. Set false for fully-autonomous mode.
    require_approval: bool = Field(default=True)

    # When true, a cheap conservative LLM call inspects the refined spec
    # after refinement.  If the change is "obviously safe" (cosmetic,
    # doc-only, single-file, no logic changes) the ticket skips the
    # human approval gate and goes straight to READY.  When false
    # (default), every gated ticket waits for a human click.
    auto_approve_enabled: bool = Field(default=False)
    # Model for the auto-approve triage call — must be fast and cheap.
    auto_approve_model: str = Field(default="deepseek/deepseek-v4-flash")

    # --- dual-model review gate (implement → deliver) ---
    # When true, the implement stage transitions to code_review instead of
    # deliverable. A dedicated review agent audits the diff blind before the
    # deliver stage pushes + opens the PR. Default False (opt-in).
    review_enabled: bool = Field(default=False)
    # When true (and review is enabled + the review agent marks the
    # change as auto-merge-eligible), the merge stage will attempt to
    # merge its own green PR via the forge API without waiting for a
    # human. Default False (opt-in).
    auto_merge_enabled: bool = Field(default=False)
    # When True (and a human reviewer requests changes on the PR),
    # the merge stage will invoke the review-revision agent to
    # implement the requested changes automatically. Default False
    # (opt-in — this is a powerful autonomous capability).
    review_feedback_enabled: bool = Field(default=False)
    # When True (default), a cheap triage LLM call runs before the full
    # refine agent.  Drafts that are already precise, single-scoped, and
    # implementation-ready skip the full refine — saving cost & latency.
    # Set False to force full refine for all tickets without a deploy.
    refine_triage_enabled: bool = Field(default=True)
    # When True, a deterministic pre-refine gate verifies that file paths
    # and line ranges cited in the ticket draft still exist on the
    # working branch's HEAD.  When the cited evidence has gone stale
    # (upstream rewrite, sibling commit, or hallucinated finding) the
    # ticket is short-circuited to DONE before the expensive refine
    # agent runs.  Default False (opt-in).
    freshness_gate_enabled: bool = Field(default=False)
    # When True, the refine stage runs a post-refinement review pass that
    # strips verbose exploratory narrative from the spec, producing a
    # concise version while saving the verbose original as an artifact.
    # Defaults to False (opt-in) to avoid surprising behaviour changes.
    spec_review_enabled: bool = Field(default=False)
    # When True (default), a cheap scope-triage LLM call inspects
    # out-of-scope file changes before blocking the ticket. The agent
    # decides EXPAND (legitimate), REJECT (scope creep), or ESCALATE
    # (uncertain). Set False to restore immediate BLOCKED behaviour.
    scope_triage_enabled: bool = Field(default=True, alias="MILL_SCOPE_TRIAGE_ENABLED")
    # Model for the review agent. Defaults to the capable coordinator model.
    # Override to use a *different* model for a genuinely independent review
    # perspective (the dual-model benefit).
    review_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Model for the review-revision agent. Defaults to the capable
    # coordinator model. Override to use a different model.
    review_revision_model: str = Field(default="deepseek/deepseek-v4-pro")
    # When True, the deliver stage generates a structured PR body
    # (Summary / Changes / Test Plan) from the implementation diff
    # via a cheap one-shot LLM call instead of pasting the raw spec.
    pr_summary_enabled: bool = Field(default=False, alias="MILL_PR_SUMMARY_ENABLED")
    # Model for the PR-summary generation call — must be cheap and
    # fast (one-shot, small diff, structured output).
    pr_summary_model: str = Field(
        default="deepseek/deepseek-v4-flash", alias="MILL_PR_SUMMARY_MODEL"
    )
    # When True, Forge.create_repo() is permitted to create repositories
    # via the forge API. Default False (opt-in) — the operator must
    # explicitly enable this and ensure the GitHub App installation has
    # the necessary repository-creation scope.
    enable_repo_creation: bool = Field(default=False)
    # Maximum number of CODE_REVIEW → READY → DOCUMENTING → CODE_REVIEW
    # round-trips before escalating to DELIVERABLE for human merge approval.
    # A value ≤ 0 means escalate on the first REQUEST_CHANGES (the loop is
    # effectively disabled). Default 3.
    review_max_rounds: int = Field(default=3, ge=0)
    # How many model requests the review agent may make in one run
    # (counts each tool call + each reasoning step + the final verdict).
    # 40 is the empirical floor for a medium PR (4-6 files): read_file
    # per modified file + diff walk + verdict + a few post_comment
    # round-trips. 20 was the original default and routinely BLOCKED
    # medium PRs with "review agent error — resumable" mid-review.
    review_request_limit: int = Field(default=40)
    # How many model requests the scope-triage agent may make per
    # invocation (main call + any tool calls). Default 4.
    scope_triage_request_limit: int = Field(default=4)

    # Model for the documentation agent. Defaults to the capable
    # coordinator model.
    doc_model: str = Field(default="deepseek/deepseek-v4-flash")

    # --- retrospect stage (done -> reviewed) ---
    # When True, retrospect may file an improvement DRAFT. Until the
    # human-gate-after-refine exists, that draft auto-flows to done and
    # is retrospected again — set False to analyse without spawning.
    retrospect_spawn_drafts: bool = Field(default=True)
    # When True, retrospect may append AGENT.md proposals to
    # AGENT_CANDIDATES.md for human review.
    retrospect_spawn_agented_proposals: bool = Field(
        default=True, alias="MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS"
    )
    # (Removed) retrospect_deep_analysis_frequency: deep-analysis mode
    # was retired — per-trace inspection is now owned by the periodical
    # cost-evaluation pipeline (cost_reconciliation_runner +
    # trace_health_runner + expensive-item detector).
    # Model for the trace inspector sub-agent — a dedicated cheap model
    # that inspects a single trace's full observation tree.
    trace_inspector_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    # Model used by the periodic trace-review runner — a cheap-by-design
    # flash model so a 50-trace sweep doesn't burn the audit budget.
    trace_review_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    # Outlier thresholds for the deterministic trace-review classifier.
    # A trace is flagged for LLM inspection when ANY hit.
    #
    # Cost and observation count are flagged RELATIVELY: the runner
    # computes the median across the current batch and flags traces
    # whose value exceeds ``median × multiplier``. A multiplier of 3.0
    # means "3x the typical trace in this window." Batches with fewer
    # than 3 traces fall back to no relative flag (insufficient
    # baseline) — binary flags (tool errors, rejected generations,
    # ask_user loops, explore storms) still fire normally.
    trace_review_cost_multiplier: float = Field(
        default=3.0,
    )
    trace_review_obs_multiplier: float = Field(
        default=3.0,
    )
    # ``repeated_tool`` stays an absolute threshold because each tool
    # has its own "normal" usage profile — making it relative would
    # require a per-tool batch median, which is too noisy with small
    # samples.
    trace_review_max_repeated_tool: int = Field(
        default=50,
    )
    # Hard cap on the total number of drafts a single trace-review
    # pass may file. The inspector emits one finding per flagged trace
    # and a typical batch flags 5-10 traces with 2-5 findings each →
    # up to 50 drafts per cycle (89 trace-review drafts piled up after
    # one 2026-05-28 cycle). Findings are individually low-signal and
    # the cross-trace analyzer is the right surface for recurring
    # patterns; capping per-cycle bleeds keeps the board readable.
    trace_review_max_drafts_per_run: int = Field(
        default=5,
    )
    # First-run lookback window when no watermark exists yet (hours).
    trace_review_initial_lookback_hours: int = Field(
        default=24,
    )
    # When set, every trace-review draft lands on THIS repo's board,
    # regardless of which repo the source trace lived on. Trace-review
    # findings are agent-side improvements (mill code, mill prompts);
    # filing them on each application repo's board scatters work that
    # belongs in one place. Leave empty to preserve the legacy
    # source-repo routing.
    trace_review_target_repo_id: str = Field(
        default="",
    )
    # Window (seconds) for correlating an incomplete trace with a
    # process restart. When an ``incomplete_trace`` flag fires AND
    # the trace's latest timestamp falls within this many seconds of
    # ``_process_started_at``, the ``restart_correlated`` flag is
    # appended — signalling the Phase 2 inspector that the root cause
    # is likely a restart kill, not an agent-loop bug.
    trace_review_restart_correlation_window_seconds: int = Field(
        default=60,
    )
    # Recency window (days) for the pre-filing duplicate check in the
    # trace-review runner.  A candidate prior ticket is considered for
    # dedup when its created_at is within this window of `now`.  Default
    # 7 mirrors `dedup_lookback_days` (used by the refine-stage dedup
    # guard) but is independent because the two checks live at different
    # stages and may want different policies.
    trace_review_dedup_lookback_days: int = Field(default=7)
    # Memory ledger for the trace inspector.
    # Unset (default) derives <data_dir>/trace_inspector_memory.md.
    trace_inspector_memory_path: Path | None = Field(default=None)
    # Path to the agent-maintained Markdown memory ledger.  Override to
    # pin a specific path; unset (default) derives <data_dir>/retrospect_memory.md.
    retrospect_memory_path: Path | None = Field(default=None)
    # human_mr_approval (PR open) re-check cadence. mill has no scheduler; this
    # timer exists only to observe the external merge event.
    merge_poll_seconds: int = Field(default=120, gt=0)
    # When true (default), the workspace's clone (repo/) is removed on
    # close to save disk space.
    prune_clone_on_close: bool = Field(default=True)
    # Maximum number of terminal-state tickets (CLOSED, ANSWERED,
    # EPIC_CLOSED) to retain.  When a ticket transitions to a terminal
    # state and the total exceeds this cap, the oldest terminal tickets
    # (by created_at) are purged — unless they are the parent of an
    # active (non-terminal) child.  Set to 0 to disable purging.
    max_archived_tickets: int = Field(default=100, ge=0)

    # --- merge stage: auto-rebase of stale PRs ---
    # When a PR in human_mr_approval becomes conflicting (other PRs merged to
    # the target branch), the merge stage invokes the rebase agent to
    # resolve conflicts automatically.  This is the max number of
    # rebase attempts per ticket before escalating to BLOCKED.
    rebase_max_attempts: int = Field(default=3, ge=0)

    # --- merge stage: auto-fix of failing remote CI ---
    # When a PR in human_mr_approval has failing CI checks, the merge stage
    # transitions to fixing_ci and invokes the ci-fix agent to resolve
    # the failures automatically.  This is the max number of ci-fix
    # attempts per ticket before escalating to BLOCKED.
    ci_fix_max_attempts: int = Field(default=2, ge=0)

    # Maximum consecutive ci-fix cycles that produce no code changes before
    # escalating to BLOCKED.  A "no-change" cycle is one where the ci-fix
    # agent reports success but the local HEAD matches the remote (no commits
    # were produced).  Set to 0 to disable the ceiling (preserves pre-0.32
    # behaviour of relying solely on ci_fix_max_attempts for the outer bound).
    ci_max_auto_retries: int = Field(default=3, ge=0)

    # Maximum review-revision attempts per ticket before escalating to BLOCKED.
    review_revision_max_attempts: int = Field(default=2, ge=1)

    # --- target-branch CI monitor ---
    # CI monitor enabled/interval are now per-repo fields on RepoConfig
    # (see config/repos.yaml).  ci_log_max_bytes stays global — it is an
    # operational cap, not a per-repo policy decision.
    ci_log_max_bytes: int = Field(default=65536)

    # --- langfuse cleanup (caps trace count per project) ---
    # When True, the worker runs a periodic sweep that deletes the oldest
    # traces from each repo's Langfuse project, keeping at most
    # langfuse_cleanup_max_traces rows. Default False (opt-in).
    langfuse_cleanup_periodic: bool = Field(default=True)
    langfuse_cleanup_interval_seconds: int = Field(default=86400)
    langfuse_cleanup_max_traces: int = Field(default=1000)

    # --- bespoke per-repo periodic agents ---
    # When True, the worker spawns a supervisor per repo that clones the
    # repo, scans ``.robotsix-mill/agents/<name>.yaml``, and runs each
    # bespoke agent on its own declared interval. Master switch — set
    # False to disable bespoke-agent discovery for the entire process
    # (per-repo opt-out is controlled by RepoConfig.bespoke_periodic).
    bespoke_periodic: bool = Field(
        default=True,
    )
    # How often (seconds) the bespoke supervisor refreshes its clone
    # and reconciles which YAMLs are scheduled. A new YAML committed
    # to the managed repo lands within this window; one removed gets
    # its loop cancelled in the same cycle.
    bespoke_discovery_interval_seconds: int = Field(
        default=600,
    )

    # --- audit agent (meta-audit for quality/security coverage) ---
    # When True, the worker runs periodic audit passes at the configured
    # interval. Default False (opt-in).
    audit_periodic: bool = Field(default=True)
    # Interval between periodic audit passes (seconds). Only used when
    # MILL_AUDIT_PERIODIC=true.
    audit_interval_seconds: int = Field(default=86400)
    # Path to the audit agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/audit_memory.md.
    audit_memory_path: Path | None = Field(default=None)

    # --- trace-health check ---
    # When True, the worker runs periodic trace-health checks at the
    # configured interval. Default False (opt-in).
    trace_health_periodic: bool = Field(default=True)
    # Interval between automatic trace-health checks (seconds). Only
    # used when MILL_TRACE_HEALTH_PERIODIC=true. Enforced minimum 3600s
    # (1h) in the worker to avoid hammering Langfuse.
    trace_health_interval_seconds: int = Field(default=86400)

    # --- trace-review ---
    # When True, the worker runs periodic trace-review passes at the
    # configured interval. Scans recent Langfuse traces, flags outliers
    # statistically (cost / observation count vs. batch median ×
    # multiplier) and absolutely (tool errors, rejected generations,
    # ask_user loops, explore storms, repeated-tool ceilings), runs
    # the cheap flash inspector over the flagged subset, and files
    # draft tickets with proposed solutions. Default True (opt-out).
    trace_review_periodic: bool = Field(default=True)
    # Interval between automatic trace-review passes (seconds). Default
    # daily. Enforced minimum 3600s (1h) in the worker.
    trace_review_interval_seconds: int = Field(default=86400)

    # --- cost warmer ---
    # When True, the worker runs a slow background task that walks every
    # non-archived ticket on each repo and refreshes its cached Langfuse
    # cost. Without this the board's /tickets list (which polls every
    # 1s with blocking_cost=False to stay fast) shows $0 for any ticket
    # whose detail drawer has never been opened — operators only see the
    # actual cost when they click the ticket. The warmer pre-fills the
    # cache so the cost column is always populated.
    cost_warmer_periodic: bool = Field(default=True)
    # Seconds between full cycles. Default 20s: a full sweep of a
    # busy board (200+ tickets) finishes in ~15s with the default
    # concurrency, so the column refreshes faster than the cache TTL
    # — idle tickets stay populated without ever showing a $0 dip.
    cost_warmer_interval_seconds: int = Field(default=20)
    # Concurrent Langfuse calls during a sweep. Each call takes
    # ~250ms; with concurrency=4 a 200-ticket sweep takes ~12s. Bump
    # higher for snappier refreshes on large boards (mind the
    # Langfuse rate limit); drop to 1 to revert to fully-serial.
    cost_warmer_concurrency: int = Field(default=4)
    # Independent on/off for the fast loop (see comment below).
    # Gated separately from the slow loop so an operator can disable
    # the per-active-ticket Langfuse polling without losing the
    # comprehensive sweep, e.g. when Langfuse rate-limits become a
    # concern.
    cost_warmer_fast_periodic: bool = Field(default=True)
    # Seconds between *fast* warmer cycles. The slow warmer above
    # walks every ticket and is fine for idle ones, but a full sweep
    # takes a minute or more on a busy board — long enough that an
    # actively-running ticket's cost looks frozen to the operator
    # watching it. The fast loop walks ONLY tickets in active stages
    # (refine, implement, review, …) and bypasses the cache TTL, so a
    # board user sees the cost climb in near-real-time.
    cost_warmer_fast_interval_seconds: int = Field(default=5)

    # --- timeout escalation ---
    # When True, the worker runs periodic timeout-escalation passes at the
    # configured interval. Default True (opt-out). Detects tickets stuck in
    # AWAITING_USER_REPLY longer than the threshold and escalates to BLOCKED.
    timeout_escalation_periodic: bool = Field(default=True)
    # Interval between timeout-escalation passes (seconds). Only used when
    # MILL_TIMEOUT_ESCALATION_PERIODIC=true.
    timeout_escalation_interval_seconds: int = Field(default=3600)
    # Staleness threshold: tickets in AWAITING_USER_REPLY with updated_at
    # older than this many seconds are escalated to BLOCKED.
    # Default 259200 = 3 days.  Set to ≤ 0 to disable escalation
    # entirely while leaving the poll loop running.
    timeout_escalation_threshold_seconds: int = Field(default=259200)

    # --- test-gap agent (dedicated test-coverage oversight) ---
    # Model for the test-gap agent. Defaults to the same capable model
    # as audit/health. Override with MILL_TEST_GAP_MODEL.
    test_gap_model: str = Field(default="deepseek/deepseek-v4-flash")
    # When True, the worker runs periodic test-gap passes at the
    # configured interval. Default False (opt-in).
    test_gap_periodic: bool = Field(default=True)
    # Interval between periodic test-gap passes (seconds). Only used
    # when MILL_TEST_GAP_PERIODIC=true.
    test_gap_interval_seconds: int = Field(default=86400)
    # Path to the test-gap agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives
    # <data_dir>/test_gap_memory.md.
    test_gap_memory_path: Path | None = Field(default=None)

    # --- agent-check agent (agent-definition coherence) ---
    # Model for the agent-check meta-agent. Defaults to the same cheap
    # model as other read-only periodic agents. Override with
    # MILL_AGENT_CHECK_MODEL.
    agent_check_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Path to the agent-check agent's Markdown memory ledger. Override
    # to pin a specific path; unset (default) derives
    # <data_dir>/agent_check_memory.md.
    agent_check_memory_path: Path | None = Field(default=None)
    # Opt-in periodic agent-check pass. Defaults to False (off); flip
    # to true to schedule the pass every ``agent_check_interval_seconds``
    # in addition to the on-demand POST /agent-check and CLI.
    agent_check_periodic: bool = Field(default=True)
    # Seconds between periodic agent-check passes when
    # MILL_AGENT_CHECK_PERIODIC=true. Minimum enforced at 60s in the
    # worker loop.
    agent_check_interval_seconds: int = Field(default=86400)

    # --- health agent (codebase-health inspection) ---
    # Model for the health agent. Defaults to the same capable model as
    # audit. Override with MILL_HEALTH_MODEL.
    health_model: str = Field(default="deepseek/deepseek-v4-flash")
    # When True, the worker runs periodic health passes at the
    # configured interval. Default False (opt-in).
    health_periodic: bool = Field(default=True)
    # Interval between periodic health passes (seconds). Only used when
    # MILL_HEALTH_PERIODIC=true.
    health_interval_seconds: int = Field(default=86400)
    # Path to the health agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/health_memory.md.
    health_memory_path: Path | None = Field(default=None)

    # --- survey agent (OSS project discovery) ---
    # Survey is a discovery + structured-output agent: read README,
    # do a few web_research calls, propose draft tickets. It does NOT
    # do deep reasoning over code — flash is plenty. v4-pro was the
    # historical default and burned $15.32 on a single survey trace
    # (1bfa36ab7c5abc838d3934..., 2026-05-29) by accumulating ~3M
    # prompt tokens across 22 chat calls at v4-pro pricing. Flipping
    # to flash drops that to ~$1.50–$2 worst-case; the operator can
    # override via `core.models.survey` in YAML if a specific repo
    # needs deeper reasoning.
    survey_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Cap the survey main agent's tool-call request budget. The
    # ancient $15.32 trace had 22 chat calls and 25 web_fetch
    # calls — well past diminishing returns; this is what motivated
    # any cap at all.
    #
    # The "keep trying subjects until one yields a draft" prompt
    # (agent_definitions/periodic/survey.yaml) targets ≤10 requests
    # per subject attempt, up to 3 attempts per run if the first
    # subjects don't reveal a citable gap. Worst case: 3 × ~10 =
    # 30 requests, plus pydantic-ai validation retries + the final
    # structured-output round → 40 is the safe ceiling.
    #
    # Per-call cost on the survey model is ~$0.02-0.05, so 40
    # caps worst-case spend at ~$0.80-2.00 per run. Significantly
    # below the historical $15 runaway and a reasonable price for
    # the guarantee that every run produces a draft.
    survey_request_limit: int = Field(default=40)
    # Path to the survey agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/survey_memory.md.
    survey_memory_path: Path | None = Field(default=None)
    # Opt-in periodic survey pass. Defaults to True (on by default —
    # "default yes"). Flip to false to disable the automatic daily
    # cadence while still allowing on-demand POST /survey and
    # board-button triggers.
    survey_periodic: bool = Field(default=True)
    # Seconds between automatic survey passes when
    # MILL_SURVEY_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    survey_interval_seconds: int = Field(default=86400)

    # --- bc_check agent (backward-compatibility inspection) ---
    # Model for the bc-check agent. Defaults to the same capable model
    # as other read-only periodic agents. Override with
    # MILL_BC_CHECK_MODEL.
    bc_check_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Path to the bc-check agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives
    # <data_dir>/bc_check_memory.md.
    bc_check_memory_path: Path | None = Field(default=None)
    # Opt-in periodic bc-check pass. Defaults to False (off); flip to
    # true to schedule the pass every ``bc_check_interval_seconds`` in
    # addition to the on-demand CLI.
    bc_check_periodic: bool = Field(default=True)
    # Seconds between periodic bc-check passes when
    # MILL_BC_CHECK_PERIODIC=true. Minimum enforced at 60s in the
    # worker loop.
    bc_check_interval_seconds: int = Field(default=86400)

    # --- module_curator agent (module-taxonomy drift detection) ---
    # Model for the module-curator agent. Defaults to the same capable
    # model as other read-only periodic agents. Override with
    # MILL_MODULE_CURATOR_MODEL.
    module_curator_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Path to the module-curator agent's Markdown memory ledger.
    # Override to pin a specific path; unset (default) derives
    # <data_dir>/module_curator_memory.md.
    module_curator_memory_path: Path | None = Field(default=None)
    # Opt-in periodic module-curator pass. Defaults to True (opt-out);
    # set false to disable the daily module-taxonomy drift check on
    # this repo.
    module_curator_periodic: bool = Field(default=True)
    # Seconds between periodic module-curator passes when
    # MILL_MODULE_CURATOR_PERIODIC=true. Minimum enforced at 60s in
    # the worker loop.
    module_curator_interval_seconds: int = Field(default=86400)

    # --- cost-reconciliation agent (OpenRouter ↔ Langfuse cost drift) ---
    # Model for the cost-reconciliation agent. Defaults to the same
    # capable model as other periodic agents. Override with
    # MILL_COST_RECONCILIATION_MODEL.
    cost_reconciliation_model: str = Field(default="deepseek/deepseek-v4-pro")
    # Path to the cost-reconciliation agent's Markdown memory ledger.
    # Override to pin a specific path; unset (default) derives
    # <data_dir>/cost_reconciliation_memory.md.
    cost_reconciliation_memory_path: Path | None = Field(default=None)
    # Opt-in periodic cost-reconciliation pass. Defaults to False (off);
    # flip to true to schedule the pass every
    # ``cost_reconciliation_interval_seconds``.
    cost_reconciliation_periodic: bool = Field(default=True)
    # Seconds between periodic cost-reconciliation passes when
    # MILL_COST_RECONCILIATION_PERIODIC=true. Minimum enforced at 60s
    # in the worker loop.
    cost_reconciliation_interval_seconds: int = Field(default=86400)

    # --- data-dir audit agent (.data/ monotonic growth survey) ---
    # Model for the data-dir audit agent. Defaults to flash —
    # the agent does file listing + size checks, not deep reasoning.
    # Override with MILL_DATA_DIR_AUDIT_MODEL.
    data_dir_audit_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Path to the data-dir audit agent's Markdown memory ledger.
    # Override to pin a specific path; unset (default) derives
    # <data_dir>/data_dir_audit_memory.md.
    data_dir_audit_memory_path: Path | None = Field(default=None)
    # Master switch for the periodic data-dir audit pass.
    # Default True — the agent is harmless when idle (no findings).
    data_dir_audit_periodic: bool = Field(default=True)
    # Seconds between periodic data-dir audit passes when
    # MILL_DATA_DIR_AUDIT_PERIODIC=true. Minimum enforced at 60 s
    # in the worker loop.
    data_dir_audit_interval_seconds: int = Field(default=86400)
    # Threshold (bytes) for the top-N largest-items check.
    # Files and directories whose cumulative size reaches this
    # threshold are reported as oversized.  Default 100 MiB.
    # Override with MILL_DATA_DIR_AUDIT_SIZE_THRESHOLD_BYTES.
    data_dir_audit_size_threshold_bytes: int = Field(default=104_857_600, ge=0)
    # If a file or directory grew by at least this many bytes since
    # the last audit pass, flag it for growth.  Default 10 MiB.
    # Override with MILL_DATA_DIR_AUDIT_GROWTH_DELTA_BYTES.
    data_dir_audit_growth_delta_bytes: int = Field(default=10485760)
    # If a file or directory grew by at least this percentage since
    # the last audit pass, flag it for growth.  Default 20 (%).
    # Override with MILL_DATA_DIR_AUDIT_GROWTH_DELTA_PCT.
    data_dir_audit_growth_delta_pct: int = Field(default=20)
    # Maximum number of drafts created per data-dir audit pass.
    # Findings beyond this cap are dropped and re-considered on the
    # next scheduled pass. Mirrors trace_review_max_drafts_per_run.
    # Override with MILL_DATA_DIR_AUDIT_MAX_DRAFTS_PER_PASS.
    data_dir_audit_max_drafts_per_pass: int = Field(default=5)

    # --- completeness_check agent (feature-wiring completeness) ---
    # Model for the completeness-check agent. Defaults to the same
    # capable model as other read-only periodic agents. Override with
    # MILL_COMPLETENESS_CHECK_MODEL.
    completeness_check_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Path to the completeness-check agent's Markdown memory ledger.
    # Override to pin a specific path; unset (default) derives
    # <data_dir>/completeness_check_memory.md.
    completeness_check_memory_path: Path | None = Field(default=None)
    # Opt-in periodic completeness-check pass. Defaults to False (off);
    # flip to true to schedule the pass every
    # ``completeness_check_interval_seconds`` in addition to the
    # on-demand CLI.
    completeness_check_periodic: bool = Field(default=True)
    # Seconds between periodic completeness-check passes when
    # MILL_COMPLETENESS_CHECK_PERIODIC=true. Minimum enforced at 60s
    # in the worker loop.
    completeness_check_interval_seconds: int = Field(default=86400)

    # --- copy-paste agent (deterministic clone detection and triage) ---
    # Model for the copy-paste agent. Defaults to the same capable model
    # as audit/health. Override with MILL_COPY_PASTE_MODEL.
    copy_paste_model: str = Field(default="deepseek/deepseek-v4-pro")
    # Path to the copy-paste agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives
    # <data_dir>/copy_paste_memory.md.
    copy_paste_memory_path: Path | None = Field(default=None)
    # Opt-in periodic copy-paste pass. Defaults to True (opt-out);
    # set false to disable the weekly clone-detection sweep.
    copy_paste_periodic: bool = Field(default=True)
    # Seconds between periodic copy-paste passes when
    # MILL_COPY_PASTE_PERIODIC=true. Default 604800 (1 week). Minimum
    # enforced at 60s in the worker loop.
    copy_paste_interval_seconds: int = Field(default=604800)

    # --- config-sync agent (config ↔ .env ↔ docs drift detection) ---
    # Model for the config-sync agent. Defaults to a cheap model (read-only
    # file parsing — no web research or code generation).
    config_sync_model: str = Field(default="deepseek/deepseek-v4-flash")
    # Path to the config-sync agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/config_sync_memory.md.
    config_sync_memory_path: Path | None = Field(default=None)
    # Opt-in periodic config-sync pass. Default false (agents default off
    # unless noted). Set true to enable automatic daily drift detection.
    config_sync_periodic: bool = Field(default=True)
    # Seconds between automatic config-sync passes when
    # MILL_CONFIG_SYNC_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    config_sync_interval_seconds: int = Field(default=86400)

    # --- meta-agent (cross-repo extraction/alignment survey) ---
    # Master switch for the daily meta-agent pass. Defaults to False
    # (off) — the operator must register the meta board in repos.yaml
    # first.  Flip to True to enable the global daily schedule.
    meta_periodic: bool = Field(default=False)
    # Seconds between automatic meta-agent passes. Default 86400 (1 day).
    # Minimum enforced at 60 s in the worker loop.
    meta_interval_seconds: int = Field(default=86400)

    # --- action-agent memory paths ---
    # Path to the implement agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/implement_memory.md.
    implement_memory_path: Path | None = Field(default=None)
    # Path to the refine agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/refine_memory.md.
    refine_memory_path: Path | None = Field(default=None)
    # Path to the document agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/doc_memory.md.
    doc_memory_path: Path | None = Field(default=None)
    # Path to the ci-fix agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/ci_fix_memory.md.
    ci_fix_memory_path: Path | None = Field(default=None)
    # Path to the review-revision agent's Markdown memory ledger.
    # Override to pin a specific path; unset (default) derives
    # <data_dir>/review_revision_memory.md.
    review_revision_memory_path: Path | None = Field(default=None)
    # Path to the rebase agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/rebase_memory.md.
    rebase_memory_path: Path | None = Field(default=None)
    # Path to the ci-fix agent's structured pattern memory.  Override
    # to pin a specific path; unset (default) derives
    # <data_dir>/ci_patterns.json.
    ci_patterns_path: Path | None = Field(default=None)

    # --- tracing (optional) ---
    langfuse_base_url: str | None = Field(default=None, alias="LANGFUSE_BASE_URL")
    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_project_id: str | None = Field(default=None, alias="LANGFUSE_PROJECT_ID")

    # --- notifications (optional) ---
    ntfy_url: str | None = Field(default=None, alias="NTFY_URL")
    ntfy_token: str | None = Field(default=None, alias="NTFY_TOKEN")

    def workspaces_dir_for(self, board_id: str) -> Path:
        """Per-repo workspaces directory. *board_id* is required —
        raises ``ValueError`` when empty."""
        if not board_id:
            raise ValueError(
                "workspaces_dir_for: board_id is required. "
                "The board-less <data_dir>/workspaces is gone."
            )
        return self.data_dir / board_id / "workspaces"

    def memory_file_for(self, name: str, board_id: str) -> Path:
        """Return the per-repo memory ledger path for *name*
        (e.g. ``"implement"``, ``"refine"``, ``"audit"``).

        Honors any explicit ``<name>_memory_path`` setting override
        (env / YAML); otherwise routes to
        ``<data_dir>/<board_id>/<name>_memory.md``.  *board_id* is
        required — raises ``ValueError`` when empty.

        Memory ledgers are repo-specific observation logs (codebase
        conventions, testing patterns, gotchas) — each repo
        accumulates its own.
        """
        if not board_id:
            raise ValueError(
                "memory_file_for: board_id is required. "
                "The board-less <data_dir>/<name>_memory.md is gone."
            )
        override = getattr(self, f"{name}_memory_path", None)
        if override is not None:
            return override
        return self.data_dir / board_id / f"{name}_memory.md"

    @property
    def tracing_enabled(self) -> bool:
        """True when all three Langfuse credentials are configured."""
        secrets = get_secrets()
        return bool(
            secrets.langfuse_base_url
            and secrets.langfuse_public_key
            and secrets.langfuse_secret_key
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
    def config_sync_memory_file(self) -> Path:
        """Resolved path to the agent-maintained config-sync memory ledger."""
        if self.config_sync_memory_path is not None:
            return self.config_sync_memory_path
        return self.data_dir / "config_sync_memory.md"

    @property
    def bc_check_memory_file(self) -> Path:
        """Resolved path to the agent-maintained bc-check memory ledger."""
        if self.bc_check_memory_path is not None:
            return self.bc_check_memory_path
        return self.data_dir / "bc_check_memory.md"

    @property
    def cost_reconciliation_memory_file(self) -> Path:
        """Resolved path to the agent-maintained cost-reconciliation memory ledger."""
        if self.cost_reconciliation_memory_path is not None:
            return self.cost_reconciliation_memory_path
        return self.data_dir / "cost_reconciliation_memory.md"

    @property
    def data_dir_audit_memory_file(self) -> Path:
        """Resolved path to the agent-maintained data-dir audit memory ledger."""
        if self.data_dir_audit_memory_path is not None:
            return self.data_dir_audit_memory_path
        return self.data_dir / "data_dir_audit_memory.md"

    @property
    def completeness_check_memory_file(self) -> Path:
        """Resolved path to the agent-maintained completeness-check memory ledger."""
        if self.completeness_check_memory_path is not None:
            return self.completeness_check_memory_path
        return self.data_dir / "completeness_check_memory.md"

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
    def doc_memory_file(self) -> Path:
        """Resolved path to the agent-maintained document memory ledger."""
        if self.doc_memory_path is not None:
            return self.doc_memory_path
        return self.data_dir / "doc_memory.md"

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

    @property
    def ci_patterns_file(self) -> Path:
        """Resolved path to the ci-fix agent's structured pattern memory."""
        if self.ci_patterns_path is not None:
            return self.ci_patterns_path
        return self.data_dir / "ci_patterns.json"

    def ci_patterns_file_for(self, board_id: str = "") -> Path:
        """Per-repo resolved path for the ci-fix pattern memory.

        Falls back to the global path when no board_id is provided or
        when ``ci_patterns_path`` is explicitly overridden in config.
        """
        if self.ci_patterns_path is not None:
            return self.ci_patterns_path
        if board_id:
            return self.data_dir / board_id / "ci_patterns.json"
        return self.data_dir / "ci_patterns.json"

    @property
    def review_revision_memory_file(self) -> Path:
        """Resolved path to the agent-maintained review-revision memory ledger."""
        if self.review_revision_memory_path is not None:
            return self.review_revision_memory_path
        return self.data_dir / "review_revision_memory.md"

    # ------------------------------------------------------------------
    #  Validators
    # ------------------------------------------------------------------

    # -- interval minimums ---------------------------------------------

    @field_validator("trace_health_interval_seconds")
    @classmethod
    def _validate_trace_health_interval(cls, v: int) -> int:
        if v < 3600:
            raise ValueError("trace_health_interval_seconds must be ≥ 3600")
        return v

    @field_validator("trace_review_interval_seconds")
    @classmethod
    def _validate_trace_review_interval(cls, v: int) -> int:
        if v < 3600:
            raise ValueError("trace_review_interval_seconds must be ≥ 3600")
        return v

    # -- cross-field checks --------------------------------------------

    @model_validator(mode="after")
    def _validate_cross_field(self) -> "Settings":
        # forge_auth=app requires GitHub App credentials
        if self.forge_auth == "app":
            if not self.github_app_id and not self.github_app_private_key_path:
                raise ValueError(
                    "FORGE_AUTH=app requires at least one of github_app_id "
                    "or github_app_private_key_path to be set"
                )

        # forge_kind needs forge_remote_url
        if self.forge_kind in ("github", "gitlab"):
            if not self.forge_remote_url:
                raise ValueError(
                    f"forge_kind={self.forge_kind} requires forge_remote_url to be set"
                )

        # rate_limit_fallback_model non-empty → retries ≥ 1
        if self.rate_limit_fallback_model and self.rate_limit_fallback_retries < 1:
            raise ValueError(
                "rate_limit_fallback_retries must be ≥ 1 when rate_limit_fallback_model is set"
            )

        # review_enabled → review_model must be non-empty
        if self.review_enabled and not self.review_model:
            raise ValueError(
                "review_model must be non-empty when review_enabled is True"
            )

        return self


def load_settings() -> Settings:
    """Load and return a :class:`Settings` instance from env / ``.env`` files."""
    return Settings()


# ---------------------------------------------------------------------------
#  Secrets model
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


class Secrets(BaseModel):
    """Secrets loaded from ``config/secrets.yaml``.

    Never merged into ``Settings`` — secrets are kept in a separate
    model with redacted ``repr`` / ``model_dump`` and debug-logged
    attribute access.
    """

    openrouter_api_key: str | None = None
    forge_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_app_private_key_path: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str | None = None
    langfuse_project_id: str | None = None
    langfuse_project_name: str | None = None
    openrouter_management_key: str | None = None
    ntfy_url: str | None = None
    ntfy_token: str | None = None

    def __init__(self, _secrets_file: str | None = None, **data: Any) -> None:
        """Construct a ``Secrets`` instance.

        If ``_secrets_file`` is provided it is used as the YAML source;
        otherwise ``MILL_SECRETS_FILE`` is consulted, falling back to
        ``config/secrets.yaml``.  YAML values are passed as field
        defaults, which explicit ``**data`` kwargs can override.
        """
        from .config_loader import load_secrets_yaml

        file_path: str | None = _secrets_file
        if file_path is None:
            import os

            file_path = os.environ.get("MILL_SECRETS_FILE")

        yaml_data = load_secrets_yaml(file_path)
        merged = {**yaml_data, **data}
        super().__init__(**merged)

    def __repr__(self) -> str:
        field_names = list(type(self).model_fields.keys())
        inner = ", ".join(f"{name}='***'" for name in field_names)
        return f"Secrets({inner})"

    def model_dump(self, *, redact: bool = True, **kwargs: Any) -> dict[str, Any]:
        """Dump fields to dict, redacting all values by default."""
        d: dict[str, Any] = super().model_dump(**kwargs)
        if redact:
            return {k: "***" for k in d}
        return d

    def __getattribute__(self, name: str) -> Any:
        # Log every "public" field access at DEBUG level.
        # We must bypass our own override for private/special attrs
        # and for the fields dict itself to avoid infinite recursion.
        if not name.startswith("_") and name not in (
            "model_fields",
            "model_config",
            "model_dump",
            "__class__",
            "__dict__",
        ):
            fields = type(self).model_fields
            if name in fields:
                frame = inspect.currentframe()
                if frame is not None:
                    caller_frame = frame.f_back
                    if caller_frame is not None:
                        caller_module = caller_frame.f_globals.get(
                            "__name__", "unknown"
                        )
                    else:
                        caller_module = "unknown"
                else:
                    caller_module = "unknown"
                # Use a logger scoped to this module so tests can capture it
                _logger = logging.getLogger(__name__)
                _logger.debug("Secrets.%s accessed by %s", name, caller_module)
        try:
            return super().__getattribute__(name)
        except AttributeError:
            # Pydantic v2 field lookup: model_computed_fields etc.
            return object.__getattribute__(self, name)


def load_secrets(secrets_file: str | None = None) -> Secrets:
    """Load and return a :class:`Secrets` instance from YAML.

    If *secrets_file* is provided it is used as the YAML source;
    otherwise ``MILL_SECRETS_FILE`` is consulted, falling back to
    ``config/secrets.yaml``.
    """
    return Secrets(_secrets_file=secrets_file)


_secrets: Secrets | None = None


def get_secrets() -> Secrets:
    """Return a cached :class:`Secrets` singleton, constructing it on first call."""
    global _secrets
    if _secrets is None:
        _secrets = Secrets()
    return _secrets


def _reset_secrets() -> None:
    """Clear the cached :class:`Secrets` singleton (for tests)."""
    global _secrets
    _secrets = None


# ---------------------------------------------------------------------------
#  Repos registry — per-repo board & Langfuse config
# ---------------------------------------------------------------------------


class RepoConfig(BaseModel):
    """Configuration for a single repository — its board identity,
    Langfuse observability project credentials, and per-repo CI
    monitor settings."""

    repo_id: str
    board_id: str
    langfuse_project_name: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str = "https://cloud.langfuse.com"
    forge_remote_url: str | None = None
    ci_monitor_enabled: bool = True
    ci_monitor_interval_seconds: int = 86400
    # Number of tickets from THIS repo the worker will process in
    # parallel. Per-repo isolation: each repo gets its own consumer
    # pool, so a busy repo can't starve another. Default 1 keeps the
    # blast radius of any one ticket's bad behaviour contained.
    max_concurrency: int = 1
    # The shell command the test gate runs in the sandbox for tickets
    # in THIS repo. Default empty — the test gate short-circuits to
    # PASS when no command is set, which matches repos that don't
    # have a test suite yet (e.g. a doc-only repo). Set in repos.yaml
    # per repo, e.g. ``test_command: "pytest -q"``.
    test_command: str = ""
    # Per-repo periodic-agent enable flags. Default FALSE for every
    # one — a repo opts IN by setting the flag to true in repos.yaml
    # NOTE: the per-repo ``*_periodic`` enable flags were REMOVED. A periodic
    # workflow now runs for a repo iff the repo ships
    # ``.robotsix-mill/periodic/<name>.yaml`` (file presence = enabled; see
    # agents/periodic_loader.py + the worker's periodic supervisor). The
    # global ``Settings.<name>_periodic`` switches remain as fleet-wide
    # kill-switches.
    language: str | None = None

    @field_validator("repo_id", "board_id")
    @classmethod
    def _validate_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("ci_monitor_interval_seconds")
    @classmethod
    def _validate_ci_monitor_interval_seconds(cls, v: int) -> int:
        if v < 60:
            raise ValueError("ci_monitor_interval_seconds must be ≥ 60")
        return v

    @field_validator("max_concurrency")
    @classmethod
    def _validate_max_concurrency(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_concurrency must be ≥ 1")
        return v


class ReposRegistry(BaseModel):
    """Container holding all :class:`RepoConfig` entries keyed by repo ID."""

    repos: dict[str, RepoConfig]

    @model_validator(mode="after")
    def _validate_keys_match_repo_ids(self) -> "ReposRegistry":
        for key, config in self.repos.items():
            if config.repo_id != key:
                raise ValueError(
                    f"Repo key '{key}' does not match "
                    f"RepoConfig.repo_id '{config.repo_id}'"
                )
        return self


def load_repos_config(config_file: str | None = None) -> ReposRegistry:
    """Load repos configuration from ``config/repos.yaml`` (or override).

    Reads YAML via :func:`~robotsix_mill.config_loader.load_repos_yaml`,
    constructs a :class:`RepoConfig` for each entry, validates, and
    returns a :class:`ReposRegistry`.
    """
    from .config_loader import load_repos_yaml

    raw = load_repos_yaml(config_file)
    repos: dict[str, RepoConfig] = {}
    for repo_id, repo_data in raw.items():
        langfuse = repo_data.get("langfuse", {}) if isinstance(repo_data, dict) else {}
        ci_monitor = (
            repo_data.get("ci_monitor", {}) if isinstance(repo_data, dict) else {}
        )
        repos[repo_id] = RepoConfig(
            repo_id=repo_id,
            board_id=repo_data.get("board_id", "")
            if isinstance(repo_data, dict)
            else "",
            langfuse_project_name=langfuse.get("project_name", ""),
            langfuse_public_key=langfuse.get("public_key", ""),
            langfuse_secret_key=langfuse.get("secret_key", ""),
            langfuse_base_url=langfuse.get("base_url", "https://cloud.langfuse.com"),
            forge_remote_url=repo_data.get("forge_remote_url")
            if isinstance(repo_data, dict)
            else None,
            ci_monitor_enabled=ci_monitor.get("enabled", True)
            if isinstance(ci_monitor, dict)
            else True,
            ci_monitor_interval_seconds=ci_monitor.get("interval_seconds", 86400)
            if isinstance(ci_monitor, dict)
            else 86400,
            max_concurrency=repo_data.get("max_concurrency", 1)
            if isinstance(repo_data, dict)
            else 1,
            test_command=repo_data.get("test_command", "")
            if isinstance(repo_data, dict)
            else "",
            language=repo_data.get("language") if isinstance(repo_data, dict) else None,
        )
    return ReposRegistry(repos=repos)


_repos_config: ReposRegistry | None = None


def get_repos_config() -> ReposRegistry:
    """Return a cached :class:`ReposRegistry` singleton, constructing it
    on first call."""
    global _repos_config
    if _repos_config is None:
        _repos_config = load_repos_config()
    return _repos_config


def get_repo_config(repo_id: str) -> RepoConfig:
    """Look up *repo_id* in :func:`get_repos_config` and return its
    :class:`RepoConfig`.

    Raises :class:`~robotsix_mill.config_loader.ConfigError` for unknown IDs.
    """
    from .config_loader import ConfigError

    registry = get_repos_config()
    try:
        return registry.repos[repo_id]
    except KeyError:
        sorted_keys = sorted(registry.repos.keys())
        raise ConfigError(f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}")


def _reset_repos_config() -> None:
    """Clear the cached :class:`ReposRegistry` singleton (for tests)."""
    global _repos_config
    _repos_config = None
