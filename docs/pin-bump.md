# Pin-bump pipeline

The pin-bump agent is a scheduled periodic workflow that detects
outdated dependency pins across managed repositories and opens PRs to
bump them. **The scheduled runner (detection + PR actuator) is fully
delivered:** every registered repo with a `.robotsix-mill/periodic/pin_bump.yaml`
presence file triggers a pass that:

1. Computes the internal-dependency graph and logs current pin SHAs.
2. For every stale pin (SHA ≠ latest on the dependency's default
   branch), clones the consuming repo, edits `pyproject.toml`,
   runs a coherence check via `uv lock`, pushes a branch, and opens a
   cross-repo PR via the forge API.  Pins that are already at the
   latest SHA are skipped (idempotent).
3. Before opening a PR, the actuator checks for an existing open
   bump PR on the same branch — if one exists, the bump is skipped
   to avoid duplicates.
4. Repos are processed in topological order (dependencies first).
   A repo whose ``uv lock`` coherence check reports conflicts is
   skipped with a ``WARNING`` and no PR; other repos still proceed.
5. The per-repo ``max_inflight_prs`` setting caps the number of
   concurrently open pin-bump PRs. When the cap is reached, further
   bumps for that repo are skipped with an ``INFO`` log.

This document covers the configuration wiring, presence-file trigger,
dispatch, and network egress requirements.

---

## Configuration

### Global defaults

The master switch and interval live under `periodic.pin_bump` in
`config/config.json` (defaults in `config/config.example.json`):

```yaml
periodic:
  pin_bump:
    enabled: false          # opt-in — enable the periodic pin-bump pass
    interval_seconds: 86400 # seconds between passes (default: 1 day)
```

Environment variable overrides:

| Env var | Default | Description |
|---------|---------|-------------|
| `MILL_PIN_BUMP_PERIODIC` | `false` | Master switch for the pin-bump pass |
| `MILL_PIN_BUMP_INTERVAL_SECONDS` | `86400` | Seconds between passes |

### Per-repo presence file

Each managed repo enables pin-bump scanning by committing a presence
file at `.robotsix-mill/periodic/pin_bump.yaml`. A name-only file
inherits the built-in defaults:

```yaml
# .robotsix-mill/periodic/pin_bump.yaml
name: pin_bump
```

Repos that do not commit this file are skipped. The presence file may
override `interval_seconds` or `enabled`:

```yaml
# .robotsix-mill/periodic/pin_bump.yaml
name: pin_bump
enabled: true
interval_seconds: 43200  # twice daily
```

---

## Scheduled runner (harness)

The `pin_bump` workflow is registered as `schedule_only` in the
built-in kind map (`periodic_loader.py`), which means it carries no
LLM prompt and is dispatched to a deterministic runner. The dispatch
entry lives in `_SCHEDULE_ONLY_RUNNERS` (`poll_loops.py`):

```
"pin_bump": "robotsix_mill.runners.pin_bump_runner:run_pin_bump_pass"
```

The runner (`src/robotsix_mill/runners/pin_bump_runner.py`) performs a
**detection + PR actuator** pass:

1. Loads the repos registry (`get_repos_config()`).
2. For each registered repo with a `forge_remote_url` and a working
   forge token, clones the default branch and reads `pyproject.toml`.
3. Calls `build_internal_dep_graph(pyproject_map, registry)` from
   `deps/internal_graph.py` to compute:
   - `InternalDepGraph.topo_order` — topological sort (leaves first).
   - `InternalDepGraph.pins` — current `{repo_id: {dep: GitPin(git_url, rev)}}`.
4. Logs the topological order and each pin's current SHA at `INFO`.
5. Runs the **PR actuator** (`run_pin_bump_pr_actuator`): for every
   stale pin (where `git ls-remote HEAD` on the dependency returns a
   different SHA):
   - Checks for an existing open bump PR on the same branch
     (`forge.pr_status`) and skips if one is already open.
   - Respects `repo_config.max_inflight_prs` (default 3) — skips the
     bump when the number of open `mill/pin-bump/*` PRs for that repo
     has reached the cap.
   - Clones the consuming repo, updates the `rev` in
     `pyproject.toml`, runs a coherence check via `uv lock`
     (`run_coherence_check` from `deps/coherent_resolver.py`). If the
     check reports conflicting URLs, the repo is skipped with a
     `WARNING` — no PR is opened.
   - Commits, pushes a `mill/pin-bump/<dep>` branch, and opens a PR
     via the forge API.
   Pins already at the latest SHA are skipped (idempotent).

Expected failures are caught and logged — the pass never raises out of
the scheduler loop:

* Duplicate-PR detection and in-flight cap throttling are logged at
  ``INFO`` level (normal operational conditions).
* All other recoverable failures (missing remote URL, missing forge
  token, clone failure, cyclic dependency graph, ``ls-remote`` failure,
  coherence-check conflict, push failure, PR-creation failure) are
  logged at ``WARNING`` level.

---

## Network egress requirements

The pin-bump pass runs inside the sandbox container. It needs outbound
network access to query package registries for latest-version metadata:

| Registry | Host | Protocol | Notes |
|----------|------|----------|-------|
| PyPI | `pypi.org` | HTTPS | Python package index |
| npm | `registry.npmjs.org` | HTTPS | JavaScript package registry |
| crates.io | `crates.io` | HTTPS | Rust package registry |
| GitHub Releases | `api.github.com` | HTTPS | GitHub release tags (for GitHub-hosted dependencies) |

The sandbox routes all egress through the configured proxy
(`sandbox.proxy_url`, default `http://sandbox-proxy:8888`). Ensure the
proxy allows these hosts. No inbound access is required.

If the sandbox runs with `--network none` (no proxy configured), the
pin-bump pass will fail at the registry-query step. The worker logs a
warning and skips the pass for that repo.

---

## Credential requirements

The pin-bump pass does **not** require package-registry credentials for
public registries — version metadata queries are unauthenticated.

For **private registries** or registries with rate limits, the sandbox
must be provisioned with the appropriate credentials. This is handled
through the existing `extra_sandbox_packages` mechanism or by baking
credentials into the sandbox image.

To create PRs, the pin-bump pass uses the standard forge credentials
already configured in `config/config.json` (`forge_token` or GitHub
App). No additional forge credentials are needed.

---

## See also

- [configuration.md](configuration.md) — full configuration reference
- [github-app.md](github-app.md) — forge authentication setup
- [docker-architecture.md](docker-architecture.md) — sandbox network model
