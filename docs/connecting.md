# Connecting

`robotsix-auto-mail` needs IMAP and SMTP connection parameters. They can be
supplied via **YAML config files**, a **TOML config file**, **environment
variables**, or a **combination**.

The recommended approach for operators is the **YAML defaults + local
overrides** pattern, which ships with the Docker Compose setup described
below.

New users can also run `robotsix-auto-mail detect` to auto-generate config
from just an email address — see [Auto-detection with
`detect`](#auto-detection-with-detect).

## Quick start — Docker Compose (recommended)

The project includes a `docker-compose.yml` that builds the container and
mounts configuration without rebuilding the image.

```sh
# 1. Create your local config from the template
cp config/mail.local.example.yaml config/mail.local.yaml

# 2. Edit it with your real credentials
$EDITOR config/mail.local.yaml

# 3. Build the image
docker compose build

# 4. Run commands via `docker compose run`
docker compose run robotsix-auto-mail probe
docker compose run robotsix-auto-mail ingest
docker compose run robotsix-auto-mail board
```

### How it works

- `config/mail.defaults.yaml` *(tracked in git)* ships every config field
  at its default value.
- `config/mail.local.yaml` *(git-ignored)* contains only the fields the
  operator wants to override — typically `imap.host`, `smtp.host`,
  `auth.username`, and `auth.password` (optional — can come from `secrets.yaml`).
- The config loader deep-merges the two files: defaults first, local on top.
- The `./config` directory is bind-mounted read-only into the container at
  `/home/mailbot/config`, so editing `config/mail.local.yaml` on the host
  is picked up immediately — no rebuild needed.
- The `MAIL_CONFIG_PATH` environment variable is set to
  `/home/mailbot/config/mail.local.yaml` by `docker-compose.yml`.
- The mail database persists in a named Docker volume (`mail_data`).

## Auto-detection with `detect`

Instead of manually researching and writing config, you can auto-generate it
from just an email address. The `detect` command uses an LLM to look up the
correct IMAP/SMTP settings for your provider.

### Setup

```sh
# Install the optional LLM dependency
pip install robotsix-auto-mail[llm]

# Set your OpenRouter API key (required)
export LLM_API_KEY=sk-or-v1-…

# Optional: choose a different model (default: deepseek/deepseek-v4-flash)
export LLM_MODEL=anthropic/claude-3-haiku
```

### Minimal usage

```sh
robotsix-auto-mail detect user@gmail.com
```

This auto-detects settings, writes `config/mail.local.yaml` (without the
password), prompts for the password interactively, and writes
`config/secrets.yaml` alongside it.

### Scripting usage

```sh
robotsix-auto-mail detect user@gmail.com \
    --password "app-password" \
    --output config/mail.local.yaml
```

### Options

| Option | Required | Default | Purpose |
|---|---|---|---|
| `EMAIL` (positional) | yes | – | Email address to detect settings for |
| `--password` | no | (prompted) | Password to write to `secrets.yaml` |
| `--output PATH` | no | `config/mail.local.yaml` | Write mail config to this path |
| `--stdout` | no | – | Print config to stdout instead of writing to file |

### Docker invocation

```sh
# Set your OpenRouter API key
export LLM_API_KEY=sk-or-v1-…

# Detect provider settings and write config
docker compose run robotsix-auto-mail detect user@gmail.com

# Verify connectivity with the generated config
docker compose run robotsix-auto-mail probe
```

The `detect` command writes `config/mail.local.yaml` and (when a password is
supplied) `config/secrets.yaml` into the bind-mounted `./config` directory on
the host.  No image rebuild is needed — the files are available immediately.

The `--password` flag works the same as in native mode.  When omitted, an
interactive prompt appears (requires a TTY — use `docker compose run` without
`-T`).

### Caveats

- **LLM output should be verified.** Run `robotsix-auto-mail probe` after
  generating config to confirm connectivity.
- The `detect` command does **not** connect to any mail server — it is
  purely a config-file generator.
- For users who prefer manual config, the traditional approach (editing
  `config/mail.local.yaml` by hand) is unaffected and fully supported.

## Configuration keys

### YAML config (recommended)

#### Defaults file (`config/mail.defaults.yaml`)

```yaml
imap:
  host: ""          # required — operator must supply in mail.local.yaml
  port: 993
  tls_mode: direct-tls
  folder: INBOX

smtp:
  host: ""          # required — operator must supply in mail.local.yaml
  port: 587
  tls_mode: starttls

auth:
  username: ""      # required — operator must supply in mail.local.yaml
  password: ""      # optional — set via secrets.yaml or MAIL_PASSWORD env var

store:
  path: /home/mailbot/data/mail.db
```

#### Local overrides (`config/mail.local.yaml`)

```yaml
imap:
  host: imap.example.com
  # port: 993
  # tls_mode: direct-tls

smtp:
  host: smtp.example.com
  # port: 587
  # tls_mode: starttls

auth:
  username: user@example.com
  password: ""  # password stored in config/secrets.yaml

# store:
#   path: /home/mailbot/data/mail.db
```

| Key | Required | Default | Purpose |
|---|---|---|---|
| `imap.host` | yes | – | IMAP server hostname |
| `imap.port` | no | `993` | IMAP server port |
| `imap.tls_mode` | no | `"direct-tls"` | IMAP TLS mode |
| `imap.folder` | no | `"INBOX"` | IMAP mailbox folder name |
| `smtp.host` | yes | – | SMTP server hostname |
| `smtp.port` | no | `587` | SMTP server port |
| `smtp.tls_mode` | no | `"starttls"` | SMTP TLS mode |
| `auth.username` | yes | – | Login username (typically the full email address) |
| `auth.password` | no | – | Login password (optional — can come from `secrets.yaml`) |
| `store.path` | no | `"mail.db"` | Filesystem path for the SQLite database |

### TOML config file (alternative)

The TOML file mirrors the same settings under three sections:

```toml
[imap]
host = "imap.example.com"
port = 993
tls_mode = "direct-tls"

[smtp]
host = "smtp.example.com"
port = 587
tls_mode = "starttls"

[auth]
username = "user@example.com"
password = ""  # password stored in config/secrets.yaml
```

| Key | Required | Default | Purpose |
|---|---|---|---|
| `imap.host` | yes | – | IMAP server hostname |
| `imap.port` | no | `993` | IMAP server port |
| `imap.tls_mode` | no | `"direct-tls"` | IMAP TLS mode |
| `smtp.host` | yes | – | SMTP server hostname |
| `smtp.port` | no | `587` | SMTP server port |
| `smtp.tls_mode` | no | `"starttls"` | SMTP TLS mode |
| `auth.username` | yes | – | Login username |
| `auth.password` | no | – | Login password (optional — can come from `secrets.yaml`) |

A commented template is available at `config/mail.example.toml`.

### Secrets file (`config/secrets.yaml`)

The mail password can be stored in a separate `config/secrets.yaml` file
instead of embedding it in `config/mail.local.yaml`.  This keeps credentials
isolated from general configuration, making it safer to share or back up
your mail config.

```sh
cp config/secrets.example.yaml config/secrets.yaml
$EDITOR config/secrets.yaml
```

```yaml
# config/secrets.yaml
mail_password: "your-app-password-here"
```

- `config/secrets.yaml` is **git-ignored** — it will never be committed.
- The path can be overridden via the `MAIL_SECRETS_FILE` environment variable.
- If the file is missing or has an empty password, the password from
  `mail.local.yaml` (or `MAIL_PASSWORD`) is used as a fallback.
- The password value is **redacted** in logs and debug output.

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MAIL_IMAP_HOST` | yes | – | IMAP server hostname |
| `MAIL_SMTP_HOST` | yes | – | SMTP server hostname |
| `MAIL_USERNAME` | yes | – | Login username (typically the full email address) |
| `MAIL_PASSWORD` | yes | – | Login password |
| `MAIL_IMAP_PORT` | no | `993` | IMAP server port |
| `MAIL_IMAP_TLS_MODE` | no | `direct-tls` | TLS negotiation for IMAP — one of `direct-tls`, `starttls`, `none` |
| `MAIL_SMTP_PORT` | no | `587` | SMTP server port |
| `MAIL_SMTP_TLS_MODE` | no | `starttls` | TLS negotiation for SMTP — one of `starttls`, `direct-tls`, `none` |
| `MAIL_IMAP_FOLDER` | no | `INBOX` | IMAP mailbox folder name |
| `MAIL_DB_PATH` | no | `mail.db` | Filesystem path for the SQLite database |
| `MAIL_CONFIG_PATH` | no | `config/mail.toml` | Filesystem path to the config file (TOML or YAML) |
| `MAIL_DEFAULTS_PATH` | no | – | Filesystem path to the YAML defaults file (auto-detected alongside `MAIL_CONFIG_PATH` when unset) |

**TLS modes**

| Mode | Behaviour |
|---|---|
| `direct-tls` | TLS from the first byte, no plaintext negotiation (IMAP port 993, SMTP port 465) |
| `starttls` | Plain connection upgraded to TLS via STARTTLS (IMAP port 143, SMTP port 587) |
| `none` | No TLS at all — **insecure, for local development only** |

## Precedence rules

`mail.load()` resolves configuration in this order:

1. **Environment variables are evaluated first.** If all four required
   variables (`MAIL_IMAP_HOST`, `MAIL_SMTP_HOST`, `MAIL_USERNAME`,
   `MAIL_PASSWORD`) are set, they are used and file config is ignored.
2. **File fallback.** If only required fields are missing from the
   environment (no invalid values), `load()` reads the config file at
   `MAIL_CONFIG_PATH` (default: `config/mail.toml`). If the path ends with
   `.yaml` or `.yml` it is parsed as YAML; otherwise as TOML.
3. **Defaults merge (YAML only).** If a defaults file is set via
   `MAIL_DEFAULTS_PATH`, or a file named `mail.defaults.yaml` exists
   alongside the main config, it is loaded (without validation) and
   deep-merged — the main config overrides the defaults field-by-field.
4. **Env-override merge.** Every environment variable that *is* set is
   then re-applied on top of the file values. This lets you keep shared
   settings in a config file while overriding just the password via
   `MAIL_PASSWORD`, for example.
5. **Secrets application.** If `config/secrets.yaml` exists with a
   non-empty `mail_password`, it overrides the password from the config
   file or environment variables.

If any environment variable has an *invalid* value (e.g. a non-integer
port), the error is raised immediately — the file fallback is skipped so
your typo is not silently swallowed.

## Example setups

### Docker Compose with YAML (recommended)

```yaml
# config/mail.local.yaml (git-ignored)
imap:
  host: imap.mail.example.com
  port: 993
  tls_mode: direct-tls

smtp:
  host: smtp.mail.example.com
  port: 587
  tls_mode: starttls

auth:
  username: user@mail.example.com
  password: ""  # password stored in config/secrets.yaml
```

```sh
docker compose run robotsix-auto-mail probe
```

### Generic IMAP + SMTP (.env)

```sh
# .env
MAIL_IMAP_HOST=imap.mail.example.com
MAIL_IMAP_PORT=993
MAIL_IMAP_TLS_MODE=direct-tls
MAIL_SMTP_HOST=smtp.mail.example.com
MAIL_SMTP_PORT=587
MAIL_SMTP_TLS_MODE=starttls
MAIL_USERNAME=user@mail.example.com
MAIL_PASSWORD=your-app-password-here
```

### Generic IMAP + SMTP (TOML)

```toml
# config/mail.toml
[imap]
host = "imap.mail.example.com"
port = 993
tls_mode = "direct-tls"

[smtp]
host = "smtp.mail.example.com"
port = 587
tls_mode = "starttls"

[auth]
username = "user@mail.example.com"
password = ""  # password stored in config/secrets.yaml
```

## The `probe` command

Once your configuration is in place, run the probe to verify connectivity:

```sh
$ robotsix-auto-mail probe
```

### What it does

`probe` loads the mail configuration, then:

- Opens an authenticated IMAP connection and prints the server greeting,
  capability list, and mailbox folder listing.
- Opens an authenticated SMTP connection and prints the EHLO response
  and ESMTP feature set.

No email is read or sent — this is a read-only diagnostic command.

### Representative output

```text

IMAP Probe
------------------------------------------------------------
Greeting: * OK [CAPABILITY IMAP4rev1 …] IMAP server ready
Capabilities:
  - IMAP4rev1
  - STARTTLS
  - AUTH=PLAIN
  - …

Folders:
  INBOX
    attributes: (none)
    delimiter:  /
  Drafts
    attributes: \HasNoChildren
    delimiter:  /
  Sent
    attributes: \HasNoChildren
    delimiter:  /

SMTP Probe
------------------------------------------------------------
EHLO response: 250-smtp.mail.example.com
250-PIPELINING
250-SIZE 35651584
250-STARTTLS
250-AUTH PLAIN LOGIN
250-ENHANCEDSTATUSCODES
250 8BITMIME

ESMTP features:
  AUTH: PLAIN LOGIN
  ENHANCEDSTATUSCODES: (empty)
  PIPELINING: (empty)
  SIZE: 35651584
  STARTTLS: (empty)
```

Exit code is `0` when both probes succeed, `1` when either fails.

## The `ingest` command

The ingestion pipeline is documented separately — see
[docs/ingestion.md](ingestion.md) for the full ingestion model, datastore
schema, idempotency guarantees, configuration, and CLI usage.

## The `board` command

Once mail has been ingested (see [The `ingest` command](#the-ingest-command)),
view it with the read-only board:

```sh
$ robotsix-auto-mail board
```

`board` opens the local SQLite datastore and prints an "Inbox" header followed
by a rendered card for each stored message.  Each card shows:

- `From:` — the sender's address
- `Subject:` — the message subject (or `(no subject)` when blank)
- `Date:` — formatted as `YYYY-MM-DD HH:MM` (UTC)
- a body preview — the first 150 characters of the plain-text body, truncated
  with `…` when longer (or `(no body)` when no plain-text body is available)

Cards are separated by a 60-character `-` rule.  A message count line follows
the last card.

When the inbox is empty the command prints `Your inbox is empty.`.

The command is read-only — it never modifies the database or contacts a mail
server.

### Representative output

```text

Inbox
------------------------------------------------------------
From:    alice@example.com
Subject: Hello
Date:    2025-06-01 14:30

Just checking in!
------------------------------------------------------------
From:    bob@example.com
Subject: Meeting notes
Date:    2025-06-02 09:15

Here are the notes from this morning's standup.  We agreed to
move the deadline to Friday and Alice will follow up on the…
2 message(s)
```

### Empty inbox

```text

Inbox
------------------------------------------------------------
Your inbox is empty.
```

Exit code is `0` on success, `1` when configuration cannot be loaded.

## The `serve` command

For a persistent, browser-based view of ingested mail, use the `serve`
subcommand.  This starts a long-running HTTP server that hosts a read-only
kanban board at `/board`.

```sh
$ robotsix-auto-mail serve
# Listening on http://0.0.0.0:8080/board
```

### Options

| Option | Default | Purpose |
|---|---|---|
| `--port` | `8080` | Port to listen on |

### The board page

Open `http://localhost:<port>/board` in a browser.  The page shows ingested
mail in **four columns** — Inbox, Triaging, Done, Archive — each with a card
count badge.  Every mail card has a **Move** dropdown that lets you change
the card's status column via `POST /move`.  The board is the interface: no
separate client is needed.

The page includes `<meta http-equiv="refresh" content="30">`, so the board
auto-refreshes every 30 seconds.

### Contrast with `board`

| | `board` | `serve` |
|---|---|---|
| **Output** | Plain text to stdout | HTML page in a browser |
| **Layout** | Single Inbox column | Four columns (Inbox, Triaging, Done, Archive) |
| **Lifetime** | One-shot — prints and exits | Persistent HTTP daemon |
| **Interaction** | Read-only | Move dropdowns (`POST /move`) |
| **Refresh** | Manual (re-run the command) | Automatic (30-second meta refresh) |

Both commands read from the same local SQLite datastore — no configuration
changes are needed to switch between them.