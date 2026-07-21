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
    # When True, the worker spawns a supervisor per repo that clones the
    # repo, scans ``.robotsix-mill/agents/<name>.yaml``, and runs each
    # bespoke agent on its own declared interval. Master switch — set
    # False to disable bespoke-agent discovery for the entire process
    # (per-repo opt-out is controlled by RepoConfig.bespoke_periodic).
    bespoke_periodic: bool = Field(
        default=True,
        description="Master switch: when true, spawn a supervisor per repo that runs bespoke per-repo periodic agents.",
    )
    # How often (seconds) the bespoke supervisor refreshes its clone
    # and reconciles which YAMLs are scheduled. A new YAML committed
    # to the managed repo lands within this window; one removed gets
    # its loop cancelled in the same cycle.
    bespoke_discovery_interval_seconds: int = Field(
        default=600,
        description="Seconds between bespoke supervisor clone-refresh and YAML reconciliation cycles.",
    )

    # --- audit agent (meta-audit for quality/security coverage) ---
    # When True, the worker runs periodic audit passes at the configured
    # interval. Default False (opt-in).
    audit_periodic: bool = Field(
        default=True,
        description="When true, run periodic meta-audit passes for quality/security coverage.",
    )
    # Interval between periodic audit passes (seconds). Only used when
    # MILL_AUDIT_PERIODIC=true.
    audit_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic audit passes.",
    )

    # --- trace-health check ---
    # When True, the worker runs periodic trace-health checks at the
    # configured interval. Default False (opt-in).
    trace_health_periodic: bool = Field(
        default=True,
        description="When true, run periodic trace-health checks.",
    )
    # Interval between automatic trace-health checks (seconds). Only
    # used when MILL_TRACE_HEALTH_PERIODIC=true. Enforced minimum 3600s
    # (1h) in the worker to avoid hammering Langfuse.
    trace_health_interval_seconds: int = Field(
        default=86400,
        description="Seconds between automatic trace-health checks.",
    )

    # --- trace-review ---
    # When True, the worker runs periodic trace-review passes at the
    # configured interval. Scans recent Langfuse traces, flags outliers
    # statistically (cost / observation count vs. batch median ×
    # multiplier) and absolutely (tool errors, rejected generations,
    # ask_user loops, explore storms, repeated-tool ceilings), runs
    # the cheap flash inspector over the flagged subset, and files
    # draft tickets with proposed solutions. Default True (opt-out).
    trace_review_periodic: bool = Field(
        default=True,
        description="When true, run periodic trace-review passes (scans traces, flags outliers, files drafts).",
    )
    # Interval between automatic trace-review passes (seconds). Default
    # daily. Enforced minimum 3600s (1h) in the worker.
    trace_review_interval_seconds: int = Field(
        default=86400,
        description="Seconds between automatic trace-review passes.",
    )

    # (cost-cache warming is no longer a backend daemon — the board's
    # /tickets poll drives it on demand via runtime/cost_warm.py.)

    # --- timeout escalation ---
    # When True, the worker runs periodic timeout-escalation passes at the
    # configured interval. Default True (opt-out). Detects tickets stuck in
    # AWAITING_USER_REPLY longer than the threshold and escalates to BLOCKED.
    timeout_escalation_periodic: bool = Field(
        default=True,
        description="When true, run periodic timeout-escalation passes for stuck AWAITING_USER_REPLY tickets.",
    )
    # Interval between timeout-escalation passes (seconds). Only used when
    # MILL_TIMEOUT_ESCALATION_PERIODIC=true.
    timeout_escalation_interval_seconds: int = Field(
        default=3600,
        description="Seconds between timeout-escalation passes.",
    )
    # Staleness threshold: tickets in AWAITING_USER_REPLY with updated_at
    # older than this many seconds are escalated to BLOCKED.
    # Default 259200 = 3 days.  Set to ≤ 0 to disable escalation
    # entirely while leaving the poll loop running.
    timeout_escalation_threshold_seconds: int = Field(
        default=259200,
        description="Staleness threshold: tickets in AWAITING_USER_REPLY older than this are escalated to BLOCKED.",
    )

    # --- docstring-coverage agent (public-API documentation oversight) ---
    # When True, the worker runs periodic docstring-coverage passes.
    docstring_coverage_periodic: bool = Field(
        default=True,
        description="When true, run periodic docstring-coverage passes for public-API documentation oversight.",
    )
    # Interval between periodic docstring-coverage passes (seconds).
    docstring_coverage_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic docstring-coverage passes.",
    )

    # --- test-gap agent (dedicated test-coverage oversight) ---
    # Model for the test-gap agent. Defaults to the same capable model
    # as audit/health. Override with MILL_TEST_GAP_MODEL.
    # When True, the worker runs periodic test-gap passes at the
    # configured interval. Default False (opt-in).
    test_gap_periodic: bool = Field(
        default=True,
        description="When true, run periodic test-gap passes for test-coverage oversight.",
    )
    # Interval between periodic test-gap passes (seconds). Only used
    # when MILL_TEST_GAP_PERIODIC=true.
    test_gap_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic test-gap passes.",
    )

    # --- module-size agent (oversized-file oversight) ---
    # When True, the worker runs periodic module-size passes.
    module_size_periodic: bool = Field(
        default=True,
        description="When true, run periodic module-size passes for oversized-file oversight.",
    )
    # Interval between periodic module-size passes (seconds).
    module_size_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic module-size passes.",
    )

    # --- agent-check agent (agent-definition coherence) ---
    # Opt-in periodic agent-check pass. Defaults to False (off); flip
    # to true to schedule the pass every ``agent_check_interval_seconds``
    # in addition to the on-demand POST /agent-check and CLI.
    agent_check_periodic: bool = Field(
        default=True,
        description="When true, run periodic agent-check passes for agent-definition coherence.",
    )
    # Seconds between periodic agent-check passes when
    # MILL_AGENT_CHECK_PERIODIC=true. Minimum enforced at 60s in the
    # worker loop.
    agent_check_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic agent-check passes.",
    )

    # --- health agent (codebase-health inspection) ---
    # Model for the health agent. Defaults to the same capable model as
    # audit. Override with MILL_HEALTH_MODEL.
    # When True, the worker runs periodic health passes at the
    # configured interval. Default False (opt-in).
    health_periodic: bool = Field(
        default=True,
        description="When true, run periodic health passes for codebase-health inspection.",
    )
    # Interval between periodic health passes (seconds). Only used when
    # MILL_HEALTH_PERIODIC=true.
    health_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic health passes.",
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
    )
    # Per-survey-run web_fetch budget — a second tier of budget tracking
    # that spans the entire survey run (not reset between ask_web_knowledge
    # consults). Defaults to 5 calls / 500 KB total bytes, matching the
    # web_search cap — both are per-run, cross-consult budgets.
    survey_web_fetch_max_calls: int = Field(
        default=5,
        ge=1,
        description="Maximum web_fetch calls per survey run.",
    )
    survey_web_fetch_max_total_bytes: int = Field(
        default=500_000,
        ge=0,
        description="Maximum fetch bytes per survey run. 0 disables.",
    )
    # Per-survey-run web_search budget — caps web_search invocations at 5
    # per survey run regardless of how many ask_web_knowledge consults.
    survey_web_search_max_calls: int = Field(
        default=5,
        ge=1,
        description="Maximum web_search calls per survey run.",
    )
    # Opt-in periodic survey pass. Defaults to True (on by default —
    # "default yes"). Flip to false to disable the automatic weekly
    # cadence while still allowing on-demand POST /survey and
    # board-button triggers.
    survey_periodic: bool = Field(
        default=True,
        description="When true, run periodic survey passes for OSS project discovery.",
    )
    # Seconds between automatic survey passes when
    # MILL_SURVEY_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    survey_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between automatic survey passes.",
    )

    # --- bc_check agent (backward-compatibility inspection) ---
    # Opt-in periodic bc-check pass. Defaults to False (off); flip to
    # true to schedule the pass every ``bc_check_interval_seconds`` in
    # addition to the on-demand CLI.
    bc_check_periodic: bool = Field(
        default=True,
        description="When true, run periodic backward-compatibility inspection passes.",
    )
    # Seconds between periodic bc-check passes when
    # MILL_BC_CHECK_PERIODIC=true. Minimum enforced at 60s in the
    # worker loop.
    bc_check_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic bc-check passes.",
    )

    # --- module_curator agent (module-taxonomy drift detection) ---
    # Opt-in periodic module-curator pass. Defaults to True (opt-out);
    # set false to disable the daily module-taxonomy drift check on
    # this repo.
    module_curator_periodic: bool = Field(
        default=True,
        description="When true, run daily module-taxonomy drift detection.",
    )
    # Seconds between periodic module-curator passes when
    # MILL_MODULE_CURATOR_PERIODIC=true. Minimum enforced at 60s in
    # the worker loop.
    module_curator_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic module-curator passes.",
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
    )

    # --- data-dir GC — deterministic periodic disk reclamation ---
    # Master switch for the periodic data-dir GC pass.
    # Default True — the agent is harmless when idle (no findings).
    data_dir_gc_periodic: bool = Field(
        default=True,
        description="Master switch for the periodic data-dir GC pass.",
    )
    # Seconds between periodic data-dir GC passes when
    # MILL_DATA_DIR_GC_PERIODIC=true. Minimum enforced at 60 s
    # in the worker loop.
    data_dir_gc_interval_seconds: int = Field(
        default=86400,
        description="Seconds between periodic data-dir GC passes.",
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
    )

    # --- dependabot-alert ingest (deterministic cross-repo poll) ---
    # Master switch for the Dependabot vulnerability-alert ingest poll loop.
    # When on, the worker iterates every registered repo, lists its OPEN
    # GitHub Dependabot alerts, and files one deduped draft per new alert.
    # Default True — harmless when idle (no alerts → no drafts).
    # Override with MILL_DEPENDABOT_INGEST_PERIODIC.
    dependabot_ingest_periodic: bool = Field(
        default=True,
        description="When true, iterate registered repos and file deduped drafts for new Dependabot alerts.",
    )
    # Seconds between Dependabot ingest passes when
    # MILL_DEPENDABOT_INGEST_PERIODIC=true. Minimum enforced at 60 s in the
    # worker loop. Default 86400 (1 day).
    # Override with MILL_DEPENDABOT_INGEST_INTERVAL_SECONDS.
    dependabot_ingest_interval_seconds: int = Field(
        default=86_400,
        description="Seconds between Dependabot ingest passes.",
    )
    # Maximum number of Dependabot drafts created per ingest pass (across all
    # repos in that pass). Findings beyond this cap are dropped and
    # re-considered on the next scheduled pass.
    # Override with MILL_DEPENDABOT_INGEST_MAX_DRAFTS_PER_PASS.
    dependabot_ingest_max_drafts_per_pass: int = Field(
        default=5,
        ge=0,
        description="Maximum Dependabot drafts per ingest pass. 0 disables.",
    )

    # --- completeness_check agent (feature-wiring completeness) ---
    # Opt-in periodic completeness-check pass. Defaults to False (off);
    # flip to true to schedule the pass every
    # ``completeness_check_interval_seconds`` in addition to the
    # on-demand CLI.
    completeness_check_periodic: bool = Field(
        default=True,
        description="When true, run periodic feature-wiring completeness checks.",
    )
    # Seconds between periodic completeness-check passes when
    # MILL_COMPLETENESS_CHECK_PERIODIC=true. Minimum enforced at 60s
    # in the worker loop.
    completeness_check_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic completeness-check passes.",
    )
    completeness_check_request_limit: int = Field(
        default=80,
        description="Request budget for the completeness-check agent.",
    )

    # --- forge-parity agent (forge adapter drift detection) ---
    # Opt-in periodic forge-parity pass. Defaults to True (opt-out);
    # set false to disable the weekly forge-adapter drift detection.
    forge_parity_periodic: bool = Field(
        default=True,
        description="When true, run weekly forge-adapter drift detection.",
    )
    # Seconds between periodic forge-parity passes when
    # MILL_FORGE_PARITY_PERIODIC=true. Default 604800 (1 week). Minimum
    # enforced at 60s in the worker loop.
    forge_parity_interval_seconds: int = Field(
        default=604800,
        description="Seconds between periodic forge-parity passes.",
    )

    # --- copy-paste agent (deterministic clone detection and triage) ---
    # Opt-in periodic copy-paste pass. Defaults to True (opt-out);
    # set false to disable the weekly clone-detection sweep.
    copy_paste_periodic: bool = Field(
        default=True,
        description="When true, run weekly clone-detection sweeps.",
    )
    # Seconds between periodic copy-paste passes when
    # MILL_COPY_PASTE_PERIODIC=true. Default 604800 (1 week). Minimum
    # enforced at 60s in the worker loop.
    copy_paste_interval_seconds: int = Field(
        default=604800,
        description="Seconds between periodic copy-paste passes.",
    )

    # --- state-sync agent (cross-surface State enum consistency) ---
    # Opt-in periodic state-sync pass. Defaults to True (opt-out);
    # set false to disable the daily State-enum consistency check.
    state_sync_periodic: bool = Field(
        default=True,
        description="When true, run daily State-enum consistency checks.",
    )
    # Seconds between periodic state-sync passes when
    # MILL_STATE_SYNC_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    state_sync_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic state-sync passes.",
    )

    # --- frontend-sync agent (board frontend → ticket system sync) ---
    # Opt-in periodic frontend-sync pass. Defaults to True (opt-out);
    # set false to disable the daily board frontend sync pass.
    frontend_sync_periodic: bool = Field(
        default=True,
        description="When true, run daily board frontend sync checks.",
    )
    # Seconds between periodic frontend-sync passes when
    # MILL_FRONTEND_SYNC_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    frontend_sync_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between periodic frontend-sync passes.",
    )

    # --- pin-bump agent (scheduled dependency pin-bump PR actuator) ---
    # Opt-in periodic pin-bump pass. Defaults to False (off).
    # The PR actuator (SHA-latest resolution → pyproject.toml edit →
    # uv lock → cross-repo PR) is now delivered.
    pin_bump_periodic: bool = Field(
        default=False,
        description="When true, run scheduled dependency pin-bump PR actuator passes.",
    )
    # Seconds between periodic pin-bump passes when
    # MILL_PIN_BUMP_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    pin_bump_interval_seconds: int = Field(
        default=86400,
        description="Seconds between periodic pin-bump passes.",
    )

    # --- triage-boilerplate agent (recurring triage pattern detection) ---
    # Opt-in periodic triage-boilerplate pass. Defaults to True (opt-out);
    # set false to disable the weekly triage-pattern scan.
    triage_boilerplate_periodic: bool = Field(
        default=True,
        description="When true, run weekly triage-pattern scans.",
    )
    # Seconds between periodic triage-boilerplate passes when
    # MILL_TRIAGE_BOILERPLATE_PERIODIC=true. Default 604800 (1 week). Minimum
    # enforced at 60s in the worker loop.
    triage_boilerplate_interval_seconds: int = Field(
        default=604800,
        description="Seconds between periodic triage-boilerplate passes.",
    )

    # --- config-sync agent (config ↔ .env ↔ docs drift detection) ---
    # Opt-in periodic config-sync pass. Default false (agents default off
    # unless noted). Set true to enable automatic daily drift detection.
    config_sync_periodic: bool = Field(
        default=True,
        description="When true, run daily config ↔ .env ↔ docs drift detection.",
    )
    # Seconds between automatic config-sync passes when
    # MILL_CONFIG_SYNC_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    config_sync_interval_seconds: int = Field(
        default=86400,
        description="Seconds between automatic config-sync passes.",
    )

    # --- member-sync (deterministic workspace-member discovery/registration) ---
    # Opt-in periodic member-sync pass. Default true: workspace members are
    # auto-discovered from each managed repo's vcs2l manifest and registered
    # in config/repos.yaml. Deterministic — no model, no memory ledger.
    member_sync_periodic: bool = Field(
        default=True,
        description="When true, auto-discover workspace members from managed repo vcs2l manifests.",
    )
    # Seconds between automatic member-sync passes when
    # MILL_MEMBER_SYNC_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60s in the worker loop.
    member_sync_interval_seconds: int = Field(
        default=86400,
        description="Seconds between automatic member-sync passes.",
    )

    # --- meta-agent (cross-repo extraction/alignment survey) ---
    # Master switch for the daily meta-agent pass. Defaults to False
    # (off) — the operator must register the meta board in repos.yaml
    # first.  Flip to True to enable the global daily schedule.
    meta_periodic: bool = Field(
        default=False,
        description="When true, run the daily cross-repo meta-agent pass for extraction/alignment proposals.",
    )
    # Seconds between automatic meta-agent passes. Default 86400 (1 day).
    # Minimum enforced at 60 s in the worker loop.
    meta_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between automatic meta-agent passes.",
    )

    # --- run-health (global, cross-board run-registry monitor) ---
    # When True, a global daily pass reads every board's run registry over a
    # window, flags failed/degraded runs deterministically, runs one LLM pass
    # to separate real failures from legitimate empties, and files
    # high-confidence draft tickets to the mill board. On by default: this is
    # the meta-checker that watches the OTHER periodic agents' health, so it
    # should run everywhere unless a deployment explicitly opts out.
    run_health_periodic: bool = Field(
        default=True,
        description="When true, run daily cross-board run-registry health monitoring.",
    )
    # Seconds between automatic run-health passes. Default 86400 (1 day).
    run_health_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between automatic run-health passes.",
    )
    # Lookback window (hours) over which run registries are scanned.
    run_health_window_hours: int = Field(
        default=168,
        description="Lookback window (hours) for run-registry scans.",
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
    # When True, a periodic pass re-checks BLOCKED tickets whose block note
    # cites pre-existing target-branch CI debt.  When all the named workflows
    # have turned green on the target branch, the ticket is auto-resumed back
    # to IMPLEMENT_COMPLETE.  On by default — harmless when idle (no matching
    # BLOCKED tickets → no-op).
    ci_debt_recheck_periodic: bool = Field(
        default=True,
        description="When true, periodically re-check BLOCKED tickets with pre-existing CI debt and auto-resume when debt clears.",
    )
    # Seconds between CI-debt recheck passes when
    # MILL_CI_DEBT_RECHECK_PERIODIC=true.  Default 3600 (1 hour).
    # Minimum enforced at 60 s in the worker loop.
    ci_debt_recheck_interval_seconds: int = Field(
        default=3600,
        description="Seconds between CI-debt recheck passes.",
    )

    # --- changelog-autofill (schedule-only pass that updates changelogs from merged PRs) ---
    # Master switch for the changelog-autofill schedule-only pass. Defaults to
    # True (opt-out) — the pass reads merged PRs and writes CHANGELOG entries.
    changelog_autofill_periodic: bool = Field(
        default=True,
        description="When true, run periodic changelog-autofill passes that update changelogs from merged PRs.",
    )
    # Seconds between automatic changelog-autofill passes when
    # MILL_CHANGELOG_AUTOFILL_PERIODIC=true. Default 86400 (1 day). Minimum
    # enforced at 60 s in the worker loop.
    changelog_autofill_interval_seconds: int = Field(
        default=86400,
        description="Seconds between changelog-autofill passes.",
    )

    # --- diagnostic (daily deterministic diagnostic agent) ---
    # When True, a global daily pass iterates the pluggable diagnostic check
    # registry. Off by default — the skeleton ships with zero checks; later
    # tickets add checks then operators opt in.
    diagnostic_periodic: bool = Field(
        default=False,
        description="When true, run daily deterministic diagnostic checks.",
    )
    # Seconds between automatic diagnostic passes. Default 86400 (1 day).
    diagnostic_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between automatic diagnostic passes.",
    )
    # Board the diagnostic agent routes board/trace activity to.
    diagnostic_target_repo_id: str = Field(
        default="robotsix-mill",
        description="Board the diagnostic agent routes findings to.",
    )
    # Repos the daily diagnostic agent monitors each pass. Empty (default)
    # falls back to the single `diagnostic_target_repo_id` for backward
    # compatibility. Add/remove repos here — no code change required.
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

    # --- recurring CI failure fix-proposal generation ---
    # Number of distinct tickets that must hit the same normalized
    # CI failure key before the recurring-CI diagnostic check auto-files
    # a fix-proposal draft ticket.  Set to 0 to disable.
    diagnostic_ci_failure_threshold: int = Field(
        default=3,
        ge=0,
        description="Distinct-ticket threshold for auto-filing CI fix proposals.",
    )

    # --- orphaned-PR check (deterministic per-repo stale-PR cleanup) ---
    # Master switch for the orphaned-PR check pass. Defaults to False
    # (opt-in) — closing PRs and filing tracking tickets are destructive
    # actions.  Flip to True to enable the periodic pass.
    orphaned_pr_check_periodic: bool = Field(
        default=False,
        description="When true, run periodic orphaned-PR detection and cleanup.",
    )
    # Seconds between orphaned-PR check passes when
    # MILL_ORPHANED_PR_CHECK_PERIODIC=true.  Minimum enforced at 3600 s
    # (1 hour) in the worker loop.
    orphaned_pr_check_interval_seconds: int = Field(
        default=86400,
        description="Seconds between orphaned-PR check passes.",
    )
    # Minimum age (hours) of a ticket before its PR is considered for
    # orphan classification.  Skips tickets younger than this to avoid
    # racing the deliver stage.
    orphaned_pr_min_age_hours: int = Field(
        default=4,
        ge=1,
        description="Minimum ticket age (hours) before its PR is considered for orphan classification.",
    )
    # Maximum number of combined close+file actions per pass run.
    # Findings beyond this cap are deferred to the next scheduled pass.
    orphaned_pr_max_actions_per_pass: int = Field(
        default=5,
        ge=1,
        description="Maximum combined close+file actions per orphaned-PR pass.",
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
    )
    orphaned_pr_max_files_per_pass: int = Field(
        default=5,
        ge=1,
        description="Maximum file-ticket actions per orphaned-PR pass (in addition to combined cap).",
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
    # When True, the worker runs periodic repo-description-sync passes at the
    # configured interval. Default True (opt-out). Reads the repo's README,
    # compares against the forge description, and updates it when empty/stale.
    repo_description_sync_periodic: bool = Field(
        default=True,
        description="When true, run periodic repo-description-sync passes.",
    )
    # Interval between repo-description-sync passes (seconds). Default 86400
    # (daily). Enforced minimum 3600s (1 hour) in the worker.
    repo_description_sync_interval_seconds: int = Field(
        default=604800,  # 7d — weekly default; per-repo override via YAML
        description="Seconds between repo-description-sync passes.",
    )
