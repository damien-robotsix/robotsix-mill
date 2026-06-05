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
- The mail database persists in `./.mail_data` on the host (bind-mounted,
  git-ignored).

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
refines on failure. The LLM step needs a `pydantic-ai` installation and an
API key; autoconfig and MX detection do not.

### Setup

```sh
# Installs dev dependencies (incl. pydantic-ai) from the committed uv.lock,
# so you get the exact same resolved versions as CI. The dev tooling lives
# in the `dev` extra, which `--extra dev` pulls in. After changing
# dependencies in pyproject.toml, run `uv lock` and commit the updated
# uv.lock.
uv sync --extra dev

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

# archive:
#   root: robotsix-mail-archive
#   enabled: true

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
| `ingest.interval_minutes` | no | `15` | Minutes between automatic ingest cycles (`ingest --watch`) |
| `archive.root` | no | `"robotsix-mail-archive"` | Root folder for the self-managed archive structure |
| `archive.enabled` | no | `true` | Whether to create/manage the archive folder structure |
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
| `MAIL_INGEST_INTERVAL` | no | `15` | Minutes between automatic ingest cycles (`ingest --watch`) |
| `MAIL_ARCHIVE_ROOT` | no | `robotsix-mail-archive` | Root folder for the self-managed archive structure |
| `MAIL_ARCHIVE_ENABLED` | no | `true` | Whether to create/manage the archive folder structure |
| `MAIL_CONFIG_PATH` | no | `config/mail.local.yaml` | Filesystem path to the YAML config file |
| `LLM_API_KEY` | no | – | LLM provider API key (overrides `llm.api_key`); required for `detect` |
| `LLM_MODEL` | no | `deepseek/deepseek-v4-flash` | LLM model name (overrides `llm.model`) |

**TLS modes**

| Mode | Behaviour |
|---|---|
| `direct-tls` | TLS from the first byte, no plaintext negotiation (IMAP port 993, SMTP port 465) |
| `starttls` | Plain connection upgraded to TLS via STARTTLS (IMAP port 143, SMTP port 587) |
| `none` | No TLS at all — **insecure, for local development only** |

### Self-managed archive structure

`robotsix-auto-mail` manages its own archive folder hierarchy, rooted at
`archive.root` (default `robotsix-mail-archive`). On the first ingest a quick
LLM call proposes an appropriate layout based on the mailbox's existing
folders; the resulting folder list is then persisted in the SQLite
`watermark` table under the key `archive_structure` and reused verbatim on
every subsequent run — no folders are listed, no LLM is called, and nothing
is recreated.

Set `archive.enabled` (env `MAIL_ARCHIVE_ENABLED`) to `false` to disable
archive management entirely: `setup_archive` is never called, no watermark is
written, and ingestion proceeds normally. Re-enabling it later runs setup on
the next ingest (since the watermark was never set).

Because the structure is remembered after the first run, **changing
`archive.root` afterwards does not move or recreate any folders** — the
persisted `archive_structure` watermark short-circuits subsequent runs. A new
root only takes effect on a fresh run that has no watermark yet.

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

### The `/config-sync` endpoint

In addition to the board page, the server hosts a `POST /config-sync` endpoint
that runs the optional LLM drift advisory agent and returns structured JSON.
This is useful for external schedulers (cron, systemd timer) or monitoring
systems that want to check for configuration drift on demand.

#### Request

```sh
curl -X POST http://localhost:8080/config-sync
```

No request body is required.

#### Response on success (HTTP 200)

Content-Type: `application/json`

```json
{
  "proposals": [
    {
      "title": "imap_folder default mismatch",
      "body": "Docs say INBOX.All but the dataclass default is INBOX.",
      "affected_field": "imap_folder",
      "confidence": "high"
    }
  ]
}
```

When no drift is detected, the `proposals` array is empty.

#### Error responses (HTTP 503)

If the LLM extra (`pydantic-ai`) is not installed or the agent encounters an
error (e.g., missing API key):

```json
{"error": "Config-sync advisory requires the optional LLM extra, which is not installed"}
```

#### Requirements

The endpoint requires the same setup as the CLI `config-sync` command:
- The `pydantic-ai` package (install via `pip install robotsix-auto-mail[dev]`)
- An LLM API key (via `LLM_API_KEY` env or `llm.api_key` in config)

The endpoint applies dedup filtering by default (consulting the persisted
ledger in the SQLite `watermark` table), so previously-seen drift proposals
are suppressed automatically.

## The `config-sync` command

For operators who want to audit their configuration, the `config-sync`
subcommand runs an **optional, advisory LLM agent** that examines four
configuration surfaces and proposes human-readable drift corrections:

```sh
$ robotsix-auto-mail config-sync
```

This is an **advisory tool only** — it does not replace the deterministic
`scripts/config/check_config_sync.py` CI gate (which is fast and free).
A successful run exits code `0` even when drift is found, so it won't break
operator scripts.

### Advisory tool vs. the deterministic gate

`robotsix-auto-mail` checks configuration consistency at two distinct layers,
and they are **complementary** — neither replaces the other:

| | `scripts/config/check_config_sync.py` | `config-sync` advisory agent |
|---|---|---|
| **Role** | Authoritative CI / pre-commit gate | Optional operator-facing advisory tool |
| **Mechanism** | Deterministic, rule-based checks | LLM inspection of config surfaces |
| **Cost** | Fast and free (no LLM, no API key) | Requires `pydantic-ai` + an LLM API key |
| **Coverage** | Known, encoded drift patterns | *Unanticipated* drift the rules don't encode |
| **On drift** | Fails the build (blocks merge) | Reports proposals; still exits `0` |
| **When it runs** | Every commit / PR, automatically | On demand, when an operator chooses |

`scripts/config/check_config_sync.py` is the **source of truth**: it is the
fast, free, deterministic gate that blocks merges on known configuration drift,
and it runs automatically on every commit and pull request. The `config-sync`
LLM agent (CLI subcommand and `POST /config-sync` endpoint) is an **optional,
operator-facing advisory tool** that surfaces *unanticipated* drift patterns the
deterministic checker doesn't encode. Because a successful advisory run exits
`0` even when it reports drift, it **does not gate anything** — running
`config-sync` is never a substitute for the deterministic gate passing.

### When to use which

- **Rely on the deterministic gate for every commit and PR.** It is automatic,
  free, and authoritative — it is what actually keeps configuration surfaces in
  sync, and a green build means the known drift checks pass.
- **Reach for the advisory tool occasionally / on demand.** Good moments are
  after a large config refactor, when onboarding a new configuration surface, or
  on a periodic external schedule (e.g. a cron job hitting `POST /config-sync`)
  to catch drift the deterministic rules don't yet cover. Treat its proposals as
  hints to review, not as merge blockers.

### Options

| Option | Default | Purpose |
|---|---|---|
| `--api-key` | – | OpenRouter API key; overrides `LLM_API_KEY` env and config file |
| `--output-format` | `text` | Output format: `text` (human-readable) or `json` (machine-readable) |
| `--dedup` | – | Consult/update the dedup memory ledger to suppress previously-seen findings; requires a loadable config (for db path) |

### Requirements

The `config-sync` command requires:
- The `pydantic-ai` package (install via `pip install robotsix-auto-mail[dev]`)
- An LLM API key (via `--api-key`, `LLM_API_KEY` env, or `llm.api_key` in config)

When `--dedup` is **not** passed, the command does not require a full mail config
— it skips config loading and uses `conn=None`. When `--dedup` **is** passed,
it loads the config to retrieve `db_path` for the dedup ledger.

### The dedup memory ledger

Operators who run the advisory tool regularly would otherwise see the same drift
proposals on every run. The **dedup memory ledger** prevents that repeated noise:
it persists a fingerprint of every drift proposal that has already been surfaced,
stored in the SQLite `watermark` table under the key `config_sync_ledger`. On
subsequent runs, proposals whose fingerprints are already recorded (i.e. those
already seen, accepted, or rejected) are suppressed, so only genuinely new drift
is reported.

The two entry points apply the ledger differently, and this asymmetry is
intentional:

- The **CLI** applies dedup only when you pass `--dedup` (which requires a
  loadable config, since the ledger lives in the configured database).
- The **`POST /config-sync` endpoint** applies dedup **by default**, consulting
  and updating the ledger on every request — well suited to a periodic external
  scheduler that should only be alerted about previously-unseen drift.

#### Ledger state semantics

The ledger lives in the SQLite `watermark` table under the key
`config_sync_ledger`, stored as a single JSON object keyed by a per-finding
fingerprint:

```json
{
  "<fingerprint>": {
    "title": "imap_folder default mismatch",
    "affected_field": "imap_folder",
    "state": "pending"
  }
}
```

- **Fingerprint basis.** Each `<fingerprint>` is a SHA-256 hash derived from a
  proposal's **stable identity fields only** — `affected_field` + `title`. The
  `body` is deliberately **excluded** so that a reworded body (the LLM rephrases
  its prose between runs) does not escape dedup and resurface the same finding
  as new.
- **States.** An entry's `state` is one of `pending`, `accepted`, or `rejected`.
  All three suppress re-reporting equally — once a fingerprint is recorded in
  *any* state, that proposal is filtered out of future `--dedup` CLI runs and
  `POST /config-sync` responses.
- **First-seen proposals are recorded as `pending`** automatically. The
  `accepted` and `rejected` states are **reserved/internal**: they are set only
  by the internal `set_finding_state()` helper in
  `src/robotsix_auto_mail/config_sync.py` and are **not currently exposed**
  through any CLI flag or HTTP endpoint. Operators cannot set those states
  today; there is no merge/decline command. (Behavioural exposure, if ever
  wanted, would be a separate ticket.)

### Responding to drift proposals

The advisory agent only *reports* — it never edits config or files anything.
Acting on a proposal is the operator's job. For each `DriftProposal` in the
text or JSON output, look at its `title`, `body`, `affected_field`, and
`confidence`, then decide:

- **Real divergence → reconcile the authoritative surfaces.** If the proposal
  describes a genuine inconsistency, fix it by editing the surfaces the
  deterministic checker compares so that
  `python scripts/config/check_config_sync.py` goes green again. Those surfaces
  are:
  - the `MailConfig` dataclass (`src/robotsix_auto_mail/config/__init__.py`),
  - the YAML template (`config/mail.local.example.yaml`),
  - `.env.example`, and
  - the two config tables in this file — "YAML config file" and "Environment
    variables".

  The `FIELD_TO_YAML` / `FIELD_TO_ENV` mappings in
  `scripts/config/check_config_sync.py` are the **source of truth** for which
  YAML key and environment variable each `MailConfig` field corresponds to;
  reconcile every surface to agree with them.
- **Intentional divergence → ignore the proposal.** If the reported difference
  is a deliberate design choice the deterministic rules simply don't model,
  treat the proposal as a false positive and do nothing — no code change is
  needed.

Either way, the dedup ledger suppresses an already-surfaced proposal on the
next `--dedup` CLI run or `POST /config-sync` request **regardless of your
decision**, because any recorded state (`pending` / `accepted` / `rejected`)
suppresses re-reporting.

#### Worked reconciliation example

Suppose an advisory run surfaces this proposal:

```text

