# Observability for the refine agent

When robotsix-mill refines a ticket for a managed repo, the **refine
agent** can consult that repo's runtime observability data to produce a
better-grounded spec:

- **Per-repo Langfuse traces** — the managed repo's own LLM agents
  (e.g. robotsix-auto-mail's triage / draft-reply / archive-structure
  agents) are traced in a Langfuse project. Refine can query recent and
  relevant traces from that project.
- **Deployed application logs** — the managed repo's live deployment
  writes log files to a folder mill can read. Refine can grep those for
  actual errors/warnings (ingestion, IMAP, pipeline, …).

This is **strictly opt-in per repo**. A repo with no observability
configuration behaves exactly as it does today — see
[Graceful degradation](#graceful-degradation-config-missing).

---

## Configuration schema

Observability has two independent configuration surfaces.

### Langfuse (in `config/repos.yaml`, per repo)

Each repo's Langfuse project is declared under its entry's `langfuse:`
block in the operator-managed `config/repos.yaml`. This table mirrors
the *Field reference* in
[configuration.md → Repos registry](../config/configuration.md#repos-registry)
so the two stay consistent:

| YAML key | Required | Default | Description |
|----------|----------|---------|-------------|
| `repos.<id>.langfuse.project_name` | yes | — | Langfuse project name for this repo's traces |
| `repos.<id>.langfuse.public_key` | yes | — | Langfuse public key for this repo's project |
| `repos.<id>.langfuse.secret_key` | yes | — | Langfuse secret key for this repo's project |
| `repos.<id>.langfuse.base_url` | no | `https://cloud.langfuse.com` | Langfuse base URL |
| `repos.<id>.langfuse_from` | no | — | Inherit another repo's Langfuse project (whole workspace shares ONE project) |

**Inheritance with `langfuse_from`.** When `langfuse_from` is set, the
repo inherits the named master repo's Langfuse project. In that case the
`langfuse:` block **MUST be omitted** — a repo with `langfuse_from` must
not carry its own keys. There is **no chaining** (you cannot point at a
repo that itself inherits) and **no self-reference**. This is the field
member-sync sets automatically for auto-registered workspace members.

### Deployed logs (in mill's central `config/repos.yaml`)

The operator declares a repo's deployed-log folder as a per-repo key in
mill's central, **gitignored** `config/repos.yaml` — alongside
`board_id` / `forge_remote_url` / `langfuse:`. The value is a
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

- **Langfuse keys live in `config/repos.yaml`**, which is
  operator-managed and **gitignored** (exactly like
  the `config/config.yaml` `secrets:` block) — it is never committed. Keys are read from
  `RepoConfig` at call time, not stamped onto the global `Secrets`
  singleton.
- **`deployed_log_folder` lives in `config/repos.yaml` too** — it is a
  deployment-specific host path, kept central with the (also gitignored)
  Langfuse keys rather than committed into the managed repo.
- Use **`langfuse_from`** to avoid duplicating keys across a workspace's
  member repos — one project, inherited.
- **No `${ENV_VAR}` interpolation.** `config/repos.yaml` does **not**
  perform environment-variable substitution; the loader does not
  implement it. Put the literal keys in the (gitignored) file — do not
  expect `${LANGFUSE_SECRET_KEY}`-style references to be expanded.

---

## Setting up observability for a managed repo

Using robotsix-auto-mail as the example:

1. **Configure Langfuse in `config/repos.yaml`.** Add or locate the
   repo's entry and fill its `langfuse:` block (`project_name`,
   `public_key`, `secret_key`, optional `base_url`). If the repo is a
   workspace member that should share its master's project, set
   `langfuse_from: <master-repo-id>` instead and omit the `langfuse:`
   block.
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
    langfuse:
      project_name: "robotsix-auto-mail"
      public_key: "pk-lf-..."
      secret_key: "sk-lf-..."
      base_url: "https://cloud.langfuse.com"  # optional — defaults to cloud
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

These use the per-repo `RepoConfig` Langfuse credentials when the
ticket's repo has them configured; otherwise they fall back to the
global `Secrets`.

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

- **No `langfuse:` / `langfuse_from`** → the refine agent still gets the
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
