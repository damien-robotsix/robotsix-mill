## 0.0.0 (unreleased)

- Extract ``_paginated_get`` helper to ``forge/_github_pagination.py``, fixing a
  data-loss bug where 6 GitHub API methods silently returned at most 100 items
  (branches, PRs, reviews, comments, files, labels).  The new helper integrates
  with the existing 401-retry pattern and is reused by all 9 paginated methods.
- GET /tickets: add `offset`, `limit`, `sort_by`, and `created_after` query params for pagination, sorting, and time-based filtering. Defaults preserve backward-compatible behavior.
- Sandbox spawn: retry on transient container-wait EOF. When `docker run` exits 125 with "unexpected EOF" in stderr (the socket-proxy haproxy severing a long-lived wait stream), the sandbox now cleans up any leaked container and retries up to 3 times. Non-EOF 125 errors still raise immediately (genuine daemon/config errors). This is defense-in-depth on top of the deploy-compose haproxy timeout fix.
- deploy compose: raise the socket-proxy's hardcoded haproxy `timeout client/server` from 10m to 4h. The 10m default severed the docker-wait stream of any sandbox run longer than 10 minutes ("error waiting for container: unexpected EOF"), the root cause of the 2026-07-10..12 intermittent sandbox outages. The tecnativa image exposes no TIMEOUT_* env knobs, so the processed config is patched via sed like the existing docker-events fix.
- Clear implement fingerprint guard on transient failures: when a transient infrastructure error kills an implement run, `_handle_stage_error` now deletes `artifacts/implement.md` before scheduling the retry, preventing permanent "spec unchanged since last implement attempt" blocks.
- Chat skill (`/chat-skill`): replace absolute "No deletion" rule with confirmation-gated DELETE support, including a historical rationale note preferring `closed` when a legal edge exists and reserving deletion for fingerprint-guarded blocked tickets or operator-requested removals.
- Add self-documenting `help` target to Makefile (`make` or `make help` lists all targets with descriptions via `##` comments)
- Update stale `forge/gitlab.py` references to `forge/gitlab/core.py` in agent prompts, docs, config, and remove dead flake8 ignore entry; regenerate `mypy-baseline-test.txt`.)
- Review stage now short-circuits rename-only PRs to the cheap level-1 model,
  matching the existing config-only shortcut and saving ~$0.02–0.04 per review.
- Add exhaustive security-sink scan step to implement agent system prompt: before pushing, the agent must identify every location in the diff where user-controlled data reaches a CodeQL-sensitive sink (URL, filesystem path, log call, etc.) and apply the same sanitization pattern consistently across all sinks, preferring structurally sound approaches (`httpx.URL.copy_with()`, `pathlib.Path.resolve()`, structured logging) over reactive string guards.
- Extract `_resolve_repo_config` helper in `cli/__init__.py`, replacing
  3 duplicate repo-resolution blocks in `_run_and_print` (member-sync,
  trace-review, roadmap-sync). (mill: cli: add `_resolve_repo_config` helper to eliminate 3 duplicate repo-resolution blocks in `_run_and_print` (20260712T234135Z-cli-add-resolve-repo-config-helper-to-el-01f7))
- Update stale `pip-audit` references to `uv audit --frozen` in CONTRIBUTING.md (security-audit table row, dependency-review note, license-gate note).
- Upgrade transitive dependency `click` from 8.1.8 to 8.4.2 to resolve PYSEC-2026-2132 advisory
- Refine stage: add deterministic pre-check for documentation-only drafts. When every file path in the draft is under ``docs/`` or has a ``.md`` extension (and no code files are touched), the triage+refine LLM calls are skipped entirely — the ticket routes directly to implement with a "Documentation-only change" verdict. Saves ~$0.0025 and ~40s latency per doc-only ticket.
- **Refine agent** now receives robotsix-standards content as read-only context during spec drafting
  * Pre-fetch of key standards pages (repo-baseline, README) with 72h file-based cache
  * Injected before the title/draft sections in the user prompt
  * The refine agent runs an internal conformance check before finalization and surfaces
    detected violations as questions/flags in the spec
  * Degrades gracefully — when the fetch fails, the spec is marked "standards context unavailable"
  * Acceptance criterion: a commitizen/scm-release spec is flagged as conflicting with the
    documented towncrier + shared auto-release pattern
