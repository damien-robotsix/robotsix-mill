"""Settings field mixin: web access, human approval gate, retrospect, trace inspector, db maintenance, sandbox reaper, CI monitor, langfuse cleanup, token-metrics.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

from robotsix_mill._resources import (
    language_instructions_dir as _resources_language_instructions_dir,
)
from robotsix_mill._resources import (
    skills_dir as _resources_skills_dir,
)


class _StagesSettings(BaseModel):
    # --- agent web access (refine + implement) ---
    # Web search is delegated to a cheap, bounded SUB-agent: the main
    # (expensive) agent never carries OpenRouter's ":online" suffix, it
    # only gets a `web_research(query)` tool whose body runs this small
    # model — with ":online" + web_fetch — and returns just a concise
    # conclusion. This kills the per-request web-search surcharge on the
    # pricey model and keeps its context lean (conclusions, not pages).
    web_search: bool = Field(
        description="Enable web search via a cheap bounded sub-agent for refine and implement agents.",
        default=True,
    )
    web_research_request_limit: int = Field(
        description="Request budget for the web_research sub-agent spawned by refine/implement.",
        default=16,
        ge=1,
        json_schema_extra={"advanced": True},
    )
    web_research_fetch_max_calls: int = Field(
        description="Maximum real (cache-miss) web_fetch calls per web_research sub-agent invocation.",
        default=4,
        ge=1,
        json_schema_extra={"advanced": True},
    )
    # web_fetch runs in its OWN container: network ON, but NO repo/data
    # mount, non-root, read-only, fixed curl. Trade-off accepted: an
    # agent could encode data into a fetched URL. http(s) only.
    fetch_image: str = Field(
        description="Docker image used for isolated web_fetch calls (curl-based, network-only, no repo mount).",
        default="curlimages/curl:8.17.0",
        json_schema_extra={"advanced": True},
    )
    web_fetch_max_bytes: int = Field(
        description="Maximum raw bytes per web_fetch call.",
        default=2_000_000,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    web_fetch_timeout: int = Field(
        description="Timeout (seconds) per web_fetch call.",
        default=30,
        gt=0,
        json_schema_extra={"advanced": True},
    )
    # Post-extraction cap, applied AFTER HTML→text stripping. The
    # network-level ``web_fetch_max_bytes`` bounds raw bytes; this
    # bounds what the agent ACTUALLY sees in its context. Default
    # 200 KB ≈ 50K tokens — enough for one doc page worth of prose,
    # not enough to nuke a refine context with a 315 KB markup dump.
    # Configured via ``web.fetch_max_text_bytes`` in the YAML config.
    web_fetch_max_text_bytes: int = Field(
        default=40_000,
        description="Post-extraction character cap per web_fetch (after HTML-to-text stripping).",
        json_schema_extra={"advanced": True},
    )
    # When True, web_fetch returns the raw response body verbatim
    # (no HTML→text stripping, no per-run URL dedupe). Operator
    # escape hatch for the rare case the agent needs the markup
    # itself (parsing structure, inspecting attributes). Default
    # False — every agent we ship is a prose consumer.
    # Configured via ``web.fetch_raw`` in the YAML config.
    web_fetch_raw: bool = Field(
        default=False,
        description="When true, web_fetch returns the raw response body verbatim (no HTML stripping, no per-run URL dedupe).",
        json_schema_extra={"advanced": True},
    )
    # Bounded web-fetch budget, reset once per ``ask_web_knowledge``
    # consult and shared across every ``web_research`` sub-agent it
    # spawns. The ``*_request_limit`` knobs count MODEL requests, not
    # tool calls, so they can't bound fetch fan-out — a single consult
    # can issue dozens of ``web_fetch`` calls (7 searches × up-to-8
    # requests × multiple fetches → ~1.9M input tokens in one observed
    # refine specimen). These two caps bound that explosion directly.
    # Cache hits and ``web_fetch_raw`` returns do NOT count.
    # Max real (cache-miss) fetches per consult.
    # Configured via ``web.fetch_max_calls`` in the YAML config.
    web_fetch_max_calls: int = Field(
        description="Maximum real (cache-miss) web_fetch calls per web_knowledge consultation.",
        default=15,
        ge=1,
        json_schema_extra={"advanced": True},
    )
    # Cumulative ceiling on returned (post-extraction, post-cap) text
    # bytes per consult; ``0`` disables the byte ceiling.
    # Configured via ``web.fetch_max_total_bytes`` in the YAML config.
    web_fetch_max_total_bytes: int = Field(
        description="Cumulative ceiling on returned text bytes per web_knowledge consultation. 0 disables.",
        default=2_000_000,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # Per-TRACE (cross-consult) web budget for the refine stage,
    # mirroring the proven survey caps. The per-consult ``web_fetch_max_*``
    # fields above bound a single ``ask_web_knowledge`` call; these bound
    # every fetch/search across one whole refine run, so a refine loop
    # can't re-bill millions of input tokens on runaway web I/O. Reset
    # once at the start of each refine trace (see ``run_refine_agent``).
    # Max real (cache-miss) fetches across one refine trace.
    refine_web_fetch_max_calls: int = Field(
        description="Maximum real web_fetch calls across one refine trace.",
        default=5,
        ge=1,
        json_schema_extra={"advanced": True},
    )
    # Max fetch bytes across one refine trace; ``0`` disables the ceiling.
    refine_web_fetch_max_total_bytes: int = Field(
        description="Maximum fetch bytes across one refine trace. 0 disables.",
        default=500_000,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # Max web_search calls across one refine trace.
    refine_web_search_max_calls: int = Field(
        description="Maximum web_search calls across one refine trace.",
        default=5,
        ge=1,
        json_schema_extra={"advanced": True},
    )
    # Pre-write Python syntax check on `write_file` / `edit_file`. When
    # True (default) a SyntaxError aborts the edit and the agent gets
    # an actionable error string instead of writing broken code that
    # would only be caught one expensive test cycle later.
    # Configured via ``core.lint_on_edit`` in the YAML config.
    lint_on_edit: bool = Field(
        description="When true, pre-write Python syntax check on write_file/edit_file calls.",
        default=True,
    )
    # Character cap on an *implicit full* ``read_file`` (offset=1,
    # limit=None) payload, applied by ``fs_tools._bound_full_read``.
    # Over the cap the tool returns a head+tail slice plus an elision
    # marker that steers the agent to re-read the omitted region with
    # offset/limit; explicit ranged reads are never truncated. 50,000
    # chars ≈ 12.5K tokens — comfortably above ordinary hand-written
    # source modules (which are returned in full) so only large
    # generated/lock/baseline files (uv.lock ≈ 290 KB,
    # mypy-baseline.txt ≈ 121 KB) get trimmed before they bloat the
    # prefix that is re-billed on every later tool turn. 0 disables the
    # guard. Configured via ``core.read_file_max_chars`` in the YAML
    # config.
    read_file_max_chars: int = Field(
        description="Character cap on implicit full read_file payloads. 0 disables.",
        default=50_000,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # Directory of skill docs (skills/<name>/SKILL.md) injected into the
    # refine + implement agents' system prompt. Relative to CWD (/app in
    # the container, repo root locally).
    skills_dir: Path = Field(
        description="Directory of skill docs injected into refine and implement agent system prompts.",
        default_factory=_resources_skills_dir,
    )
    # Directory of per-language instruction Markdown snippets
    # (agent_definitions/language_instructions/<language>.md) injected
    # into the implement agent's system prompt. Resolved via
    # importlib.resources so it works in both editable and installed
    # (container) modes.
    language_instructions_dir: Path = Field(
        description="Directory of per-language instruction Markdown snippets.",
        default_factory=_resources_language_instructions_dir,
    )

    # --- human approval gate (refine -> implement) ---
    # When true (default), the refine stage transitions to
    # human_issue_approval instead of ready — a human must approve before
    # the implement stage kicks in. A cheap conservative LLM auto-approval
    # check runs before the gate: obviously-safe changes (cosmetic, doc-only,
    # single-file, no logic changes) skip the human step automatically.
    # Set false for fully-autonomous mode.
    require_approval: bool = Field(
        description="When true, refined tickets require human approval before implement stage (auto-approve skips the human gate for obviously-safe changes).",
        default=True,
    )

    # --- retrospect stage (done -> reviewed) ---
    # When True, retrospect may file an improvement DRAFT. Until the
    # human-gate-after-refine exists, that draft auto-flows to done and
    # is retrospected again — set False to analyse without spawning.
    retrospect_spawn_drafts: bool = Field(
        description="When true, retrospect may file improvement draft tickets.",
        default=True,
    )
    # When True, retrospect files a draft ticket per AGENT.md
    # proposal on the originating repo's board.
    retrospect_spawn_agented_proposals: bool = Field(
        description="When true, retrospect files draft tickets for AGENT.md proposals.",
        default=True,
    )
    # (Removed) retrospect_deep_analysis_frequency: deep-analysis mode
    # was retired — per-trace inspection is now owned by the periodical
    # pipeline (trace_health_runner + expensive-item detector).
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
        description="Cost outlier multiplier: traces exceeding batch median x this are flagged for inspection.",
        default=3.0,
        json_schema_extra={"advanced": True},
    )
    trace_review_per_obs_cost_threshold: float = Field(
        description="Per-observation cost threshold for trace-review outlier detection.",
        default=0.001,
        json_schema_extra={"advanced": True},
    )
    trace_review_obs_multiplier: float = Field(
        description="Observation count multiplier: traces exceeding batch median x this are flagged.",
        default=3.0,
        json_schema_extra={"advanced": True},
    )
    # ``repeated_tool`` stays an absolute threshold because each tool
    # has its own "normal" usage profile — making it relative would
    # require a per-tool batch median, which is too noisy with small
    # samples.
    trace_review_max_repeated_tool: int = Field(
        description="Absolute threshold for repeated-tool-call flagging in trace review.",
        default=50,
        json_schema_extra={"advanced": True},
    )
    # Hard cap on the total number of tool calls the trace inspector
    # may make per trace.  100 tool calls is far beyond what any
    # legitimate trace analysis requires — only clearly broken runs
    # are terminated.  When exceeded, the inspector raises
    # ``UsageLimitExceeded`` and the trace is marked as errored.
    trace_review_max_tool_calls: int = Field(
        description="Hard cap on total tool calls per trace inspector run.",
        default=100,
        json_schema_extra={"advanced": True},
    )
    # Hard cap on the number of tool-call errors before the trace
    # inspector is auto-terminated.  A healthy inspection should have
    # near-zero errors; 20 indicates a broken execution loop.
    trace_review_max_errors: int = Field(
        description="Hard cap on tool-call errors before the trace inspector is auto-terminated.",
        default=20,
        json_schema_extra={"advanced": True},
    )
    # Model tier for the trace inspector.  Level 1 (cheapest flash), the default;
    # raising it costs more and is opt-in only.
    # Per-stage capability level, following llmio's L1..L4 convention:
    # 1 = cheap (OpenRouter flash), 2 = intermediate (OpenRouter pro),
    # 3 = Claude subscription (opus), 4 = frontier (Claude fable).  Keyed by
    # the agent definition ``name`` (``implement``, ``ci_fix``, ``review``,
    # ``refine``, ``rebase``, ``retrospect``, ``document``, …); an absent key
    # keeps the level declared in ``agent_definitions/<name>.yaml``.  This is
    # THE knob for moving a stage between pay-per-token and flat-rate tiers —
    # e.g. ``{"implement": 3, "ci_fix": 3}`` runs both on the subscription.
    # Call-site cheap routes (config-only implement/review → level 1, the
    # refine trivial route) still apply on top.
    agent_levels: dict[str, int] = Field(
        description=(
            "Per-stage llmio capability level (1-4) keyed by agent definition "
            "name; unset stages keep their YAML default."
        ),
        default_factory=dict,
        json_schema_extra={"advanced": True},
    )

    @field_validator("agent_levels")
    @classmethod
    def _validate_agent_levels(cls, value: dict[str, int]) -> dict[str, int]:
        bad = {k: v for k, v in value.items() if not (1 <= int(v) <= 4)}
        if bad:
            raise ValueError(f"agent_levels values must be 1..4, got {bad}")
        return {k: int(v) for k, v in value.items()}

    trace_review_model_level: int = Field(
        description="Model tier for the trace inspector (1=flash, 2=pro, 3=opus).",
        default=1,
        ge=1,
        le=3,
        json_schema_extra={"advanced": True},
    )
    # When True (default), triage-trivial tickets are routed to
    # ``refine_trivial_model_level`` instead of the YAML default (3 / Opus).
    # Set False to force all refines through the default level.
    refine_trivial_routing_enabled: bool = Field(
        description="When true, triage-trivial tickets route to refine_trivial_model_level instead of the YAML default.",
        default=True,
        json_schema_extra={"advanced": True},
    )
    # Model level used for trivial-scope refines.  Default 2 =
    # pay-per-token level-2 model (OpenRouter, ~$0.001+/run) — cheap
    # enough for straightforward gap-fill tickets while still capable.
    # Set to 3 for flat-cost Claude subscription (sonnet, marginal $0)
    # or 1 for the cheapest flash model.
    refine_trivial_model_level: int = Field(
        description="Model level for trivial-scope refines (1=flash, 2=pro, 3=subscription).",
        default=2,
        ge=1,
        le=3,
        json_schema_extra={"advanced": True},
    )
    # Claude model alias used when a trivial/forced-cheap refine routes to
    # the level-3 subscription.  ``sonnet`` is the cheapest alias already
    # trusted by the ``"simple"`` path.  Only the Claude-SDK branch (level 3)
    # consumes this; OpenRouter levels 1/2 ignore it.
    refine_trivial_subscription_model: str = Field(
        description="Claude model alias for trivial/forced-cheap refines on the subscription tier.",
        default="sonnet",
        json_schema_extra={"advanced": True},
    )
    # When True (default), non-trivial level-3 refines route to a cheaper
    # Claude alias (sonnet) for "simple" tickets and keep Opus only for
    # "needs-exploration" tickets — all on the same claudeSDK subscription
    # transport.  Set False for a clean rollback to Opus-always.
    refine_subscription_tier_routing_enabled: bool = Field(
        description="When true, non-trivial level-3 refines route to sonnet for simple tickets, opus for complex.",
        default=True,
        json_schema_extra={"advanced": True},
    )
    # Claude model alias for non-escalated level-3 refines (complexity="simple").
    # Only the Claude-SDK branch (level 3) consumes this; OpenRouter levels 1/2 ignore it.
    refine_subscription_model_default: str = Field(
        description="Claude model alias for non-escalated level-3 refines (complexity=simple).",
        default="sonnet",
        json_schema_extra={"advanced": True},
    )
    # Claude model alias for escalated level-3 refines (complexity="needs-exploration").
    # Only the Claude-SDK branch (level 3) consumes this; OpenRouter levels 1/2 ignore it.
    refine_subscription_model_complex: str = Field(
        description="Claude model alias for escalated level-3 refines (complexity=needs-exploration).",
        default="opus",
        json_schema_extra={"advanced": True},
    )
    # When True (default), a non-trivial level-3 refine that WOULD route to
    # Opus (complexity="needs-exploration") is downgraded to a cheaper Claude
    # alias (refine_subscription_model_findings) when the triage stage already
    # produced substantial exploration findings (root cause known). Opus is
    # kept only when triage findings are absent or too short. Set False for a
    # clean rollback to the prior simple/complex binary.
    refine_findings_downgrade_enabled: bool = Field(
        description="When true, Opus refines with substantial triage findings downgrade to a cheaper model.",
        default=True,
        json_schema_extra={"advanced": True},
    )
    # Minimum stripped-character length of the triage exploration findings for
    # the Opus->cheaper downgrade above to fire. Below this, findings are
    # treated as insufficient and Opus is kept.
    refine_findings_downgrade_min_chars: int = Field(
        description="Minimum stripped-character length of triage findings for the Opus-to-cheaper downgrade.",
        default=150,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # Claude model alias used when the findings-present downgrade fires.
    # Defaults to sonnet (same tier the "simple" path already trusts). Only
    # the Claude-SDK branch (level 3) consumes this; OpenRouter levels 1/2 ignore it.
    refine_subscription_model_findings: str = Field(
        description="Claude model alias used when the findings-present downgrade fires.",
        default="sonnet",
        json_schema_extra={"advanced": True},
    )
    # Maximum number of "changes requested" re-refine rounds before the
    # refine agent is forced to the cheap model (``refine_trivial_model_level``)
    # regardless of the persisted triage verdict.  A value of 0 disables
    # the counter-forced downgrade entirely — every sendback runs at full
    # Opus unless already caught by trivial-scope routing.
    max_re_refine_cycles_before_cheap: int = Field(
        description="Sendback re-refine rounds before forcing the cheap model. 0 disables.",
        default=2,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # Per-ticket ceiling on total refine passes before escalating to BLOCKED
    # for human review.  Guards against unbounded re-refinement loops (e.g.
    # operator sendback → refine → sendback → ...) that burn subscription
    # quota without converging.  Set to 0 to disable the cap entirely.
    max_refine_passes_per_ticket: int = Field(
        description="Per-ticket ceiling on total refine passes before escalating to BLOCKED. 0 disables.",
        default=3,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # When True, a refine run re-entered after an operator sendback
    # ("changes requested:") reuses the prior refined description.md as the
    # agent's starting point and applies only the operator's delta, instead of
    # re-deriving the spec from the original draft. Set False to always refine
    # from scratch.
    refine_delta_reuse_enabled: bool = Field(
        description="When true, re-refines reuse the prior description.md and apply only the operator's delta.",
        default=True,
        json_schema_extra={"advanced": True},
    )
    # When True, retry/audit/re-refine passes on the same ticket receive
    # only the delta (failing item + minimal spec) rather than the full
    # accumulated lifecycle context.  Applied fleet-wide — both
    # subscription and OpenRouter-backed stages.  Reduces late-pass
    # context size 20-40% vs the first pass, saving marginal tokens on
    # OpenRouter and helping subscription stages stay under plan ceilings.
    delta_context_retry_enabled: bool = Field(
        description="When true, retry/audit/re-refine passes receive only the delta rather than full context.",
        default=True,
        json_schema_extra={"advanced": True},
    )
    # ---------- trace inspector dynamic budget ----------
    # Floor for the tools-on request budget.  Even a tiny trace gets
    # enough requests to read at least one code locus and emit a
    # grounded finding.
    trace_review_inspector_min_requests: int = Field(
        description="Floor for the trace inspector's tools-on request budget.",
        default=20,
        json_schema_extra={"advanced": True},
    )
    # Ceiling for the tools-on request budget.  Caps the formula so
    # a trace with 10 000 observations doesn't get an absurd budget.
    trace_review_inspector_max_requests: int = Field(
        description="Ceiling for the trace inspector's tools-on request budget.",
        default=80,
        json_schema_extra={"advanced": True},
    )
    # Requests granted per observation before clamping to min/max.
    # 0.1 → every 10 observations earn one request.  A 235-obs trace
    # gets floor(23.5) = 23 requests, comfortably above the floor.
    trace_review_inspector_requests_per_obs: float = Field(
        description="Requests granted per trace observation before clamping to min/max.",
        default=0.1,
        json_schema_extra={"advanced": True},
    )
    # Observation count above which the inspector drops code-access
    # tools and uses the cheap tool-less summary path instead.  A
    # trace this large cannot be deep-verified in a bounded run.
    trace_review_inspector_max_obs_for_tools: int = Field(
        description="Observation count above which the inspector drops code-access tools.",
        default=200,
        json_schema_extra={"advanced": True},
    )
    # Request budget for the tool-less (summary-only) path.
    trace_review_inspector_toolless_requests: int = Field(
        description="Request budget for the tool-less (summary-only) inspector path.",
        default=3,
        json_schema_extra={"advanced": True},
    )
    # Request budget for the interactive ``langfuse_inspect_trace``
    # tool path (invoked by the refine/answer agent).  Ad-hoc
    # inspections should be a quick, bounded confirmation — not an
    # unbounded deep audit.  Default 15 keeps per-call cost around
    # $0.10–$0.15 instead of $0.85.
    trace_review_tool_request_limit: int = Field(
        description="Request budget for the interactive langfuse_inspect_trace tool.",
        default=15,
        json_schema_extra={"advanced": True},
    )
    # Hard cap on the total number of drafts a single trace-review
    # pass may file. The inspector emits one finding per flagged trace
    # and a typical batch flags 5-10 traces with 2-5 findings each →
    # up to 50 drafts per cycle (89 trace-review drafts piled up after
    # one 2026-05-28 cycle). Findings are individually low-signal and
    # the cross-trace analyzer is the right surface for recurring
    # patterns; capping per-cycle bleeds keeps the board readable.
    trace_review_max_drafts_per_run: int = Field(
        description="Hard cap on total drafts a single trace-review pass may file.",
        default=5,
        json_schema_extra={"advanced": True},
    )
    # Minimum inspector confidence for a finding to be filed as a draft.
    # The inspector deliberately downgrades weak/uncertain/non-concrete
    # findings to ``confidence="low"`` (and prefixes ``proposed_solution``
    # with ``REQUIRES_HUMAN_REVIEW:``). Defaulting the floor to ``"medium"``
    # drops exactly those self-flagged-weak findings — the low-signal
    # ``tool_error`` / ``agent_limitation`` telemetry (e.g. a single
    # out-of-range ``read_file`` or a slow survey run) that flooded the
    # human-approval gate one-ticket-per-observation — while preserving the
    # long-standing "medium files" behaviour. Tighten to ``"high"`` (file
    # only findings whose symptom was seen in the trace AND confirmed against
    # code) for a stricter gate, or widen to ``"low"`` to file everything.
    trace_review_min_confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Minimum inspector confidence for a finding to be filed as a draft.",
        json_schema_extra={"advanced": True},
    )
    # Hard cap on the number of LLM inspector calls per trace-review
    # run.  Directly bounds LLM spend independently of the draft cap
    # (which only counts filed findings — a zero-finding inspection
    # consumes an LLM call but produces no draft).  0 disables the cap.
    trace_review_max_inspections_per_run: int = Field(
        description="Hard cap on LLM inspector calls per trace-review run. 0 disables.",
        default=5,
        json_schema_extra={"advanced": True},
    )
    # Hard cap on how many traces a single trace-review run pulls full
    # detail for (and holds in memory at once). The runner pre-loads every
    # trace's detail + observations into memory; an unbounded window (which
    # happens when a run is interrupted before it can advance the watermark)
    # made that grow without limit and exhaust the host. When the window
    # holds more than this, the run processes the OLDEST N and advances the
    # watermark to the last processed trace, so it converges incrementally
    # instead of re-loading an ever-growing backlog. 0 disables the cap.
    trace_review_max_traces_per_run: int = Field(
        description="Hard cap on how many traces a single trace-review run pulls full detail for. 0 disables.",
        default=300,
        json_schema_extra={"advanced": True},
    )
    # First-run lookback window when no watermark exists yet (hours).
    trace_review_initial_lookback_hours: int = Field(
        description="First-run lookback window (hours) when no watermark exists yet.",
        default=24,
        json_schema_extra={"advanced": True},
    )
    # When set, every trace-review draft lands on THIS repo's board,
    # regardless of which repo the source trace lived on. Trace-review
    # findings are agent-side improvements (mill code, mill prompts);
    # filing them on each application repo's board scatters work that
    # belongs in one place. Leave empty to preserve the legacy
    # source-repo routing.
    trace_review_target_repo_id: str = Field(
        description="Target repo for trace-review draft tickets. Empty preserves source-repo routing.",
        default="",
    )
    # Window (seconds) for correlating an incomplete trace with a
    # process restart. When an ``incomplete_trace`` flag fires AND
    # the trace's latest timestamp falls within this many seconds of
    # ``_process_started_at``, the ``restart_correlated`` flag is
    # appended — signalling the Phase 2 inspector that the root cause
    # is likely a restart kill, not an agent-loop bug.
    trace_review_restart_correlation_window_seconds: int = Field(
        description="Window (seconds) for correlating incomplete traces with process restarts.",
        default=60,
        json_schema_extra={"advanced": True},
    )
    # Recency window (days) for the pre-filing duplicate check in the
    # trace-review runner.  A candidate prior ticket is considered for
    # dedup when its created_at is within this window of `now`.  Default
    # 7 mirrors `dedup_lookback_days` (used by the refine-stage dedup
    # guard) but is independent because the two checks live at different
    # stages and may want different policies.
    trace_review_dedup_lookback_days: int = Field(
        description="Recency window (days) for pre-filing duplicate check in trace-review.",
        default=7,
        json_schema_extra={"advanced": True},
    )
    # When True (default), scanner periodic passes (docstring_coverage,
    # module_size, test_gap, health, completeness_check) that produce
    # multiple findings per run are rolled up into a single rollup
    # ticket listing all findings, instead of filing one ticket per
    # finding.  Estimated ~80% inflow reduction for scanner sources.
    # Set to False to restore the legacy one-ticket-per-finding
    # behaviour.
    scanner_rollup: bool = Field(
        description="Roll up multi-finding scanner passes into a single ticket per run.",
        default=True,
    )
    # Hard cap on the total number of drafts a single scanner pass may
    # file.  When ``scanner_rollup`` is True this is effectively 1 (the
    # rollup itself).  When rollup is disabled, this bounds the per-run
    # ticket count so a scanner that suddenly flags 50+ gaps does not
    # flood the board.
    scanner_max_drafts_per_run: int = Field(
        description="Hard cap on drafts a single scanner pass may file.",
        default=5,
        json_schema_extra={"advanced": True},
    )
    # Hard cap on the total number of drafts a single retrospect pass
    # may file (systemic draft + concrete follow-up).  Default 2 matches
    # the existing two-path ceiling; set lower when the board is
    # overloaded.
    retrospect_max_drafts_per_run: int = Field(
        description="Hard cap on drafts a single retrospect pass may file.",
        default=2,
        json_schema_extra={"advanced": True},
    )
    # Recency window (days) for the advisory pre-filing duplicate check in
    # epic decomposition (``dedup.find_child_overlaps``).  A proposed child
    # is flagged when a prior ticket created within this window matches its
    # scope.  Mirrors ``trace_review_dedup_lookback_days`` but is independent
    # so the epic-decomposition policy can diverge.
    epic_dedup_lookback_days: int = Field(
        description="Recency window (days) for advisory duplicate detection in epic decomposition.",
        default=7,
        json_schema_extra={"advanced": True},
    )
    # Path to the agent-maintained Markdown memory ledger.  Override to
    # pin a specific path; unset (default) derives <data_dir>/retrospect_memory.md.
    retrospect_memory_path: Path | None = Field(
        default=None,
        description="Path to the retrospect agent's Markdown memory ledger.",
    )
    # human_mr_approval (PR open) re-check cadence. mill has no scheduler; this
    # timer exists only to observe the external merge event.
    merge_poll_seconds: int = Field(
        description="Re-check cadence (seconds) for human_mr_approval (PR open) waiting for external merge.",
        default=120,
        gt=0,
        json_schema_extra={"advanced": True},
    )
    # When true (default), the workspace's clone (repo/) is removed on
    # close to save disk space.
    prune_clone_on_close: bool = Field(
        description="When true, the workspace clone (repo/) is removed on ticket close.",
        default=True,
        json_schema_extra={"advanced": True},
    )
    # Maximum number of terminal-state tickets (CLOSED, ANSWERED,
    # EPIC_CLOSED) to retain.  When a ticket transitions to a terminal
    # state and the total exceeds this cap, the oldest terminal tickets
    # (by created_at) are purged — unless they are the parent of an
    # active (non-terminal) child.  Set to 0 to disable purging.
    max_archived_tickets: int = Field(
        description="Maximum terminal-state tickets to retain. 0 disables purging.",
        default=40,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # --- db maintenance (periodic archive purge + per-ticket event cap) ---
    # Runs a periodic sweep that (a) purges terminal tickets exceeding
    # max_archived_tickets, (b) prunes oldest TicketEvent rows on
    # non-terminal tickets exceeding max_events_per_ticket, and (c) runs
    # PRAGMA optimize to reclaim freed pages. Set interval to 0 to disable.
    db_maintenance_interval_seconds: int = Field(
        description="Seconds between periodic DB maintenance passes. 0 = disabled.",
        default=86400,
        json_schema_extra={"advanced": True},
    )

    # --- sandbox reaper (periodic orphan-container cleanup) ---
    # Periodically force-removes leaked mill-sbx-*/mill-fetch-* sandbox
    # containers whose uptime exceeds twice command_timeout (a live sandbox
    # is bounded by command_timeout, so anything older is provably orphaned).
    # Defends against containers orphaned by a mill crash/restart mid-run,
    # which otherwise run forever (--rm only fires on container exit, and
    # the timeout is parent-process enforced). The startup reaper in lifespan
    # is the complementary guard. Set interval to 0 to disable.
    sandbox_reaper_interval_seconds: int = Field(
        description="Seconds between sandbox reaper passes. 0 = disabled.",
        default=3600,
        json_schema_extra={"advanced": True},
    )
    # Maximum TicketEvent rows to retain per non-terminal ticket.
    # Events beyond this cap are pruned (oldest first).  Set to 0 to disable
    # per-ticket event capping entirely (archive purge still runs).
    max_events_per_ticket: int = Field(
        description="Maximum TicketEvent rows per non-terminal ticket. 0 disables capping.",
        default=200,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Maximum Comment rows to retain per non-terminal ticket. Comments
    # beyond this cap are pruned (oldest first), but OPEN threads (and
    # their replies) are never pruned so ask_user auto-resume and active
    # discussions are preserved. Set to 0 to disable comment capping.
    max_comments_per_ticket: int = Field(
        description="Maximum Comment rows per non-terminal ticket. Open threads are never pruned. 0 disables.",
        default=500,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # --- target-branch CI monitor ---
    # CI monitor enabled/interval are now per-repo fields on RepoConfig
    # (see config/repos.yaml).  ci_log_max_bytes stays global — it is an
    # operational cap, not a per-repo policy decision.
    ci_log_max_bytes: int = Field(
        description="Maximum bytes of CI log output to capture per check run.",
        default=65536,
        json_schema_extra={"advanced": True},
    )

    # --- langfuse cleanup (caps trace count for the shared workspace project) ---
    # Periodically deletes the oldest traces from the shared workspace
    # Langfuse project, keeping at most langfuse_cleanup_max_traces rows.
    # Set interval to 0 to disable.
    langfuse_cleanup_interval_seconds: int = Field(
        description="Seconds between Langfuse trace cleanup passes. 0 = disabled.",
        default=86400,
        json_schema_extra={"advanced": True},
    )
    langfuse_cleanup_max_traces: int = Field(
        description="Maximum traces to retain in the Langfuse project during cleanup.",
        default=5000,
        json_schema_extra={"advanced": True},
    )
    # --- token-metrics aggregation (daily stage×model token percentiles) ---
    # A global, no-LLM pass that reads per-step mill.step_usage metadata
    # from the shared Langfuse project's list endpoint and writes a compact
    # per-call token histogram snapshot to <data_dir>/token_metrics/. Set
    # interval to 0 to disable.
    token_metrics_aggregation_interval_seconds: int = Field(
        description="Seconds between token-metrics aggregation passes. 0 = disabled.",
        default=86400,
        json_schema_extra={"advanced": True},
    )
    token_metrics_aggregation_window_seconds: int = Field(
        description="Lookback window (seconds) of Langfuse traces aggregated each pass.",
        default=86400,
        json_schema_extra={"advanced": True},
    )
    sandbox_op_timeout: int = Field(
        description="Per-docker-exec timeout (seconds) for individual sandbox operations. 0 disables.",
        default=300,
        ge=0,
        validation_alias=AliasChoices("sandbox_op_timeout", "MILL_SANDBOX_OP_TIMEOUT"),
        json_schema_extra={"advanced": True},
    )
    implement_pass_timeout: int = Field(
        description="Progress-reset watchdog (seconds) for the implement agent. "
        "The timer resets on every tool call (read_file, write_file, explore, "
        "etc.) — the agent is killed only after this many seconds of NO "
        "progress. 0 disables the watchdog and falls back to the flat "
        "coordinator_timeout_seconds cap.",
        default=300,
        ge=0,
        validation_alias=AliasChoices(
            "implement_pass_timeout", "MILL_IMPLEMENT_PASS_TIMEOUT"
        ),
        json_schema_extra={"advanced": True},
    )
    # Lines of file context around each changed region preloaded in the
    # implement preseed. On a retry pass the prior attempt's edits are
    # already on disk, so the reference_files excerpt-preload sends only
    # the changed lines plus this much surrounding context (mirroring
    # ``review_preseed_context_lines``) instead of re-sending whole files.
    # 0 preloads only the changed lines themselves.
    implement_preseed_context_lines: int = Field(
        description="Lines of file context around each changed region preloaded in the implement retry preseed. 0 preloads only changed lines.",
        default=40,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # Cap for the message_history replayed on a resumed implement pass:
    # keep only the last N assistant tool-call rounds and summarise the
    # older prefix. 0 disables compaction (full history is replayed).
    implement_history_max_turns: int = Field(
        description="Last-N tool-call turns kept when compacting a resumed implement message_history. 0 disables compaction.",
        default=8,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    # Maximum characters of the rolling summary that replaces the dropped
    # older turns in a compacted implement message_history.
    implement_history_summary_max_chars: int = Field(
        description="Maximum characters of the rolling summary for dropped implement history turns.",
        default=3000,
        ge=0,
        json_schema_extra={"advanced": True},
    )
