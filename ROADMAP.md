# Implementation Roadmap

This document orders the build-out of `robotsix-auto-mail` from a stub into a working system. Phases are listed in dependency order — each phase assumes the preceding phases are complete.

---

## Phase 1: Foundation — project scaffold

### Goal

A runnable project skeleton with build tooling, linting, a test runner, and a well-defined directory layout. There is no application logic yet — the deliverable is a green CI build running a no-op test.

### Deliverables

- Language choice and rationale (documented in a decision log or the README).
- Build tooling configured: a single command compiles/lints/tests the project.
- Test runner integrated and passing (one trivial test that asserts `true`).
- Directory layout established: source tree, test tree, configuration samples.
- Dependency-management file (lockfile / manifest), initially empty or with only dev-tool dependencies.
- `LICENSE` file added at the repo root (the README flags this as outstanding).
- CI pipeline (if applicable) running lint + tests and reporting pass/fail.

### Dependencies

None — this is the starting point.

---

## Phase 2: Mail server connectivity

### Goal

Establish authenticated, encrypted connections to a real mail server over standard protocols (IMAP for reading, SMTP for sending). The connectivity layer is the bedrock for every mail operation that follows.

### Deliverables

- An **IMAP client** that can:
  - Connect to a server, upgrade to TLS/STARTTLS.
  - Authenticate with username and password (or OAuth2 if the chosen library supports it).
  - List available mailbox folders.
- An **SMTP client** that can:
  - Connect to a server, upgrade to TLS/STARTTLS.
  - Authenticate with credentials.
  - Send a simple plain-text message.
- A configuration mechanism that reads server hostnames, ports, TLS settings, and credentials from environment variables or a config file (credentials must never be hardcoded).
- A CLI command (or library entry-point) that exercises both connections and prints a summary of the server's capabilities and folder list.
- Unit tests that use test doubles / fake servers — the test suite must pass in CI without access to a real mail server.
- Documentation: a short "Connecting" section in the README (or a dedicated doc) covering configuration keys and example usage.

### Dependencies

- **Phase 1** — the project scaffold must exist so connectivity code has somewhere to live and can be built/tested.

---

## Phase 3: Mail ingestion — read and parse

### Goal

Fetch mail from a configured IMAP inbox, parse each message into a structured record, and store it persistently in a local datastore. Repeated ingestion runs must be idempotent — the same message is never stored twice.

### Deliverables

- An **ingestion pipeline** that:
  - Connects to the IMAP server and selects the configured inbox (or a configurable folder).
  - Fetches new messages (those whose IMAP UID or `Message-ID` header has not been seen).
  - Parses each MIME message into a structured record: sender, recipients (To/Cc), subject, date, body (plain-text and/or HTML), attachment metadata.
  - Handles multipart MIME correctly (nested parts, mixed content types, filenames for attachments).
- A **local datastore** (embedded, e.g. SQLite) that persists ingested mail records and the last-seen IMAP UID / `Message-ID` watermark so restarts never re-fetch old mail.
- Idempotency: re-running ingestion after an interrupted run must not create duplicate rows.
- CLI command (or library entry-point) to run ingestion on demand and report how many messages were newly stored.
- Tests that exercise parsing of sample MIME messages (including multipart) and verify idempotency against a test database.
- Documentation: update the README or add a doc describing the ingestion model, the datastore schema, and how idempotency is guaranteed.

### Dependencies

- **Phase 2** — IMAP connectivity is required to fetch messages from the server.
- Implicit: Phase 1 (scaffold) provides the build/test harness.

---

## Phase 4: Board view — inbox column

### Goal

Expose ingested mail through a self-contained board interface so a user can see their inbox without leaving the application. The board is a single column (Inbox) and is read-only in this phase.

### Deliverables

- A **board interface** that:
  - Reads mail records from the local datastore.
  - Renders them in a single-column "Inbox" view.
  - Each item shows at minimum: sender, subject, date, and a body preview.
  - Is self-contained — served as an HTML page by the application, rendered as a terminal UI, or presented through another directly-usable interface (not a third-party service).
- The board is **read-only**: no drag-and-drop, no column changes, no reply/archive/delete actions.
- Navigation/filtering (sort by date, search by subject or sender) is nice-to-have but not required for this phase.
- Tests that verify the board renders stored mail records correctly and handles an empty inbox gracefully.
- Documentation: update the README or add a doc explaining how to start and access the board view.

### Dependencies

- **Phase 3** — the board reads from the local datastore populated by the ingestion pipeline.
- **Phase 2** — although the board itself does not connect to a mail server, it depends transitively on Phase 2 because Phase 3 does.
- Implicit: Phase 1 (scaffold).

---

## Phase 5: Iteration — from board to workflow

### Goal (partially delivered)

The multi-column kanban board (Inbox, Triaging, Done, Archive) with per-card
Move dropdowns is **delivered** — see the `serve` subcommand and
[docs/connecting.md](docs/connecting.md#the-serve-command).

### Direction (future work)

- **Actions on mail items**: reply (via SMTP), archive, delete, mark as read/unread.
- **Rules and automation**: user-defined rules for routing incoming mail into specific columns (e.g. by sender, subject pattern).
- **Multi-mailbox support**: ingest and display mail from more than one IMAP account or folder.

### Dependencies

- **Phases 2–4** — the complete core loop must be stable before workflow features are added.

---

## Cross-cutting concerns

These apply to every phase and are not gated by a specific milestone:

### Configuration-driven

All operational parameters — server hostnames, ports, TLS settings, credentials, polling intervals, datastore paths — are read from a config file or environment variables. No values are hardcoded. Each phase adds its own configuration keys, documented alongside the feature.

### Testability

Every phase includes automated tests that run in CI without external dependencies (no real mail server, no network access). Test doubles, fake servers, and disposable datastores are the default strategy.

### Documentation

Each phase updates the project documentation (primarily the README) so the project remains understandable as it grows. At minimum: an overview of what the phase delivers, configuration keys added, and how to run the new functionality.