- Consolidate duplicate `_parse_iso_utc` into `forge/base.py`; remove the copy from `forge/github_pr.py` and the original from `forge/github.py` (both now import from `base`).
- Merged hooks module into stages: moved ``run_prepare_hook`` to
  ``src/robotsix_mill/stages/hooks.py``, moved tests to
  ``tests/stages/test_hooks.py``, updated all import sites, and
  removed the standalone hooks module from ``docs/modules.yaml``.
- Refine stage: add doc-only gate that skips the multi-LLM refine
  analysis when a draft touches only documentation files
  (`docs/**`, `*.md`, `CHANGELOG.md`) and no code/config files
  (`.py`, `.ts`, `.js`, `.yaml`, `.yml`).  Doc-only tickets are
  auto-approved deterministically with a templated verdict
  ("Documentation-only change; no code review needed").
- Fix stale ``forge/gitlab.py`` path references in the forge_parity periodic agent prompt; now points to ``forge/gitlab/core.py`` after the monolithic adapter was split into a package.
- Skip ``TestEditsFormatterReverted`` tests when ``ruff`` is not installed (base/production container)
- Remove dead backward-compat aliases `load_yaml_config` and `load_secrets_yaml` from `config/loader.py` (no callers remain). (mill: Add pagination (offset/limit) + sort/filter to GET /tickets and widen chat truncation (20260712T120553Z-add-pagination-offset-limit-sort-filter-01fd) [WIP])
- Added `docs/repo-scaffold/index.md` documenting the repo creation workflow and workspace member sync, and registered the docs path in `docs/modules.yaml`.
- Remove stale `reply_to_thread`/`close_thread` error-recovery guidance from `retrospect.yaml` system prompt (both tools are disabled for this agent).
- Reorganize stage documentation into `docs/stages/`: move `approval-gate.md`,
  `merge-stage.md`, `scope-triage.md`, `retrospect-memory.md`,
  `blocked-ticket-recovery.md`, and `reference/stages.md` into the new directory,
  update `docs/modules.yaml` stages module paths, and fix all cross-references
  in `README.md`, `ARCHITECTURE.md`, `docs/agents/index.md`, `docs/cli/usage.md`,
  `docs/vcs/README.md`, and `mkdocs.yml`.
- Add `GitHubForgeSecurityMixin` with `enable_vulnerability_alerts()`, `enable_automated_security_fixes()`, and `ensure_dependency_graph_enabled()` methods so the maintenance agent can programmatically enable Dependabot alerts, automated security fixes, and the dependency graph on GitHub repos.
- Add `UV_MALWARE_CHECK=1` to all CI workflows that run `uv` commands (ci.yml, security-audit.yml, release.yml, dependency-review.yml), enabling uv's install-time malicious-package scanning as a complementary layer to `uv audit`.
- Consolidate `autoupdate` module into `dev-tooling`: move source to
  `src/robotsix_mill/dev_tooling/autoupdate/`, tests to
  `tests/dev-tooling/autoupdate/`, and docs to
  `docs/dev-tooling/autoupdate/`.  Update console_scripts entry point
  and remove the standalone `autoupdate` module entry from
  `docs/modules.yaml`.
- Update CI overview table in `CONTRIBUTING.md`: remove stale `docker-publish.yml` references, correct `ci.yml` row to describe actual steps, and update Trivy section to reference the shared reusable `docker-release.yml` workflow.
- Security audit: replace `pip-audit` with `uv audit --frozen` for dependency CVE scanning (4–10× faster, no separate install step). SBOM generation now uses `uv audit --output-format json`.
- Register `robotsix-chat-mobile` as a tracked repo/board in the committed config example (`config/config.example.json`), with `board_id: robotsix-chat-mobile` and `forge_remote_url: https://github.com/damien-robotsix/robotsix-chat-mobile`.
- Create `docs/cli/usage.md` with a comprehensive CLI command reference.
  Add `docs/cli/**/*` to the `cli` module's paths in `docs/modules.yaml`
  and add cross-references from `approval-gate.md`,
  `blocked-ticket-recovery.md`, and `configuration.md`.
