# robotsix-auto-mail

Automated email handling — sending, receiving, and routing email through programmatic interfaces.

## Purpose

`robotsix-auto-mail` is a dedicated module for automated email processing. Once implemented, it will handle tasks like sending, receiving, and routing email programmatically, removing manual email steps from automated workflows.

## Project status

This project is currently a stub — there is no source code, no build pipeline, and no runtime. The repository exists as a placeholder for future implementation work.

**Language:** The project will be written in **Python** (3.12+), chosen for its standard-library support for IMAP, SMTP, MIME parsing, and SQLite — the four core capabilities required by the [ROADMAP](ROADMAP.md). Full rationale is in [ADR 0001](docs/decisions/0001-programming-language.md).

## Directory layout

| Directory | Role |
|---|---|
| `src/robotsix_auto_mail/` | Production Python package, following the `src` layout prescribed by [ADR 0001](docs/decisions/0001-programming-language.md). |
| `tests/` | Test code mirroring the `src/` package structure. |
| `config/` | Example and sample configuration files for operators. |
| `docs/` | Project documentation, including architecture decision records. |
| Root | Top-level project configuration, build scripts, and this README. |

## Connecting

Configuration keys, precedence rules, and a walkthrough of the `probe` diagnostics command are documented in [docs/connecting.md](docs/connecting.md).

## License

This project is licensed under the MIT License (SPDX: `MIT`). See [LICENSE](LICENSE) for the full text.
