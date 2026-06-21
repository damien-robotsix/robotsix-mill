# JavaScript / Node.js language conventions

(Used by the implement and refine agents, and by the review-type agents
that read/critique code — retrospect, review.)

## Manifest & lockfile workflow

- `package.json` is committed to version control.
- `package-lock.json` is **committed** to version control and is the
  source of truth for reproducible installs.
- **Never** hand-edit `package-lock.json` — it is generated from
  `package.json` by `npm install`.
- **When `package.json` dependency lines change** (in `dependencies`,
  `devDependencies`, or `peerDependencies`), run `npm install` (or
  `npm install --package-lock-only`) to regenerate `package-lock.json`,
  and include the lockfile diff in the same commit.
- Purely structural or metadata-only `package.json` edits (e.g. a
  `scripts` entry, a config section, `name`, `version`) do **NOT**
  require lockfile regeneration.
- CI uses `npm ci`, which fails if the lockfile is stale relative to
  `package.json` — this is intentional.

## Sandbox constraints (critical)

The `npm` CLI (v9) is available in the sandbox. The sandbox has
filtered network access: the npm registry (`registry.npmjs.org`) is
allowlisted for Node.js repo tooling, in addition to PyPI and GitHub.

### `npm install` fails with registry or credential errors

Some dependencies (private registries, git-hosted packages requiring
credentials, packages behind a VPN) may be unreachable from the
sandbox. In that case:

- Commit the `package.json` change.
- In your summary, note that a human must run `npm install` with the
  appropriate credentials and commit the updated lockfile.
- Do **not** `ask_user` or file a ticket for the inability to fetch
  packages — the operator expects the agent to note the required
  human step instead.

## Test invocation

```bash
npm test
```

## Linter / formatter

```bash
npx eslint . && npx prettier --check .
```
