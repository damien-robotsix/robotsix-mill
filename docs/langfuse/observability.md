# Observability for the refine agent

When robotsix-mill refines a ticket for a managed repo, the **refine
agent** can consult that repo's runtime observability data to produce a
better-grounded spec:

- **Langfuse traces** — the managed repo's own LLM agents (e.g.
  robotsix-auto-mail's triage / draft-reply / archive-structure agents)
  are traced in a global Langfuse project. Refine can query recent and
  relevant traces from that project.
- **Deployed application logs** — the managed repo's live deployment
  writes log files to a folder mill can read. Refine can grep those for
  actual errors/warnings (ingestion, IMAP, pipeline, …).

Langfuse is **global** (one project configured in the `secrets:` block
of `config/config.json`, applied uniformly to every repo by
`_apply_global_langfuse`). Deployed logs are **opt-in per repo**. See
[Graceful degradation](#graceful-degradation-config-missing).

---

## Configuration schema

Observability has two independent configuration surfaces.

### Langfuse (global `secrets:` block, applied uniformly)

Langfuse credentials are configured in a **single global `secrets:` block**
in `config/config.json` (`MILL_CONFIG_FILE` or `MILL_SECRETS_FILE`). The
`_apply_global_langfuse()` function in `repos.py` reads these credentials
and populates every repo's langfuse fields uniformly — there is **no**
per-repo `langfuse:` block. A per-repo `langfuse:` key in
`config/repos.yaml` would be silently ignored.

The relevant `Secrets` fields (see
[configuration.md → Secrets reference](../config/configuration.md#secrets-reference)):

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `langfuse_public_key` | yes | — | Langfuse public key for the shared workspace project |
| `langfuse_secret_key` | yes | — | Langfuse secret key for the shared workspace project |
| `langfuse_base_url` | no | `https://cloud.langfuse.com` | Langfuse base URL |
| `langfuse_project_name` | no | — | Langfuse project name for trace attribution |
| `langfuse_project_id` | no | — | Langfuse project ID for trace attribution |

All repos share the same project. There is no per-repo project isolation
and no `langfuse_from` inheritance — the old field has been removed.

### Deployed logs (in mill's central `config/repos.yaml`)

The operator declares a repo's deployed-log folder as a per-repo key in
mill's central, **gitignored** `config/repos.yaml` — alongside
`board_id` / `forge_remote_url`. The value is a
deployment-specific host path, so it must **not** be committed into the
managed repo (the old repo-owned `.robotsix-mill/config.yaml` key is
deprecated and ignored — a deprecation warning is logged if it is still
present):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `deployed_log_folder` | `str` | no | Path to the live deployment's log directory. Either absolute, or **relative to the repo root** (relative paths are resolved against the repo dir, and a warning is logged for relative paths). When absent — or when it does not point at an existing directory — the log tooling is silently skipped. |

---

## Secret handling

The actual mechanism, not an idealized one:

- **Langfuse keys live in the `secrets:` block of `config/config.json`**
  (overridable via `MILL_SECRETS_FILE`), which is operator-managed and
  **gitignored** — it is never committed. The `Secrets` fields
  (`langfuse_public_key`, `langfuse_secret_key`, `langfuse_base_url`,
  `langfuse_project_id`, `langfuse_project_name`) are read at startup by
  `_apply_global_langfuse()` and applied uniformly to every repo. There is
  no per-repo `langfuse:` block — one global project for all repos.
- **`deployed_log_folder` lives in `config/repos.yaml`** — it is a
  deployment-specific host path, kept central with the (also gitignored)
  repo config rather than committed into the managed repo.
- **No `${ENV_VAR}` interpolation.** `config/repos.yaml` does **not**
  perform environment-variable substitution; the loader does not
  implement it. Put the literal paths in the (gitignored) file — do not
  expect `${DEPLOYED_LOG_FOLDER}`-style references to be expanded.

---

## Setting up observability for a managed repo

Using robotsix-auto-mail as the example:

1. **Configure Langfuse in `config/config.json`.** Add the global
   `langfuse_public_key`, `langfuse_secret_key`, and optional
   `langfuse_project_name`, `langfuse_project_id`, and `langfuse_base_url`
   to the `"secrets"` block. These are applied uniformly to every repo —
   there is no per-repo configuration.
2. **Set `deployed_log_folder` in the repo's `config/repos.yaml`
   entry.** Point it at the directory the live deployment writes its
   logs to (absolute, or relative to the repo root).
3. **Ensure the deployment writes its logs to that folder** where mill
   can read it (i.e. the path resolves to an existing directory in the
   environment refine runs in).

---

## Example configuration for robotsix-auto-mail

`config/repos.yaml` — the operator-managed, gitignored registry:

```yaml
# config/repos.yaml
repos:
  robotsix-auto-mail:
    board_id: "auto-mail"
    forge_remote_url: "https://github.com/robotsix/robotsix-auto-mail.git"
    deployed_log_folder: /var/log/robotsix-auto-mail
```

Langfuse credentials go in `config/config.json`'s `"secrets"` block, not here:

```jsonc
// config/config.json (excerpt)
{
  "secrets": {
    "langfuse_public_key": "pk-lf-...",
    "langfuse_secret_key": "sk-lf-...",
    "langfuse_base_url": "https://cloud.langfuse.com",
    "langfuse_project_name": "robotsix-auto-mail"
  }
}
```

---

## How the refine agent uses this data

The tools below are wired into `agent_definitions/refine.yaml`'s
`tools:` block.

### Langfuse tools

Built in `src/robotsix_mill/agents/langfuse_tools.py` and wired in
`src/robotsix_mill/stages/refine/refining.py`:

- `langfuse_session_summary`
- `langfuse_list_traces`
- `langfuse_trace_detail`
- `langfuse_session_cost`
- `langfuse_inspect_trace`
- `inspect_cost`

These use the global `Secrets` Langfuse credentials.

### `query_app_logs`

Built by `make_log_query_tool` in
`src/robotsix_mill/agents/log_tools.py`. It is injected **only** when
the repo's `deployed_log_folder` (from `config/repos.yaml`) resolves to
an existing directory. Its
parameters:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `keywords` | `""` | Space-separated terms, matched case-insensitively and OR'd — a line is returned if it contains at least one term. Empty means "return the most recent lines". |
| `since_hours` | `24` | File-**mtime** recency gate: files whose modification time is older than this many hours are skipped entirely (log-line timestamp formats vary and are not reliably parseable). |
| `max_lines` | `200` | Cap on returned lines. When the cap trims matches, a trailing `... (truncated, N more matching lines)` marker is appended. |

In addition, the refine orchestration injects a Markdown **log summary**
into the agent context when the folder resolves — a directory listing
(file sizes + mtimes) plus tail previews — to orient the agent before it
drills in with `query_app_logs`.

---

## Graceful degradation (config missing)

Observability is additive — its absence never changes baseline
behavior:

- **No Langfuse secrets configured** → the refine agent still gets the
  Langfuse tools, backed by the **global `Secrets`** credentials.
- **No resolvable `deployed_log_folder`** (absent, or not pointing at an
  existing directory) → **no** `query_app_logs` tool and **no** log
  summary are injected, a warning is logged, and refinement proceeds
  exactly as before.

A repo with no observability configuration at all behaves identically to
today.

---

## See also

- [docs/config/configuration.md](../config/configuration.md) — full configuration reference,
  including the Repos registry and `.robotsix-mill/config.yaml` fields
- [index.md](index.md) — documentation home
