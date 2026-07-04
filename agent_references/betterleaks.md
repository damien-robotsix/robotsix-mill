# Betterleaks — pre-commit hook for secret detection

Betterleaks is a secret-detection scanner designed as a faster, more
configurable alternative to detect-secrets. Repo:
https://github.com/betterleaks/betterleaks

## Pre-commit hook

Hook id: `betterleaks`. Latest version: **v1.6.0**.

### Baseline mode is CLI-only

The `--baseline` flag is **not supported as a pre-commit `args` entry**.
Pre-commit hooks receive a list of staged files as positional arguments,
which interferes with `--baseline` parsing. To use baseline mode, either:

- Run `betterleaks scan --baseline .betterleaks.baseline` as a manual
  CLI step (not through pre-commit), or
- Use a `local` pre-commit hook with `entry: betterleaks` and
  `pass_filenames: false`.

### Standard pre-commit config

```yaml
- repo: https://github.com/betterleaks/betterleaks
  rev: v1.6.0
  hooks:
    - id: betterleaks
```

No `args` needed for the default scan mode.

## Configuration: `.betterleaks.toml`

Betterleaks reads a `.betterleaks.toml` file in the repo root.
Config precedence (highest to lowest):

1. CLI flags (e.g. `betterleaks scan --exclude ...`)
2. `.betterleaks.toml` in the repo root
3. Built-in defaults

### Format

```toml
[scan]
# Patterns to exclude from scanning (glob syntax)
exclude = [
    "*.baseline",
    "tests/fixtures/*",
    "poetry.lock",
]

[rules]
# Disable specific rules by id
disable = ["generic-api-key"]

# Override rule thresholds
[rules.thresholds]
"generic-api-key" = { entropy = 4.5, min_length = 8 }
```

The `[scan]` section controls file exclusion; `[rules]` controls which
detectors run and their sensitivity.

## Migration from detect-secrets

When migrating from detect-secrets:

1. Remove the `detect-secrets` hook from `.pre-commit-config.yaml`.
2. Add the `betterleaks` hook (see standard config above).
3. Generate an initial baseline: `betterleaks scan --baseline .betterleaks.baseline`
4. Add `.betterleaks.baseline` to git.
5. Create `.betterleaks.toml` with exclusions matching your old
   `.secrets.baseline` allowlist entries.
