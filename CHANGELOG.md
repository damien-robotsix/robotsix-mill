## 0.0.0 (unreleased)

- **refine**: add per-ticket refine-pass cap and convergence detection
  to prevent unbounded re-refinement loops that burn subscription
  quota.  A new `max_refine_passes_per_ticket` setting (default 3)
  escalates tickets to BLOCKED when the cap is exhausted without
  convergence.  A pre-refine input-convergence guard skips the
  expensive refine agent when the on-disk description is byte-identical
  to the previous pass's output and no new reviewer comments exist.
  A post-refine output-convergence check stops incrementing the pass
  counter when successive passes produce identical results.  New
  `refine_passes` and `refine_output_hash` fields on the Ticket model
  persist the counter and hash across runs.

- **ci-dedup**: include the workflow file path in CI failure fingerprint
  hashing so that the same error in different workflows produces
  distinct fingerprints.  `_ci_draft_fingerprint` now accepts an
  optional `path` keyword; the CI monitor poll loop passes the
  workflow's file path, and the refine gate extracts it from a new
  `**Path:**` metadata line in the draft body.

- **docs**: add missing entries for 7 stage/core env vars in
  `docs/configuration.md` (`ci_fix_wait_poll_interval_s`,
  `ci_fix_wait_timeout_s`, `max_implement_review_cycles`,
  `retrospect_candidates_max_entries`, `doc_classifier_diff_max_chars`,
  `network_probe_host`, `network_outage_retry_seconds`).
- **cost attribution**: add ``trace_observation_summary()`` helper in the
  Langfuse client that distills per-trace observations into a compact summary
  (model, token counts, tool-call list, error/warning counts).  Enrich
  ``GET /traces/recent`` with an ``observationSummary`` field and add a new
  ``GET /traces/{trace_id}`` endpoint returning full trace detail including all
  observations.  The diagnostic data layer's ``_normalize_trace`` now also
  carries the summary.  This lets the fleet-level cost analyst attribute
  spend to model tier, token volume, and tool-call patterns instead of
  receiving ``insufficient data`` for every trace.
- **implement**: stop fabricating GitHub Action commit SHAs when writing
  workflow files.  The implement agent now emits tag references
  (e.g., `actions/setup-python@v5.4.0`) instead of attempting to resolve
  tags to SHAs in the network-isolated sandbox.  Renovate's
  `pinGitHubActionDigests` preset handles the actual pinning.
- **retrospect**: retire the `AGENT_CANDIDATES.md` file-writing sink
  (`_maybe_write_agented_proposals`) so each AGENT.md proposal is
  filed only as a draft ticket, not duplicated into both a ticket
  and the candidates review file.  The candidates file and its HTTP
  routes remain for manual review of externally-added entries.
- **ci**: remove `github-actions` ecosystem from Dependabot config to
  silence failing graph-submission check, triggered by external
  reusable workflow references (`.github/workflows/` in cross-org
  `uses:` lines) that Dependabot cannot handle.  The `uv` ecosystem was
  previously removed for the same class of failure.
- **board UI**: add `orphaned_pr_check` entry to `SOURCE_CLASS` map and
  `.src-orphaned-pr-check` CSS badge rule so orphaned-PR-check tickets
  get a distinct source badge instead of falling back to `.src-user`.
- **implement**: add `deptry .` to the pre-flight toolchain run before
  marking `implement_complete`, alongside the existing ruff and mypy
  checks (Pre-Stop Self-Check step 0 + language instruction section).
- **config**: add `board_manager_max_concurrent` setting (default 3) to
  control max simultaneous BoardManager LLM requests. Wired through
  YAML defaults, loader alias mapping, lifespan `BoardManager()`
  constructor, and config test.

- **config**: remove zombie `core.limits.parallel_explore_max` setting (
  Pydantic field, YAML default, loader mapping, docs, vulture whitelist);
  no longer consumed after the `parallel_explore` batching refactor.

- **explore**: optimize `parallel_explore` to batch all questions into a
  single scout call instead of spawning one independent agent per question.
  This sends the ~68k-char system prompt only once, cutting input-token
  cost by a factor of N for N questions. Single-question calls are
  delegated directly with no batch wrapper.

- **forge**: add `close_pr()` and `post_pr_comment()` to the `Forge` ABC
  with GitHub (PATCH pull state, POST issues comment) and GitLab
  (PUT state_event=close, POST notes) implementations. Both return
  ``True``/``False`` and never raise, enabling the upcoming orphaned-PR
  cleanup periodic agent.
- **periodic**: add orphaned-PR check pass (opt-in, `orphaned_pr_check_periodic`)
  that lists open PRs per managed repo, classifies mill-authored ones as
  orphaned when no active ticket drives them, and either auto-closes the PR
  (with a comment) or files a tracking ticket.  Defaults to dry-run mode;
  gated on `orphaned_pr_dry_run`, age, and per-pass action cap.
  Includes a granular ``OrphanClassification`` enum (superseded,
  conflicting-and-abandoned, ticket-done-unmerged, etc.) produced by the
  ``classify_orphaned_prs()`` core algorithm, with the enumerated
  classification list available on ``OrphanedPrCheckResult.classifications``
  for downstream inspection.
