"""Settings field mixin: bespoke + periodic agents.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class _PeriodicSettings(BaseModel):
    # --- bespoke per-repo periodic agents ---
    # Master switch: spawns a supervisor per repo that runs bespoke
    # per-repo periodic agents. Set discovery interval to 0 to disable.
    # How often (seconds) the bespoke supervisor refreshes its clone
    # and reconciles which YAMLs are scheduled. A new YAML committed
    # to the managed repo lands within this window; one removed gets
    # its loop cancelled in the same cycle.
    bespoke_discovery_interval_seconds: int = Field(
        default=600,
        description="Seconds between bespoke supervisor clone-refresh and YAML reconciliation cycles. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- audit agent (meta-audit for quality/security coverage) ---
    # Interval between periodic audit passes (seconds). Set to 0 to disable.
    audit_interval_seconds: int = Field(
        default=1209600,  # 14d — per-repo override via YAML
        description="Seconds between periodic audit passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- trace-health check ---
    # Interval between automatic trace-health checks (seconds).
    # Enforced minimum 3600s (1h) in the worker to avoid hammering Langfuse.
    # Set interval to 0 to disable.
    trace_health_interval_seconds: int = Field(
        default=86400,
        description="Seconds between automatic trace-health checks. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- trace-review ---
    # Interval between automatic trace-review passes (seconds). Default
    # 14 days. Enforced minimum 3600s (1h) in the worker. Set to 0 to disable.
    trace_review_interval_seconds: int = Field(
        default=1209600,  # 14d — per-repo override via YAML
        description="Seconds between automatic trace-review passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # (cost-cache warming is no longer a backend daemon — the board's
    # /tickets poll drives it on demand via runtime/cost_warm.py.)

    # --- timeout escalation ---
    # Interval between timeout-escalation passes (seconds). Default 3600.
    # Set to 0 to disable. Detects tickets stuck in AWAITING_USER_REPLY
    # longer than the threshold and escalates to BLOCKED.
    timeout_escalation_interval_seconds: int = Field(
        default=3600,
        description="Seconds between timeout-escalation passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    # Staleness threshold: tickets in AWAITING_USER_REPLY with updated_at
    # older than this many seconds are escalated to BLOCKED.
    # Default 259200 = 3 days.  Set to ≤ 0 to disable escalation
    # entirely while leaving the poll loop running.
    timeout_escalation_threshold_seconds: int = Field(
        default=259200,
        description="Staleness threshold: tickets in AWAITING_USER_REPLY older than this are escalated to BLOCKED.",
        json_schema_extra={"advanced": True},
    )

    # --- docstring-coverage agent (public-API documentation oversight) ---
    # Interval between periodic docstring-coverage passes (seconds).
    # Set to 0 to disable.
    docstring_coverage_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic docstring-coverage passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- test-gap agent (dedicated test-coverage oversight) ---
    # Interval between periodic test-gap passes (seconds). Set to 0 to disable.
    test_gap_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic test-gap passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- module-size agent (oversized-file oversight) ---
    # Interval between periodic module-size passes (seconds). Set to 0 to disable.
    module_size_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic module-size passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- agent-check agent (agent-definition coherence) ---
    # Interval between periodic agent-check passes (seconds). Set to 0 to
    # disable. Minimum enforced at 60s in the worker loop.
    agent_check_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic agent-check passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- health agent (codebase-health inspection) ---
    # MILL_HEALTH_PERIODIC=true.
    health_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic health passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

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
    survey_request_limit: int = Field(
        default=40,
        description="Per-call request budget for the survey agent.",
        json_schema_extra={"advanced": True},
    )
    # Per-survey-run web_fetch budget — a second tier of budget tracking
    # that spans the entire survey run (not reset between ask_web_knowledge
    # consults). Defaults to 5 calls / 500 KB total bytes, matching the
    # web_search cap — both are per-run, cross-consult budgets.
    survey_web_fetch_max_calls: int = Field(
        default=5,
        ge=1,
        description="Maximum web_fetch calls per survey run.",
        json_schema_extra={"advanced": True},
    )
    survey_web_fetch_max_total_bytes: int = Field(
        default=500_000,
        ge=0,
        description="Maximum fetch bytes per survey run. 0 disables.",
        json_schema_extra={"advanced": True},
    )
    # Per-survey-run web_search budget — caps web_search invocations at 5
    # per survey run regardless of how many ask_web_knowledge consults.
    survey_web_search_max_calls: int = Field(
        default=5,
        ge=1,
        description="Maximum web_search calls per survey run.",
        json_schema_extra={"advanced": True},
    )
    # MILL_SURVEY_PERIODIC=true. Default 1209600 (14 days). Minimum
    # enforced at 60s in the worker loop.
    survey_interval_seconds: int = Field(
        default=1209600,  # 14d — per-repo override via YAML
        description="Seconds between automatic survey passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- bc_check agent (backward-compatibility inspection) ---
    # MILL_BC_CHECK_PERIODIC=true. Minimum enforced at 60s in the
    # worker loop.
    bc_check_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic bc-check passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- module_curator agent (module-taxonomy drift detection) ---
    # MILL_MODULE_CURATOR_PERIODIC=true. Minimum enforced at 60s in
    # the worker loop.
    module_curator_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic module-curator passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    # Request budget for the module-curator run.  The agent walks the
    # repo tree, reads docs/modules.yaml, calls validate_artifact on
    # every cited path, and invokes explore scouts — a workload
    # comparable to ``explore`` (default 100) plus extra tool calls,
    # so 120 provides headroom.  Override with
    # MILL_MODULE_CURATOR_REQUEST_LIMIT if a board outgrows it.
    module_curator_request_limit: int = Field(
        default=120,
        ge=1,
        description="Request budget for the module-curator agent.",
        json_schema_extra={"advanced": True},
    )

    # --- mypy-baseline agent (mypy type-check baseline management) ---
    mypy_baseline_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic mypy-baseline passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- data-dir GC — deterministic periodic disk reclamation ---
    # MILL_DATA_DIR_GC_PERIODIC=true. Minimum enforced at 60 s
    # in the worker loop.
    data_dir_gc_interval_seconds: int = Field(
        default=86400,
        description="Seconds between periodic data-dir GC passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    # Opt-in GC: prune workspace directories of tickets in a terminal
    # state (CLOSED / EPIC_CLOSED / ANSWERED) during the data-dir GC
    # pass, before size measurement. Default False for one release
    # cycle; flip to True in a follow-up once observed clean.
    # Override with MILL_DATA_DIR_GC_PRUNE_CLOSED.
    data_dir_gc_prune_closed: bool = Field(
        default=False,
        description="When true, prune workspace directories of terminal-state tickets during GC.",
    )
    # Minimum age (seconds since the ticket entered its terminal state)
    # before its workspace becomes eligible for prune_closed GC. Recent
    # closures are kept for post-mortems. Default 7 days.
    # Override with MILL_DATA_DIR_GC_PRUNE_CLOSED_AGE_SECONDS.
    data_dir_gc_prune_closed_age_seconds: int = Field(
        default=604_800,
        ge=0,
        description="Minimum age (seconds) of a terminal ticket before its workspace is eligible for GC.",
        json_schema_extra={"advanced": True},
    )
    # Default-on GC: prune the reproducible git clones (``repo/`` and
    # ``repos/``) inside workspaces of terminal-state tickets at the
    # start of each data-dir GC pass, before size measurement.
    # Clones are the heavy tail of workspaces/ growth; description.md,
    # artifacts/ and screenshots/ are preserved for post-mortems
    # (unlike the whole-workspace prune_closed above).
    # Override with MILL_DATA_DIR_GC_PRUNE_TERMINAL_CLONES.
    data_dir_gc_prune_terminal_clones: bool = Field(
        default=True,
        description="When true, prune git clones inside workspaces of terminal-state tickets.",
    )
    # Minimum age (seconds since the ticket entered its terminal state)
    # before its clones are pruned. Clones are cheap to recreate, so
    # the guard is short. Default 1 day.
    # Override with MILL_DATA_DIR_GC_PRUNE_TERMINAL_CLONES_AGE_SECONDS.
    data_dir_gc_prune_terminal_clones_age_seconds: int = Field(
        default=86_400,
        ge=0,
        description="Minimum age (seconds) before terminal-ticket clones are pruned.",
        json_schema_extra={"advanced": True},
    )
    # Default-on GC: prune the ``.venv`` inside workspaces of PARKED
    # tickets — BLOCKED and the human-approval waits. These are not
    # terminal, so neither prune_closed nor prune_terminal_clones can
    # touch them, and a parked ticket can sit for weeks. Measured
    # 2026-08-06 on the deploy box: 157 parked workspaces holding 45 GB
    # of .venv, 34 GB of it under BLOCKED — on a volume whose exhaustion
    # had itself blocked 146 of those tickets. That is a closed loop:
    # ENOSPC blocks a ticket, and the block then pins the disk the next
    # ticket needs.
    #
    # Only ``.venv`` is removed, never the clone: uncommitted work and
    # git history stay inspectable for the human the ticket is parked
    # for, while ``uv sync`` reproduces the venv on resume.
    # Override with MILL_DATA_DIR_GC_PRUNE_PARKED_VENVS=false.
    data_dir_gc_prune_parked_venvs: bool = Field(
        default=True,
        description="When true, prune .venv directories inside workspaces of parked (blocked/awaiting-human) tickets.",
    )
    # Minimum age (seconds since the ticket entered its parked state)
    # before its .venv is pruned. Short, because the venv is pure cache
    # and a ticket resumed within the hour still re-syncs cheaply from
    # the shared package cache. Default 1 hour.
    # Override with MILL_DATA_DIR_GC_PRUNE_PARKED_VENVS_AGE_SECONDS.
    data_dir_gc_prune_parked_venvs_age_seconds: int = Field(
        default=3_600,
        ge=0,
        description="Minimum age (seconds) a ticket must have been parked before its .venv is pruned.",
        json_schema_extra={"advanced": True},
    )
    # Default-on DB row GC: purge oldest terminal-ticket rows (and their
    # associated events, comments, and proposed actions) when the count
    # of terminal tickets exceeds max_archived_tickets. This is a
    # periodic safety net — the reactive trigger on transition still
    # fires, but this ensures stalled boards (e.g. tickets piling up in
    # DONE, which is not an archivable state) eventually get cleaned.
    # Override with MILL_DATA_DIR_GC_PRUNE_DB_ROWS=false.
    data_dir_gc_prune_db_rows: bool = Field(
        default=True,
        description="When true, purge oldest terminal-ticket database rows exceeding max_archived_tickets.",
    )
    # Default-on GC: truncate over-cap *_memory.md files on disk
    # before size measurement, using the same tail_keep primitive
    # the agent already uses at read/write time.  Eliminates recurring
    # unbounded: tickets for memory ledgers that grew under old code
    # paths and are rarely re-written.
    # Override with MILL_DATA_DIR_GC_PRUNE_MEMORY_LEDGERS=false.
    data_dir_gc_prune_memory_ledgers: bool = Field(
        default=True,
        description="When true, truncate over-cap memory ledger files on disk before size measurement.",
    )
    # Default-on GC: prune orphan workspace directories (ticket absent
    # from the board DB) older than the configured age at the start of
    # each data-dir GC pass, before size measurement. Orphans are
    # never filed as tickets — they are GC'd silently.
    # Override with MILL_DATA_DIR_GC_PRUNE_ORPHANS=false.
    data_dir_gc_prune_orphans: bool = Field(
        default=True,
        description="When true, prune orphan workspace directories (ticket absent from DB) older than the configured age.",
    )
    # Minimum age (seconds since the ticket-ID timestamp) before an
    # orphan workspace becomes eligible for GC. Default 1 day — long
    # enough to never race a just-created workspace whose ticket row
    # hasn't been committed yet.
    # Override with MILL_DATA_DIR_GC_PRUNE_ORPHANS_AGE_SECONDS.
    data_dir_gc_prune_orphans_age_seconds: int = Field(
        default=86_400,
        ge=0,
        description="Minimum age (seconds) of an orphan workspace before GC.",
        json_schema_extra={"advanced": True},
    )

    # --- dependabot-alert ingest (deterministic cross-repo poll) ---
    # MILL_DEPENDABOT_INGEST_PERIODIC=true. Minimum enforced at 60 s in the
    # worker loop. Default 86400 (1 day).
    # Override with MILL_DEPENDABOT_INGEST_INTERVAL_SECONDS.
    dependabot_ingest_interval_seconds: int = Field(
        default=86_400,
        description="Seconds between Dependabot ingest passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    # Maximum number of Dependabot drafts created per ingest pass (across all
    # repos in that pass). Findings beyond this cap are dropped and
    # re-considered on the next scheduled pass.
    # Override with MILL_DEPENDABOT_INGEST_MAX_DRAFTS_PER_PASS.
    dependabot_ingest_max_drafts_per_pass: int = Field(
        default=5,
        ge=0,
        description="Maximum Dependabot drafts per ingest pass. 0 disables.",
        json_schema_extra={"advanced": True},
    )

    # Default ceiling on draft tickets created by ONE periodic pass run.
    # Applies to every pass that does not set its own cap — 16 of the 17
    # built-in periodic passes and all bespoke passes were previously
    # unbounded, which is how the board reached 325 open drafts against
    # single-digit daily throughput. Findings past the cap are dropped and
    # resurface on the next run, so the pass stays useful without letting
    # one run flood the board. A pass that sets max_drafts (or
    # max_drafts_fn) keeps its own value.
    # Override with MILL_PERIODIC_MAX_DRAFTS_PER_RUN; 0 disables draft
    # creation from uncapped passes entirely.
    periodic_max_drafts_per_run: int = Field(
        default=3,
        ge=0,
        description=(
            "Default maximum draft tickets per periodic pass run, for passes "
            "without their own cap. 0 disables draft creation from them."
        ),
        json_schema_extra={"advanced": True},
    )

    # --- completeness_check agent (feature-wiring completeness) ---
    # MILL_COMPLETENESS_CHECK_PERIODIC=true. Minimum enforced at 60s
    # in the worker loop.
    completeness_check_interval_seconds: int = Field(
        default=1209600,  # 14d — per-repo override via YAML
        description="Seconds between periodic completeness-check passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    completeness_check_request_limit: int = Field(
        default=80,
        description="Request budget for the completeness-check agent.",
        json_schema_extra={"advanced": True},
    )

    # --- forge-parity agent (forge adapter drift detection) ---
    # MILL_FORGE_PARITY_PERIODIC=true. Default 604800 (1 week). Minimum
    # enforced at 60s in the worker loop.
    forge_parity_interval_seconds: int = Field(
        default=604800,
        description="Seconds between periodic forge-parity passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- copy-paste agent (deterministic clone detection and triage) ---
    # MILL_COPY_PASTE_PERIODIC=true. Default 604800 (1 week). Minimum
    # enforced at 60s in the worker loop.
    copy_paste_interval_seconds: int = Field(
        default=604800,
        description="Seconds between periodic copy-paste passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- state-sync agent (cross-surface State enum consistency) ---
    # MILL_STATE_SYNC_PERIODIC=true. Default 604800 (7 days). Minimum
    # enforced at 60s in the worker loop.
    state_sync_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic state-sync passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- frontend-sync agent (board frontend → ticket system sync) ---
    # MILL_FRONTEND_SYNC_PERIODIC=true. Default 604800 (7 days). Minimum
    # enforced at 60s in the worker loop.
    frontend_sync_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic frontend-sync passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- pin-bump agent (scheduled dependency pin-bump PR actuator) ---
    # MILL_PIN_BUMP_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    pin_bump_interval_seconds: int = Field(
        default=86400,
        description="Seconds between periodic pin-bump passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- triage-boilerplate agent (recurring triage pattern detection) ---
    # MILL_TRIAGE_BOILERPLATE_PERIODIC=true. Default 604800 (1 week). Minimum
    # enforced at 60s in the worker loop.
    triage_boilerplate_interval_seconds: int = Field(
        default=604800,
        description="Seconds between periodic triage-boilerplate passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- config-sync agent (config ↔ .env ↔ docs drift detection) ---
    # MILL_CONFIG_SYNC_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    config_sync_interval_seconds: int = Field(
        default=86400,
        description="Seconds between automatic config-sync passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- member-sync (deterministic workspace-member discovery/registration) ---
    # MILL_MEMBER_SYNC_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    member_sync_interval_seconds: int = Field(
        default=86400,
        description="Seconds between automatic member-sync passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- meta-agent (cross-repo extraction/alignment survey) ---
    # Minimum enforced at 60 s in the worker loop.
    meta_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between automatic meta-agent passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- run-health (global, cross-board run-registry monitor) ---
    run_health_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between automatic run-health passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    # Lookback window (hours) over which run registries are scanned.
    run_health_window_hours: int = Field(
        default=168,
        description="Lookback window (hours) for run-registry scans.",
        json_schema_extra={"advanced": True},
    )
    # Board the run-health agent files its drafts to (the mill board).
    run_health_target_repo_id: str = Field(
        default="robotsix-mill",
        description="Board the run-health agent files its drafts to.",
    )
    # Path to the run-health agent's Markdown memory ledger. Override to pin
    # a specific path; unset (default) derives <data_dir>/<board>/run_health_memory.md.
    run_health_memory_path: Path | None = Field(
        default=None,
        description="Path to the run-health agent's Markdown memory ledger.",
    )

    # --- CI-debt recheck (auto-resume tickets blocked by pre-existing CI debt) ---
    # MILL_CI_DEBT_RECHECK_PERIODIC=true.  Default 3600 (1 hour).
    # Minimum enforced at 60 s in the worker loop.
    ci_debt_recheck_interval_seconds: int = Field(
        default=3600,
        description="Seconds between CI-debt recheck passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- changelog-autofill (schedule-only pass that updates changelogs from merged PRs) ---
    # MILL_CHANGELOG_AUTOFILL_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60 s in the worker loop.
    changelog_autofill_interval_seconds: int = Field(
        default=86400,
        description="Seconds between changelog-autofill passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- diagnostic (daily deterministic diagnostic agent) ---
    diagnostic_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between automatic diagnostic passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    # Board the diagnostic agent routes board/trace activity to.
    diagnostic_target_repo_id: str = Field(
        default="robotsix-mill",
        description="Board the diagnostic agent routes findings to.",
    )
    # Repos the daily diagnostic agent monitors each pass. Empty (default)
    # falls back to the single `diagnostic_target_repo_id` for backward
    # compatibility. Add/remove repos here — no code change required.
    # --- config pin-drift check -------------------------------------------
    config_pin_drift_interval_seconds: int = Field(
        default=86_400,
        ge=60,
        description="Seconds between config pin-drift passes.",
    )
    # Keys whose pin is KNOWN to differ from the default on purpose. Drift is
    # reported against this baseline — the same ratchet the mypy baseline uses
    # — so a deliberate operator choice is recorded once and only genuinely new
    # divergence surfaces.
    config_pin_drift_baseline: list[str] = Field(
        default_factory=list,
        description=(
            "Settings keys whose pinned value deliberately differs from the "
            "code default; excluded from pin-drift reporting."
        ),
        json_schema_extra={"advanced": True},
    )

    diagnostic_monitored_repo_ids: list[str] = Field(
        default_factory=list,
        description="Repos the daily diagnostic agent monitors each pass.",
    )

    # --- diagnostic event store ---
    # Explicit file path for the JSONL diagnostic event store.  When
    # unset (default) the path is derived per-repo:
    # ``<data_dir>/<board_id>/diagnostic_events.jsonl``.
    diagnostic_events_path: Path | None = Field(
        default=None,
        description="Explicit path for the diagnostic event store JSONL file.",
    )

    # --- diagnostic event aging ---
    # Events older than this many days are silently dropped during
    # list/filter operations.  This prevents stale failures from
    # permanently inflating the recurring-CI count.  Set to 0 to keep
    # events indefinitely (original behaviour).
    diagnostic_events_max_age_days: int = Field(
        default=90,
        ge=0,
        description=(
            "Days after which diagnostic events are considered stale "
            "and excluded from recurring-failure counts.  0 = no expiry."
        ),
        json_schema_extra={"advanced": True},
    )

    # --- recurring CI failure fix-proposal generation ---
    # Number of distinct tickets that must hit the same normalized
    # CI failure key before the recurring-CI diagnostic check auto-files
    # a fix-proposal draft ticket.  Set to 0 to disable.
    diagnostic_ci_failure_threshold: int = Field(
        default=3,
        ge=0,
        description="Distinct-ticket threshold for auto-filing CI fix proposals.",
        json_schema_extra={"advanced": True},
    )

    # --- orphaned-PR check (deterministic per-repo stale-PR cleanup) ---
    # MILL_ORPHANED_PR_CHECK_PERIODIC=true.  Minimum enforced at 3600 s
    # (1 hour) in the worker loop.
    orphaned_pr_check_interval_seconds: int = Field(
        default=86400,
        description="Seconds between orphaned-PR check passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )
    # Minimum age (hours) of a ticket before its PR is considered for
    # orphan classification.  Skips tickets younger than this to avoid
    # racing the deliver stage.
    orphaned_pr_min_age_hours: int = Field(
        default=4,
        ge=1,
        description="Minimum ticket age (hours) before its PR is considered for orphan classification.",
        json_schema_extra={"advanced": True},
    )
    # Maximum number of combined close+file actions per pass run.
    # Findings beyond this cap are deferred to the next scheduled pass.
    orphaned_pr_max_actions_per_pass: int = Field(
        default=5,
        ge=1,
        description="Maximum combined close+file actions per orphaned-PR pass.",
        json_schema_extra={"advanced": True},
    )
    # Dry-run mode: log intent only, make zero forge mutations.
    # Default True for safety — flip to False to enable real actions.
    orphaned_pr_dry_run: bool = Field(
        default=True,
        description="When true, log intent only for orphaned-PR actions — no forge mutations.",
    )
    # Bot author logins trusted for orphaned-PR actions. When non-empty,
    # only PRs whose author_login is in this list are eligible for
    # auto-close or tracking-ticket filing.  When empty, the runner
    # resolves the bot login via ``forge.get_authenticated_user_login()``
    # and uses that as the sole trusted login.  If that also returns an
    # empty string, the author guard is bypassed (fail-open).
    orphaned_pr_bot_logins: list[str] = Field(
        default_factory=list,
        description="Bot author logins trusted for orphaned-PR actions. Empty = auto-resolve from forge.",
    )

    # Per-type action caps (applied in addition to orphaned_pr_max_actions_per_pass).
    # Separate limits avoid a burst of close actions consuming all of the combined cap.
    orphaned_pr_max_closes_per_pass: int = Field(
        default=10,
        ge=1,
        description="Maximum close actions per orphaned-PR pass (in addition to combined cap).",
        json_schema_extra={"advanced": True},
    )
    orphaned_pr_max_files_per_pass: int = Field(
        default=5,
        ge=1,
        description="Maximum file-ticket actions per orphaned-PR pass (in addition to combined cap).",
        json_schema_extra={"advanced": True},
    )
    # Opt-in: also file a tracking ticket for FOREIGN (non-board) open PRs —
    # those whose head branch does NOT start with ``settings.branch_prefix``
    # (e.g. ``dependabot/*``, human ``feature/*`` branches). Foreign PRs are
    # never closed by this pass; a tracking ticket is filed so the board can
    # review and merge or close them. Default False (opt-in). File-ticket
    # actions count against the same per-pass caps as the mill-PR actions.
    orphaned_pr_track_foreign_prs: bool = Field(
        default=False,
        description="When true, also file tracking tickets for foreign (non-mill) open PRs.",
    )

    # --- repo-description-sync (keeps forge description in sync with README) ---
    # (7 days). Enforced minimum 3600s (1 hour) in the worker.
    repo_description_sync_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between repo-description-sync passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- roadmap-sync (keeps forge roadmap project in sync with board epics) ---
    # Enforced minimum 3600s (1 hour) in the worker.
    roadmap_sync_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between roadmap-sync passes. 0 = disabled.",
        json_schema_extra={"advanced": True},
    )

    # --- board-hygiene (draft TTL auto-close + open-ticket cap) ---
    # auto-closed by the board-hygiene pass.  "Untouched" means no state
    # transition, comment, or event has updated ``Ticket.updated_at``.
    # Drafts that are children of an epic (``parent_id IS NOT NULL``) and
    # epics themselves are skipped — only standalone drafts are closed.
    # Set to 0 to disable (no drafts will be auto-closed regardless of age).
    board_hygiene_draft_ttl_days: int = Field(
        default=7,
        ge=0,
        description="Maximum age (days) of an untouched draft before auto-close. 0 = disabled.",
    )
    # Ceiling on total open (non-terminal) tickets per board.  When the
    # count reaches this cap, machine-ingest requests (``POST /tickets/ingest``)
    # append their findings to a rollup epic instead of creating new
    # standalone tickets.  Human-created tickets (``POST /tickets``) are
    # exempt.  Set to 0 to disable the cap (no limit).
    board_hygiene_max_open_tickets: int = Field(
        default=0,
        ge=0,
        description="Ceiling on total open tickets per board. 0 = disabled.",
    )
