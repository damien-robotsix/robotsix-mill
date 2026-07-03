# JavaScript / Node.js language conventions

The authoritative JavaScript language conventions live at the
[robotsix-standards JavaScript page](
  https://damien-robotsix.github.io/robotsix-standards/javascript/
).  This file covers only mill-specific operational content.

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
