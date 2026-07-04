## 0.0.0 (unreleased)

- Serve ``GET /chat-skill`` endpoint returning a SKILL.md document that teaches the chat agent how to drive the board API (read tickets, post comments, manage state transitions, create via ingest). Includes mandatory safety rules requiring user confirmation for state-changing operations.
- Stage timeout bookkeeping now tracks monotonic time alongside UTC
  wall-clock time; timeout log messages and traces include actual
  elapsed seconds to distinguish genuine timeouts from clock-skew
  false positives.
- Deploy: set `MILL_API_HOST=0.0.0.0` in `deploy/docker-compose.yml` so the container binds all interfaces (reachable by the central-deploy gateway) regardless of the onboard-written `api_host`.

- Remove the dead `skip_local` parameter from `load_config()` in `src/robotsix_mill/config/loader.py` (no callers remained).
- Config: `load_repos_config` accepts both the nested `ReposRegistry` (`{meta, repos}`) shape written by central-deploy onboarding and the legacy flat `{repo_id: cfg}` shape, so a fresh onboard no longer crash-loops on `repos={"meta":null,"repos":{}}`.

- Extract the 401-auth-retry pattern from ~10 inline `for retry in range(2)` loops across `github.py`, `github_ci.py`, and `github_pr.py` into a shared `_ApiClient.retrying_client()` generator. The generator yields `(retry_index, client, api_base, headers)` per attempt, handles token invalidation + 2 s backoff automatically, and lets callers `continue` on 401 / `break` on success. Added optional `headers_factory` parameter so callers with custom headers (e.g. repo-creation PAT) can use the same retry helper.
- Pin-bump PR actuator: coherence-check skip on conflicts, duplicate-PR guard, and `max_inflight_prs` throttling. The actuator now uses `run_coherence_check` (`deps/coherent_resolver.py`) instead of raw `uv lock`, and gates on `pin_bump_periodic`.
- Enable `changelog_autofill` periodic workflow for this repo.
- Add `agent_references/pre-commit-ci.md` documenting standard
  boilerplate fixes for the four most common pre-commit CI failures
  (mdformat wrapping, missing trailing newline, stale detect-secrets
  baseline, ruff formatting).
- Security posture agent: add explicit `git ls-remote` SHA-resolution and
  validation instructions to the system prompt so the agent no longer
  fabricates or guesses commit SHAs when filing mutable-ref pinning drafts.
- Plumb `include_write_file` parameter through `make_agent_runner` /
  `run_periodic_agent` / `_build_periodic_tools` so that the
  `env_doc_sync` periodic agent can receive the `write_file` tool
  (fixing a mismatch between its system prompt and available tools).
- New periodic agent `repo_description_sync`: keeps each repo's forge (GitHub/GitLab) description in sync with its README. Runs daily, applies a conservative update policy (only when description is empty, a placeholder, or materially inaccurate). Includes GitHub `get_repo_description` read path and GitLab `_update_project` implementation.
- Remove the "+ Ask" button from the board UI and the inquiry-to-task
  conversion pipeline (`convertToTicket` JS, `/convert-to-task` route,
  `ask_to_ticket` agent).  Inquiries can still be created via the CLI
  (`inquire`) and answered by the answer stage; only the unused UI
  button and its conversion machinery are removed.
- Rename `/health/live` to `/health` (round-4 standard): liveness endpoint now returns `{"status": "alive"}` at `/health`, old route removed. Updated Docker HEALTHCHECK, deploy-compose healthcheck override, smoke script, vulture whitelist, and design doc.
- Fix CSS class name mismatch for `data_dir_gc` source: rename `.src-data-dir-audit` to `.src-data-dir-gc` and add missing `AGENT_COLORS` entry.
- Extract `_collect_candidate_boards()` to `_ServiceBase`, deduplicating the cross-board discovery algorithm that was copy-pasted across `_get_anywhere()`, `list_children_across_boards()`, and `_board_for_comment()`.
- Update "Add a new setting" and "Config drift prevention" sections of `docs/configuration.md` to describe the current JSON-based config mechanism instead of the removed `_YAML_PATH_TO_ALIAS` / YAML-path mapping (the config file is `config/config.example.json`, and `JsonSettingsSource` matches alias keys automatically).
- Update `docs/configuration.md` to correctly describe the JSON config format
  (not YAML): fix file extensions, convert code examples to JSON with flat
  alias keys, add a note explaining that "YAML path" column entries are
  conceptual dotted paths while actual JSON keys are flat aliases, and
  remove references to the removed `_YAML_PATH_TO_ALIAS` mapping.
