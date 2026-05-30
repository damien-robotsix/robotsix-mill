# Mail Ingestion

`robotsix-auto-mail` fetches messages from a configured IMAP inbox, parses them
into structured records, and stores them idempotently in a local SQLite
database.

## High-level flow

```
connect → fetch → parse → store → update watermark
```

1. **Connect** — open an authenticated IMAP connection using the configured
   credentials and server details.
2. **Fetch** — read the current IMAP UID watermark from the local database.
   Only messages with UIDs greater than the watermark are retrieved from the
   server.
3. **Parse** — each raw MIME message is parsed into a structured record
   (sender, subject, date, recipients, body, attachments).
4. **Store** — each record is inserted into the `mail_records` table. If a
   message with the same `Message-ID` header already exists, it is skipped as
   a duplicate.
5. **Update watermark** — after the full batch has been processed, the
   watermark is advanced to the highest IMAP UID in the batch.

## CLI usage

```sh
$ robotsix-auto-mail ingest
```

A single pass: fetch new mail since the watermark, store it, and exit.

### Watch mode (automatic, on an interval)

```sh
$ robotsix-auto-mail ingest --watch
```

Runs a cycle, then repeats every `ingest.interval_minutes` (config, default
15; override with `MAIL_INGEST_INTERVAL`).  A failed cycle is logged and the
loop continues; Ctrl-C stops it cleanly.  This is the default command of the
`robotsix-auto-mail` Docker service, so `docker compose up -d` keeps the
board's datastore fed automatically.

### Dry-run mode

```sh
$ robotsix-auto-mail ingest --dry-run
```

When `--dry-run` is active the pipeline still fetches messages from IMAP,
parses them, and runs the duplicate check — but it **skips** inserting records
and updating the watermark.  The `Stored` count reflects messages that *would
have been* inserted (i.e. whose `Message-ID` was not already in the database).
A `DRY RUN — nothing stored` banner is printed.

### Representative output

```text
Fetched: 12 messages
Stored:  10 new
Skipped:  1 duplicate
Errors:   1
  UID 42 (<msg-id@example.com>): failed to parse raw bytes as MIME message
```

### Exit codes

Exit code `0` whenever the pipeline runs to completion, **even when
per-message errors are present**.  Exit code `1` only on configuration-load
failures (missing or invalid config) and fatal connection failures (e.g. IMAP
server unreachable).  This makes the exit code suitable for cron and
automation — a single malformed message will not cause a non-zero exit.

## Datastore schema

The local SQLite database (default: `.data/mail.db`) contains two tables, created
automatically on first run (`CREATE TABLE IF NOT EXISTS`).

### `mail_records` — parsed messages

| Column | Type | Role |
|---|---|---|
| `id` | `INTEGER` | Auto-increment primary key (internal row ID). |
| `imap_uid` | `INTEGER` | IMAP UID of the message on the server at fetch time. |
| `message_id` | `TEXT NOT NULL UNIQUE` | The `Message-ID` header value. The `UNIQUE` constraint is the first line of deduplication. |
| `sender` | `TEXT NOT NULL` | `From` header (RFC 5322). |
| `subject` | `TEXT NOT NULL` | `Subject` header, decoded from RFC 2047 if necessary. |
| `date` | `TEXT NOT NULL` | `Date` header parsed to ISO 8601 (empty string if unparseable). |
| `recipients_json` | `TEXT NOT NULL` | JSON object with `"to"` and `"cc"` keys, each an array of address strings. |
| `body_plain` | `TEXT NOT NULL` | Decoded `text/plain` body (empty string if absent). |
| `body_html` | `TEXT NOT NULL` | Decoded `text/html` body (empty string if absent). |
| `attachments_json` | `TEXT NOT NULL` | JSON array of attachment metadata (filename, MIME type, size). |

### `watermark` — fetch progress

| Column | Type | Role |
|---|---|---|
| `key` | `TEXT PRIMARY KEY` | Watermark identifier (the value is `"imap_uid"`). |
| `value` | `TEXT NOT NULL` | The highest IMAP UID successfully fetched and stored. |

The watermark table stores a single row with key `imap_uid`.  On every
successful ingest, this value is advanced to the maximum UID in the batch.

## Idempotency

The pipeline is safe to re-run even if a previous run crashed partway through.

### Level 1: `Message-ID` uniqueness

The `UNIQUE` constraint on `mail_records.message_id` prevents storing the same
message twice.  If a re-fetch or re-run attempts to insert a message whose
`Message-ID` is already present, the insert is silently skipped and the
message counts as a duplicate.

### Level 2: IMAP UID watermark

The watermark (`imap_uid` in the `watermark` table) tracks the highest UID
that has been successfully stored.  Each fetch only retrieves messages with
UIDs strictly greater than this value, so messages from completed batches are
never re-fetched.

### Crash recovery scenarios

- **Crash before watermark update:** Stored messages have their `message_id`
  recorded.  On re-run the same UIDs are re-fetched (watermark hasn't moved),
  but the `UNIQUE` constraint causes them to be skipped as duplicates.
- **Crash after watermark update:** The watermark has already advanced;
  subsequent runs start from the new watermark and never re-fetch those UIDs.
- **Empty batch:** If no new messages exist, the pipeline returns immediately
  without touching the watermark.

## Configuration

The `ingest` subcommand uses the same IMAP connection and authentication
settings as the rest of `robotsix-auto-mail` (see
[docs/connecting.md](connecting.md)).  Two additional keys control the local
datastore and the watch interval:

| Variable | YAML key | Default | Purpose |
|---|---|---|---|
| `MAIL_DB_PATH` | `store.path` | `.data/mail.db` | Filesystem path to the SQLite database |
| `MAIL_IMAP_FOLDER` | `imap.folder` | `INBOX` | IMAP mailbox folder to fetch from |
| `MAIL_INGEST_INTERVAL` | `ingest.interval_minutes` | `15` | Minutes between cycles in `--watch` mode |

The database is created automatically on first use — no manual setup is needed.
