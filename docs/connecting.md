# Connecting

`robotsix-auto-mail` needs IMAP and SMTP connection parameters. They can be
supplied via **environment variables**, a **TOML config file**, or a
**combination** of both.

## Configuration keys

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
| `MAIL_CONFIG_PATH` | no | `config/mail.toml` | Filesystem path to the TOML config file |

**TLS modes**

| Mode | Behaviour |
|---|---|
| `direct-tls` | TLS from the first byte, no plaintext negotiation (IMAP port 993, SMTP port 465) |
| `starttls` | Plain connection upgraded to TLS via STARTTLS (IMAP port 143, SMTP port 587) |
| `none` | No TLS at all — **insecure, for local development only** |

### TOML config file

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
password = "s3cret"
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
| `auth.password` | yes | – | Login password |

A commented template is available at `config/mail.example.toml`.

## Precedence rules

`mail.load()` resolves configuration in this order:

1. **Environment variables are evaluated first.** If all four required
   variables (`MAIL_IMAP_HOST`, `MAIL_SMTP_HOST`, `MAIL_USERNAME`,
   `MAIL_PASSWORD`) are set, they are used and the TOML file is ignored.
2. **TOML fallback.** If only required fields are missing from the
   environment (no invalid values), `load()` reads the TOML file at
   `MAIL_CONFIG_PATH` (default: `config/mail.toml`).
3. **Env-override merge.** Every environment variable that *is* set is
   then re-applied on top of the TOML values. This lets you keep shared
   settings in a TOML file while overriding just the password via
   `MAIL_PASSWORD`, for example.

If any environment variable has an *invalid* value (e.g. a non-integer
port), the error is raised immediately — the TOML fallback is skipped so
your typo is not silently swallowed.

## Example setups

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
password = "your-app-password-here"
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

Once your configuration is in place, run the ingestion pipeline to fetch new mail
from the IMAP server and store it in the local datastore:

```sh
$ robotsix-auto-mail ingest
```

### What it does

`ingest` loads the mail configuration, then:

- Opens (or creates) the local SQLite database at the configured `db_path`
  (default: `mail.db`).
- Opens an authenticated IMAP connection.
- Reads the current IMAP UID watermark from the database so only messages
  newer than the last fetch are retrieved.
- Fetches the batch of new messages from the server.
- Parses each raw MIME message into a structured record (sender, subject,
  date, recipients, body, attachments).
- Stores each record idempotently — if a message with the same `Message-ID`
  header already exists in the database, it is counted as a duplicate skip
  rather than inserted again.
- Advances the watermark to the highest IMAP UID in the batch so the next
  run starts from where this one left off.
- Prints a human-readable summary of how many messages were fetched, newly
  stored, skipped as duplicates, and failed with errors.

If any individual message cannot be parsed or stored, its error is printed and
the pipeline continues with the remaining messages.  Unparseable messages are
skipped permanently (their UID is recorded past the watermark).

### Representative output

```text
Fetched: 12 messages
Stored:  10 new
Skipped:  1 duplicate
Errors:   1
  UID 42 (<msg-id@example.com>): failed to parse raw bytes as MIME message
```

Exit code is `0` when no errors occurred, `1` when any errors were collected
(including configuration-load failures).

### Idempotency

The pipeline is safe to re-run even if a previous run crashed partway through:

- **Crash before watermark update:** Already-stored messages have their
  `message_id` recorded in the database.  On re-run, the same UIDs are
  re-fetched (the watermark hasn't moved), but the `UNIQUE` constraint on
  `message_id` causes them to be counted as duplicates (skipped) rather than
  stored again.
- **Crash after watermark update:** The watermark has already advanced, so
  subsequent runs start from the new watermark and never re-fetch those UIDs.
- **Empty batch:** If no new messages exist, the pipeline returns immediately
  without touching the watermark.

### Configuration

The `ingest` subcommand uses the same configuration path as `probe` (env vars
or TOML file).  Two additional keys control the local datastore:

| Variable | TOML key | Default | Purpose |
|---|---|---|---|
| `MAIL_DB_PATH` | `store.path` | `mail.db` | Filesystem path to the SQLite database |
| `MAIL_IMAP_FOLDER` | `imap.folder` | `INBOX` | IMAP mailbox folder to fetch from |

The database is created automatically on first use — no manual setup is needed.
