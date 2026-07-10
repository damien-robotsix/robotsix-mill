# CLI usage

`robotsix-mill` is the management-plane CLI for the mill pipeline. It is
a thin HTTP client over the FastAPI service — every command talks to the
API.

## Quick reference

| Command | Description |
|---|---|
| `robotsix-mill serve` | Run the API + event-driven worker |
| `robotsix-mill repos list` | List registered repos and their boards |
| `robotsix-mill ticket new` | Create a new ticket |
| `robotsix-mill ticket list` | List tickets, optionally filtered by state |
| `robotsix-mill ticket show <id>` | Show one ticket + its history |
| `robotsix-mill ticket approve <id>` | Approve a ticket awaiting human approval |
| `robotsix-mill ticket resume-blocked <id>` | Resume a blocked ticket |
| `robotsix-mill epic new` | Create a new epic |
| `robotsix-mill inquire` | Ask a one-shot question (no code-change lifecycle) |
| `robotsix-mill audit` | Run an audit pass |
| `robotsix-mill health` | Run a health pass |
| `robotsix-mill agent-check` | Run an agent definition coherence check |
| `robotsix-mill test-gap` | Run a test-gap coverage inspection pass |
| `robotsix-mill config-sync` | Run a config/docs drift detection pass |
| `robotsix-mill member-sync` | Run a workspace member-sync (vcs2l manifest) pass |
| `robotsix-mill bc-check` | Run a backward-compatibility inspection pass |
| `robotsix-mill completeness-check` | Run a feature-completeness inspection pass |
| `robotsix-mill survey` | Run an OSS project discovery survey pass |
| `robotsix-mill copy-paste` | Run a code-duplication detection pass |
| `robotsix-mill forge-parity` | Run a forge-parity drift detection pass |
| `robotsix-mill run-health` | Run a health analysis over recent run outcomes |
| `robotsix-mill diagnostic` | Run a daily diagnostic pass |
| `robotsix-mill trace-health` | Check Langfuse for unsessioned/unnamed traces |
| `robotsix-mill trace-review` | Run a trace-review pass over recent Langfuse traces |
| `robotsix-mill langfuse-cleanup` | Delete excess Langfuse traces |
| `robotsix-mill roadmap-sync` | Run a roadmap-sync pass |
| `robotsix-mill module-curator` | Run a module taxonomy drift detection pass |
| `robotsix-mill meta` | Run the meta pass (extraction + alignment across repos) |
| `robotsix-mill verify` | Verify TicketEvent hash-chain integrity |
| `robotsix-mill --print-completion` | Print shell completion script (bash/zsh/tcsh) |

Most commands support `--json` for machine-readable output.

## Service lifecycle

### `robotsix-mill serve`

Start the API server and event-driven worker loop.

```sh
# Multi-repo mode: serves every repo in config/repos.yaml
robotsix-mill serve

# Single-repo override (useful for tests/dev):
robotsix-mill serve --repo-id my-repo
```

When `config/repos.yaml` is empty, the server refuses to start (exit
code 2) with an error message. An unknown `--repo-id` also causes an
error exit.

See also: [configuration.md](../configuration.md) for `MILL_API_URL`,
`MILL_API_HOST`, `MILL_API_PORT`, and other env vars consumed by the
service.

### `robotsix-mill repos list`

List all registered repos and their board IDs.

```sh
robotsix-mill repos list
```

Output columns: `REPO_ID`, `BOARD_ID`, `SOURCE`.

## Ticket operations

### `robotsix-mill ticket new`

Create a new ticket. The worker picks it up on the next poll cycle.

```sh
# Minimal:
robotsix-mill ticket new --title "Fix login redirect"

# With a description from a file:
robotsix-mill ticket new --title "Fix login redirect" \
  --description-file path/to/body.md

# Read description from stdin:
echo "The login page redirects to /old instead of /new" \
  | robotsix-mill ticket new --title "Fix login redirect" \
    --description-file -

# Target a specific repo (required when multiple repos are configured):
robotsix-mill ticket new --title "Fix login redirect" --repo-id my-repo

# Attach screenshots:
robotsix-mill ticket new --title "Broken button" \
  --screenshot screenshot1.png --screenshot screenshot2.png
```

Prints the new ticket ID on success.

### `robotsix-mill ticket list`

List tickets, optionally filtered by state.

```sh
# All tickets:
robotsix-mill ticket list

# Filter by state:
robotsix-mill ticket list --state ready

# Filter by repo:
robotsix-mill ticket list --repo-id my-repo
```

Output: `<id>\t<state>\t<title>`, one ticket per line.

### `robotsix-mill ticket show <id>`

Show the full JSON body of a ticket plus its event history.

```sh
robotsix-mill ticket show 20250331T142030Z-fix-auth-timeout-a3f2
```

### `robotsix-mill ticket approve <id>`

Approve a ticket that is in `human_issue_approval` state, advancing it
to `ready` so the implement stage can start. The approval gate is
enabled by default (`MILL_REQUIRE_APPROVAL=true`).