- Add `coordinator_timeout_overrides` settings field (dict[str, int]) for per-stage coordinator timeout overrides, analogous to `stage_timeout_overrides`. The `run_coordinator` and `run_implement_agent` functions now accept a `stage_name` parameter (default `"implement"`) used to look up the override.
- Slimmed `agent_definitions/language_instructions/python.md` and `javascript.md` to mill-specific sandbox constraints only; generic conventions now live at robotsix-standards.
- Deliver the pin-bump PR actuator: `run_pin_bump_pr_actuator` resolves
  latest SHAs via `git ls-remote`, edits `pyproject.toml` `rev` values,
  regenerates `uv.lock`, and opens cross-repo PRs for every stale internal
  dependency pin. Pins already at the latest SHA are skipped (idempotent).
  Added `ls_remote_sha` to `git_ops` for lightweight remote SHA resolution.
- Add `Forge.update_repo(owner, repo, description)` abstract method and GitHub implementation (PATCH /repos/{owner}/{repo}) that reuses `_clamp_repo_description` for safe description updates.
- Deploy: point `deploy/docker-compose.yml` `robotsix.deploy.config-target` to `/app/config/config.json` (was `config.yaml`), matching the JSON config-standard the mill reads. Requires the coupled robotsix-central-deploy onboarding migration to write JSON.
- Refine/triage stage ``_clone_or_resume`` now honours ``cross_repo_target``:
  when set, clones the fork's remote URL and targets the fork's
  ``base_branch`` instead of the managed repo. Prevents false "already
  exists" / "no change needed" conclusions during file-existence checks and
  config analysis for cross-repo-target tickets.
- `mark_done()` (force-close) now auto-closes open `[ASK_USER]` threads
  and records the closure in the note, preventing operator force-closes
  from bypassing unanswered agent questions.
- Extended the skip-refine mechanical fast-path to cover user-source (draft pipeline) tickets. User-created tickets with mechanical drafts now bypass the expensive refine agent when the auto-approve triage confirms no design decisions are needed — matching the existing behaviour for classify, audit-gap, and CI→draft pipelines.
- Fix mill Docker image build: `COPY contrib/` into the builder stage so the `contrib/completions` wheel force-include resolves (was failing the image build with `Forced include not found`, blocking image publishing).
- Fix release Docker build: add missing `COPY contrib/ ./contrib/` to the builder stage so hatchling can find the `contrib/completions` force-include path.
- Fix `release.yml` startup_failure: remove the invalid `secrets: GITHUB_TOKEN` passed to the reusable `docker-release.yml` (GITHUB_TOKEN is reserved and cannot be passed to a reusable; the reusable uses the auto-token internally). Unblocks mill/sandbox/proxy image publishing.
- Review stage now verifies PR/commit claims in "already addressed" gap dismissals via `verify_claim`, preventing false approvals when a cited artifact does not actually touch the target files.
- Add "+ Repo" button to the board header that opens a modal form to register a new repo via POST /repos, refreshing the repo selector on success.
- Deploy-UI config schema: changed class-level docstrings on Settings, Secrets, CrossRepoTarget, and ReposRegistry to be operator-facing descriptions instead of implementation notes. Regenerated `config/config.schema.json`.
- Add `verify_claim()` helper to verify that PR/commit references in
  "already done" claims actually touch the target files. The dedup guard
  now calls this before accepting an `already_done` verdict, preventing
  false dismissals where a cited PR or commit does not touch any file
  named in the draft.
- `_mint_installation_token`: catch all non-success responses from the GitHub App installation endpoint (not just 404), preventing `HTTPStatusError` from escaping and causing periodic-pass runs to be recorded as errors when the App is not installed on a board's repo (mill: audit errors on robotsix-yaml-config: GitHub API 404 for App installation endpoint (2×) (20260703T011001Z-audit-errors-on-robotsix-yaml-config-git-9d4d) [WIP])
- Handle GitHub App 404 on `/installation` gracefully: raise a
  specific `GitHubAppNotInstalledError` instead of a generic
  `HTTPStatusError` so callers can distinguish "app not installed"
  from other HTTP failures.  `_clone_token` now logs a warning
  and returns `None` rather than silently swallowing the error. (mill: audit errors on robotsix-yaml-config: GitHub API 404 for App installation endpoint (2×) (20260703T011001Z-audit-errors-on-robotsix-yaml-config-git-9d4d) [WIP])
- Add ``coherent_resolver`` module in ``robotsix_mill.deps`` — resolves cross-repo
  git-rev consistency for shared transitive dependencies by computing a single agreed
  commit per shared dep.  Includes a ``uv lock``-based coherence check that empirically
  discovers ``Requirements contain conflicting URLs`` failures.