- Fix two function-signature inaccuracies in `docs/vcs/README.md`: `fetch()` now shows the keyword-only `*` delimiter, and `branch_has_net_diff()` now shows the correct parameter names and description.
- Add `docs/autoupdate/index.md` documenting the autoupdate CLI, flock-based
  locking, git fetch/merge/restart lifecycle, deployed-SHA recording, and
  the `dev/mill-autoupdate.sh` wrapper.
- Reorganize runtime documentation into a per-module `docs/runtime/` directory:
  moved board operations and API reference, added worker/routes/tracing/run-registry
  docs, and updated cross-references.
- docs(vcs): fix material inaccuracies in `docs/vcs/README.md` — corrected `post_push_check` signature (added missing `target` param) and return values (`PASS`/`NOT_LANDED`/`FOREIGN_DIVERGENCE`/`UNAVAILABLE`), removed phantom `force=False` from `push_with_lease`, added note about `post_push_check` callers (merge/review-revision stages); also fixed `reconcile_with_remote_pr` param order, `diff_base`, `try_rebase_onto`, `ls_remote_sha` signatures and `branch_ancestry` return dict keys
- Add `docs/vcs/README.md` documenting the vcs module's git CLI wrappers (clone, branch, commit, push, inspection, recovery). Registers `docs/vcs/**/*` in `docs/modules.yaml` under the vcs module.
- Reorganize forge module documentation: create `docs/forge/` directory, move `docs/design/forge-architecture.md` and `docs/github-app.md` into it, and add focused docs for authentication, GitLab backend, CI monitoring, and code scanning.
- Remove duplicate `CHANGELOG.md` entry from `docs/modules.yaml` `project-root` module paths.
- **feat:** `POST /tickets/{id}/resume-blocked` accepts an optional `{"note": "..."}` body. The note is recorded as an `operator`-authored comment and, when resuming a BLOCKED ticket back into READY, clears a stale `artifacts/implement.md` so an explicit operator justification lets the retry proceed instead of immediately re-blocking on the implement stage's unchanged-spec guard. `robotsix-mill ticket resume-blocked <id>` gained a matching `--note` flag.
- **fix:** config/repos.example.yaml — remove misleading per-repo `langfuse:` blocks (per-repo Langfuse config is silently ignored by the loader; Langfuse is configured globally only). Add explanatory comment near the `repos:` section header.
- Move `docs/expert-yaml-schema.md` → `docs/agent-definitions/expert-yaml-schema.md`, updating all cross-references in README.md, docs/index.md, docs/agents/index.md, and mkdocs.yml; add `docs/agent-definitions/**/*` to the agent-definitions module in docs/modules.yaml.
- Move dev-tooling docs (`ci-policy.md`, `deployment.md`, `reusable-workflow-callers.md`) from `docs/` to `docs/dev-tooling/`; update `mkdocs.yml` nav, cross-references, and module paths. (mill: Reorganize module dev-tooling: align to per-module layout (src/docs/tests) (20260705T234900Z-reorganize-module-dev-tooling-align-to-p-0030))
- Move `docs/security.md` → `docs/sandbox/security.md` and update all cross-references (README, ARCHITECTURE, mkdocs.yml). Add `docs/sandbox/**/*` to sandbox module paths in `docs/modules.yaml`.
- Move notifications documentation from `docs/notifications.md` to `docs/notify/notifications.md`, aligning with the per-module doc layout.
- Move `docs/meta-board.md` → `docs/meta/meta-board.md`; update cross-references in `AGENT.md`, `mkdocs.yml`, and meta module paths in `docs/modules.yaml`.
- Move core module docs into per-module directory: `docs/dedup-guard.md`, `docs/ticket-provenance.md`, `docs/workspace-cleanup.md`, and `docs/screenshots.md` → `docs/core/`. Updated `mkdocs.yml` nav, `docs/modules.yaml` core module paths, and `README.md` cross-references.
- Move runner documentation into per-module directory: `docs/orphaned-pr-check.md` → `docs/runners/orphaned-pr-check.md`, `docs/pin-bump.md` → `docs/runners/pin-bump.md` (aligns with source layout under `src/robotsix_mill/runners/`)
- Moved langfuse docs into `docs/langfuse/`: `observability.md` and `trace-health.md` now live under the per-module directory)
- Add unit tests for `ReviewRevisionMixin` covering both `_run_review_revision` (missing clone, artifact I/O, reconcile outcomes, retry counters, agent success/failure paths) and `_review_changes_requested_outcome` (feature flag, transient errors, empty comment/body synthesis, artifact persistence).
- Add unit tests for `towncrier.py` fragment generation (12 tests covering TOML parsing, file I/O, dedup, error handling, and edge cases).
- skip-changelog (test-only addition)
- Remove misleading `langfuse_from` comment from `config/repos.example.yaml`. The
  key has no code support in `RepoConfig` or any loader; operators who copied
  it into their config were setting a silently-ignored key.