- **docs**: fix 15 configuration-table rows that incorrectly showed `—`
  (YAML-only) in the "Env var" column.  All 15 fields derive valid
  `MILL_*` environment variables through Pydantic's `env_prefix="MILL_"`
  setting; the docs now list the correct env-var name.
- **ci-monitor**: add content-based deduplication for repeated CI failures.
  Each CI-failure draft is now labelled with a `ci_fp:<hash>` fingerprint
  derived from the error content (stripped of metadata, timestamps, and run
  URLs).  Before filing a new draft, the CI monitor checks for a recent
  (``dedup_lookback_days``, default 7 days) ticket with the same fingerprint
  and consolidates the recurrence into a comment instead of filing a
  duplicate.  This prevents the pipeline from filing a fresh ticket for every
  push when the root cause is an ops-only blocker (e.g. a missing repository
  secret).  The forge ``list_workflow_runs`` dict now includes a ``path`` key
  (the workflow file path, e.g. ``.github/workflows/release-please.yml``).
- **docs**: document `MILL_AUDIT_REQUEST_LIMIT`, `MILL_TEST_GAP_REQUEST_LIMIT`,
  `MILL_TEST_GAP_MAX_TOOL_CALLS`, and `MILL_TEST_GAP_MAX_ERRORS` env vars in
  `configuration.md` under new `#### audit` and `#### test_gap` subsections.
- **agents**: add `triage_boilerplate` periodic agent (YAML definition +
  Python runner module) to identify recurring triage patterns and propose
  boilerplate response templates. Follows the standard periodic-agent
  pattern with structured output (`TriageBoilerplateResult`), memory
  ledger round-tripping, and a ``STRUCTURED OUTPUT IS MANDATORY`` block.

- **agents**: implement agent's CHANGELOG insertion instructions now include
  explicit guidance for handling multi-line entries with continuation lines,
  preventing the new entry from being inserted between a bullet's first line
  and its continuation.  Review agent also gains a CHANGELOG well-formedness
  check to detect corruption before human approval.
- **agents**: move per-call content (`repo_dir`, `language_instructions`,
  `deployed_log_summary`) from system prompt to user prompt so the static
  system preamble is identical across every agent call within a ticket
  lifecycle.  This lets the Claude CLI's automatic prompt caching cache
  the system prompt after the first call, reducing per-call input token
  cost for the pay-per-token orchestrator trace and lowering the per-call
  floor across all subscription agent calls.
- **service**: `transition()` and `mark_done()` now refuse the `done` transition when
  duplicate towncrier changelog fragments exist on the ticket's branch HEAD.
  `mark_done()` also now rejects tickets in the `blocked` state (must be resumed
  first).  This closes a bypass where `mark_done` could force-close a blocked
  ticket without resolving the fragment conflict.
- **stages**: route config/docs-only tickets (`.md`, `.yaml`, `.toml`, etc.) to the
  cheaper level-1 (flash) model in `_select_agent_level`, avoiding overprovisioning on
  trivial single-file changes.

- **stages**: add per-stage outcome cache (`_stage_cache.py`) keyed on input hash
  to short-circuit repeated refine and review runs over unchanged ticket content
  or diffs, collapsing the tail of near-identical re-check passes that burn
  subscription headroom without producing new output.