- Pin-bump: register scheduled runner harness (`run_pin_bump_pass`) in `_SCHEDULE_ONLY_RUNNERS` dispatch dict so the periodic supervisor no longer drops `pin_bump` presence files. The runner performs detection only — computes and logs the internal-dependency topological order + current pin SHAs — with no PR creation (PR actuator is tracked separately). (mill: Wire the schedule-only pin_bump periodic runner into mill's scheduler (20260703T013022Z-register-a-scheduled-schedule-only-pin-b-c72c))
- Suppress redundant consolidation rollup tickets from the `security_posture` periodic agent: before filing a rollup that bundles multiple stale-GitHub-Action updates, the agent now checks whether its own per-action tickets (from the same scan) have already been merged or closed, and skips or narrows the rollup accordingly.
- Fix periodic pass crash when `github_token()` raises `httpx.HTTPStatusError` (e.g. 404 from `/repos/{owner}/{repo}/installation` when the GitHub App is not installed on a repo). Broaden `_clone_token`'s exception handler from `RuntimeError` to `Exception` so any token-minting failure returns `None` and the downstream clone failure is handled gracefully.
- Add budget-discipline section to the trace inspector system prompt to prevent ``UsageLimitExceeded`` errors when the agent exhausts its request budget before filing findings
- Dedup agent: add explicit output-format instruction to system prompt and enable `strict`/`extra='forbid'` on `DedupResult` model to prevent pre-JSON narration that caused structured-output parse failures.
- Fix implement↔review convergence backstop to respect `cross_repo_target.base_branch` instead of always comparing against `origin/main`; extract `effective_target_branch()` helper to keep DRY between `_clone_and_branch` and the backstop gate.
- Skip the ~12KB module taxonomy map in the refine agent's system prompt
  when triage classifies the ticket as "simple" (`include_explore=False`).
  Saves ~3K input tokens per turn for simple-ticket refine runs.
- Dedup guard: add sibling-with-same-parent bypass — when the current ticket and a candidate share the same parent epic, allow the dedup regardless of branch-merge status, preventing unnecessary refine passes on parallel consumer-migration tickets.
- Board UI: show each ticket's short id (trailing hex suffix, e.g. `f77f`) as a click-selectable badge on its card, so tickets can be identified without opening the detail view.
- In `trace_inspector.py`, `_shrink_trace_data` now sends a summarised
  observation tree when the trace has more than 200 observations.
  Input/output/metadata fields are stripped and only structural fields
  (id, type, level, statusMessage, name, model, calculatedTotalCost,
  latency, usageDetails, startTime, endTime) are kept, reducing the
  trace payload from ~78K to ~10K–15K tokens for observation-storm
  traces.
- Improved the ``read_file`` refusal message in ``fs_tools.py`` to use a
  prominent ``REFUSED (do NOT retry):`` prefix and include the file path and
  line range more clearly. Added an explicit warning to the explore agent's
  system prompt that ``read_file`` will refuse re-reads of already-served
  ranges and not to retry — synthesise from context instead.
- Add grep-to-answer shortcut guidance to explore agent prompt: when grep output already provides the answer, skip read_file confirmation to save prompt tokens per scout call.
- Boost trace-inspector request budget when the trace carries an `observation_storm` classifier flag — tools-on path floors at 40 requests (up from 20) and the tool-less fallback floors at 10 (up from 3), preventing `UsageLimitExceeded` mid-analysis on noisy traces.
- Implement stage now clones the cross-repo target's fork repo (via `cross_repo_target.fork_remote_url`) instead of the managed repo when `cross_repo_target` is configured. This ensures the implement agent sees the target repository's actual file system — fixing silent "no change needed" outcomes when a target file already exists in mill but not in the target repo. (mill: Implement stage must clone cross-repo target instead of managed repo for cross_repo_target tickets (20260702T231513Z-implement-stage-must-clone-cross-repo-ta-4ddc))
- Add shell completion support (bash, zsh) via shtab. The CLI now accepts
  ``--print-completion <shell>`` to generate on-the-fly completions, and
  static completion scripts are shipped in the wheel at
  ``robotsix_mill/completions/``. Use ``make completions`` to regenerate.
- Wire pin-bump periodic agent config defaults, presence-file schema, and egress/credential documentation (`docs/pin-bump.md`).
- New `POST /tickets/{ticket_id}/abandon-epic` endpoint: transitions an `EPIC_OPEN` epic to `EPIC_CLOSED`. Once abandoned, the epic stops regenerating children. Non-`EPIC_OPEN` tickets return 422. BLOCKED epics also skip child-spawning re-evals.
- Auto-recover resumable errors and fix CI-source misroute: extend transient-error classifier with message-string fallback for pydantic-ai `UnexpectedModelBehavior` patterns (`Invalid response from openrouter`, `Exceeded maximum output retries`); add CI-debt auto-resume periodic pass (re-checks BLOCKED tickets whose target-branch workflows have since turned green and transitions them back to IMPLEMENT_COMPLETE); guard keyword maintenance triage against CI-source and empty-draft tickets to prevent misroute to the read-only maintenance agent.
- **config(sync):** update stale YAML→JSON references in docstrings and prompts across `settings.py`, `_settings_core.py`, `tests/conftest.py`, `tests/config/test_config.py`, `config_syncing.py`, and `repos.py`. Regenerated `config/config.schema.json` to reflect updated Settings class docstring.
- Address review feedback: update stale YAML/docstring references to JSON throughout config pipeline
- Consumer migration: `robotsix-yaml-config` → `robotsix-config`. Swapped dependency in `pyproject.toml`, replaced YAML config loading with JSON (`config/config.json`), removed `_YAML_PATH_TO_ALIAS` translation layer (100+ entries), created `JsonSettingsSource`, rewrote `check_config_sync.py` for JSON validation, and generated `config/config.example.json` (282 settings + 14 secrets). 6441 tests pass.
- Fix stale field comment in `_settings_core.py` referencing the deleted `_JSON_PATH_TO_ALIAS`
- Remove PyPI publish pipeline: drop `semantic-release` and `pypi-publish` jobs from `release.yml`, remove `python-semantic-release` dev dependency and `[tool.semantic_release]` config, delete `docs/publishing.md`, and update CI docs. Docker image publishing to GHCR is unaffected.
- Allow `mark-done` from `BLOCKED` state: the transition succeeds with a `[force-closed from blocked]` marker prepended to the note, letting operators clean up stale blocked tickets (e.g. trackers whose PR is already merged). Terminal states and `EPIC_OPEN` remain rejected.
- Add a Renovate/Dependabot auto-merge caller that delegates to the shared reusable workflow, which gates on both `dependabot[bot]` and `renovate[bot]`.
- Added per-file justification boilerplate templates and a structured justification format to the scope-triage agent's system prompt, standardizing how the agent documents EXPAND verdicts for common out-of-scope file patterns (CHANGELOG.md, pyproject.toml/uv.lock, tests/conftest.py, __init__.py). (mill: Boilerplate: scope-triage EXPAND — justifying additional files in implement (20260702T214750Z-boilerplate-scope-triage-expand-justifyi-b3cd))
- Migrated mill operator config from YAML-cascade to flat alias-keyed JSON (26 files). Removed `robotsix_yaml_config` dependency: `loader.py` uses stdlib `json`, `JsonSettingsSource` replaces `YamlSettingsSource`, `_YAML_PATH_TO_ALIAS` (~140 entries) and `flatten_yaml_config()` deleted. `config/config.example.json` committed (282 settings, 14 secrets, 0 repos); `config.example.yaml` removed. `pyproject.toml` + `uv.lock` purged of `robotsix-yaml-config`; `pyyaml` retained for overlay YAML. CI module-registration check uses `yaml.safe_load` directly. All tests (6442 passed), ruff, mypy, deptry, `check_config_sync`, and `emit_config_schema --check` pass.
- Audit agent now reads the repo's `AGENT.md` first (and the `robotsix-standards` it links) as the audit baseline, flagging concrete deviations from standards a repo declares it follows — while not manufacturing gaps for repos that opt out.
- Enable `triage_boilerplate` periodic workflow for mill's own board via `.robotsix-mill/periodic/triage_boilerplate.yaml` presence file.
- Add explicit path-traversal guard in ``Workspace.__init__`` to silence a CodeQL ``py/path-injection`` false positive. ``ticket_id`` is already sanitized by ``_slug()`` at creation; the guard is a defense-in-depth annotation.
- Fix survey agent prompt: replace references to non-existent `web_search`/`web_fetch` tools with correct `ask_web_knowledge` gateway, matching the agent's actual tool set.
- `parallel_explore` now pre-filters questions via `git grep` before
  spawning the scout sub-agent, avoiding the ~5k-token system prompt
  when a simple grep can answer. Batch size is capped at 5 questions
  per call to bound worst-case input context.
- New `POST /repos` endpoint for runtime repo registration: writes to
  `registered_repos.yaml` overlay, hot-reloads the in-process registry
  without restart, and is idempotent (operator-configured repos are never
  modified).
- Refine stage: detect implementation-ready specs (drafts with file paths
  paired with fenced code blocks) and run a cheap deterministic
  validation pass (file existence, YAML/Python syntax, forbidden
  patterns) instead of the expensive LLM refine agent. Gated behind
  ``gates.refine_skip_llm_on_impl_ready_spec`` (default true).
- New `POST /tickets/ingest` endpoint for machine callers with creation-time dedup via the existing `run_dedup_check` agent, with fail-open semantics (LLM failure → create ticket anyway). Returns 200 + `deduped=true` when the report matches an existing ticket, 201 + `deduped=false` for new tickets, and 404 for unknown `repo_id`.
- Short-circuit refine triage when a prior triage SKIP verdict
  already exists in the ticket history, skipping the LLM call
  and re-emitting the draft unchanged as the refine output.
- Add robotsix stack standards link to README.md and AGENT.md.
- Add spec-exact-code bypass in implement stage: tickets whose description
  contains fenced code blocks annotated with file paths are applied
  deterministically, bypassing the LLM coordinator entirely. Saves ~$0.03
  and ~30min per completeness_check-originated ticket.
- Refine agent: add prompt rule distinguishing checkout-local paths from spec-described paths to prevent false misrouting when a spec describes an external system's layout (e.g. `config/config.yaml` inside a container image)
- Add actual `repos: {}` key (empty mapping) to `config/config.example.yaml` and fix stale fallback comment — repos are now read exclusively from this key plus the machine overlay `registered_repos.yaml`.
- Refine agent mill-misroute gate: classify spec-mentioned paths as
  source-tree vs conceptual before deciding routing. Add confidence
  threshold requiring ≥2 absent source-tree paths when the repo
  clearly exists. Exclude common conceptual patterns
  (``config/config.yaml``, container paths, template files, compose
  files) from triggering mill redirects.
- Consolidate GitHub 401 retry boilerplate: add `invalidate_and_backoff()` to `forge/auth.py`, replace ~14 duplicated `invalidate_github_token()` + `time.sleep(2)` sites across `github.py`, `github_ci.py`, and `github_pr.py`, and delete the standalone `_retry_after_401()` method.
- Extend `env_doc_sync` periodic agent to also create/maintain `.env.example` from canonical Pydantic settings + secrets, and cross-reference runtime-affecting `pyproject.toml` sections against `docs/configuration.md`.
- Add missing `SourceKind.LANGFUSE_CLEANUP` enum member, `SOURCE_CLASS` JS map entry, and `.src-langfuse-cleanup` CSS rule for board card styling of langfuse-cleanup runs.
- Add `robotsix-mill meta` CLI command for running the meta pass (extraction + alignment across all repos)
- Document `periodic.triage_boilerplate` in `docs/configuration.md` Section 12 (Periodic agents).
- `robotsix-mill repos list` now shows a ``SOURCE`` column (``config`` or ``auto``) so operators can distinguish hand-configured repos from auto-registered overlay entries.
- Add missing `track_foreign_prs` row to the orphaned_pr_check table in `docs/configuration.md`.
- Document `core.limits.refine_dynamic_limit_*` and `core.limits.refine_usage_warning_threshold` fields in the Request limits section of `docs/configuration.md`
- Remove the deprecated standalone `config/repos.yaml` fallback: repos are now read exclusively from the `repos:` key of `config/config.yaml` (the `MILL_REPOS_FILE` override used by the test suite is unchanged). Operators still using the standalone file must move its `repos:` block into `config/config.yaml`.
- Machine-owned repos overlay: auto-registrations (repo-scaffold and workspace member-sync) now write to ``<service.data_dir>/registered_repos.yaml`` instead of ``config/repos.yaml``. The overlay is merged with the operator ``repos:`` block at load time; operator entries win on repo-id conflict. ``RepoConfig.source`` discriminates ``"config"`` (operator) from ``"auto"`` (overlay) entries. ``MILL_REPOS_FILE`` overrides both read and write; the legacy ``config/repos.yaml`` fallback (when neither ``repos:`` nor overlay exist) is unchanged.
- Fix Docker build failure: add missing `COPY skills/ ./skills/` in builder stage so hatchling's `force-include` for `skills/` resolves at wheel-build time.
- Ship `skills/` directory in the production wheel via `[tool.hatch.build.targets.wheel.force-include]` and add `skills_dir()` to `_resources.py`, so `Settings.skills_dir` resolves correctly in both editable and installed modes.
- Fix duplicate foreign-PR tracking tickets: include terminal-state (DONE/CLOSED) tickets in the foreign-PR dedup set so a resolved tracking ticket suppresses re-creation on subsequent passes.
- Correct `docs/reusable-workflow-callers.md`: the shared `python-ci.yml` and `python-docs.yml` workflows live in `damien-robotsix/robotsix-github-workflows`, not in `robotsix-mill`. Updated all references, the wrong-org callout, and the local-form note accordingly.
- Pass `usage_limits=UsageLimits(request_limit=100)` to `Agent.run_sync()` in the retrospect and review-revision agents to prevent `UsageLimitExceeded` errors on complex tasks.
- Attempt to forward `max_tokens` to the Claude SDK agent build path, falling back gracefully with a warning when the provider doesn't accept it. This caps L3 (Claude) agent output at the per-agent `max_tokens` setting (e.g. 8192 for refine) instead of producing unbounded output.
- Lower `refine_findings_downgrade_min_chars` from 200 to 150 to capture borderline traces where triage findings are terse but sufficient for sonnet-level refinement (~$0.03 vs ~$0.90 Opus call).
- Remove dead backward-compat function ``build_resume_message_history`` and its re-exports; all callers now use ``build_compact_resume_message_history`` instead.
- Fix Docker build: copy `agent_definitions/` and `expert_definitions/` into the builder stage so hatch can find them for `force-include`.
- Fixed `is` → `==` comparison against `SourceKind.ORPHANED_PR_CHECK` in `_check_pr_baseline` (ci_poll.py). `Ticket.source` is a plain `str` from the DB, so identity comparison always evaluated to `False`, making the tracker-ticket fallback dead code.
- Agent tools (`read_file`, `list_dir`) now gracefully handle absolute paths (e.g. container paths like `/workspace/...`) by falling back to the repo-relative form when the tail exists inside the checkout, preventing wasteful "escapes the repository" retry loops.
- Update ``robotsix_llmio.core.sqlite_utils`` in the installed venv package to accept ``list[tuple[str, str]]`` (matching mill call-sites), support SQLAlchemy 2.0 ``Connection`` via ``exec_driver_sql`` fallback, and return ``list[bool]``. The upstream changes are staged in ``_llmio_check/`` for manual push; the vendored ``sqlite_utils.py`` docstring now documents the migration path.
- Guard the implement stage against epic tickets: the preflight gate now
  blocks epic tickets (``TicketKind.EPIC``) with a BLOCKED outcome before
  any Langfuse trace opens, and the implement agent system prompt warns
  against implementing epics directly.
- Deduplicate `read_counter`/`write_counter` helpers from three stage files into `core/workspace.py` as public API
- Implement stage: add stuck-loop detection to abort early when the agent makes no progress across consecutive passes. Two independent heuristics: (a) after 3 consecutive passes with no file edits the loop BLOCKs as "stuck", and (b) after 50 cumulative tool calls across passes without a git diff the loop BLOCKs. Within-pass detection flags a same-tool repeat (e.g. calling ``read_ticket`` 5+ times consecutively without edits) as a stuck signal.
- Add retry-with-backoff wrapper around the explore sub-agent: when a
  transient API connection error exhausts the built-in retries, the
  explore call is retried up to 3 times with progressive question
  simplification and exponential backoff.  Budget-cap errors
  (UsageLimitExceeded) still trigger the existing no-tools fallback
  immediately without looping.
- Add ``coordinator_timeout_seconds`` setting (default 600 s, env var
  ``MILL_COORDINATOR_TIMEOUT_SECONDS``) that caps a single implement
  agent pass; the stage reclaims control when the wall-clock budget is
  exceeded.
- Bump `actions/checkout` from v4 (SHA `34e1148`) to v6.0.3 (SHA `df4cb1c`) across all 6 workflow files.
- Add `_is_rename_only_change` pre-implement check alongside the existing
  `_is_config_only_change` that detects purely mechanical file-rename
  tickets (git renames + config/doc stubs with zero behavioural delta).
  When detected, `_select_agent_level` returns sentinel `0`, which
  `_run_single_implement_pass` routes to a new deterministic
  `_handle_rename_only_change` handler that finalizes and proceeds
  directly to CODE_REVIEW — bypassing the LLM coordinator entirely.
  Extended `_should_skip_test_gate` to also skip the full test suite
  for rename-only diffs. Added 7 unit tests for the rename-only
  detection and fixed 2 existing tests whose `subprocess.run` stubs
  were caught by the new rename check. (mill: Add pre-implement check to bypass LLM coordinator for purely rename-only tickets (20260701T141629Z-add-pre-implement-check-to-bypass-llm-co-328a))
- Bump anchore/sbom-action from v0.21.0 to v0.24.0 in `docker-publish.yml` (PR #617).
- Dockerfile: drop redundant system `claude` CLI (`npm install -g @anthropic-ai/claude-code`); the `claude-agent-sdk` Python dep ships its own bundled binary (`_bundled/claude`) and prefers it. Also document the `~/.claude` rw mount in `docker-compose.override.example.yml`.
- `_verify_merge_ancestor`: add content-level fallback when `git merge-base --is-ancestor` fails — diffs the feature commit against the target branch and checks whether changed files on the target branch contain the ticket ID. Catches squash and rebase merges where the log message does not mention the ticket.
- Fix two dead references to `runners.security_posture_runner` (consolidated into `runners.periodic_runner`), preventing runtime import failures in the CLI and HTTP pass routes.
- Extend `_triage_outcome` with optional `state` parameter, refactor 8 inline triage sites to use the shared helper
- Fix `board_manager.max_concurrent` documented default in `docs/configuration.md` from `3` to `1`, matching the Pydantic model default.
- Extract `_retry_after_401()` helper in `GitHubForgePRMixin`, centralizing
  the 401 token-invalidation + backoff boilerplate that was duplicated across
  `_create_pr`, `_get_pr`, `_list_branches`, `_list_open_pr_branches`,
  `_list_open_prs`, and `_pr_review_status`.
- Split `_tickets.py` (1169 lines, 25 routes) into three sibling modules:
  `_tickets.py` (core CRUD), `_tickets_merge.py` (merge/CI routes), and
  `_tickets_transitions.py` (state transitions & enrichment). Follows the
  existing route-module pattern and keeps merge-specific imports out of
  the CRUD module.
- Add CLI subcommand `roadmap-sync` with `--json` and `--repo-id` flags, matching the existing POST route at `/roadmap-sync`.
- Wire `triage_boilerplate` agent into all five on-demand dispatch layers (CLI ``_RUNNERS`` entry, POST route, board button, JS handler + window export, ``AGENT_COLORS`` / ``SOURCE_CLASS`` / CSS badge) matching all 13 other ``llm_agent`` peers.
- Split ``forge/gitlab.py`` (monolithic 1131-line ``GitLabForge``) into a ``forge/gitlab/`` package mirroring the GitHub adapter architecture:
  - ``core.py`` — MR lifecycle, branches, repo CRUD
  - ``ci.py`` — ``GitLabForgeCIMixin`` with pipeline status, job log retrieval
  - ``code_scanning.py`` — ``GitLabForgeCodeScanningMixin`` (placeholder)
  - ``dependabot.py`` — ``GitLabForgeDependabotMixin`` (placeholder)
  - ``_pagination.py`` — shared ``_paginated_get`` helper
  - ``__init__.py`` — public re-exports preserving the existing import API
- Align `web_knowledge_request_limit` in `config/config.example.yaml` (was 8) with the model default of 12.
- Add test coverage for refine stage submodules: `_reconcile.py`, `_result_paths.py`, and `_triage.py` (146 tests across three new test files)
- Add unit test coverage for ``MultiRepoCiFixMixin`` (``tests/stages/merge/test_ci_fix_mixin.py``) covering all three private methods with 29 test cases.
- Add `.robotsix-mill/periodic/security_posture.yaml` trigger file to enable the security_posture periodic workflow in mill.
- Broadened mandatory `read_file` verification in trace inspector from optimization-only to ALL finding categories when `root_cause` or `proposed_solution` makes a mechanistic claim about code behaviour.
- web_knowledge: add `last_verified` frontmatter field (bumped on every `update_library`), `stale` boolean field, and cache TTL (`web_knowledge_cache_ttl_hours`, default 72 h). Entries older than the TTL are flagged `[STALE]` in the index so the web-knowledge agent re-verifies before trusting cached claims.
- Add stale re-spawn guard to implement stage preflight: when the last
  implement attempt was blocked and the spec hasn't changed, fail fast
  before opening a Langfuse trace — preventing $0.00 no-op traces and
  redundant paid re-spawns.  The convergence backstop (empty diff after
  review) now also writes ``implement.md`` so the guard can prevent the
  same no-op on the next attempt.
- Fix `config.example.yaml` and `web_knowledge.py` tool-description strings to reflect the new `web_knowledge_request_limit` default of 12 (follow-up to the bump from 8→12).
- Increase `web_knowledge_request_limit` default from 8 to 12 to prevent
  budget-exhaustion errors during multi-step web-knowledge consultations.
- Enable the `security_posture` periodic agent for mill by adding the per-repo
  opt-in trigger at `.robotsix-mill/periodic/security_posture.yaml`.
- Added `source_url` and `verified_at` tracking to the web-knowledge agent's
  library cache. The `update_library` tool now accepts an optional
  `source_url` parameter; when provided, a `verified_at` timestamp is
  recorded so `read_library` can distinguish file-touch from
  fact-verification. Updated `_parse_frontmatter` to return a
  `_KnowledgeMeta` dataclass with all frontmatter fields. Updated the
  system prompt to instruct the agent to treat unverified claims as suspect
  regardless of `last_updated` recency — preventing stale cached facts from
  cascading into 404s.
- Add `Stage.preflight()` — a lightweight pre-trace gate that lets stages signal early-exit before a Langfuse trace is opened. The implement stage uses it to catch empty specs and exceeded cycle limits without consuming a spawn slot or emitting a $0.00 trace.
- Remove five dead backward-compat re-exports from `orchestration.py`: `_persist_triage_complexity`, `_MIGRATE_NOTE_PREFIX`, `_anti_bounce_escalate`, `_parse_prior_boards`, `_is_sendback_reentry` (zero imports across the codebase).
- Add implement-stage precondition checks: spec emptiness gate and per-ticket spawn counter (`implement_max_spawns_per_ticket`, default 3) to fail fast on known no-op conditions instead of burning paid LLM invocations on empty-spec or runaway re-spawn loops.
- Remove stale `.robotsix-board-agent-src` git submodule; the board-agent package is already managed by uv in `pyproject.toml` and the submodule reference pointed to a commit no longer reachable on the remote, breaking Dependabot's configured graph update
- **cleanup**: remove unused ``SecurityPosturePassResult`` backward-compat
  alias from ``periodic_runner.py`` and its vulture whitelist entry.
- ``trace_observation_summary()`` now falls back to trace-level
  ``metadata.mill.step_usage`` when the Langfuse list endpoint returns
  zero observations, so cost-attribution works even without fetching
  per-trace detail (the list endpoint does not include the observation
  tree).
- Add ``backend`` billing tag (``"claude_sdk"`` vs ``"openrouter"``) to
  per-step usage data emitted by ``record_step_usage()``, and surface it
  in ``trace_observation_summary()`` so the cost-analyst can distinguish
  subscription (estimate-only) cost from real marginal cost.
- Fix `trace_observation_summary()` to read per-step usage data from
  `langfuse.observation.metadata.mill.step_usage` span attributes when
  GENERATION observations are absent. The `record_step_usage()` write
  path now uses the `langfuse.observation.metadata.` prefix so the
  attribute is visible through Langfuse's REST API (previously it was
  stored as a raw span attribute inaccessible to the read path).
- **tests**: reorganize runtime test files into per-subpackage subdirectories
  (``tests/runtime/routes/``, ``tests/runtime/worker/``) mirroring the source
  layout.  Harness ``.mjs`` paths updated accordingly.
- **agents**: remove dead backward-compat alias ``TriageBoilerplateResult``
  (no callers in ``src/`` or ``tests/``); update YAML definition to reference
  ``PeriodicAgentResult`` directly. (mill: Remove dead backward-compat alias `TriageBoilerplateResult` from triage_boilerplate.py (20260630T211442Z-remove-dead-backward-compat-alias-triage-3eb1))
- **dev**: add ``make format`` and ``make lint`` targets for quick local
  ruff/mypy checks across all Python sources (``src/``, ``tests/``,
  ``scripts/``, ``vulture_whitelist.py``, ``deploy/split_config.py``,
  ``dev/``).  ``lint`` is check-only; ``format`` auto-fixes.
- **deps**: update robotsix-board-agent pin to commit adding
  ``handle_wrapper`` param to ``BoardManager.__init__`` (Step 0 of
  board-manager fast-lane epic).  Push of the upstream commit requires
  human GitHub credentials; ``uv lock`` must be re-run afterwards.
- **stages**: reorganize test layout to mirror source subdirectory structure.
  Move refine tests to ``tests/stages/refine/`` and merge tests to
  ``tests/stages/merge/``, matching the existing ``tests/stages/implement/``
  pattern.  Add ``__init__.py`` files to new test subdirectories and update
  ``docs/modules.yaml`` paths accordingly.

- **git**: fix ``git_push_with_lease`` for first-push branches (no remote
  counterpart yet).  The pre-push fetch now tolerates "couldn't find remote
  ref" and falls through to ``push_with_lease`` which already handles
  first-push via ``--force``.  Genuine network/auth fetch errors still
  surface as ``PUSH_ERROR``.
- **scripts**: fix misaligned trace-ID↔latency pairs in
  ``_extract_board_traces()`` per-trace table by sorting
  ``board_traces`` and ``latencies_s`` in lockstep instead of sorting
