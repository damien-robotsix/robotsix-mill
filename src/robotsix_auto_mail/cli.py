"""CLI for robotsix-auto-mail.

Entry point: ``main()``, exposed via console_scripts in pyproject.toml.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import getpass
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from robotsix_auto_mail import __version__
from robotsix_auto_mail.config import MailConfig, load
from robotsix_auto_mail.db import (
    MailRecord,
    get_record_by_message_id,
    init_db,
    list_records,
)
from robotsix_auto_mail.format import _BODY_PREVIEW_LIMIT, _format_date
from robotsix_auto_mail.imap import ImapAuthError, ImapClient, ImapError
from robotsix_auto_mail.pipeline import IngestResult, ingest_mail
from robotsix_auto_mail.smtp_client import (
    SmtpAuthError,
    SmtpClient,
    SmtpError,
)

if TYPE_CHECKING:
    from robotsix_auto_mail.detect import MailProvider


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

    config_sync_parser = sub.add_parser(
        "config-sync",
        help="Run the LLM config-drift advisory agent (advisory only; "
             "does not replace the deterministic check_config_sync.py CI gate)",
    )
    config_sync_parser.add_argument(
        "--api-key", default=None,
        help="OpenRouter API key. Overrides LLM_API_KEY env and config file.",
    )
    config_sync_parser.add_argument(
        "--output-format", choices=["text", "json"], default="text",
        help="Output format for drift findings (default: %(default)s).",
    )
    config_sync_parser.add_argument(
        "--dedup", action="store_true", default=False,
        help="Consult/update the dedup memory ledger so previously-seen "
             "findings are suppressed. Requires a loadable config (for db_path).",
    )

    triage_parser = sub.add_parser(
        "triage",
        help="Run the LLM inbox-triage agent and record advisory action "
             "statuses (does not move mail in the mailbox)",
    )
    triage_parser.add_argument(
        "--api-key", default=None,
        help="OpenRouter API key. Overrides LLM_API_KEY env and config file.",
    )
    triage_parser.add_argument(
        "--output-format", choices=["text", "json"], default="text",
        help="Output format for triage decisions (default: %(default)s).",
    )

    triage_set_parser = sub.add_parser(
        "triage-set",
        help="Record a user triage decision for a single message "
             "(advisory; does not move mail in the mailbox)",
    )
    triage_set_parser.add_argument(
        "message_id", help="Message-ID of the mail to triage.",
    )
    triage_set_parser.add_argument(
        "action",
        help="Triage action: answer, archive, delete, ignore, or user_triage.",
    )

    triage_rules_parser = sub.add_parser(
        "triage-rules",
        help="Propose deterministic triage rules from triage history and "
             "list the accepted (active) rules (advisory; no LLM call)",
    )
    triage_rules_parser.add_argument(
        "--output-format", choices=["text", "json"], default="text",
        help="Output format for rule proposals (default: %(default)s).",
    )

    triage_rules_set_parser = sub.add_parser(
        "triage-rules-set",
        help="Accept or reject a proposed triage rule by fingerprint; "
             "accepted rules become active deterministic rules",
    )
    triage_rules_set_parser.add_argument(
        "fingerprint", help="Fingerprint of the triage rule proposal.",
    )
    triage_rules_set_parser.add_argument(
        "state",
        help="New state: accepted or rejected.",
    )

    config_sync_set_parser = sub.add_parser(
        "config-sync-set",
        help="Mark a config-drift finding accepted or rejected so it is "
             "suppressed by the dedup memory ledger",
    )
    config_sync_set_parser.add_argument(
        "fingerprint", help="Fingerprint of the config-drift finding.",
    )
    config_sync_set_parser.add_argument(
        "state",
        help="Ledger state: pending, accepted, or rejected.",
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


def _get_password(args: argparse.Namespace) -> str | None:
    """Get password from args or interactive prompt.

    Returns the password, or ``None`` if the user cancelled (EOF / KeyboardInterrupt).
    """
    password: str | None = args.password
    if password is None and not args.stdout:
        try:
            password = getpass.getpass("Email password: ")
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\nDetection cancelled.\n")
            return None
    elif password is None and args.stdout:
        password = ""  # no prompt in stdout mode  # nosec B105
    return password


def _detect_settings(
    email: str,
    api_key: str | None,
    autoconfig_lookup: Callable[[str], "MailProvider | None"],
    mx_lookup: Callable[[str], list[str]],
    provider_from_mx: Callable[[list[str]], "MailProvider | None"],
    detect_provider: Callable[..., "MailProvider"],
    _detection_error: type[Exception],
) -> tuple["MailProvider | None", list[str]]:
    """Run the provider-detection ladder for *email*.

    Tries, in order:
    1. ``autoconfig_lookup`` (Thunderbird/Outlook-style autodiscovery)
    2. MX-record lookup → ``provider_from_mx``
    3. LLM ``detect_provider`` (requires *api_key*)

    Returns ``(provider, mx_hosts)`` where *provider* is a
    ``MailProvider`` and *mx_hosts* is the (possibly empty) list of
    MX hostnames discovered during step 2 — needed later when the
    verification loop asks the LLM for a refinement.

    Prints progress messages to stderr (exactly as today).

    Returns ``(None, mx_hosts)`` when ``detect_provider``
    raises ``DetectionError`` (the error is printed to stderr).
    """
    sys.stderr.write(f"Detecting settings for {email}…\n")
    mx_hosts: list[str] = []
    provider = autoconfig_lookup(email)
    if provider is not None:
        sys.stderr.write(
            f"  autoconfig: imap={provider.imap_host} "
            f"smtp={provider.smtp_host}\n"
        )
    else:
        sys.stderr.write("  autoconfig: no match — checking MX records…\n")
        mx_hosts = mx_lookup(email)
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
                    email,
                    api_key=api_key,
                    mx_hosts=mx_hosts,
                )
            except _detection_error as exc:
                sys.stderr.write(f"Error: {exc}\n")
                return None, mx_hosts
            sys.stderr.write(
                f"  LLM: imap={provider.imap_host} "
                f"smtp={provider.smtp_host}\n"
            )
    return provider, mx_hosts


@dataclasses.dataclass(frozen=True)
class _RefineOutcome:
    """Result of one refinement strategy: a rebuilt config and/or provider.

    ``config`` is ``None`` when the strategy produced no new config (the
    user cancelled, or the LLM returned no refinement).  ``provider`` is
    set only when the strategy updated the working provider (LLM refine).
    """

    config: MailConfig | None = None
    provider: "MailProvider | None" = None


def _refine_password(
    build: Callable[["MailProvider", str | None], MailConfig],
    provider: "MailProvider",
) -> _RefineOutcome:
    """Re-prompt the password after a reachable-but-rejected auth failure."""
    sys.stderr.write("The server is reachable but the password was rejected.\n")
    try:
        new_pw = getpass.getpass("Re-enter email password: ")
    except (EOFError, KeyboardInterrupt):
        return _RefineOutcome()
    if not new_pw:
        return _RefineOutcome()
    return _RefineOutcome(config=build(provider, new_pw))


def _refine_with_llm(
    build: Callable[["MailProvider", str | None], MailConfig],
    provider: "MailProvider",
    config: MailConfig,
    result: _VerifyResult,
    *,
    email: str,
    api_key: str | None,
    mx_hosts: list[str],
    detect_provider: Callable[..., "MailProvider"],
    _detection_error: type[Exception],
) -> _RefineOutcome:
    """Ask the LLM for a refined provider after a host/connection failure."""
    sys.stderr.write("Refining the host with the LLM…\n")
    try:
        refined = detect_provider(
            email,
            api_key=api_key,
            feedback=_verify_feedback(config, result),
            mx_hosts=mx_hosts,
        )
    except _detection_error as exc:
        sys.stderr.write(f"  LLM refinement error: {exc}\n")
        refined = None
    if refined is None:
        return _RefineOutcome()
    sys.stderr.write(f"  LLM: imap={refined.imap_host} smtp={refined.smtp_host}\n")
    return _RefineOutcome(config=build(refined, config.password), provider=refined)


def _refine_manual(config: MailConfig, result: _VerifyResult) -> _RefineOutcome:
    """Prompt the user for the failing host(s) as a last resort."""
    sys.stderr.write(
        "Could not auto-detect a working host — please enter it manually.\n"
    )
    updated = _prompt_hosts(config, result)
    if updated is None:
        return _RefineOutcome()
    return _RefineOutcome(config=updated)


def _report_failure(output_path: Path) -> None:
    """Print the final verification-failed message before returning 1."""
    sys.stderr.write(
        f"\nVerification FAILED — could not confirm the settings. "
        f"Edit {output_path} and re-run `probe`.\n"
    )


def _verify_and_refine(
    provider: "MailProvider",
    *,
    email: str,
    api_key: str | None,
    mx_hosts: list[str],
    output_path: Path,
    password: str | None,
    password_from_args: str | None,
    no_verify: bool,
    provider_to_config: Callable[..., MailConfig],
    render_config: Callable[[MailConfig], str],
    detect_provider: Callable[..., "MailProvider"],
    _detection_error: type[Exception],
) -> int:
    """Verify *config* by connecting, refining on failure.

    Refinement strategy (bounded):
    1. Auth-only failure → re-prompt password (max 2 attempts
       for interactively-entered passwords; 0 when ``--password``
       was supplied).
    2. Host/connection failure → ask the LLM for a refined provider
       (max 2 attempts), then fall back to a manual interactive
       prompt.

    Returns 0 when verification succeeds, 1 when all budgets are
    exhausted.  Writes the (possibly refined) config to *output_path*
    after each change so the on-disk file stays in sync.
    """
    from robotsix_auto_mail.config import ConfigurationError

    prev: MailConfig | None = None
    if output_path.exists():
        try:
            prev = MailConfig.from_yaml(output_path, validate=False)
        except (ConfigurationError, OSError):
            prev = None

    def _build(prov: "MailProvider", pw: str | None) -> MailConfig:
        cfg = provider_to_config(prov, email, password=pw or "")
        if prev is not None:
            cfg = dataclasses.replace(
                cfg,
                llm_api_key=prev.llm_api_key,
                llm_model=prev.llm_model,
                db_path=prev.db_path,
                password=pw or prev.password,
            )
        return cfg

    def _write(cfg: MailConfig) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_config(cfg))

    config = _build(provider, password)
    _write(config)
    sys.stderr.write(f"Config written to {output_path}\n")

    if not config.password:
        sys.stderr.write(
            f"No password provided — add it to {output_path} "
            "(or set MAIL_PASSWORD), then run `probe` to verify.\n"
        )
        return 0
    if no_verify:
        return 0

    # -- verify + refine loop --
    #   connection/TLS failure → refine host via the LLM (bounded), then a
    #   manual prompt;  auth failure → re-prompt the password.
    llm_budget = 2
    # only re-prompt the password when it was entered interactively
    pw_budget = 2 if password_from_args is None else 0
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
            outcome = _refine_password(_build, provider)
            if outcome.config is None:
                break
            config = outcome.config
            _write(config)
            continue

        if result.host_problem and llm_budget > 0:
            llm_budget -= 1
            outcome = _refine_with_llm(
                _build,
                provider,
                config,
                result,
                email=email,
                api_key=api_key,
                mx_hosts=mx_hosts,
                detect_provider=detect_provider,
                _detection_error=_detection_error,
            )
            if outcome.provider is not None:
                provider = outcome.provider
            if outcome.config is not None:
                config = outcome.config
                _write(config)
                continue

        if result.host_problem and not manual_used:
            manual_used = True
            outcome = _refine_manual(config, result)
            if outcome.config is None:
                break
            config = outcome.config
            _write(config)
            continue

        break

    _report_failure(output_path)
    return 1


def _cmd_detect(args: argparse.Namespace) -> int:
    """Run the detect subcommand: auto-detect provider settings, write the
    config, and verify it by connecting — refining with autoconfig, the LLM,
    and finally a manual prompt when the servers cannot be reached.
    Returns 0 on success, 1 on any error.
    """
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

    from robotsix_auto_mail.config import load_llm
    api_key, _ = load_llm()
    provider, mx_hosts = _detect_settings(
        args.email, api_key, autoconfig_lookup, mx_lookup,
        provider_from_mx, detect_provider, DetectionError)
    if provider is None:
        return 1
    password = _get_password(args)
    if password is None:
        return 1
    if args.stdout:
        config = provider_to_config(provider, args.email, password=password or "")
        sys.stderr.write(
            f"# Detected settings for {args.email} — verify before using.\n"
            "# Save this as config/mail.local.yaml.\n"
        )
        sys.stdout.write(render_config(config))
        return 0
    return _verify_and_refine(
        provider,
        email=args.email, api_key=api_key, mx_hosts=mx_hosts,
        output_path=Path(args.output), password=password,
        password_from_args=args.password, no_verify=args.no_verify,
        provider_to_config=provider_to_config, render_config=render_config,
        detect_provider=detect_provider, _detection_error=DetectionError)


def _cmd_config_sync(args: argparse.Namespace) -> int:
    """Run the config-drift advisory agent and render its proposals.

    This is an advisory tool, not a CI gate: a successful run returns 0
    even when drift proposals are found.  Returns 1 only on error (missing
    pydantic_ai, ConfigSyncError, missing API key).
    """
    try:
        from robotsix_auto_mail.config_sync import (
            ConfigSyncError,
            run_config_sync_agent,
        )
    except ImportError:
        sys.stderr.write(
            "The 'config-sync' command requires the pydantic-ai package. "
            "Install it with: pip install robotsix-auto-mail[dev]\n"
        )
        return 1

    # Resolve the dedup connection only when --dedup is requested; like
    # detect, the advisory tool should not require a full mail config to run.
    conn = None
    if args.dedup:
        config = _load_config_or_exit()
        conn = init_db(config.db_path)

    try:
        result = run_config_sync_agent(api_key=args.api_key, conn=conn)
    except ConfigSyncError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    if args.output_format == "json":
        sys.stdout.write(json.dumps(result.model_dump(), indent=2) + "\n")
        # Advisory tool: a non-empty result is informational, not a gate.
        return 0

    _print_header(sys.stdout, "Config Drift Advisory")
    if not result.proposals:
        sys.stdout.write("No config drift detected.\n")
        # Advisory tool: a non-empty result is informational, not a gate.
        return 0

    for proposal in result.proposals:
        sys.stdout.write(f"\n{proposal.title}\n")
        sys.stdout.write(f"  confidence: {proposal.confidence}\n")
        field = proposal.affected_field if proposal.affected_field else "(none)"
        sys.stdout.write(f"  affected field: {field}\n")
        sys.stdout.write(f"\n{proposal.body}\n")

    # Advisory tool: a non-empty result is informational, not a gate.
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    """Run the inbox-triage agent and render the recorded decisions.

    This is an advisory tool, not a CI gate: a successful run returns 0 even
    when triage decisions are produced.  Returns 1 only on error (missing
    pydantic_ai, TriageError).
    """
    try:
        from robotsix_auto_mail.triage import TriageError, run_triage_agent
    except ImportError:
        sys.stderr.write(
            "The 'triage' command requires the pydantic-ai package. "
            "Install it with: pip install robotsix-auto-mail[dev]\n"
        )
        return 1

    config = _load_config_or_exit()
    conn = init_db(config.db_path)
    try:
        decisions = run_triage_agent(conn, api_key=args.api_key)
    except TriageError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    finally:
        conn.close()

    if args.output_format == "json":
        payload = [d.model_dump() for d in decisions]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    _print_header(sys.stdout, "Inbox Triage")
    if not decisions:
        sys.stdout.write("No inbox mail to triage.\n")
        return 0

    for decision in decisions:
        sys.stdout.write(f"\n{decision.message_id}\n")
        sys.stdout.write(f"  action: {decision.action}\n")
        sys.stdout.write(f"  confidence: {decision.confidence}\n")
        reason = decision.reason if decision.reason else "(none)"
        sys.stdout.write(f"  reason: {reason}\n")

    return 0


def _cmd_triage_set(args: argparse.Namespace) -> int:
    """Record a user triage decision for a single message.

    Returns 0 on success, 1 when the message_id is unknown or the action is
    invalid.
    """
    try:
        from robotsix_auto_mail.triage import (
            VALID_TRIAGE_ACTIONS,
            TriageError,
            record_human_decision,
            set_triage_decision,
        )
    except ImportError:
        sys.stderr.write(
            "The 'triage-set' command requires the pydantic-ai package. "
            "Install it with: pip install robotsix-auto-mail[dev]\n"
        )
        return 1

    if args.action not in VALID_TRIAGE_ACTIONS:
        sys.stderr.write(
            f"Error: invalid action {args.action!r}. "
            f"Must be one of {sorted(VALID_TRIAGE_ACTIONS)}\n"
        )
        return 1

    config = _load_config_or_exit()
    conn = init_db(config.db_path)
    try:
        if get_record_by_message_id(conn, args.message_id) is None:
            sys.stderr.write(
                f"Error: no mail with message_id {args.message_id!r}\n"
            )
            return 1
        try:
            set_triage_decision(
                conn, args.message_id, args.action, source="user"
            )
            record_human_decision(conn, args.message_id, args.action)
        except TriageError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
    finally:
        conn.close()

    sys.stdout.write(
        f"Recorded user triage decision: {args.message_id} -> {args.action}\n"
    )
    return 0


def _cmd_triage_rules(args: argparse.Namespace) -> int:
    """Propose deterministic triage rules and list the active rules.

    Deterministic — derived from triage history, no LLM / pydantic-ai
    required.  This is advisory: a successful run returns 0 even when new
    proposals are found.
    """
    from robotsix_auto_mail.triage import (
        _load_active_rules,
        _rule_fingerprint,
        propose_triage_rules,
        record_and_filter_rule_proposals,
    )

    config = _load_config_or_exit()
    conn = init_db(config.db_path)
    try:
        proposals = propose_triage_rules(conn)
        new_proposals = record_and_filter_rule_proposals(conn, proposals)
        active_rules = _load_active_rules(conn)
    finally:
        conn.close()

    if args.output_format == "json":
        payload = {
            "proposals": [
                {**p.model_dump(), "fingerprint": _rule_fingerprint(p)}
                for p in new_proposals
            ],
            "active_rules": [r.model_dump() for r in active_rules],
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    _print_header(sys.stdout, "Triage Rule Proposals")
    if not new_proposals:
        sys.stdout.write("No new triage rule proposals.\n")
    else:
        for proposal in new_proposals:
            sys.stdout.write(f"\n{proposal.title}\n")
            sys.stdout.write(
                f"  fingerprint: {_rule_fingerprint(proposal)}\n"
            )
            sys.stdout.write(f"  confidence: {proposal.confidence}\n")
            sys.stdout.write(
                f"  rule: {proposal.match_type}={proposal.match_value} "
                f"-> {proposal.action}\n"
            )
            sys.stdout.write(f"\n{proposal.body}\n")

    sys.stdout.write("\nActive rules:\n")
    if not active_rules:
        sys.stdout.write("  (none)\n")
    else:
        for rule in active_rules:
            sys.stdout.write(
                f"  {rule.match_type}={rule.match_value} -> {rule.action}\n"
            )
    return 0


def _cmd_triage_rules_set(args: argparse.Namespace) -> int:
    """Accept or reject a proposed triage rule by fingerprint.

    Deterministic — no LLM / pydantic-ai required.  Returns 0 on success,
    1 when the fingerprint is unknown or the state is invalid.
    """
    from robotsix_auto_mail.triage import TriageError, set_rule_state

    valid_states = {"accepted", "rejected"}
    if args.state not in valid_states:
        sys.stderr.write(
            f"Error: invalid state {args.state!r}. "
            f"Must be one of {sorted(valid_states)}\n"
        )
        return 1

    config = _load_config_or_exit()
    conn = init_db(config.db_path)
    try:
        set_rule_state(conn, args.fingerprint, args.state)
    except TriageError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    finally:
        conn.close()

    sys.stdout.write(
        f"Recorded triage rule state: {args.fingerprint} -> {args.state}\n"
    )
    return 0


def _cmd_config_sync_set(args: argparse.Namespace) -> int:
    """Record a user decision for a single config-drift finding.

    Returns 0 on success, 1 when the fingerprint is unknown or the state is
    invalid.
    """
    try:
        from robotsix_auto_mail.config_sync import (
            _VALID_LEDGER_STATES,
            ConfigSyncError,
            set_finding_state,
        )
    except ImportError:
        sys.stderr.write(
            "The 'config-sync-set' command requires the pydantic-ai package. "
            "Install it with: pip install robotsix-auto-mail[dev]\n"
        )
        return 1

    if args.state not in _VALID_LEDGER_STATES:
        sys.stderr.write(
            f"Error: invalid state {args.state!r}. "
            f"Must be one of {sorted(_VALID_LEDGER_STATES)}\n"
        )
        return 1

    config = _load_config_or_exit()
    conn = init_db(config.db_path)
    try:
        set_finding_state(conn, args.fingerprint, args.state)
    except ConfigSyncError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    finally:
        conn.close()

    sys.stdout.write(
        f"Recorded config-drift finding state: "
        f"{args.fingerprint} -> {args.state}\n"
    )
    return 0


def _cmd_serve(config: MailConfig, *, port: int) -> int:
    """Run the serve subcommand: start the web board HTTP server.

    Returns 0 on clean shutdown, 1 if the port is already in use.
    """
    from http.server import HTTPServer

    from robotsix_auto_mail.server import make_board_handler

    handler_class = make_board_handler(config.db_path)

    print(f"Serving board on http://0.0.0.0:{port}/board")
    try:
        # Binding to 0.0.0.0 is intentional: ``serve_board`` is a local dev
        # convenience tool, not a production server.
        server = HTTPServer(("0.0.0.0", port), handler_class)  # noqa: S104  # nosec B104
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

    if args.command == "config-sync":
        return _cmd_config_sync(args)

    if args.command == "triage":
        return _cmd_triage(args)

    if args.command == "triage-set":
        return _cmd_triage_set(args)

    if args.command == "triage-rules":
        return _cmd_triage_rules(args)

    if args.command == "triage-rules-set":
        return _cmd_triage_rules_set(args)

    if args.command == "config-sync-set":
        return _cmd_config_sync_set(args)

    # No command given — print help and exit 1.
    parser.print_help(sys.stderr)
    return 1
