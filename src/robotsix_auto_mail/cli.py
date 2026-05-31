"""CLI for robotsix-auto-mail.

Entry point: ``main()``, exposed via console_scripts in pyproject.toml.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import getpass
import sys
import time
from typing import TextIO

from robotsix_auto_mail import __version__
from robotsix_auto_mail.config import MailConfig, load
from robotsix_auto_mail.db import MailRecord, init_db, list_records
from robotsix_auto_mail.format import _format_date
from robotsix_auto_mail.imap import ImapAuthError, ImapClient, ImapError
from robotsix_auto_mail.pipeline import IngestResult, ingest_mail
from robotsix_auto_mail.smtp_client import (
    SmtpAuthError,
    SmtpClient,
    SmtpError,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="robotsix-auto-mail",
        description="Diagnose and operate on mail servers.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    sub = parser.add_subparsers(dest="command", title="subcommands")
    sub.add_parser(
        "probe", help="Probe IMAP and SMTP servers for diagnostics"
    )
    ingest_parser = sub.add_parser(
        "ingest", help="Fetch new mail and store it locally"
    )
    ingest_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Fetch and parse messages without storing or advancing watermark",
    )
    ingest_parser.add_argument(
        "--watch",
        action="store_true",
        default=False,
        help=(
            "Keep running, ingesting on an interval (minutes) set by "
            "ingest.interval_minutes in the config (default 15)"
        ),
    )

    sub.add_parser("board", help="Display ingested mail in a read-only board view")

    serve_parser = sub.add_parser(
        "serve", help="Start the web board server"
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: %(default)s)",
    )

    detect_parser = sub.add_parser(
        "detect",
        help="Auto-detect email provider settings via LLM and write config",
    )
    detect_parser.add_argument(
        "email",
        help="Email address to detect provider settings for",
    )
    detect_parser.add_argument(
        "--password",
        default=None,
        help=(
            "Password to write into the config file. "
            "When omitted, prompts interactively."
        ),
    )
    detect_parser.add_argument(
        "--output",
        default="config/mail.local.yaml",
        help="Write mail config to this file path (default: %(default)s)",
    )
    detect_parser.add_argument(
        "--stdout",
        action="store_true",
        default=False,
        help="Print mail config to stdout instead of writing to file",
    )
    detect_parser.add_argument(
        "--no-verify",
        action="store_true",
        default=False,
        help=(
            "Skip the post-write IMAP/SMTP connection check. "
            "By default detect verifies the settings once a password is known."
        ),
    )

    return parser


def _print_header(
    file: TextIO, title: str, width: int = 60, char: str = "-"
) -> None:
    file.write(f"\n{title}\n{char * width}\n")


def _cmd_probe(config: MailConfig) -> int:
    """Run the probe subcommand: connect to IMAP + SMTP and print metadata.

    Returns 0 when both succeed, 1 when either fails.
    """
    failures = 0

    # -- IMAP ---------------------------------------------------------------
    _print_header(sys.stdout, "IMAP Probe")

    try:
        with ImapClient(config) as imap:
            greeting = imap.server_greeting
            if greeting is not None:
                sys.stdout.write(
                    f"Greeting: {greeting.decode('utf-8', errors='replace')}\n"
                )
            else:
                sys.stdout.write("Greeting: (none)\n")

            sys.stdout.write("Capabilities:\n")
            for cap in imap.capabilities:
                sys.stdout.write(f"  - {cap}\n")

            # Folders
            sys.stdout.write("\nFolders:\n")
            folders = imap.list_folders()
            if folders:
                for fi in folders:
                    attrs = " ".join(fi.attributes) if fi.attributes else "(none)"
                    delim = fi.delimiter if fi.delimiter else "(none)"
                    sys.stdout.write(f"  {fi.name}\n")
                    sys.stdout.write(f"    attributes: {attrs}\n")
                    sys.stdout.write(f"    delimiter:  {delim}\n")
            else:
                sys.stdout.write("  (no folders returned)\n")
    except ImapError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        failures += 1

    # -- SMTP ---------------------------------------------------------------
    _print_header(sys.stdout, "SMTP Probe")

    try:
        with SmtpClient(config) as smtp:
            ehlo = smtp.ehlo_response
            if ehlo is not None:
                sys.stdout.write(
                    f"EHLO response: {ehlo.decode('utf-8', errors='replace')}\n"
                )
            else:
                sys.stdout.write("EHLO response: (none)\n")

            sys.stdout.write("\nESMTP features:\n")
            features = smtp.esmtp_features
            if features:
                for key, value in sorted(features.items()):
                    sys.stdout.write(f"  {key}: {value}\n")
            else:
                sys.stdout.write("  (no features)\n")

            # Deliberately: no send() call.  This is diagnostic-only.
    except SmtpError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        failures += 1

    return 0 if failures == 0 else 1


def _ingest_cycle(config: MailConfig, *, dry_run: bool = False) -> int:
    """Run a single ingest pass: fetch, parse, store, and update watermark.

    Returns 0 when the pipeline runs (including per-message errors),
    or 1 for a fatal connection failure (ImapClient raise).
    """
    result: IngestResult | None = None
    conn = init_db(config.db_path)
    try:
        with ImapClient(config) as imap_client:
            result = ingest_mail(conn, imap_client, config, dry_run=dry_run)
    except Exception:
        # Fatal connection failure — ImapClient(config) raised.
        result = None
    finally:
        conn.close()

    # If ImapClient(config) raised before ingest_mail ran, result is None.
    if result is None:
        return 1

    # -- Print summary -------------------------------------------------------
    if dry_run:
        sys.stdout.write("DRY RUN — nothing stored\n")

    sys.stdout.write(f"Fetched: {result.total_fetched:>2} messages\n")
    sys.stdout.write(f"Stored:  {result.stored:>2} new\n")
    sys.stdout.write(f"Skipped: {result.skipped:>2} duplicate\n")
    sys.stdout.write(f"Errors:  {len(result.errors):>2}\n")

    if result.errors:
        for err_obj in result.errors:
            # Guard against empty message_id.
            mid = f" ({err_obj.message_id})" if err_obj.message_id else ""
            sys.stdout.write(f"  UID {err_obj.uid}{mid}: {err_obj.error}\n")

    return 0


def _cmd_ingest(
    config: MailConfig, *, dry_run: bool = False, watch: bool = False
) -> int:
    """Run the ingest subcommand once, or repeatedly in watch mode.

    In watch mode it loops forever, running an ingest cycle every
    ``config.ingest_interval_minutes`` minutes.  A failed cycle is logged
    and the loop continues; Ctrl-C exits cleanly with 0.
    """
    if not watch:
        return _ingest_cycle(config, dry_run=dry_run)

    interval_minutes = max(1, config.ingest_interval_minutes)
    sys.stdout.write(
        f"Watch mode: ingesting every {interval_minutes} min "
        "(Ctrl-C to stop).\n"
    )
    sys.stdout.flush()
    try:
        while True:
            try:
                _ingest_cycle(config, dry_run=dry_run)
            except Exception as exc:  # never let one bad cycle kill the loop
                sys.stderr.write(f"Ingest cycle failed: {exc}\n")
            sys.stdout.write(f"Next ingest in {interval_minutes} min.\n")
            sys.stdout.flush()
            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        sys.stdout.write("\nWatch stopped.\n")
        return 0


_BODY_PREVIEW_LIMIT = 150
_SEPARATOR = "-" * 60 + "\n"



def _render_card(record: MailRecord, file: TextIO) -> None:
    """Render a single mail record to *file*."""
    # Sender
    file.write(f"From:    {record.sender}\n")

    # Subject
    subject = record.subject if record.subject.strip() else "(no subject)"
    file.write(f"Subject: {subject}\n")

    # Date
    file.write(f"Date:    {_format_date(record.date)}\n")

    # Body preview
    body = record.body_plain
    if not body or not body.strip():
        preview = "(no body)"
    elif len(body) > _BODY_PREVIEW_LIMIT:
        preview = body[:_BODY_PREVIEW_LIMIT] + "\u2026"
    else:
        preview = body
    file.write(f"\n{preview}\n")


def _render_board(records: list[MailRecord], file: TextIO) -> None:
    """Render every *record* in the inbox board view to *file*."""
    if not records:
        file.write("Your inbox is empty.\n")
        return

    for i, record in enumerate(records):
        if i > 0:
            file.write(_SEPARATOR)
        _render_card(record, file)

    count = len(records)
    file.write(f"{count} message(s)\n")


def _cmd_board(config: MailConfig) -> int:
    """Run the board subcommand: display ingested mail in a read-only view.

    Returns 0 on success, 1 on failure to load configuration.
    """
    conn = init_db(config.db_path)
    try:
        records = list_records(conn)
    finally:
        conn.close()

    _print_header(sys.stdout, "Inbox")
    _render_board(records, sys.stdout)

    return 0


@dataclasses.dataclass(frozen=True)
class _VerifyResult:
    """Outcome of a quiet IMAP + SMTP connection check.

    ``*_auth`` is True when the server was reachable but authentication
    failed (i.e. the host is right but the password is wrong).
    """

    imap_ok: bool
    smtp_ok: bool
    imap_error: str = ""
    smtp_error: str = ""
    imap_auth: bool = False
    smtp_auth: bool = False

    @property
    def ok(self) -> bool:
        return self.imap_ok and self.smtp_ok

    @property
    def host_problem(self) -> bool:
        """A failure that is NOT an auth failure (wrong host/port/TLS)."""
        imap_host_bad = not self.imap_ok and not self.imap_auth
        smtp_host_bad = not self.smtp_ok and not self.smtp_auth
        return imap_host_bad or smtp_host_bad

    @property
    def only_auth_problem(self) -> bool:
        """Reachable everywhere, but at least one authentication failed."""
        return not self.ok and not self.host_problem


def _verify_config(config: MailConfig) -> _VerifyResult:
    """Attempt authenticated IMAP and SMTP connections, quietly.

    Returns a :class:`_VerifyResult` categorising each side as ok, an auth
    failure, or a connection/TLS failure.  Prints nothing.
    """
    imap_ok = smtp_ok = False
    imap_error = smtp_error = ""
    imap_auth = smtp_auth = False

    try:
        with ImapClient(config) as imap:
            imap.list_folders()
        imap_ok = True
    except ImapAuthError as exc:
        imap_error, imap_auth = str(exc), True
    except ImapError as exc:
        imap_error = str(exc)

    try:
        with SmtpClient(config):
            pass
        smtp_ok = True
    except SmtpAuthError as exc:
        smtp_error, smtp_auth = str(exc), True
    except SmtpError as exc:
        smtp_error = str(exc)

    return _VerifyResult(
        imap_ok=imap_ok,
        smtp_ok=smtp_ok,
        imap_error=imap_error,
        smtp_error=smtp_error,
        imap_auth=imap_auth,
        smtp_auth=smtp_auth,
    )


def _report_verify_result(result: _VerifyResult) -> None:
    """Print a one-line-per-server summary of a verification attempt."""
    for label, ok, auth, err in (
        ("IMAP", result.imap_ok, result.imap_auth, result.imap_error),
        ("SMTP", result.smtp_ok, result.smtp_auth, result.smtp_error),
    ):
        if ok:
            sys.stderr.write(f"  {label}: ok\n")
        else:
            kind = "auth" if auth else "connection"
            sys.stderr.write(f"  {label}: {kind} failed — {err}\n")


def _verify_feedback(config: MailConfig, result: _VerifyResult) -> str:
    """Describe the connection failures for the LLM refinement prompt."""
    parts: list[str] = []
    if not result.imap_ok and not result.imap_auth:
        parts.append(
            f"IMAP host {config.imap_host!r} (port {config.imap_port}, "
            f"{config.imap_tls_mode}) could not be reached: {result.imap_error}"
        )
    if not result.smtp_ok and not result.smtp_auth:
        parts.append(
            f"SMTP host {config.smtp_host!r} (port {config.smtp_port}, "
            f"{config.smtp_tls_mode}) could not be reached: {result.smtp_error}"
        )
    return "\n".join(parts)


def _prompt_hosts(
    config: MailConfig, result: _VerifyResult
) -> MailConfig | None:
    """Prompt the user for the host(s) that failed to connect.

    Returns an updated config, or ``None`` if the user supplied nothing new
    (or input is unavailable), so the caller can stop instead of looping.
    """
    imap_host = config.imap_host
    smtp_host = config.smtp_host
    changed = False
    try:
        if not result.imap_ok and not result.imap_auth:
            ans = input(f"Enter IMAP host [{config.imap_host}]: ").strip()
            if ans:
                imap_host, changed = ans, True
        if not result.smtp_ok and not result.smtp_auth:
            ans = input(f"Enter SMTP host [{config.smtp_host}]: ").strip()
            if ans:
                smtp_host, changed = ans, True
    except (EOFError, KeyboardInterrupt):
        return None
    if not changed:
        return None
    return dataclasses.replace(
        config, imap_host=imap_host, smtp_host=smtp_host
    )


def _cmd_detect(args: argparse.Namespace) -> int:
    """Run the detect subcommand: auto-detect provider settings, write the
    config, and verify it by connecting — refining with autoconfig, the LLM,
    and finally a manual prompt when the servers cannot be reached.

    Returns 0 on success, 1 on any error.
    """
    # -- lazy-import detect (pydantic_ai is optional) --
    try:
        from robotsix_auto_mail.detect import (
            DetectionError,
            autoconfig_lookup,
            detect_provider,
            mx_lookup,
            provider_from_mx,
            provider_to_config,
            render_config,
        )
    except ImportError:
        sys.stderr.write(
            "The 'detect' command requires the pydantic-ai package. "
            "Install it with: pip install robotsix-auto-mail[dev]\n"
        )
        return 1

    from pathlib import Path

    from robotsix_auto_mail.config import ConfigurationError, load_llm

    api_key, model = load_llm()

    # -- initial detection ladder: autoconfig → MX→provider → LLM --
    sys.stderr.write(f"Detecting settings for {args.email}…\n")
    mx_hosts: list[str] = []
    provider = autoconfig_lookup(args.email)
    if provider is not None:
        sys.stderr.write(
            f"  autoconfig: imap={provider.imap_host} "
            f"smtp={provider.smtp_host}\n"
        )
    else:
        sys.stderr.write("  autoconfig: no match — checking MX records…\n")
        mx_hosts = mx_lookup(args.email)
        if mx_hosts:
            sys.stderr.write(f"  MX: {', '.join(mx_hosts[:3])}\n")
        provider = provider_from_mx(mx_hosts)
        if provider is not None:
            sys.stderr.write(
                f"  MX provider: imap={provider.imap_host} "
                f"smtp={provider.smtp_host}\n"
            )
        else:
            sys.stderr.write("  no known provider — asking the LLM…\n")
            try:
                provider = detect_provider(
                    args.email,
                    model=model,
                    api_key=api_key,
                    mx_hosts=mx_hosts,
                )
            except DetectionError as exc:
                sys.stderr.write(f"Error: {exc}\n")
                return 1
            sys.stderr.write(
                f"  LLM: imap={provider.imap_host} "
                f"smtp={provider.smtp_host}\n"
            )

    # -- password handling --
    password: str | None = args.password
    if password is None and not args.stdout:
        try:
            password = getpass.getpass("Email password: ")
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\nDetection cancelled.\n")
            return 1
    elif password is None and args.stdout:
        password = ""  # no prompt in stdout mode  # nosec B105

    # -- preserve settings detect doesn't generate (LLM credentials, a custom
    #    store path, an existing password) from an existing config file --
    output_path = Path(args.output)
    prev: MailConfig | None = None
    if not args.stdout and output_path.exists():
        try:
            prev = MailConfig.from_yaml(output_path, validate=False)
        except (ConfigurationError, OSError):
            prev = None

    def _build(prov: object, pw: str | None) -> MailConfig:
        cfg = provider_to_config(prov, args.email, password=pw or "")  # type: ignore[arg-type]
        if prev is not None:
            cfg = dataclasses.replace(
                cfg,
                llm_api_key=prev.llm_api_key,
                llm_model=prev.llm_model,
                db_path=prev.db_path,
                password=pw or prev.password,
            )
        return cfg

    config = _build(provider, password)

    # -- stdout mode: print and return (no write, no verify) --
    if args.stdout:
        sys.stderr.write(
            f"# Detected settings for {args.email} — verify before using.\n"
            "# Save this as config/mail.local.yaml.\n"
        )
        sys.stdout.write(render_config(config))
        return 0

    def _write(cfg: MailConfig) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_config(cfg))

    _write(config)
    sys.stderr.write(f"Config written to {output_path}\n")

    if not config.password:
        sys.stderr.write(
            f"No password provided — add it to {output_path} "
            "(or set MAIL_PASSWORD), then run `probe` to verify.\n"
        )
        return 0
    if args.no_verify:
        return 0

    # -- verify + refine loop --
    #   connection/TLS failure → refine host via the LLM (bounded), then a
    #   manual prompt;  auth failure → re-prompt the password.
    llm_budget = 2
    # only re-prompt the password when it was entered interactively
    pw_budget = 2 if args.password is None else 0
    manual_used = False

    while True:
        sys.stderr.write("\nVerifying connection (IMAP + SMTP)…\n")
        result = _verify_config(config)
        if result.ok:
            sys.stderr.write("Verification succeeded — settings work.\n")
            return 0
        _report_verify_result(result)

        if result.only_auth_problem and pw_budget > 0:
            pw_budget -= 1
            sys.stderr.write(
                "The server is reachable but the password was rejected.\n"
            )
            try:
                new_pw = getpass.getpass("Re-enter email password: ")
            except (EOFError, KeyboardInterrupt):
                break
            if not new_pw:
                break
            config = _build(provider, new_pw)
            _write(config)
            continue

        if result.host_problem and llm_budget > 0:
            llm_budget -= 1
            sys.stderr.write("Refining the host with the LLM…\n")
            try:
                refined = detect_provider(
                    args.email,
                    model=model,
                    api_key=api_key,
                    feedback=_verify_feedback(config, result),
                    mx_hosts=mx_hosts,
                )
            except DetectionError as exc:
                sys.stderr.write(f"  LLM refinement error: {exc}\n")
                refined = None
            if refined is not None:
                provider = refined
                sys.stderr.write(
                    f"  LLM: imap={provider.imap_host} "
                    f"smtp={provider.smtp_host}\n"
                )
                config = _build(provider, config.password)
                _write(config)
                continue

        if result.host_problem and not manual_used:
            manual_used = True
            sys.stderr.write(
                "Could not auto-detect a working host — "
                "please enter it manually.\n"
            )
            updated = _prompt_hosts(config, result)
            if updated is None:
                break
            config = updated
            _write(config)
            continue

        break

    sys.stderr.write(
        f"\nVerification FAILED — could not confirm the settings. "
        f"Edit {output_path} and re-run `probe`.\n"
    )
    return 1


def _cmd_serve(config: MailConfig, *, port: int) -> int:
    """Run the serve subcommand: start the web board HTTP server.

    Returns 0 on clean shutdown, 1 if the port is already in use.
    """
    from http.server import HTTPServer

    from robotsix_auto_mail.server import make_board_handler

    handler_class = make_board_handler(config.db_path)

    print(f"Serving board on http://0.0.0.0:{port}/board")
    try:
        server = HTTPServer(("0.0.0.0", port), handler_class)  # nosec B104
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down.")
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"Port {port} is already in use.", file=sys.stderr)
            return 1
        raise
    return 0


def _load_config_or_exit() -> MailConfig:
    """Load configuration, or print to stderr and exit with code 1 on failure."""
    try:
        return load()
    except Exception as exc:
        sys.stderr.write(f"Error loading configuration: {exc}\n")
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the appropriate subcommand handler.

    Returns 0 on success, 1 on failure.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "probe":
        return _cmd_probe(_load_config_or_exit())

    if args.command == "ingest":
        return _cmd_ingest(
            _load_config_or_exit(), dry_run=args.dry_run, watch=args.watch
        )

    if args.command == "board":
        return _cmd_board(_load_config_or_exit())

    if args.command == "serve":
        return _cmd_serve(_load_config_or_exit(), port=args.port)

    if args.command == "detect":
        return _cmd_detect(args)

    # No command given — print help and exit 1.
    parser.print_help(sys.stderr)
    return 1