- **dev**: add `.git-blame-ignore-revs` listing the five largest bulk-format/restructure
  commits so `git blame` (and GitHub's blame UI) skip them and attribute lines to the
  last meaningful human-authored change.
- **fix**: add loop detection to the deliver stage's merge guard so that
  consecutive identical "brand-new top-level file" blocks escalate to a
  human-intervention BLOCKED instead of burning cost on a deterministic
  resume→block cycle.  Controlled by `deliver_max_identical_blocks` (default
  2; set to 0 to disable).
- **docs**: document five missing periodic-agent env vars (`MILL_STALE_BRANCH_MAX_AGE_DAYS`,
  `MILL_STALE_BRANCH_CLEANUP_PREFIX_ONLY`, `MILL_TIMEOUT_ESCALATION_THRESHOLD_SECONDS`,
  `MILL_DEPENDABOT_INGEST_MAX_DRAFTS_PER_PASS`, `MILL_MODULE_CURATOR_REQUEST_LIMIT`)
  in the Periodic agents section of `configuration.md`.
- **docs**: restore 15 `MILL_*` env-var entries in `docs/configuration.md` that
  were incorrectly replaced with `—` in PR #1963; these env vars remain
  functional via pydantic-settings' `env_prefix='MILL_'` mechanism.
- **fix**: wrap synchronous forge and DB calls in `_poll_one_repo_dependabot`
  with `asyncio.to_thread` to prevent event-loop blocking during Dependabot
  alert ingest, matching the existing `_poll_one_repo_ci` pattern.
- **dev**: add `actionlint` as a pre-commit hook for GitHub Actions workflow
  linting, matching the v1.7.12 version used in CI.
- **core/service**: split `_lifecycle.py` (1442 lines) into five
  per-domain-action mixin modules — `_create_mixin.py`,
  `_transition_mixin.py`, `_migrate_mixin.py`, `_delete_mixin.py`,
  `_maintenance_mixin.py` — each under ~400 lines.  The assembled
  `TicketService` public API is unchanged.
- **docs**: fix `sandbox.image` documented default in `configuration.md`
  to match the YAML default (`robotsix/mill-sandbox:latest` instead of
  `python:3.14-slim`).
- **docs**: remove stale `MILL_*` env var references from 15
  `core.limits.*` rows in `configuration.md` sections 2–3; these
  aliases were purged during the YAML-only refactor and the fields
  are now YAML-path-only.
- **docs**: add `core.board_list_cache_ttl_seconds` to configuration
  reference table (Worker pool & retry section).
- **docs**: add six missing request-limit fields (`triage_requests`,
  `already_done_requests`, `dedup_max_candidates`, `coordinator_max_tool_calls`,
  `max_refine_explore_calls`, `max_refine_read_file_calls`) to the Request limits
  table in `configuration.md`.

- **docs**: document the 4 extra `MILL_SURVEY_*` env vars (request limit,
  web-fetch max calls/bytes, web-search max calls) in the periodic agents
  section of `configuration.md`, alongside the 3 generic periodic fields.

- **renovate**: enable `pre-commit` manager and add grouping rule so
  Renovate automatically bumps `.pre-commit-config.yaml` hook versions
  on the Monday schedule. Also fix stale Ruff pin (`v0.11.0` → `v0.15.15`
  to match `pyproject.toml`), switching from SHA to tag format.

- **pipeline**: add implement↔review convergence backstop: a configurable
  `max_implement_review_cycles` ceiling (default 10) on total implement passes
  per ticket, empty-diff detection that blocks when a review round produces no
  new commits, and repeated-findings fingerprinting in the review stage that
  escalates early when review asks are identical across rounds.  All three
  paths escalate to BLOCKED for human inspection instead of silently re-running.
- **forge**: extract `_to_repo_info` and `_paginated_get` helpers in
  `GitLabForge` to eliminate internal copy-paste duplication between
  `_create_project`/`_fork_repo` and `_list_branches`/`_list_open_pr_branches`.
- **runtime**: wrap synchronous `forge.list_workflow_runs` and
  `forge.fetch_workflow_job_logs` calls in `asyncio.to_thread` to prevent
  blocking the async event loop during CI monitor polling.

- **docs**: remove duplicate `core.limits.max_openrouter_marginal_usd_per_ticket` row from
  `docs/configuration.md` section 3 (Worker pool & retry) — merge-collision artifact
  left two identical rows.
- **docs**: document `component_agent.*` settings (6 fields) in
  `docs/configuration.md` §14, matching the Pydantic model and YAML
  defaults that were already present but undocumented.)

- **ci**: remove Dependabot `uv` ecosystem graph-submission entry from
  `.github/dependabot.yml`.  The `Configured Graph Update: uv` job has
  been failing repeatedly since 2026-06-26 due to Dependabot's internal
  infrastructure not reliably supporting Python 3.14 lockfiles with
  `[tool.uv.sources]` git dependencies.  Version updates remain with
  Renovate; the dependency graph submission was advisory-only.
- **docs**: document `MILL_RUN_HEALTH_*` extra fields (`MILL_RUN_HEALTH_WINDOW_HOURS`,
  `MILL_RUN_HEALTH_TARGET_REPO_ID`, `MILL_RUN_HEALTH_MEMORY_PATH`) in
  `docs/configuration.md` section 12 (periodic agents) with a dedicated
  `#### run_health` subsection listing all 5 env vars with YAML paths,
  defaults, and descriptions.
- **docs**: fix documented default for `MILL_MAX_ARCHIVED_TICKETS` from 100 to 40 to match the code and YAML defaults.
- **docs**: document `MILL_DB_MAINTENANCE_*` and `MILL_SANDBOX_REAPER_*`
  environment variables in `docs/configuration.md` section 12 (periodic
  agents), including `db_maintenance` and `sandbox_reaper` subsections
  with their env vars, defaults, and descriptions.
- **deps**: relax `requires-python` from `>=3.14,<3.15` back to `>=3.14`
  and revert `uv.lock` `requires-python` from `==3.14.*` to `>=3.14`.
  The `==3.14.*` lockfile format may not be recognized by Dependabot's
  `uv` ecosystem parser, causing graph-submission failures on every push.
- **docs**: add missing pipeline circuit-breaker env vars to
  `docs/configuration.md` — `codeql_fp_triage_enabled` in §7 (Approval &
  review), and `auto_fix_max_cycles`, `ping_pong_max_alternations`,
  `ticket_state_cycle_limit` in §11 (Pipeline tail). (mill: env doc sync: missing-from-docs — pipeline circuit-breaker env vars (4 fields) (20260626T204331Z-env-doc-sync-missing-from-docs-pipeline-7768))
