# Deployment Guide

How to build, run, and maintain the `robotsix-auto-mail` container — from
first checkout to production push.

`robotsix-auto-mail` is a **CLI tool** with an optional long-running
**web board daemon**. Most operations (`probe`, `ingest`, `board`) are
one-shot CLI invocations via `docker compose run`. The web kanban board
is a persistent HTTP daemon started via `docker compose up board`.
This guide covers both patterns.

---

## Prerequisites

| What | Minimum | Check with |
|---|---|---|
| Docker Engine | 20.10+ | `docker --version` |
| Docker Compose | Compose plugin 2.0+ | `docker compose version` |
| Git | any recent | `git --version` |


Installation guides (do **not** reproduce here):
- [Docker Engine install](https://docs.docker.com/engine/install/)
- [Docker Compose install](https://docs.docker.com/compose/install/)

---

## First-time setup

### 1. Clone the repository

```sh
git clone https://github.com/your-org/robotsix-auto-mail.git
cd robotsix-auto-mail
```

### 2. Create your local configuration

The recommended path is a single YAML config file:

```sh
cp config/mail.local.example.yaml config/mail.local.yaml
```

Then edit `config/mail.local.yaml` with your real IMAP and SMTP credentials:

```sh
$EDITOR config/mail.local.yaml
```

### 2a. Alternative: auto-detect provider settings (detect)

Instead of manually creating `config/mail.local.yaml`, you can auto-generate
it from just your email address:

```sh
export LLM_API_KEY=sk-or-v1-…
docker compose run robotsix-auto-mail detect user@gmail.com
```

This calls an LLM to look up the correct IMAP/SMTP settings and writes
`config/mail.local.yaml`, prompting for your password and including it in
that file.  See [docs/connecting.md](connecting.md#auto-detection-with-detect)
for full details.

The file `config/mail.local.yaml` is **git-ignored** (`config/mail.local.yaml`
in `.gitignore`), so your credentials stay local and never land in the repo.

**Using environment variables instead:**  copy `.env.example` to `.env`, edit
it, and source it before running commands:

```sh
cp .env.example .env
$EDITOR .env
set -a && source .env && set +a
```

(No `python-dotenv` is used at runtime; you must export the variables into
the shell or pass them via `docker compose run -e …`.)

---

## Build

```sh
docker compose build
```

The [`Dockerfile`](../Dockerfile) has two stages:

| Stage | What it does |
|---|---|
| `builder` | Installs the Python package (wheel) from `pyproject.toml` |
| `production` | Copies **only** the installed artifacts from `builder`, creates a non-root `mailbot` user (UID 1000), and sets the entrypoint |

The final image runs as `mailbot` (UID 1000).  The base image exposes no ports
and has no healthcheck — CLI operations are one-shot.  The `board` service in
`docker-compose.yml` maps a port for the long-running web server.

To build without the Compose cache:

```sh
docker compose build --no-cache
```

---

## Run locally

CLI operations (`probe`, `ingest`, `board`) use `docker compose run` — they are
one-shot commands.  The web board is a long-running daemon started with
`docker compose up board`; see [Start the web board](#start-the-web-board).

### Probe connectivity (always run first)

```sh
docker compose run robotsix-auto-mail probe
```

This opens an IMAP and SMTP connection, prints server diagnostics, and exits
with code `0` when both succeed.  No email is read or sent — it is a read-only
sanity check.  See [docs/connecting.md](connecting.md#the-probe-command) for
sample output.

### Ingest mail

```sh
docker compose run robotsix-auto-mail ingest
```

Fetches new messages from the configured IMAP inbox and stores them in the
local SQLite database.  See [docs/ingestion.md](ingestion.md) for the full
pipeline.

### View the inbox

```sh
docker compose run robotsix-auto-mail board
```

Prints a read-only view of stored messages.  Requires a prior `ingest` run.
See [docs/connecting.md](connecting.md#the-board-command) for output format.

### Start the web board

```sh
docker compose up board
# → http://localhost:${BOARD_PORT:-8078}/board
```

The board service runs as a long-lived daemon (restart policy: `on-failure`).
It listens on the port set by `BOARD_PORT` (default: **8078**).  Open the URL
in a browser to see the four-column kanban board with per-card Move dropdowns.
Press `Ctrl-C` to stop the daemon.

**Note:** the Docker default port is **8078** (set via `${BOARD_PORT:-8078}` in
`docker-compose.yml`), which differs from the native CLI default of 8080.  Set
`BOARD_PORT` in your shell or `.env` file to use a different port:
`BOARD_PORT=9090 docker compose up board`.

### Ephemeral containers, persistent data

Each `docker compose run` creates a **new, ephemeral** container that is
removed when the command exits.  The SQLite database lives outside the
container in `./.mail_data` on the host (a git-ignored bind-mount), so it
persists across runs and container lifecycles.

To inspect it:

```sh
ls -la .mail_data/        # mail.db lives here
```

---

## Configuration quick-reference

Configuration resolves through one cascade: built-in defaults → YAML file →
environment variables (which win field-by-field).

| Path | Mechanism | How to use |
|---|---|---|
| **YAML file** | A single `config/mail.local.yaml` | Recommended. Copy `config/mail.local.example.yaml` → `config/mail.local.yaml` and edit. |
| **Env vars** | `MAIL_IMAP_HOST`, `MAIL_SMTP_HOST`, `MAIL_USERNAME`, `MAIL_PASSWORD` (and optional `MAIL_IMAP_PORT`, …) | Set in shell or via `docker compose run -e …`. Either set all four required vars or supply a config file, or the entrypoint will refuse to start. |

Full precedence rules and every config key are documented in
**[docs/connecting.md](connecting.md)**.  Do not duplicate that reference
here — the connecting doc is authoritative.

### How configuration reaches the container

- `docker-compose.yml` sets `MAIL_CONFIG_PATH=/home/mailbot/config/mail.local.yaml`.
- The `./config:/home/mailbot/config` bind-mount maps the host `config/`
  directory into the container.
- Editing `config/mail.local.yaml` on the host takes effect on the **next**
  `docker compose run` — no rebuild required.

---

## `docker-compose.yml` structure

The Compose file defines two services that share the same image and data:
`robotsix-auto-mail` (the periodic ingester) and `board` (the web board).

### `services.robotsix-auto-mail`

| Key | Value | Why |
|---|---|---|
| `build.context` | `.` | Build from the repo root. |
| `build.dockerfile` | `Dockerfile` | The multi-stage Dockerfile. |
| `command` | `ingest --watch` | Default: run the periodic ingester. Overridden by `docker compose run … <cmd>` for one-shot commands. |
| `stdin_open` | `true` | Keeps stdin open so one-shot interactive commands (e.g. `detect`'s password prompt) work. |
| `tty` | `false` | No pseudo-TTY allocation; output is plain streams. |
| `restart` | `unless-stopped` | The default command is a long-running daemon, so it should stay up. |
| `environment` | `MAIL_CONFIG_PATH`, `LLM_API_KEY`, `LLM_MODEL` | Points the tool at the mounted config and passes LLM credentials through. |
| `volumes` | Two entries (see below) | Config bind-mount + data bind-mount. |

`docker compose up -d` runs this service (the ingester) alongside `board`.
A one-shot command overrides `command:` at runtime — e.g.
`docker compose run robotsix-auto-mail probe`.

### Volumes

| Volume | Type | Purpose |
|---|---|---|
| `./config:/home/mailbot/config` | Bind-mount | Makes host config files available inside the container without a build. |
| `./.mail_data:/home/mailbot/.data` | Bind-mount | Persists the SQLite database in the project dir (git-ignored), at the container's default store location. |

### `services.board`

The `board` service runs the same image but starts the web server:

| Key | Value | Why |
|---|---|---|
| `command` | `serve --port ${BOARD_PORT:-8078}` | Starts the web server as a daemon. |
| `restart` | `on-failure` | Restarts if the process crashes. |
| `ports` | `"${BOARD_PORT:-8078}:${BOARD_PORT:-8078}"` | Maps the board port to the host so browsers can reach it. |
| `environment` | `MAIL_CONFIG_PATH: /home/mailbot/config/mail.local.yaml` | Same config as the ingester. |
| `volumes` | Same as the ingester | Shares `./.mail_data` so the ingester and board see the same database. |

There is no `stdin_open` or `tty` — the board is a daemon, not an
interactive process.

---

## Production deployment

### Build the production image

The same `Dockerfile` that works for local development also targets
production — its final stage is already a slim, non-root production image:

```sh
docker compose build
```

For a versioned, registry-ready build:

```sh
docker build -t registry.example.com/robotsix-auto-mail:v1.0.0 .
```

### Tag and push

```sh
docker tag robotsix-auto-mail:latest registry.example.com/robotsix-auto-mail:v1.0.0
docker push registry.example.com/robotsix-auto-mail:v1.0.0
```

### Run on a production host

The same `docker compose run` pattern works — just make sure `config/` is
populated with the production credentials and the image is pulled:

```sh
# On the production host, with config/mail.local.yaml in place:
docker compose run robotsix-auto-mail probe
docker compose run robotsix-auto-mail ingest
```

If you are not using Compose on the production host, replicate the setup
with a plain `docker run`:

```sh
docker run --rm \
  -v "$(pwd)/config:/home/mailbot/config" \
  -v "$(pwd)/.mail_data:/home/mailbot/.data" \
  -e MAIL_CONFIG_PATH=/home/mailbot/config/mail.local.yaml \
  registry.example.com/robotsix-auto-mail:v1.0.0 \
  probe
```

### What the entrypoint does

Before the Python interpreter starts, [`entrypoint.sh`](../entrypoint.sh)
validates that either:

- All four `MAIL_*` environment variables are set, **or**
- `MAIL_CONFIG_PATH` points to a readable config file.

If neither condition is met, the script prints a clear error message to
`stderr` and exits with code `1`.  This means config failures surface
immediately — no Python traceback, no mysterious `KeyError` deep in the
config loader.

The entrypoint also supports optional `envsubst` templating: if `envsubst`
is available in the image and a config file is in use, the file is run
through `envsubst` before the Python CLI sees it.  If `envsubst` is not
present (it usually isn't in the slim image), the raw config file is used
as-is — this is not an error.

---

## Updating a deployment

1.  **Pull the latest code:**

    ```sh
    git pull
    ```

2.  **Rebuild the image:**

    ```sh
    docker compose build
    ```

3.  **Run as normal — the next invocation picks up the new image:**

    ```sh
    docker compose run robotsix-auto-mail ingest
    ```

Because CLI invocations are one-shot, there is no zero-downtime concern
for `probe`, `ingest`, or `board`.  Each `docker compose run` creates a
fresh container from the current image.  If the web board daemon is
running (`docker compose up board`), restart it after a rebuild:
`docker compose up -d board` (or `docker compose restart board`).

### Full reset (including database)

If you want to wipe the SQLite database and start fresh:

```sh
docker compose down
rm -rf ./.mail_data
```

Because the database is a host bind-mount (not a named volume), removing the
`./.mail_data` directory is what clears it — `docker compose down -v` will
not.  The next `ingest` re-creates the database from scratch and fetches all
messages from the watermark baseline.

---

## Troubleshooting / FAQ

### "Missing required configuration"

```text
Missing required configuration.

Provide either:
  • All four MAIL_* environment variables:
      MAIL_IMAP_HOST, MAIL_SMTP_HOST, MAIL_USERNAME, MAIL_PASSWORD
  • A YAML config file via MAIL_CONFIG_PATH
```

The entrypoint validated config before launching Python and found neither
environment variables nor a readable config file.

**Diagnose:**

```sh
# Check that the config file exists and has content
cat config/mail.local.yaml

# Check that the bind-mount is working
docker compose run robotsix-auto-mail ls -l /home/mailbot/config/mail.local.yaml
```

**Fix:**  ensure `config/mail.local.yaml` exists and is readable, **or** set
all four `MAIL_*` env vars.  If using env vars, pass them explicitly:

```sh
docker compose run -e MAIL_IMAP_HOST=imap.example.com \
  -e MAIL_SMTP_HOST=smtp.example.com \
  -e MAIL_USERNAME=user@example.com \
  -e MAIL_PASSWORD=your-password \
  robotsix-auto-mail probe
```

---

### "Config file not found: /home/mailbot/config/mail.local.yaml"

The entrypoint found `MAIL_CONFIG_PATH` set but could not read the file at
that path inside the container.

**Diagnose:**

```sh
# Does the file exist on the host?
ls -l config/mail.local.yaml

# Is the bind-mount working?
docker compose run robotsix-auto-mail ls -la /home/mailbot/config/
```

**Fix:**  create the file (`cp config/mail.local.example.yaml config/mail.local.yaml`)
or verify the bind-mount isn't being shadowed by another volume definition.

---

### IMAP / SMTP connectivity failures

`robotsix-auto-mail` exposes **no ports** — there is no local port conflict.

If `probe` fails with a connection error, the remote mail server is
unreachable from the container.  Possible causes:

- Firewall or VPN blocking outbound IMAP (993) / SMTP (587).
- Incorrect `imap.host` or `smtp.host` in `config/mail.local.yaml`.

**Diagnose:**

```sh
# Run probe as first step — it gives targeted error messages
docker compose run robotsix-auto-mail probe
```

**Fix:**  verify the hostnames and ports in your config.  Check that the
host running Docker can reach those hosts on the configured ports.

---

### Volume permissions

The container runs as `mailbot` (UID 1000) and writes the database into the
bind-mounted `./.mail_data`.  On the host the files will be owned by UID 1000;
if your host user is not UID 1000 you may need to adjust ownership, and
overriding the user (e.g. `docker compose run --user root`) can leave files
that future runs as `mailbot` cannot read.

**Diagnose:**

```sh
# See what user the container runs as
docker compose run robotsix-auto-mail whoami

# Inspect data ownership
docker compose run robotsix-auto-mail ls -la /home/mailbot/.data/
ls -la ./.mail_data/      # on the host
```

**Fix:**  do not override `--user` unless you have a specific reason.  If
the data was created under a different UID, reset it:

```sh
docker compose down
rm -rf ./.mail_data
docker compose run robotsix-auto-mail ingest   # re-creates the db as mailbot
```

---

### "envsubst: command not found" (or envsubst is silently skipped)

`entrypoint.sh` uses `envsubst` for optional config-file templating **only
if it is available**.  The slim Python image does not include `gettext`
(which provides `envsubst`), so templating is silently skipped.

This is **not an error** — the raw config file is used as-is.  If you need
`envsubst` (e.g. to inject secrets at runtime), install `gettext` in a
custom image or use a different base.  The entrypoint was designed to
degrade gracefully here.

---

### "database is locked" / SQLite database corruption

If two `ingest` commands run concurrently against the same `./.mail_data`
database, SQLite may return `database is locked`.  The tool does not use
WAL mode by default, so concurrent writers will contend.

**Fix:**  do not run concurrent `ingest` commands.  The tool is designed
for sequential, single-writer access.  If you have scheduled (cron) runs,
ensure the previous run has completed before starting the next:

```sh
# Example cron wrapper — flock prevents overlap
flock -n /tmp/mail-ingest.lock docker compose run robotsix-auto-mail ingest
```

If the database is already corrupted, reset it:

```sh
docker compose down -v
docker compose run robotsix-auto-mail ingest
```

---

## Further reading

- **[docs/connecting.md](connecting.md)** — full config key reference,
  precedence rules, and the `probe`/`board` commands.
- **[docs/ingestion.md](ingestion.md)** — ingestion pipeline, schema,
  idempotency guarantees, and `ingest` CLI usage.
- **[README.md](../README.md)** — project overview, layout, and status.