```sh
robotsix-mill ticket approve 20250331T142030Z-fix-auth-timeout-a3f2
```

See also: [approval-gate.md](../approval-gate.md) for details on the
approval workflow.

### `robotsix-mill ticket resume-blocked <id>`

Resume a blocked ticket back to the state it was blocked from. Also
clears retry backoff for retrying tickets.

```sh
# Basic resume:
robotsix-mill ticket resume-blocked 20250331T142030Z-fix-auth-timeout-a3f2

# With an operator note (recorded as a comment; also clears the
# implement stage's stale-spec guard when resuming into READY):
robotsix-mill ticket resume-blocked 20250331T142030Z-fix-auth-timeout-a3f2 \
  --note "spurious network timeout; ticket should retry fine"
```

See also: [blocked-ticket-recovery.md](../blocked-ticket-recovery.md)
for the full recovery workflow.

> **Note:** `mark-done` and `transition` are available via the HTTP API
> (`POST /tickets/{id}/mark-done`, `POST /tickets/{id}/transition`) and
> the web board — they do not have dedicated CLI subcommands.

## Epic operations

### `robotsix-mill epic new`

Create a new epic.

```sh
robotsix-mill epic new --title "Redesign auth system"

# With description from file:
robotsix-mill epic new --title "Redesign auth system" \
  --description-file path/to/epic.md

# Target a specific repo:
robotsix-mill epic new --title "Redesign auth system" --repo-id my-repo
```

## Inquiry

### `robotsix-mill inquire`

Ask a one-shot question. Creates an inquiry ticket (kind `INQUIRY`) that
the answer agent picks up — no code-change lifecycle, no state machine.

```sh
robotsix-mill inquire --title "What does the forge adapter do?"

# With body from file:
robotsix-mill inquire --title "Explain the config system" \
  --description-file question.md
```

## Periodic passes

Every periodic pass listed below runs as a one-shot CLI invocation
(typically driven by cron or a scheduler) and emits draft tickets when
it detects gaps. All accept `--json` for machine-readable output.

```sh
robotsix-mill audit              # repo audit pass
robotsix-mill health             # health pass
robotsix-mill agent-check        # agent definition coherence check
robotsix-mill test-gap           # test coverage gap detection
robotsix-mill config-sync        # config/docs drift detection
robotsix-mill bc-check           # backward-compatibility inspection
robotsix-mill completeness-check # feature-completeness inspection
robotsix-mill survey             # OSS project discovery
robotsix-mill copy-paste         # code-duplication detection
robotsix-mill forge-parity       # forge state drift detection
robotsix-mill run-health         # run outcome health analysis
robotsix-mill diagnostic         # daily diagnostic pass
robotsix-mill module-curator     # module taxonomy drift detection
```

### `robotsix-mill trace-health`

Check Langfuse for unsessioned / unnamed traces and alert if found.

```sh
robotsix-mill trace-health
robotsix-mill trace-health --json
```

### `robotsix-mill trace-review`

Run a trace-review pass over recent Langfuse traces.

```sh
robotsix-mill trace-review
robotsix-mill trace-review --repo-id my-repo
robotsix-mill trace-review --json
```

### `robotsix-mill langfuse-cleanup`

Delete excess Langfuse traces to stay under the per-project cap.

```sh
robotsix-mill langfuse-cleanup
robotsix-mill langfuse-cleanup --json
```

### `robotsix-mill member-sync`

Run a workspace member-sync pass (vcs2l manifest).

```sh
robotsix-mill member-sync
robotsix-mill member-sync --repo-id my-repo
robotsix-mill member-sync --json
```

### `robotsix-mill roadmap-sync`

Run a roadmap-sync pass.

```sh
robotsix-mill roadmap-sync
robotsix-mill roadmap-sync --repo-id my-repo
robotsix-mill roadmap-sync --json
```

### `robotsix-mill meta`

Run the meta pass (extraction + alignment proposals across all repos).

```sh
robotsix-mill meta
robotsix-mill meta --json
```

## Integrity verification

### `robotsix-mill verify`

Verify TicketEvent hash-chain integrity.

```sh
# All tickets:
robotsix-mill verify

# Single ticket:
robotsix-mill verify --ticket-id 20250331T142030Z-fix-auth-timeout-a3f2
```

Outputs a summary of events scanned, tickets verified, and any integrity
breaks found.

## Shell completion

```sh
robotsix-mill --print-completion bash  # bash completion script
robotsix-mill --print-completion zsh   # zsh completion script
robotsix-mill --print-completion tcsh  # tcsh completion script
```

Pre-generated completion scripts are available in `contrib/completions/`.

## See also

- [configuration.md](../configuration.md) — full environment variable reference
- [approval-gate.md](../approval-gate.md) — approval workflow details
- [blocked-ticket-recovery.md](../blocked-ticket-recovery.md) — recovering blocked tickets
- [docker-architecture.md](../docker-architecture.md) — how the CLI fits into the container architecture
- [index.md](../index.md) — documentation home