- **cleanup**: remove redundant `from typing import Any` from the
  `TYPE_CHECKING` block in `src/robotsix_mill/runners/trace_review_runner.py`
  (`Any` is already imported at module level).
- **ci**: raise Dependabot `uv` ecosystem `open-pull-requests-limit` from 0
  back to 1. The `[tool.uv.sources]` migration resolved the inline-URL
  parsing issue, but `limit: 0` (graph-submission-only mode) still triggers
  a Dependabot bug with git-backed packages. With `limit: 1` the full
  version-check pipeline runs and handles them correctly.
- **deps**: switch back from inline PEP 508 `@ git+https://` URLs to
  `[tool.uv.sources]` for all git dependencies. The inline-URL approach
  (e9a52c3f) still triggers Dependabot `uv` ecosystem graph-submission
  failures on main; `[tool.uv.sources]` is the configuration the grapher
  can parse correctly.
- **deps**: remove `[tool.uv.sources]` and inline all git dependencies as
  PEP 508 `@ git+https://` entries in `[project].dependencies` and
  `[dependency-groups].dev`.  The `pin_pep508_entry` Dependabot bug was
  actually in the reverse direction: `[tool.uv.sources]` is what the `uv`
  ecosystem grapher cannot resolve, while inline PEP 508 URLs work correctly.
  Revert `dependabot.yml` `open-pull-requests-limit` from 1 back to 0 — the
  graph-submission-only mode is now sufficient.
- **deps**: restore `[tool.uv.sources]` git-dependency resolution (bare names
  in `[project].dependencies`, full commit SHAs in `[tool.uv.sources]`) and
  remove inline PEP 508 `@ git+https://` URLs. The inline-URL configuration
  (restored in a prior commit) still causes Dependabot `uv` ecosystem
  graph-submission failures even with `open-pull-requests-limit: 1`. The
  `[tool.uv.sources]` + `limit: 1` combination runs the full version-check
  pipeline which handles git-backed packages correctly.
- **deps**: raise Dependabot `uv` ecosystem `open-pull-requests-limit` from 0
  to 1 to work around a known Dependabot bug in graph-submission-only mode.
  When the limit is 0 the `uv` grapher runs a reduced pipeline that cannot
  resolve `[tool.uv.sources]`-backed git dependencies; with limit ≥1 the full
  version-check pipeline runs and handles git-backed packages correctly.
- **deps**: complete the git-dependency migration: remove the remaining inline
  `@ git+https://` PEP 508 direct references from `[project].dependencies` and
  `[dependency-groups].dev` (they were already mirrored in `[tool.uv.sources]`).
  The `pin_pep508_entry` Dependabot bug drops inline URLs, so keeping them
  causes the `uv` ecosystem graph-submission to fail even when
  `[tool.uv.sources]` coexists. Switch `security-audit.yml` from `pip install`
  to `uv sync` / `uv run` so the audit jobs continue to function without the
  PEP 508 inline references.
- **deps**: revert the `[tool.uv.sources]`-only approach and restore inline
  PEP 508 `@ git+https://` direct references for all git dependencies. The
  `uv`-sources-only configuration (bare names in `[project].dependencies`,
  git URLs in `[tool.uv.sources]`) caused a Dependabot `uv` ecosystem
  graph-submission failure on every push. The prior working configuration
  (inline PEP 508 URLs with no `[tool.uv.sources]` table) is restored.
  The `security-audit.yml` `uv` migration is kept — `uv sync` / `uv run`
  handles inline PEP 508 URLs correctly.
- **deps**: pin `robotsix-yaml-config` to a specific commit to resolve a
  `uv lock` conflict with `robotsix-modules`' transitive pin; fixes the
  Dependabot `uv` ecosystem graph-submission failure on main.
- **observability**: enrich agent trace root spans with ticket state, retry
  attempt, transient error reason, review rounds, dispatch count, and blocked/
  paused origin — metadata previously only in the SQLite row or in-memory
  counters is now stamped onto Langfuse traces so expensive/runaway tickets
  are diagnosable from trace data alone.
- **periodic**: remove orphan `.robotsix-mill/periodic/board_cleanup.yaml` and
  `cost_reconciliation.yaml` presence files that had no implementation anywhere
  in the codebase, and drop the matching dead Ruff per-file-ignore entry for
  the non-existent `cost_reconciliation_runner.py`.
- **config_syncing**: document missing `recent_proposals` and `verified_proposals`
  parameters in `run_config_sync_agent` docstring.
- **board UI**: add missing `runFrontendSync()` button to the Agents dropdown
  menu in `board_html.py` — the `frontend_sync` `llm_agent` was the only one
  without a user-facing trigger in the board.
- **refine**: re-resolve clone target from the ticket's current `board_id` at
  clone time so a ticket migrated between boards before refine runs clones the
  destination board's repo (not the stale creation-time repo).
