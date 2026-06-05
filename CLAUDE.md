# robotsix-auto-mail

robotsix-auto-mail fetches mail over IMAP, parses it, and stores it in a
local SQLite datastore, with an HTTP kanban board for review.

## Archive feature

robotsix-auto-mail manages its own archive folder hierarchy, independent of
any pre-existing mailbox layout. On the first run a quick LLM call proposes
an appropriate layout based on the mailbox's existing folders; the chosen
structure is then remembered so subsequent runs reuse it without re-asking
the LLM. The implementation lives in `src/robotsix_auto_mail/archive.py`
(module `archive`) and is wired into ingestion from
`src/robotsix_auto_mail/pipeline.py`.

### How the LLM determines the structure

On the first run, `setup_archive(conn, client, *, archive_root=...,
api_key=..., tier=Tier.CHEAP)` lists the mailbox's existing folders and
calls `determine_archive_structure(...)`. That function builds an
`OpenRouterDeepseekProvider` (deepseek) agent at the `Tier.CHEAP` tier and,
via `pydantic_ai.PromptedOutput`, asks the LLM to return an
`ArchiveStructure` — a Pydantic model with a single
`folders: list[str]` field. Each entry is a sub-path relative to the
archive root, using `/` as the hierarchy separator (the list may be empty,
meaning just the root).

The instructions come from `_build_archive_system_prompt(archive_root)`,
which tells the model to return only a JSON object with a `folders` list of
sub-paths relative to the root — no root prefix, no prose, no markdown
fences.

### Where the choice is persisted and how retrieval works

The proposed structure (the full list of archive folder names) is stored as
JSON in the generic `watermark` key-value table under the key
`archive_structure` (the `_ARCHIVE_WATERMARK_KEY` constant) via
`set_watermark`. On subsequent runs `get_watermark` returns the cached JSON
and `setup_archive` short-circuits immediately — returning the remembered
list without listing folders, calling the LLM, or creating anything.

### First-run workflow vs. cheap no-op on later runs

- **First run** (no persisted structure) with a resolvable API key: the LLM
  proposes a layout, the missing folders are created, and the resulting
  full-name list is persisted to the watermark.
- **Later runs**: the watermark hit short-circuits `setup_archive`, making
  it a cheap no-op with no LLM call.

A key consequence (a deliberate repo convention): because of the watermark
short-circuit, once a structure is persisted, later changes to the root
path or other archive config do **not** re-trigger the LLM proposal — the
structure is remembered by design.

### No-API-key fallback

If no API key is resolvable, `setup_archive` does not call the LLM and does
not error. It falls back to a root-only structure (just `archive_root`), so
ingestion is never blocked by a missing key.

### Configuration options

Both fields live on `MailConfig` (`src/robotsix_auto_mail/config/__init__.py`):

- **`archive_root`** — env `MAIL_ARCHIVE_ROOT`, YAML `archive.root`.
  Defaults to `robotsix-mail-archive` (the `DEFAULT_ARCHIVE_ROOT`
  constant). Override it via the environment variable or the
  `config/mail.local.yaml` config file.
- **`archive_enabled`** — env `MAIL_ARCHIVE_ENABLED`, YAML
  `archive.enabled`. Defaults to `True`. This is the disable toggle for the
  archive feature.

### Pipeline integration

`setup_archive` is invoked from `ingest_mail` in `pipeline.py` as a
first-run step, guarded by `not dry_run` and `config.archive_enabled`. The
call is wrapped in a best-effort `try`/`except` that logs the failure (via
`_logger.exception`) but does not propagate it — an archive failure
(LLM, network, or IMAP) never aborts ingestion. Because `setup_archive`
only persists its watermark on success, a failed run naturally retries on
the next ingestion.

## Documentation conventions

When you add or change a user-facing CLI subcommand in
`src/robotsix_auto_mail/cli.py`, document it in `docs/connecting.md` in the
same PR, following the `config-sync` command section pattern (purpose,
optional-extra requirements, flags, example invocation, and output).
