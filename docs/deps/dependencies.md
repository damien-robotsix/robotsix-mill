# Dependency management: pinned lockfile + automated bump

mill depends on four sibling **shared libraries**, consumed as git
dependencies in [`pyproject.toml`](../pyproject.toml):

| Library | Where declared | Notes |
|---------|----------------|-------|
| `robotsix-llmio[openrouter_deepseek,claude_sdk]` | `dependencies` | OpenRouter model construction, DeepSeek pin, per-tier reasoning, cost, retry/transient classification (`agents/retry.py`, `agents/openrouter_cost.py` are thin shims). |
| `robotsix-board` | `dependencies` | Kanban-board frontend. Its line historically **lacked** an explicit `@main` ref (it resolved to the default branch implicitly); now normalized to `@main` to match the others — cosmetic, uv pins the resolved commit either way. |
| `robotsix-yaml-config` | `dependencies` | Shared YAML config-cascade library. |
| `robotsix-modules` | `dev` group only | Module-taxonomy JSON Schema + validation CLI. **Dev/CI-only** — used by the `robotsix-modules validate` step and the pre-commit hook; never imported by mill at runtime. Lives in the PEP 735 `[dependency-groups]` `dev` group, not a published package extra. |

All four are referenced as `git+https://…@main`. Left unpinned, every
*fresh* install re-resolves `@main` to whatever the latest commit is,
while stale local venvs keep the old build.

## The pin + bump mechanism

This repo uses the **pin + bump** approach (mill-side, no changes to the
library repos):

1. **Pin.** A `uv.lock` is **committed at the repo root** and is the
   single source of truth for installs. It records concrete resolved
   commit hashes for all four `robotsix-*` git deps. `main` therefore
   always builds reproducibly off those pinned commits — never off a
   moving `@main` HEAD.

2. **Frozen gate.** CI ([`ci.yml`](../.github/workflows/ci.yml)) and the
   `Makefile` `install` target run `uv sync --frozen` **without** a
   preceding `uv lock`. `--frozen` installs strictly from the committed
   lock and **fails the build if the lock is stale** relative to
   `pyproject.toml`. This is a hard CI **gate**, not advisory (see
   [ci-policy.md](../ci-policy.md)).

3. **Bump.** A scheduled workflow
   ([`deps-bump-schedule.yml`](../.github/workflows/deps-bump-schedule.yml))
   calls the reusable
   [`deps-bump.yml`](../.github/workflows/deps-bump.yml) weekly (cron)
   and on `workflow_dispatch`. It executes `uv lock --upgrade`
   (refreshing git refs to the latest `@main` commits) and, if `uv.lock`
   changed, opens a PR via a SHA-pinned `peter-evans/create-pull-request`
   action with labels `dependencies` and `automated`. That PR triggers
   the existing `ci.yml` on `pull_request`, running the **full pytest
   suite** (including `tests/agents/test_retry.py`).

   > **Note:** the bump PR's CI fires only because the PR is opened with
   > the `DEPS_BUMP_TOKEN` PAT. A PR created with the default
   > `GITHUB_TOKEN` does **not** trigger workflow runs (GitHub's
   > recursion guard), so without the PAT the gate would be silently
   > bypassed.

Net effect: a new shared-lib commit can reach mill **only** through the
bump PR, whose CI runs the full suite. A semantic change that breaks
mill's contract surfaces in the bump PR (red CI blocks the merge) — it
can never land silently on `main`.

> **Never hand-edit `uv.lock`.** Regenerate it with `uv lock` (or
> `uv lock --upgrade` to advance the git refs) and commit the result.

## Trade-offs

- **Adoption latency.** A genuinely-wanted library change waits for the
  next bump PR (up to a week, or trigger `deps-bump-schedule.yml` manually via
  `workflow_dispatch`). This is the cost of gating; left unpinned, the
  change arrives instantly but ungated.
- **Double-merge for coordinated cross-repo changes.** A change that
  must land in both a library and mill together requires two merges (the
  library, then mill's bump PR). Acceptable for the safety it buys.
- **Earliest-catch vs. mill-scoped (the considered-and-deferred
  alternative).** A *consumer-contract job inside each library repo's
  CI* (draft option 1) would catch a breaking change at the **earliest**
  point — before it even merges in the library. It is deliberately **not
  done here**: it requires modifying the external robotsix-llmio /
  robotsix-board / robotsix-yaml-config / robotsix-modules repositories,
  which are outside this mill repo and outside an implement agent's
  reach. Pin + bump is fully mill-side and "simplest to operate"; it
  catches breakage slightly later (in the bump PR rather than upstream)
  but with zero cross-repo coordination.
- **Renovate `lockFileMaintenance` (the alternative bump mechanism).**
  Enabling `"lockFileMaintenance"` in `renovate.json` would also refresh
  `uv.lock` periodically. It was **not** chosen because Renovate's
  `pep621` manager does **not** bump `git+https` version refs, and
  lockfile maintenance requires the Renovate runner to have `uv` +
  GitHub network access to re-resolve the git deps — making it less
  deterministic than the self-contained scheduled workflow above.

## CI-monitor heuristic

When `main` (or a baseline) goes red **and zero mill commits touch the
failing area**, suspect a shared-lib bump first: check whether the
robotsix-* pinned commits in `uv.lock` recently advanced (i.e. via a
merged bump PR). The failure is most likely a shared-lib semantic change
the contract tests caught, not a regression in mill's own code.

## Retroactive walkthrough: llmio PR #103 (`is_rate_limited`)

llmio PR #103 changed `is_rate_limited` from string-name matching to
`isinstance` checks. Because mill's deps were unpinned `@main`, that
commit propagated to mill `main` on the next fresh install, turning
`tests/agents/test_retry.py` red at `main` commit `57b902df` — poisoning
every ticket baseline as a "pre-existing failure on main" and spawning
duplicate fix drafts (7eab, e2ec).

**With pin + bump in place**, that same llmio commit could not have
reached mill `main` directly. It would have entered **only** through a
bump PR — `deps-bump.yml`'s `uv lock --upgrade` (or, under alternative
(b), Renovate `lockFileMaintenance`) advancing the llmio pin. That bump
PR's CI would have run `tests/agents/test_retry.py`, gone **red in the
PR**, and **blocked the merge** — leaving `main` green and unpoisoned.

This ticket proves only that the **gate** would have caught it. The
separate fix — making the `test_retry.py` fixture construct the real
`pydantic_ai.exceptions.UsageLimitExceeded` so it survives both
string- and isinstance-based classification — is tracked by drafts
7eab/e2ec and is **out of scope** here.