- **ci_poll**: extract duplicated PR status-check preamble into shared `_check_pr_baseline` helper, removing ~135 lines of near-identical code across three poll methods.
- **docs**: add missing `core.limits.max_openrouter_marginal_usd_per_ticket` row to configuration reference section 3 table.
- **docs**: add `core.limits.refine_requests_simple` to the request limits table in
  `docs/configuration.md`.
- **docs/configuration.md**: add missing `stale_branch_cleanup` to the named
  periodic-agent roster in section 12.
- **docs**: add `dependabot_ingest` to the periodic agent roster in
  `configuration.md` section 12 (was missing from the 22-agent list).
- **board-mill.js**: add `asked: "answer"` to `STATE_TRACE` so the history
  timeline renders `ASKED` transitions with the "answer" stage label instead
  of a bare state name.
- **board CSS**: add `.s-asked` state colour (`#f59e0b` amber) so column
  headers and event chips for the `ASKED` inquiry state render with colour.
- **board UI**: add `maintenance` entry to `STATE_TRACE` in `board-mill.js`
  so the history timeline labels maintenance transitions correctly.
- **board-mill.css**: add missing `.s-answered` state colour (teal, `#14b8a6`)
  so that column headers and event chips for the `ANSWERED` state render with
  the appropriate colour.
- **board-mill**: add missing `SOURCE_CLASS` entry and CSS badge for
  `dependabot_alerts` source kind — tickets from the dependabot-alerts scanner
  now show a dedicated amber/orange badge instead of the generic "USER" badge.
- **refine agent**: extend triage-findings instruction to prevent wasted
  `read_file` calls on paths already confirmed as nonexistent during triage.
- **trace_review**: suppress false-positive `tool_errors` flag for `run_command`
  outputs containing the expected git rebase conflict notification
  (`error: could not apply`), which the rebase agent produces when it
  encounters conflicts it is expected to resolve.
- **trace_review env-var docs**: add dedicated sub-table under section 12 of
  `docs/configuration.md` documenting all 22 `MILL_TRACE_REVIEW_*` environment
  variables with their YAML paths, defaults, and descriptions.
- Document all 16 `MILL_DATA_DIR_AUDIT_*` environment variables in
  `docs/configuration.md` section 12, including the three standard
  periodic-agent fields and the thirteen agent-specific GC/size-threshold
  settings.
- Document missing gate and refine-routing environment variables in
  `docs/configuration.md`: add `reviewer_agreement_gate_enabled` and
  `refine_mill_misroute_gate_enabled` to section 7, and add a new
  section 11.3 (Refine routing) covering the refine subscription model
  routing knobs (`refine_trivial_*`, `refine_subscription_*`,
  `refine_findings_*`, `max_re_refine_cycles_before_cheap`,
  `refine_delta_reuse_enabled`).
- Add `.github/ISSUE_TEMPLATE/config.yml` to disable blank issues and
  redirect questions, feature requests, and security reports to
  Discussions and the security policy page.
- Add `.github/PULL_REQUEST_TEMPLATE.md` with the PR checklist from
  `CONTRIBUTING.md` and a note for autonomous agents.
- **Module test layout alignment**: moved `tests/runtime/test_component_agent.py`
  to `tests/component_agent/test_component_agent.py`, matching the per-module
  directory convention used by every other module. Added
  `tests/component_agent/__init__.py`. Updated `docs/modules.yaml` accordingly.

- **Refine no-op guard**: refine passes that yielded a "no change needed"
  verdict (refiner returned `no_change_needed=True`, reviewer-agreement
  guard, or triage `NO_CHANGE` classifier) now route TASK-kind tickets
  without a branch toward `ready` (implementation) instead of auto-closing
  to `done`.  The `_guard_implementation_done` helper in
  `stages/refine/core.py` provides defense-in-depth — any future DONE
  outcome for a branchless TASK ticket that does not carry a recognised
  non-implementation prefix (dedup, freshness, obsolescence, misroute) is
  redirected to `ready`.  Maintenance tickets and human `mark_done` are
  unaffected.  The non-implementation close-prefix constants moved from
  `stages/refine/helpers.py` to `core/constants.py` so the guard can
  import them without a circular dependency.

- Exclude cross-reference sections (`## Reference`, `## See also`, `## Related work`, and any heading starting with `reference` or `see also`) from path-token extraction in `paths_excluding_out_of_scope`, preventing consumer-migration follow-ups from being filed for paths that are merely cross-referenced, not deliverables.
- Make the per-pass implement (coordinator) agent-request budget configurable
  via `MILL_PER_PASS_REQUEST_BUDGET` env var (or `core.limits.coordinator_requests`
  in YAML config).  Default raised from 200 to 500 so normal-sized tickets
  complete in a single pass.  Hard upper bound 5000 prevents runaway cost
  from misconfiguration.

- Extract duplicate `_paths_from_diff` from `document.py` and `review.py` into shared `vcs/git_ops.py`.
- Add `actionlint` step to `workflow-audit` job in `security-audit.yml` for workflow syntax checking, pinned to `rhysd/actionlint@v1.7.12`.

- Wire `frontend-sync` agent into the dispatch/UI layers: add POST route
  (`/frontend-sync`), CLI `_RUNNERS` entry, AGENT_COLORS key, and board
  button handler + window export in `board-mill.js`.

