# Pin-bump pipeline

The pin-bump agent is a scheduled periodic workflow that detects
outdated dependency pins across managed repositories and opens PRs to
bump them. The PR actuator is tracked separately; this document covers
the configuration wiring, presence-file trigger, and network egress
requirements.

---

## Configuration

### Global defaults

The master switch and interval live under `periodic.pin_bump` in
`config/config.yaml` (defaults in `config/config.example.yaml`):

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
already configured in `config/config.yaml` (`forge_token` or GitHub
App). No additional forge credentials are needed.

---

## See also

- [configuration.md](configuration.md) — full configuration reference
- [github-app.md](github-app.md) — forge authentication setup
- [docker-architecture.md](docker-architecture.md) — sandbox network model
