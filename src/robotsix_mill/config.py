"""Runtime configuration, sourced from environment / .env.

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
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
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
    retrospect_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_RETROSPECT_MODEL"
    )
    audit_model: str = Field(
        default="deepseek/deepseek-v4-pro", alias="MILL_AUDIT_MODEL"
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
    # Per-call cap for the read-only exploration sub-agent the
    # coordinator uses instead of reading the repo into its own context.
    explore_request_limit: int = Field(
        default=20, alias="MILL_EXPLORE_REQUEST_LIMIT"
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
    max_fix_attempts: int = Field(default=3, alias="MILL_MAX_FIX_ATTEMPTS")
    branch_prefix: str = Field(default="mill/", alias="MILL_BRANCH_PREFIX")
    # Max model requests per implement agent pass (pydantic-ai default is
    # 50 — far too low for an agentic coding loop on a real repo).
    agent_request_limit: int = Field(
        default=200, alias="MILL_AGENT_REQUEST_LIMIT"
    )
    # Wall-clock cap (seconds) for the agent's shell tool and the test
    # command, so a hung command can't stall a worker forever.
    command_timeout: int = Field(default=900, alias="MILL_COMMAND_TIMEOUT")
    # Safety net: if a ticket re-enters the *same* model-driven stage
    # this many times without ever progressing (e.g. its run keeps being
    # interrupted, or a stage churns), the worker escalates it to BLOCKED
    # + notifies instead of silently re-billing the LLM forever. Poll
    # stages (merge/deliver) are exempt — in_review legitimately waits.
    max_stuck_cycles: int = Field(default=3, alias="MILL_MAX_STUCK_CYCLES")

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
        default="curlimages/curl:latest", alias="MILL_FETCH_IMAGE"
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
    # awaiting_approval instead of ready — a human must approve before
    # the implement stage kicks in. Set false for fully-autonomous mode.
    require_approval: bool = Field(
        default=True, alias="MILL_REQUIRE_APPROVAL"
    )

    # --- retrospect stage (done -> reviewed) ---
    # When True, retrospect may file an improvement DRAFT. Until the
    # human-gate-after-refine exists, that draft auto-flows to done and
    # is retrospected again — set False to analyse without spawning.
    retrospect_spawn_drafts: bool = Field(
        default=True, alias="MILL_RETROSPECT_SPAWN_DRAFTS"
    )
    # Path to the agent-maintained Markdown memory ledger.  Override to
    # pin a specific path; unset (default) derives <data_dir>/retrospect_memory.md.
    retrospect_memory_path: Path | None = Field(
        default=None, alias="MILL_RETROSPECT_MEMORY_PATH"
    )
    # in_review (PR open) re-check cadence. mill has no scheduler; this
    # timer exists only to observe the external merge event.
    merge_poll_seconds: int = Field(
        default=120, alias="MILL_MERGE_POLL_SECONDS"
    )
    # When true (default), the workspace's clone (repo/) is removed on
    # close to save disk space.
    prune_clone_on_close: bool = Field(
        default=True, alias="MILL_PRUNE_CLONE_ON_CLOSE"
    )

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

    # --- scout agent (model evaluation against OpenRouter) ---
    # When True, the worker runs periodic scout passes at the configured
    # interval. Default False (opt-in).
    scout_periodic: bool = Field(
        default=False, alias="MILL_SCOUT_PERIODIC"
    )
    # Interval between periodic scout passes (seconds). Only used when
    # MILL_SCOUT_PERIODIC=true.
    scout_interval_seconds: int = Field(
        default=86400, alias="MILL_SCOUT_INTERVAL_SECONDS"
    )
    # Path to the scout agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/scout_memory.md.
    scout_memory_path: Path | None = Field(
        default=None, alias="MILL_SCOUT_MEMORY_PATH"
    )

    # --- tracing (optional) ---
    langfuse_base_url: str | None = Field(default=None, alias="LANGFUSE_BASE_URL")
    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")

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
    def audit_memory_file(self) -> Path:
        """Resolved path to the agent-maintained audit memory ledger."""
        if self.audit_memory_path is not None:
            return self.audit_memory_path
        return self.data_dir / "audit_memory.md"

    @property
    def scout_memory_file(self) -> Path:
        """Resolved path to the agent-maintained scout memory ledger."""
        if self.scout_memory_path is not None:
            return self.scout_memory_path
        return self.data_dir / "scout_memory.md"


def load_settings() -> Settings:
    return Settings()