Config Drift Advisory
------------------------------------------------------------

imap_folder documented value mismatch
  confidence: high
  affected field: imap_folder

The `MAIL_IMAP_FOLDER` row in the "Environment variables" table documents a
default of `INBOX.All`, but the MailConfig default for imap_folder is INBOX.
```

You confirm it is a **real drift** — the documented default no longer matches
the dataclass. Reconcile the affected surface(s), e.g. fix the `MAIL_IMAP_FOLDER`
row in the "Environment variables" table (and any other surface that disagrees,
such as `.env.example`) so the documented default reads `INBOX` again:

```text
| `MAIL_IMAP_FOLDER` | no | `INBOX` | IMAP mailbox folder name |
```

Then re-run the deterministic gate, which now exits `0`:

```sh
$ python scripts/config/check_config_sync.py
OK
$ echo $?
0
```

By contrast, if the proposal had flagged an **intentional** design choice the
deterministic rules don't encode — e.g. a deliberately commented-out optional
key — you would simply ignore it: no surface edit and no code change is needed.

### Representative text output

```text

Config Drift Advisory
------------------------------------------------------------

imap_folder default mismatch
  confidence: high
  affected field: imap_folder

Docs say INBOX.All but the dataclass default is INBOX.
```

When no drift is detected:

```text

