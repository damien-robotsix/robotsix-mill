# ADR 0001: Programming Language

- **Status:** Accepted
- **Date:** 2026-05-27

## Context

The `robotsix-auto-mail` project needs a programming language before any build tooling, directory layout, or CI pipeline can be configured. Every subsequent Phase 1 ticket depends on this choice.

The ROADMAP defines a system that must:

- Connect to IMAP and SMTP servers with TLS and authentication.
- Parse MIME email messages (multipart, nested parts, attachments).
- Store ingested mail in an embedded SQLite datastore.
- Expose a CLI for ingestion and diagnostics.
- Serve a board view — either as an HTML page or a terminal UI.
- Ship as a single, self-contained artifact with trivial deployment.
- Run automated tests in CI with no network access.

No team composition is documented anywhere in the repository, so "team familiarity" cannot be weighted. The evaluation prioritises domain suitability, tooling support, ecosystem maturity, and operational simplicity — criteria that apply regardless of who picks up the project.

Two languages were evaluated in depth: **Python** and **Go**. Both are mainstream, general-purpose languages with mature ecosystems well-suited to network services and CLI tooling.

## Decision

The project will be written in **Python** (3.12+).

## Rationale

### Domain suitability

Python's standard library provides production-quality modules for all four core capabilities:

| Capability | Python stdlib | Go stdlib |
|---|---|---|
| IMAP client | `imaplib` (TLS, STARTTLS, SASL auth) | Not available — requires third-party |
| SMTP client | `smtplib` (TLS, STARTTLS, AUTH) | `net/smtp` (limited — no STARTTLS, minimal auth) |
| MIME parsing | `email` (full multipart, nested parts, headers, attachments) | `mime` (basic; `mime/multipart` lacks depth for real-world email) |
| SQLite | `sqlite3` (built-in, no CGO required) | Requires `mattn/go-sqlite3` (CGO) or `modernc.org/sqlite` (pure-Go, third-party) |

Python covers IMAP, SMTP, MIME, and SQLite entirely from its standard library — zero external dependencies for the core data path. Go requires third-party libraries for IMAP and mature MIME handling, and its SMTP support in stdlib lacks STARTTLS. This directly aligns with the ROADMAP's preference for "languages that minimise external dependencies for these core capabilities."

### Tooling support

Both languages have mature, well-supported toolchains:

- **Python:** `uv` or `pip`/`venv` for dependency management, `ruff` for linting/formatting, `pytest` for testing, `mypy` for static type-checking. Configuration lives in `pyproject.toml`.
- **Go:** `go mod` for dependencies, `gofmt`/`go vet`/`golangci-lint` for linting, built-in `go test` for testing. Configuration is distributed across `go.mod`, tool configs, and Makefiles.

Python's tooling ecosystem has converged on `pyproject.toml` as a single configuration entry-point, which simplifies project setup and CI configuration.

### Ecosystem maturity

- **Python:** Decades-long track record in email processing, systems scripting, and web backends. Libraries like `click` (CLI), `flask` (HTML board view), and `rich`/`textual` (TUI) are stable and widely adopted. The packaging ecosystem (PyPI) is mature and well-governed.
- **Go:** Strong in network services and CLI tools. TUI frameworks like `bubbletea` are excellent. The module ecosystem is younger but stable. Go's strength in single-binary deployment is compelling for operational simplicity.

### Operational simplicity

- **Python:** Requires a Python runtime on the target system. Distribution can use `pex`, `shiv`, or `pyinstaller` to bundle into a single executable when needed. For development and CI, `uv` makes Python version and dependency management nearly as fast as compiled languages.
- **Go:** Compiles to a single static binary — the gold standard for deployment simplicity. This is Go's strongest advantage over Python.

The trade-off is real: Go's single-binary deployment is simpler than Python's runtime-plus-dependency model. However, for a project whose Phase 1–4 scope is personal or small-team use (local mail ingestion, local SQLite, local board view), the deployment advantage of Go is outweighed by Python's "no third-party dependencies for the core loop" advantage. Every dependency added is a maintenance burden, a security surface, and a potential breakage point — Python avoids all of that for IMAP, SMTP, MIME, and SQLite.

### Why not Go?

Go was the primary alternative and is a strong language for this domain. It was rejected for this specific project because:

1. **IMAP requires a third-party library.** The most popular Go IMAP library (`emersion/go-imap`) is community-maintained and has a history of extended dormant periods. The ROADMAP's ingestion pipeline depends on IMAP — having this in stdlib (Python) vs. a third-party dependency (Go) is a meaningful risk difference.
2. **MIME parsing in stdlib is insufficient.** Go's `mime` and `mime/multipart` packages handle basic MIME but lack the depth of Python's `email` module, which has been hardened against real-world email ambiguity for decades.
3. **SQLite requires CGO or a pure-Go reimplementation.** Both options add complexity compared to Python's built-in `sqlite3`.

These are not criticisms of Go — they reflect the fact that Python's standard library was shaped by decades of email and systems programming, giving it a uniquely strong fit for this project's exact requirements.

## Consequences

### Tooling (subsequent Phase 1 tickets)

- Build configuration will use `pyproject.toml` with `setuptools` or `hatchling` as the build backend.
- Linting and formatting will use `ruff`.
- Testing will use `pytest`.
- Static type-checking will use `mypy` (strict mode).
- Dependency management will use `uv` with a lockfile.

### Directory layout

- The project will follow the Python `src` layout: `src/robotsix_auto_mail/` for the package, `tests/` for tests, with `pyproject.toml` at the root.

### Dependency management

- Core data-path functionality (IMAP, SMTP, MIME, SQLite) will use only stdlib modules — no PyPI packages needed.
- CLI, HTML rendering, and TUI may add dependencies (e.g., `click`, `flask`, `textual`), evaluated at the start of their respective phases.

### Developer onboarding

- Contributors need Python 3.12+ and `uv` installed. Everything else is bootstrapped from the lockfile.
- No compiled language toolchain, no CGO, no system-level SQLite library required.
- Standard-library dominance means new contributors can trace the core logic without navigating third-party documentation.