- Sandbox (deploy mode): re-establish the internal egress network and the
  `sandbox-proxy` attachment before **every** sandbox spawn instead of once
  per process. A deploy can recreate the `sandbox-proxy` sibling at any
  moment, detaching it from `mill-sandbox-net`; the old once-guard then left
  every subsequent sandbox without egress (pip installs failed, all test
  suites died with `pytest: command not found`) until the mill itself
  restarted — 2026-07-05 incident, 169 tickets blocked. The attach is
  idempotent and costs two fast docker CLI calls per spawn.
- Reorganized agent documentation under `docs/agents/`: moved 7 files (`agents.md`, `reference/agents.md`, `agent-communication-research.md`, `agent-md-candidates.md`, `agent-yaml-schema.md`, `audit-agent.md`, `diagnostic-agent.md`) into the new `docs/agents/` subdirectory; updated `mkdocs.yml` nav, `docs/modules.yaml`, and all cross-references.
- Move `docs/dependencies.md` → `docs/deps/dependencies.md`, add `docs/deps/**/*` to the deps module in `docs/modules.yaml`, and create a "Deps" nav section in `mkdocs.yml`.
- Add test coverage for `ProblemDetail` (RFC 9457 error envelope) in `tests/runtime/test_errors.py`
- Migration from `robotsix-yaml-config` → `robotsix-config`: dependency swapped, config layer rewritten to use stdlib `json` + pyyaml for overlay YAML, `JsonSettingsSource` replaces `YamlSettingsSource`, `config/config.example.json` committed (was `.yaml`), schema regeneration updated.
- Diagnostic investigation: traced "interrupted by process restart" errors across 19 agent/board pairs to `RunRegistry` orphan reconciliation — identified OOM kills under combined mill + sandbox memory pressure as the most likely root cause, with deployment rollouts as a secondary contributor.
- Add `agent_references/betterleaks.md` with Betterleaks configuration reference (repo URL, hook id, version v1.6.0, baseline mode, config precedence, `.betterleaks.toml` format) to eliminate web research on future Betterleaks migrations.
- Implement agent pre-flight checks are now scope-aware: when the diff
  touches no ``.py`` files (doc-only, changelog-only, config-only PRs),
  ruff, mypy, and deptry are skipped, saving ~60-80s of wall-clock time
  per ticket.