Config Drift Advisory
------------------------------------------------------------
No config drift detected.
```

### JSON output

With `--output-format json`, the output is a single JSON object with a
`proposals` array (empty when no drift is found):

```json
{
  "proposals": [
    {
      "title": "imap_folder default mismatch",
      "body": "Docs say INBOX.All but the dataclass default is INBOX.",
      "affected_field": "imap_folder",
      "confidence": "high"
    }
  ]
}
```

Exit code is `0` on success (even with findings), `1` on error (missing API key,
`pydantic-ai` not installed, or surface read failure).

## The `triage` command

The `triage` subcommand runs an **LLM-driven inbox classifier** that reads
each ingested mail record and assigns it an *action status*:

```sh
$ robotsix-auto-mail triage
```

Action statuses are **advisory labels only** — they are stored locally in the
SQLite `triage_decisions` table and do **not** move mail in the original
mailbox or modify the kanban status column. The agent defaults uncertain cases
to `user_triage` (explicit deferral to a human) rather than guessing.

### Action statuses

| Status | Meaning |
|---|---|
| `answer` | The message needs a personal reply |
| `archive` | Keep the message for reference but no reply needed |
| `delete` | The message is junk / worthless and can be discarded |
| `ignore` | No action needed and it need not be kept |
| `user_triage` | The system is not confident — defer to a human |

### Human-decision memory

The agent learns from your manual triage decisions. When you record a user
decision (via `triage-set`, below), the system remembers that sender's
preference in a persistent, per-sender memory. On future triage runs, the agent
is biased toward repeating those preferences — it treats them as advisory
guidance (not hard rules) so it can still adapt when a message from the same
sender clearly differs in content.

For example, if you've told the system "mail from alice@x.com goes to archive
(3 times)", the prompt will note this preference and the agent will favor
archiving alice's new messages unless context suggests otherwise.

The memory is stored in the SQLite `watermark` table under the key
`triage_human_memory` (alongside other persistent metadata like the archive
structure). It survives across runs and connections.

### Options

| Option | Default | Purpose |
|---|---|---|
| `--api-key` | – | OpenRouter API key; overrides `LLM_API_KEY` env and config file |
| `--output-format` | `text` | Output format: `text` (human-readable) or `json` (machine-readable) |

### Requirements

The `triage` command requires:
- The `pydantic-ai` package (install via `pip install robotsix-auto-mail[dev]`)
- An LLM API key (via `--api-key`, `LLM_API_KEY` env, or `llm.api_key` in config)

### Representative text output

```text