- Split `src/robotsix_mill/stages/refine/orchestration.py` (2181 lines) into focused sub-modules: `_result_paths.py` (no-change/promote/single/multi result handlers), `_triage.py` (triage skip logic), `_checkpoint.py` (save/load/clear checkpoint), `_reconcile.py` (short-circuit and side-effect application). The `orchestration.py` module is now a 499-line thin coordinator delegating to sub-module functions. Backward-compatible: `RefineAgentMixin` exposes delegation staticmethods and module-level re-exports preserve test monkeypatch targets.

- Add `robotsix-deploy` package: central deployment & lifecycle server scaffold with FastAPI /health and /ready endpoints, env-based config (`DEPLOY_*` prefix), Dockerfile, docker-compose, CI workflow calling the shared `python-ci.yml`, and GHCR publish workflow calling `docker-release.yml`.
- Fix `Dockerfile.deploy` builder stage missing `git` installation — `uv pip install` needs git to clone the git-sourced dependencies (robotsix-llmio, robotsix-yaml-config, robotsix-board-agent, robotsix-agent-comm, robotsix-board) when installing from the lockfile-generated requirements.txt.

- Fix `docs/configuration.md` — correct `core.limits.test_requests` documented default from `16` to `30` to match the model and YAML defaults.

- Enable `board_cleanup` periodic workflow for `robotsix-mill` by adding the per-repo opt-in file `.robotsix-mill/periodic/board_cleanup.yaml`.
- Enable `cost_reconciliation` periodic workflow for `robotsix-mill` by adding the per-repo opt-in file `.robotsix-mill/periodic/cost_reconciliation.yaml`.
- Extend `_served_reads` closure-scoped read-file dedup to cover full-file re-reads on the Claude SDK path (`ctx=None`), matching the pydantic-ai path behavior. Full re-reads of already-loaded files are now refused instead of silently re-serving cached content. Added `_served_reads` invalidation on `write_file`, `edit_file`, and `delete_file` so mutations clear the dedup record.

- Add missing `addressing_review` entry to `STATE_TRACE` map in `board-mill.js`, fixing bare state-name display for the `ADDRESSING_REVIEW → HUMAN_MR_APPROVAL` transition in the drawer history timeline.
- Add missing `.s-implement_complete` CSS state colour (`--c: #3b82f6`) to `board-mill.css`, fixing rendering of column headers, event chips, and child-state badges for the `implement_complete` pipeline state.
- Add `.s-maintenance` CSS state-colour selector to `board-mill.css` so MAINTENANCE-state column headers and event chips render with the same gray as DRAFT.
- Add missing `.s-addressing_review` CSS selector to `board-mill.css` so tickets in `AddressingReview` state render with amber column-header and event-chip styling on the board.
- Add `frontend_sync` entry to `SOURCE_CLASS` map in `board-mill.js` and corresponding `.src-frontend-sync` CSS badge class in `board-mill.css`, so tickets created by the frontend_sync periodic agent display a dedicated purple badge instead of the generic "user" fallback.
- Add mandatory `read_file` verification gate to triage agent NO_CHANGE path, and prior-stage findings protection to dedup agent prompt, preventing hallucinated file-existence assertions that incorrectly close tickets.