- Add prompt rule to implement agent: do not `explore` to confirm an empty `list_threads` result — proceed with the task and note the inability to reply in structured output.
- Added guardrail in implement agent's Tool-use discipline: do not re-read files that an `explore` sub-agent already quoted in its summary. This reduces redundant `read_file` calls and saves token budget.
- Deps: add `robotsix-config` (pinned commit `d29de204`) as a dependency to unblock the config-standard migration (`33bf`/`da3e`) prerequisite gate; deptry-ignored until imported.
- Pipeline: a ticket already satisfied on main (empty diff vs base — clean tree, no commits beyond base, no surviving edits, tests green) now terminates DONE instead of looping empty PRs in `blocked`. Real-diff failures still block. Added an operator escape hatch: `mark-done` now works from `blocked`/`rebasing`.
- Fix `mill-socket-proxy` crash-loop: add `tmpfs: /run` for haproxy pidfile and patch the `docker-events` backend with required timeouts (`timeout connect`, `timeout http-request`, `timeout http-keep-alive`) for haproxy 3.x compatibility.
- Internal: verified trace-review classifier does not produce false-positive `tool_errors` from `validate_artifact` spans (no code change needed)
- Gate `POST /repos` behind a new `allow_runtime_repo_registration` setting (default `false`). When off, only operator-configured repos (in `config/config.json`) accept tickets; auto-registered repos are rejected by `POST /tickets`, `POST /tickets/ingest`, and `POST /repos`. Added `DELETE /repos/{id}` to deregister runtime-added repos.
- Enable the diagnostic periodic workflow: create the per-repo presence file
  (`.robotsix-mill/periodic/diagnostic.yaml`) and flip `enabled: true` in the
  agent definition. The check registry starts empty; individual checks are
  registered in follow-up tickets.
- Enable `member_sync` periodic workflow for the mill repo itself via `.robotsix-mill/periodic/member_sync.yaml` presence file.
- Serve ``GET /chat-skill`` endpoint returning a SKILL.md document that teaches the chat agent how to drive the board API (read tickets, post comments, manage state transitions, create via ingest). Includes mandatory safety rules requiring user confirmation for state-changing operations.
- Deploy: set `MILL_API_HOST=0.0.0.0` in `deploy/docker-compose.yml` so the container binds all interfaces (reachable by the central-deploy gateway) regardless of the onboard-written `api_host`.
- Adopt Alembic for SQLite schema migrations, replacing the hand-rolled
  additive-migration system in `db.py`.  An initial migration captures all
  existing tables (ticket, ticketevent, comment, memory) and columns.
  `init_db()` now runs Alembic `upgrade head` on tracked databases and
  stamps fresh/pre-Alembic databases as `head` after a one-time legacy
  migration pass.  `make migrate` and `scripts/migrate.sh` are added for
  local/CI use.  `sqlite_utils.py` is marked deprecated.
- Remove the dead `skip_local` parameter from `load_config()` in `src/robotsix_mill/config/loader.py` (no callers remained).
- Config: `load_repos_config` accepts both the nested `ReposRegistry` (`{meta, repos}`) shape written by central-deploy onboarding and the legacy flat `{repo_id: cfg}` shape, so a fresh onboard no longer crash-loops on `repos={"meta":null,"repos":{}}`.

- Enable pin-bump periodic workflow: add `.robotsix-mill/periodic/pin_bump.yaml` presence file to activate the built-in fleet-wide git-dependency pin-bump runner.
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
- Fix `release.yml` startup_failure: remove the invalid `secrets: GITHUB_TOKEN` passed to the reusable `docker-release.yml` (GITHUB_TOKEN is reserved and cannot be passed to a reusable; the reusable uses the auto-token internally). Unblocks mill/sandbox/proxy/image publishing.
- Consolidate ``sqlite_utils`` onto ``robotsix-llmio``: bump the llmio pin to ``73f2d555`` (PR #316 — multi-table batch, SA ``exec_driver_sql`` support), switch ``db.py`` to import ``run_additive_migrations`` from ``robotsix_llmio.core.sqlite_utils``, adapt the call site to the single-table-per-call API, and delete the local shim (``src/robotsix_mill/core/sqlite_utils.py``) and its tests (``tests/core/test_sqlite_utils.py``).
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
- Correct `docs/dev-tooling/reusable-workflow-callers.md`: the shared `python-ci.yml` and `python-docs.yml` workflows live in `damien-robotsix/robotsix-github-workflows`, not in `robotsix-mill`. Updated all references, the wrong-org callout, and the local-form note accordingly.
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
