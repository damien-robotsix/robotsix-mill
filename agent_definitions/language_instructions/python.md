# Python language conventions

The authoritative Python language conventions live at the
[robotsix-standards Python page](
  https://damien-robotsix.github.io/robotsix-standards/python/
).  This file covers only mill-specific operational content.

## Sandbox constraints (critical)

The `uv` Rust binary is available in the sandbox. `uv lock` and
`uv sync --frozen` work — the sandbox has filtered network access
(PyPI and GitHub only, via an egress proxy).

The agent **cannot** run `pip install`, `cargo build`, or most other
commands that fetch from the network — only `uv` commands can reach
the internet (and only to PyPI/GitHub). Note: `npm install` is
separately allowlisted for Node.js repos (see the javascript language
instructions), but is not available for general Python-repo use.

### `uv lock` fails with git credential errors

The sandbox has no GitHub credentials, so `uv lock` **will fail**
when `pyproject.toml` contains a git dependency (e.g. under
`[tool.uv.sources]`). The `GIT_TERMINAL_PROMPT=0` env var in the
sandbox container prevents hangs, but `uv lock` will still exit
non-zero with a credential error.

**Workaround:** temporarily remove the git dependency from
`pyproject.toml` and its `[tool.uv.sources]` entry, run `uv lock`,
then restore both. The lockfile will be generated without the git
dependency, which is acceptable when the git dependency is not
needed for the current change. If the dependency *is* needed, note
in your summary that a human must run `uv lock` with credentials
and commit the updated lockfile.

When non-`uv` package-manager commands would fail due to lack of
network:
- Commit the manifest change.
- In your summary, note that a human must run the package manager
  and commit the updated lockfile.
- Do **not** `ask_user` or file a ticket for the inability to fetch
  packages — the operator expects the agent to note the required
  human step instead.