- Enable `frontend_sync` periodic workflow for `robotsix-mill` by adding the per-repo opt-in file `.robotsix-mill/periodic/frontend_sync.yaml` that cross-references Python `State`/`SourceKind` enum values against CSS selectors and JS maps in `board-mill.{css,js}`.
- Add cross-repo import verification instruction to refine agent prompt: before referencing a symbol from a git-pinned dependency, verify it is importable; emit a ````prereq```` block on failure rather than assuming the module exists.
- Fix stale default in docs: `core.limits.doc_requests` is `16` (matches model and YAML), not `8`.
- Wire spec-review conciseness pass into the multi-scope degraded path so auto-approve receives the concise spec instead of the verbose original when a split result degrades with no valid children.
- Add `is_file()` pre-check in `list_dir` fs tool: when an agent calls `list_dir` on a file path, return a clear error message ("is a file, not a directory — use read_file") instead of the opaque `NotADirectoryError`.
- Cap SQLite WAL file at 2 MiB via `PRAGMA journal_size_limit` and add `PRAGMA wal_checkpoint(TRUNCATE)` to the periodic DB maintenance pass to prevent unbounded WAL growth.
- Enrich FastAPI OpenAPI schema with version, description, contact, license, servers, and tag descriptions parsed from `pyproject.toml`.
- Replace 9 inline `try: except Exception: pass` ALTER TABLE blocks in `init_db()` (one per column) with calls to `add_column_if_missing()`, which catches `sqlite3.OperationalError` specifically, consolidates all column migrations into a single transaction, and returns `bool` for whether each column was newly added. The helper lives in `src/robotsix_mill/core/sqlite_utils.py` (mirrors the API of `robotsix_llmio.core.sqlite_utils` from PR #255).
- explore sub-agent prompt: add "CONFIRM PATHS" grep guard and "NO GREP CHASING" rule to prevent wasteful grep calls on non-existent paths.
- refine agent prompt: add grep-avoidance guidance in the budget warning section — use `read_file` on a known path instead of a speculative `grep` when the file location is already known.
- Fix Dependabot `uv` ecosystem graph-submission failure: remove the redundant `[tool.uv.sources]` table from `pyproject.toml` (the `robotsix-llmio` git source is already declared via PEP 508 `@ git+https://` in `[project.dependencies]`); extend `_has_uv_sources()` in the sandbox to also detect PEP 508 `git+https://` direct references so the sandbox continues to prefer `uv sync` over `pip install` for projects with git dependencies.
- Remove stale `run_command` mention from `completeness_check` budget-warning boilerplate — the agent doesn't have `run_command` at runtime.
- Replace stale `awaiting_approval` state references with canonical `human_issue_approval` in docs (`approval-gate.md`, `audit-agent.md`, `configuration.md`, `docker-architecture.md`, `notifications.md`).
- Wire `frontend_sync` periodic agent into the dispatch pipeline: add `SourceKind.FRONTEND_SYNC`, register in `_BUILTIN_KINDS`, add `PeriodicPassConfig`, and create backward-compat runner stub.

- Replace bare string `kind` literals (`"task"`, `"epic"`, `"inquiry"`) with `TicketKind.TASK` / `TicketKind.EPIC` / `TicketKind.INQUIRY` in all test files; add `scripts/check_kind_literals.py` CI gate to prevent regressions.

- Wire `completeness_check_request_limit` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS` (`periodic.completeness_check.request_limit`) and default leaf in `config/mill.defaults.yaml`.
- Remove stale `changes/` directory and its two unused `.misc.md` changelog fragments; drop `changes/**/*` glob from `docs/modules.yaml` dev-tooling module paths.
- Add `fail_under = 80` to `[tool.coverage.report]` so `coverage report` fails when total coverage drops below 80%.

- Add 11 unit tests for `src/robotsix_mill/core/sqlite_utils.py` covering `_execute_sql`, `add_column_if_missing`, `run_additive_migrations`, and error paths; narrow `add_column_if_missing` exception catch to only suppress "duplicate column" errors (let `no such table` / syntax errors propagate instead of silently returning `False`).

- Add per-ticket circuit breaker: `max_traces_per_ticket` (trace-count guard, default 15) and `max_openrouter_marginal_usd_per_ticket` (OpenRouter spend guard, default $3.00), wired through settings, YAML config aliases, and `config/mill.defaults.yaml`; integrated into `Worker._check_progress` with Langfuse `session_traces()` to block runaway loops that the dollar cap may miss.
- Increase document stage `request_limit` from 8 to 16 to prevent `UsageLimitExceeded` errors on feature-sized tickets that need multiple file reads and edits.
- Deduplicate triage system prompt: remove redundant "Tool-use discipline" section (~378 tokens) and fold unique budget/history guidance into the "Tool: `read_file`" section (~92 tokens), saving ~286 input tokens per triage call.

- Refactor `ci_fix.py`: extract stateless helpers (formatters, hashing, `_FailingContext`) into `ci_fix_helpers.py` and CodeQL FP triage subsystem into `ci_fix_codeql.py`; update all importers.

- Wire `language_instructions_dir` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS` (`core.language_instructions_dir`), default leaf in `config/mill.defaults.yaml`, and documentation row in `docs/configuration.md`.
- Fix merge-gate stall on clean mergeable PRs: accept `mergeable_state == "unstable"` as promotable in `_ci_truly_green` (required gates passed, only non-required status non-green); add `pending` check-name list to `check_status`/`_derive_check_conclusion` return dicts; log precise blocking reason (conclusion + mergeable_state + pending checks) when re-polling `IMPLEMENT_COMPLETE`.

- Fix deptry dependency issues: add `opentelemetry-api` to the `tracing` extra (we import `opentelemetry.trace` directly) and add `opentelemetry-sdk`/`opentelemetry-exporter-otlp-proto-http` to the deptry DEP002 ignore list (they are needed transitively by `robotsix_llmio`).
- Fix `_ensure_tracing` to catch `ImportError` from `setup_langfuse_tracing()` so missing `opentelemetry` dependencies degrade gracefully instead of crashing ticket processing.
- Fix `ci_fix_request_limit` config drift: add YAML alias in `_YAML_PATH_TO_ALIAS` and default leaf `ci_fix_request_limit: 120` in `config/mill.defaults.yaml` so the setting is configurable via YAML (matching every other pipeline-level limit).
- Wire `bespoke_discovery_interval_seconds` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS`, default leaf in `config/mill.defaults.yaml`, and documentation row in `docs/configuration.md`.
- Wire `bespoke_periodic` to YAML config: add alias mapping in `_YAML_PATH_TO_ALIAS`, default leaf in `config/mill.defaults.yaml`, and documentation row in `docs/configuration.md`.
- Strengthen refine agent budget warning: hard-cap `read_file` at ≤10 calls per generation, mandate `explore`/`parallel_explore` for multi-file work, and add a 20-tool-invocation stop-rule to force delegation to sub-agents after the first 20 `read_file`+`run_command` calls.
- Fix `run_doc_agent` crash on empty `board_id`: guard all memory-ledger operations (`memory_file_for`, `load_memory`, `persist_memory`) behind a non-empty `board_id` check so the doc agent runs without a memory ledger instead of raising `ValueError` — resolves non-blocking failures on meta-split child tickets where `board_id` was empty/unresolvable.
- Stamp `board_id=ticket.board_id` on split-child and umbrella-epic `TicketService.create()` calls in `orchestration.py` so split children carry the parent ticket's resolvable board rather than silently inheriting the service's `self.board_id`.
- Refactor `_triage_skip()` in refine orchestration: extract `_parse_prior_boards()`, `_anti_bounce_escalate()`, and `_persist_triage_complexity()` helpers to reduce function length and eliminate 9-level nesting in the MIGRATE migration-history parsing path.
- Fix `build_agent()`: pass `model` through to `provider.build_agent()` in the Claude-SDK branch so the `refine_claude_model` setting (default `sonnet`) actually takes effect, right-sizing the refine stage off Opus while staying on the same subscription transport.
- Remove dead `try/except ImportError` fallback for `robotsix_board.render_config_script` in `_health.py` — `robotsix-board` is a required runtime dependency, so the `except` branch was unreachable dead code. (mill: Remove dead robotsix-board fallback from _health.py (proposal 4226 follow-up) (20260623T180731Z-remove-dead-robotsix-board-fallback-from-d4de))
- Forward `max_tokens` to the Claude SDK provider in `build_agent()` so the YAML-defined `max_tokens` cap (e.g. 8192 for `refine.yaml`) is actually enforced, preventing ~$1.71 Opus output-cost spikes.
- Add JSON-only-output directive to epic-breakdown agent system prompt to prevent prose-before-JSON failures that trigger costly pydantic-ai retries.
- Document `MILL_CI_FIX_REQUEST_LIMIT` (default 120) in `docs/configuration.md` — the Pipeline tail section already listed other `ci_fix_*` knobs but omitted this one.
- Add `MILL_MAX_EVENTS_PER_TICKET` and `MILL_MAX_COMMENTS_PER_TICKET` to `docs/configuration.md` Pipeline tail table — both fields existed in code with defaults but were missing from the documentation.
- Fix `docs/configuration.md`: update documented default for `sandbox.image` from `robotsix/mill-sandbox:latest` to `python:3.14-slim` to match the Pydantic model default in `_settings_core.py`.
- Drop dead `body` parameter from `_is_noop_draft()` in `retrospect.py` — the function always ignored it, delegating to the title-only `is_noop_report`.
- Trim auto-approve classification system prompt: stripped verbose example reason bullets and redundant formatting instructions to roughly halve cached input tokens on every OpenRouter pay-per-token call.
- Remove dead `_absorb_findings_list_shape` model validator from `RetrospectResult` — the list-of-dicts findings edge case it handled has never been re-triggered and is untested.
- Remove dead backward-compat alias `BoardCleanupPassResult` from `periodic_runner.py` and its vulture whitelist entry.
- Add `frontend_sync` periodic agent that cross-references Python enum values (State, SourceKind) against their mirrored CSS selectors (`.s-*`, `.src-*`) and JS maps (`SOURCE_CLASS`, `STATE_TRACE`, `AGENT_COLORS`), filing draft tickets for any drift between backend enums and frontend representations.
- Convert `Ticket.kind` and `TicketCreate.kind` from free-form `str` to a `TicketKind(StrEnum)` with `TASK`, `INQUIRY`, `EPIC` variants. Replaced all magic-string comparisons across 14 files. Added a comment referencing the canonical enum in `board-mill.js`.- Fix CSS badge class mismatch: `.src-env-sync` → `.src-env-doc-sync` so `env_doc_sync`-sourced tickets render with correct styling on the board.
- Wire `periodic.test_gap.request_limit` to YAML config so operators can tune the test-gap agent's request cap without code changes.
- Document `MILL_LANGFUSE_CLEANUP_MAX_TRACES` env var in `docs/configuration.md` (default 1000, cap on retained traces per Langfuse project).
- Add `core.claude_max_concurrency` YAML path mapping and defaults entry so operators can configure Claude SDK concurrency via YAML instead of only the `MILL_CLAUDE_MAX_CONCURRENCY` env var.
- Wire `pipeline.ci_fix_wait_poll_interval_s` and `pipeline.ci_fix_wait_timeout_s` to YAML config so operators can tune CI-fix wait timing without code changes.
- Fixed `docs/configuration.md`: corrected the documented default of `stage_timeout_overrides` from `{}` to `{"refine": 900}` to match the Pydantic model default, and added a note explaining the built-in refine-stage cap.
- Update `MillBoardAdapter` docstring to remove outdated fallback language now that `robotsix_board` is a required dependency.