Inbox Triage
------------------------------------------------------------

<a@x.com>
  action: answer
  confidence: high
  reason: Sender is asking a direct question that needs a response.

<b@x.com>
  action: archive
  confidence: high
  reason: Promotional content; keep for reference but no reply needed.

2 message(s) triaged
```

Exit code is `0` on success (even if decisions are produced), `1` on error
(missing API key, `pydantic-ai` not installed, or LLM failure).

## The `triage-set` command

To manually record a triage decision for a single message, use `triage-set`:

```sh
$ robotsix-auto-mail triage-set <message_id> <action>
```

This records the decision in the `triage_decisions` table with `source=user`
(distinguishing it from agent decisions) and **also updates the
human-decision memory ledger**, so future triage runs will favor that action
for mail from the same sender.

### Arguments

| Argument | Purpose |
|---|---|
| `<message_id>` | The Message-ID of the mail to triage (from `board` or `triage` output) |
| `<action>` | The action status: `answer`, `archive`, `delete`, `ignore`, or `user_triage` |

### Examples

```sh
# Record that alice@x.com's message should be archived
robotsix-auto-mail triage-set '<a@x.com>' archive

# Mark a message as needing a reply
robotsix-auto-mail triage-set '<b@x.com>' answer

# Explicitly defer to a human (use this for ambiguous messages)
robotsix-auto-mail triage-set '<c@x.com>' user_triage
```

### Behavior

- If the `message_id` is unknown, exits with code `1` and an error message.
- If the `action` is invalid, exits with code `1` and an error message.
- On success, the decision is stored and the human-decision memory is
  updated; exit code is `0`.

The next `triage` run will treat this sender's preference as advisory guidance
for future mail from the same address.

### Requirements

The `triage-set` command requires a loadable configuration (for `db_path`),
but does **not** require the `pydantic-ai` package or an LLM API key — it is
purely a local decision-recording tool.
