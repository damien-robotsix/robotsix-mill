# Connecting

`robotsix-auto-mail` needs IMAP and SMTP connection parameters. They are
resolved through a single, predictable cascade:

**built-in defaults → a YAML config file → environment variables.**

Each layer overrides the one before it, field by field. You can supply
everything in the YAML file, everything via `MAIL_*` environment variables,
or mix the two (e.g. host/username in the file, password via `MAIL_PASSWORD`).

New users can also run `robotsix-auto-mail detect` to auto-generate the YAML
file from just an email address — see [Auto-detection with
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

- `config/mail.local.yaml` *(git-ignored)* holds your settings — typically
  `imap.host`, `smtp.host`, `auth.username`, and `auth.password`. Any field
  you omit falls back to its built-in default.
- The `./config` directory is bind-mounted into the container at
  `/home/mailbot/config`, so editing `config/mail.local.yaml` on the host
  is picked up immediately — no rebuild needed.
- The `MAIL_CONFIG_PATH` environment variable is set to
  `/home/mailbot/config/mail.local.yaml` by `docker-compose.yml`.
- The mail database persists in a named Docker volume (`mail_data`).

## Auto-detection with `detect`

Instead of manually researching and writing config, you can auto-generate it
from just an email address. The `detect` command resolves the IMAP/SMTP
settings through a ladder, most authoritative first:

1. **Published autoconfig** — the Mozilla ISPDB and the domain's own
   `autoconfig.<domain>` endpoint.
2. **MX records** — a DNS-over-HTTPS lookup identifies the hosting provider
   from the domain's mail servers (e.g. `*.mail.ovh.net` → OVH), mapped to
   that provider's known IMAP/SMTP settings.
3. **LLM** — only if the first two miss; the MX hostnames are passed in as a
   hint so it identifies the provider rather than guessing blindly.

After writing the config, `detect` verifies it by connecting (see below), and
refines on failure. The LLM step needs the optional `[llm]` dependency and an
API key; autoconfig and MX detection do not.

### Setup

```sh
# Install the optional LLM dependency
pip install robotsix-auto-mail[llm]

# Set your OpenRouter API key (required)
export LLM_API_KEY=sk-or-v1-…

# Optional: choose a different model (default: deepseek/deepseek-v4-flash)
export LLM_MODEL=anthropic/claude-3-haiku
```

Instead of environment variables, you can put these in the `llm:` section of
`config/mail.local.yaml` (see [Configuration keys](#configuration-keys)). The
LLM credentials resolve through the same cascade as everything else — the
`LLM_API_KEY` / `LLM_MODEL` environment variables override the file. The same
settings will be reused by future LLM-assisted mail processing, not just
`detect`.

### Minimal usage

```sh
robotsix-auto-mail detect user@gmail.com
```

This auto-detects settings, prompts for the password interactively, writes a
single `config/mail.local.yaml` with the password included, and then verifies
the settings by connecting to the IMAP and SMTP servers (the same check as the
`probe` command). Pass `--no-verify` to skip that connection check.

Re-running `detect` over an existing file updates the mail fields but
preserves the `llm:` section, so your API key is not lost.

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
| `--password` | no | (prompted) | Password to write into the config file |
| `--output PATH` | no | `config/mail.local.yaml` | Write mail config to this path |
| `--stdout` | no | – | Print config to stdout instead of writing to file (no verification) |
| `--no-verify` | no | – | Skip the post-write IMAP/SMTP connection check |

### Docker invocation

```sh
# Set your OpenRouter API key (or put it in the config file's llm: section)
export LLM_API_KEY=sk-or-v1-…

# Detect provider settings, write config, and verify connectivity —
# all in one step (prompts for the password; uses the run TTY).
docker compose run robotsix-auto-mail detect user@gmail.com
```

The `detect` command writes `config/mail.local.yaml` (with the password
included when one is supplied) into the bind-mounted `./config` directory on
the host.  No image rebuild is needed — the file is available immediately.

The `--password` flag works the same as in native mode.  When omitted, an
interactive prompt appears (requires a TTY — use `docker compose run` without
`-T`).

### Caveats

- **LLM output can be wrong.** That is exactly why `detect` verifies by
  connecting after writing the config. If verification fails, edit
  `config/mail.local.yaml` and re-run `probe`.
- With `--no-verify` (or `--stdout`, which never writes), no connection is
  made — `detect` is then purely a config-file generator, so run
  `robotsix-auto-mail probe` yourself afterwards.
- For users who prefer manual config, the traditional approach (editing
  `config/mail.local.yaml` by hand) is unaffected and fully supported.

## Configuration keys

### YAML config file (`config/mail.local.yaml`)

Copy `config/mail.local.example.yaml` and fill in your values. Any field you
omit falls back to its built-in default.

```yaml
imap:
  host: imap.example.com
  # port: 993
  # tls_mode: direct-tls
  # folder: INBOX

smtp:
  host: smtp.example.com
  # port: 587
  # tls_mode: starttls

auth:
  username: user@example.com
  password: ""  # set your password here, or via the MAIL_PASSWORD env var

# store:
#   path: .data/mail.db

# llm:
#   api_key: sk-or-v1-…   # or via the LLM_API_KEY env var
#   model: deepseek/deepseek-v4-flash
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
| `auth.password` | no | – | Login password (may instead be supplied via `MAIL_PASSWORD`) |
| `store.path` | no | `".data/mail.db"` | Filesystem path for the SQLite database |
| `llm.api_key` | no | – | LLM provider API key for `detect` / mail processing (may instead be supplied via `LLM_API_KEY`) |
| `llm.model` | no | `"deepseek/deepseek-v4-flash"` | LLM model name |

The `auth.password` and `llm.api_key` values are **redacted** in logs and
debug output regardless of how they are supplied.

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
| `MAIL_DB_PATH` | no | `.data/mail.db` | Filesystem path for the SQLite database |
| `MAIL_CONFIG_PATH` | no | `config/mail.local.yaml` | Filesystem path to the YAML config file |
| `LLM_API_KEY` | no | – | LLM provider API key (overrides `llm.api_key`); required for `detect` |
| `LLM_MODEL` | no | `deepseek/deepseek-v4-flash` | LLM model name (overrides `llm.model`) |

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
   `MAIL_PASSWORD`) are set, they are used and the file is ignored.
2. **File fallback.** If only required fields are missing from the
   environment (no invalid values), `load()` reads the YAML config file at
   `MAIL_CONFIG_PATH` (default: `config/mail.local.yaml`).
3. **Env-override merge.** Every environment variable that *is* set is
   then re-applied on top of the file values. This lets you keep shared
   settings in the config file while overriding just the password via
   `MAIL_PASSWORD`, for example.

Fields absent from both the file and the environment fall back to their
built-in defaults.

If any environment variable has an *invalid* value (e.g. a non-integer
port), the error is raised immediately — the file fallback is skipped so
your typo is not silently swallowed.

**LLM settings** (`llm.api_key` / `llm.model`) follow the same rule —
`LLM_API_KEY` / `LLM_MODEL` override the file's `llm:` section. The `detect`
command resolves them on their own (via `load_llm()`) so it works before the
mail fields are filled in.

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
  password: your-app-password-here
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

### Config file + password from the environment

Keep non-secret settings in the YAML file and supply only the password via
`MAIL_PASSWORD` (which overrides `auth.password`):

```yaml
# config/mail.local.yaml (git-ignored)
imap:
  host: imap.mail.example.com
smtp:
  host: smtp.mail.example.com
auth:
  username: user@mail.example.com
  # password omitted — supplied via MAIL_PASSWORD below
```

```sh
export MAIL_PASSWORD=your-app-password-here
robotsix-auto-mail probe
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