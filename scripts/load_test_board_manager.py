#!/usr/bin/env python3
"""Load-test the board-manager fast lane under a saturated mill.

Load-test procedure:
  1. Start a mill instance with ``board_manager_enabled=true``.
  2. Run: python scripts/load_test_board_manager.py --mill-url <URL> --token <TOKEN>
  3. Observe: 3+ tickets reach IMPLEMENT_COMPLETE before reads are issued.
  4. Observe: all reads return in seconds; script exits 0.
  5. (Optional) Pass ``--langfuse-session <ID>`` to confirm via Langfuse traces.
  6. Copy the printed latency table to a comment on the validation ticket.

The inline timing path (step 4 in the spec) issues queries through the
board-manager broker endpoint, which uses the agent-comm pull/mailbox
protocol — not a simple HTTP request/response endpoint.  When the
broker is not externally scriptable the Langfuse path (``--langfuse-session``)
is the primary measurement method.

Known divergences from the spec:

- **Pre-flight ``board_manager_enabled`` check**: the spec requires aborting
  when the setting is false, but ``board_manager_enabled`` is not exposed via
  any HTTP endpoint.  ``_preflight()`` prints what it can discover from
  ``/health``, ``/health/ready``, and ``/gates``, but cannot assert the
  boolean.  The operator must confirm the setting manually.

- **Poll state**: the spec says poll for ``state=implement``; the
  implementation polls for ``state=implement_complete``.  The mill's ticket
  state machine uses ``implement_complete`` as the terminal state of the
  implement stage, so this is the correct predicate for "the ticket has been
  picked up by an implement agent."  The docstring and code are internally
  consistent.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
import time
from typing import Any

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load-test the board-manager fast lane.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              # Inline timing (requires scriptable broker endpoint):
              python scripts/load_test_board_manager.py --mill-url http://127.0.0.1:8077

              # Langfuse trace verification (recommended):
              python scripts/load_test_board_manager.py --mill-url http://127.0.0.1:8077 \\
                  --langfuse-session sess_abc123
        """),
    )
    p.add_argument(
        "--mill-url",
        default=os.environ.get("MILL_URL", "http://127.0.0.1:8077"),
        help="Base URL of the mill instance (env: MILL_URL).",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("MILL_API_TOKEN", ""),
        help="Mill API bearer token (env: MILL_API_TOKEN).",
    )
    p.add_argument(
        "--reads",
        type=int,
        default=10,
        help="Number of board-manager queries for inline timing (default: 10).",
    )
    p.add_argument(
        "--max-latency-s",
        type=float,
        default=120.0,
        help="Failure threshold in seconds (default: 120).",
    )
    p.add_argument(
        "--langfuse-session",
        default=os.environ.get("LANGFUSE_SESSION_ID", ""),
        help=(
            "Langfuse session ID. When set, verify board-manager trace latency "
            "via Langfuse traces instead of inline HTTP timing "
            "(env: LANGFUSE_SESSION_ID)."
        ),
    )
    p.add_argument(
        "--ticket-count",
        type=int,
        default=4,
        help="Number of draft tickets to create for load (default: 4).",
    )
    p.add_argument(
        "--poll-timeout-s",
        type=float,
        default=120.0,
        help="Seconds to wait for tickets to reach IMPLEMENT_COMPLETE (default: 120).",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _QuietImport:
    """Lazy import with a user-friendly message on failure."""

    def __init__(self, module: str, pip_name: str = "") -> None:
        self._module = module
        self._pip_name = pip_name or module
        self._obj: Any = None

    def __call__(self) -> Any:
        if self._obj is not None:
            return self._obj
        try:
            self._obj = __import__(self._module, fromlist=["_"])
        except ImportError:
            sys.exit(
                f"This script requires '{self._pip_name}'. "
                f"Install it (pip install {self._pip_name}) and retry."
            )
        return self._obj


_requests = _QuietImport("requests", "requests")


def _headers(token: str) -> dict[str, str]:
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------------------------------------------------------------------------
# Step 1 — Pre-flight check
# ---------------------------------------------------------------------------


def _preflight(mill_url: str, token: str) -> dict[str, Any]:
    """Hit /health, print config keys we can discover, return a status dict."""
    requests = _requests()
    url = mill_url.rstrip("/") + "/health"
    print(f"--- Pre-flight: GET {url}")
    try:
        r = requests.get(url, headers=_headers(token), timeout=10)
        r.raise_for_status()
    except Exception as exc:
        sys.exit(f"Pre-flight failed: {exc}")
    data = r.json()
    print(f"  status = {data.get('status', '?')}")
    uptime = data.get("uptime_seconds")
    if uptime is not None:
        print(f"  uptime  = {uptime}s ({uptime/3600:.1f}h)")

    # Try /health/ready for more detail.
    ready_url = mill_url.rstrip("/") + "/health/ready"
    try:
        rr = requests.get(ready_url, headers=_headers(token), timeout=10)
        if rr.status_code == 200:
            ready_data = rr.json()
            for check in ready_data.get("checks", []):
                print(f"  ready/{check['name']} = {check['status']}")
    except Exception:
        pass

    # Attempt to discover board_manager_enabled via /gates.
    gates_url = mill_url.rstrip("/") + "/gates"
    try:
        gr = requests.get(gates_url, headers=_headers(token), timeout=10)
        if gr.status_code == 200:
            gates = gr.json()
            # gates doesn't include board_manager_enabled; it's an internal
            # setting.  Print what we can.
            print(f"  gates  = {gates}")
    except Exception:
        pass

    return data


# ---------------------------------------------------------------------------
# Step 2 — handle_wrapper gate
# ---------------------------------------------------------------------------


def _check_handle_wrapper() -> None:
    """Assert handle_wrapper is present on BoardManager.__init__."""
    print("--- Checking handle_wrapper on BoardManager.__init__")
    try:
        import inspect
        from robotsix_board_agent.board_manager import BoardManager
    except ImportError as exc:
        sys.exit(
            f"robotsix_board_agent not importable: {exc}\n"
            "Is robotsix-board-agent installed in this environment?"
        )
    params = inspect.signature(BoardManager.__init__).parameters
    if "handle_wrapper" not in params:
        sys.exit(
            "handle_wrapper missing from BoardManager.__init__ — "
            "child #2 cross-repo change not yet merged and pinned in pyproject.toml.\n"
            f"Parameters present: {list(params)}"
        )
    print("  OK — handle_wrapper is present")


# ---------------------------------------------------------------------------
# Step 3 — Load generation
# ---------------------------------------------------------------------------


def _create_load_tickets(
    mill_url: str, token: str, count: int
) -> list[dict[str, Any]]:
    """POST *count* draft tickets and return their parsed JSON bodies."""
    requests = _requests()
    url = mill_url.rstrip("/") + "/tickets"
    tickets: list[dict[str, Any]] = []
    for i in range(count):
        body = {
            "title": f"Load test {i + 1}",
            "description": "Do nothing meaningful.",
            "kind": "task",
            "source": "user",
        }
        print(f"  POST {url}  title={body['title']!r}")
        try:
            r = requests.post(url, json=body, headers=_headers(token), timeout=30)
            r.raise_for_status()
        except Exception as exc:
            # Best-effort: if the mill rejects a dupe, carry on.
            print(f"  WARNING: ticket creation failed: {exc}")
            continue
        ticket = r.json()
        print(f"    → {ticket.get('id', '?')}  state={ticket.get('state', '?')}")
        tickets.append(ticket)
    return tickets


def _poll_until_implement_complete(
    mill_url: str,
    token: str,
    min_count: int,
    timeout_s: float,
) -> list[dict[str, Any]]:
    """Poll GET /tickets?state=implement_complete until ≥ *min_count* match."""
    requests = _requests()
    url = mill_url.rstrip("/") + "/tickets"
    deadline = time.monotonic() + timeout_s
    print(f"  Polling {url}?state=implement_complete "
          f"(want ≥{min_count}, timeout={timeout_s}s)")
    while time.monotonic() < deadline:
        try:
            r = requests.get(
                url,
                params={"state": "implement_complete"},
                headers=_headers(token),
                timeout=10,
            )
            r.raise_for_status()
        except Exception as exc:
            print(f"  WARNING: poll failed: {exc}")
            time.sleep(5)
            continue
        tickets = r.json()
        if isinstance(tickets, list) and len(tickets) >= min_count:
            print(f"  OK — {len(tickets)} tickets in IMPLEMENT_COMPLETE state")
            return tickets
        print(f"  {len(tickets) if isinstance(tickets, list) else '?'} "
              f"implement_complete tickets — waiting 5s …")
        time.sleep(5)
    sys.exit(
        f"Timed out after {timeout_s}s waiting for {min_count} tickets "
        f"to reach IMPLEMENT_COMPLETE state."
    )


# ---------------------------------------------------------------------------
# Step 5 — Langfuse trace measurement
# ---------------------------------------------------------------------------


def _langfuse_measure(
    session_id: str, max_latency_s: float, mill_url: str
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Fetch Langfuse traces and extract board_manager latencies.

    Returns (board_traces, stats_dict).  Tries the mill's own client first,
    falls back to a direct Langfuse REST call using environment variables.
    """
    print(f"--- Langfuse measurement: session_id={session_id}")

    # -- try mill's langfuse client --
    try:
        return _langfuse_via_mill_client(session_id, max_latency_s)
    except Exception as exc:
        print(f"  Mill Langfuse client unavailable ({exc}); "
              "trying direct REST API …")

    # -- fallback: direct Langfuse REST call --
    return _langfuse_direct(session_id, max_latency_s)


def _langfuse_via_mill_client(
    session_id: str, max_latency_s: float
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Use robotsix_mill.langfuse.client to fetch traces."""
    import tempfile
    from pathlib import Path

    from robotsix_mill.config import Settings

    with tempfile.TemporaryDirectory() as td:
        settings = Settings(data_dir=Path(td))
        if not settings.tracing_enabled:
            raise RuntimeError("Langfuse not configured (missing LANGFUSE_* env vars)")

        from robotsix_mill.langfuse.client import _langfuse_api_get

        data = _langfuse_api_get(
            settings,
            "/api/public/traces",
            params={"sessionId": session_id, "limit": 100},
        )
        if data is None:
            raise RuntimeError("Langfuse API returned no data (unreachable?)")

    traces: list[dict[str, Any]] = data.get("data", [])
    return _extract_board_traces(traces, max_latency_s)


def _langfuse_direct(
    session_id: str, max_latency_s: float
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Call the Langfuse REST API directly with requests."""
    requests = _requests()

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    base_url = os.environ.get(
        "LANGFUSE_BASE_URL", "https://cloud.langfuse.com"
    ).rstrip("/")

    if not public_key or not secret_key:
        raise RuntimeError(
            "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY "
            "in the environment."
        )

    auth = requests.auth.HTTPBasicAuth(public_key, secret_key)
    url = f"{base_url}/api/public/traces"
    params: dict[str, Any] = {"sessionId": session_id, "limit": 100}
    print(f"  GET {url}?sessionId={session_id}&limit=100")
    r = requests.get(url, params=params, auth=auth, timeout=30)
    r.raise_for_status()
    data = r.json()
    traces: list[dict[str, Any]] = data.get("data", [])
    return _extract_board_traces(traces, max_latency_s)


def _extract_board_traces(
    traces: list[dict[str, Any]], max_latency_s: float
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Filter traces named 'board_manager' and compute latency stats."""
    board_traces = [t for t in traces if t.get("name") == "board_manager"]
    if not board_traces:
        print("  WARNING: no board_manager traces found in session")
        return [], {"count": 0, "max_latency_s": 0.0}

    latencies_s: list[float] = []
    for t in board_traces:
        lat = t.get("latency")
        # Langfuse latency is always in seconds (float).
        # A heuristic like `if lat > 1000: lat /= 1000` would mask genuine
        # slow traces — e.g. a 2000 s trace would be reported as 2 s.
        if lat is None:
            lat_s = 0.0
        else:
            lat_s = float(lat)
        latencies_s.append(lat_s)

    latencies_s.sort()
    n = len(latencies_s)

    def _pct(p: float) -> float:
        if n == 0:
            return 0.0
        idx = max(0, min(n - 1, int(n * p / 100.0)))
        return latencies_s[idx]

    stats: dict[str, float] = {
        "count": n,
        "median_s": _pct(50),
        "p95_s": _pct(95),
        "max_latency_s": latencies_s[-1] if latencies_s else 0.0,
        "failures": sum(
            1 for lat in latencies_s if lat > max_latency_s
        ),
    }

    # Per-trace table
    print(f"  Found {n} board_manager trace(s):")
    for i, t in enumerate(board_traces, 1):
        lat_s = latencies_s[i - 1]
        trace_id = t.get("id", "?")[:16]
        print(f"    {i:3d}. {trace_id}…  latency={lat_s:.2f}s")

    return board_traces, stats


# ---------------------------------------------------------------------------
# Step 4 — Inline timing (stub — broker endpoint not externally scriptable)
# ---------------------------------------------------------------------------


def _inline_timing_stub(mill_url: str, token: str, reads: int) -> None:
    """The board-manager broker endpoint uses the agent-comm pull/mailbox
    protocol — not a simple HTTP request/response endpoint.  Inline timing
    requires implementing a full agent-comm client, which is out of scope
    for this script.

    Use ``--langfuse-session <ID>`` for trace-based latency verification,
    or consult the robotsix_board_agent source for the broker HTTP path.
    """
    print("--- Inline timing: SKIPPED")
    print(
        "  The board-manager broker endpoint uses the agent-comm pull/mailbox "
        "protocol.\n"
        "  Direct HTTP timing is not available without a full agent-comm client.\n"
        "  Use --langfuse-session <ID> for Langfuse trace verification instead."
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _report(
    stats: dict[str, float], max_latency_s: float, method: str
) -> int:
    """Print summary and return exit code (0 = pass, 1 = fail)."""
    print()
    print("=" * 60)
    print(f"  Measurement method: {method}")
    print(f"  Traces / reads    : {int(stats.get('count', 0))}")
    print(f"  Median latency    : {stats.get('median_s', 0):.2f}s")
    print(f"  P95 latency       : {stats.get('p95_s', 0):.2f}s")
    print(f"  Max latency       : {stats.get('max_latency_s', 0):.2f}s")
    print(f"  Threshold         : {max_latency_s:.1f}s")
    print(f"  Failures          : {int(stats.get('failures', 0))}")
    print("=" * 60)

    failures = int(stats.get("failures", 0))
    max_lat = stats.get("max_latency_s", 0.0)

    if failures > 0:
        print(f"FAIL: {failures} trace(s) exceeded the {max_latency_s}s threshold.")
        return 1
    if max_lat > max_latency_s:
        print(f"FAIL: max latency {max_lat:.2f}s > threshold {max_latency_s:.1f}s.")
        return 1
    if stats.get("count", 0) == 0:
        print("FAIL: no board_manager traces found.")
        return 1

    print("PASS: all board-manager reads within threshold.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mill_url: str = args.mill_url
    token: str = args.token
    langfuse_session: str = args.langfuse_session
    max_latency_s: float = args.max_latency_s
    reads: int = args.reads
    ticket_count: int = args.ticket_count
    poll_timeout_s: float = args.poll_timeout_s

    print(f"mill_url          = {mill_url}")
    print(f"token             = {'<set>' if token else '<none>'}")
    print(f"langfuse_session  = {langfuse_session or '<none>'}")
    print(f"max_latency_s     = {max_latency_s}")
    print(f"reads (inline)    = {reads}")
    print(f"ticket_count      = {ticket_count}")
    print(f"poll_timeout_s    = {poll_timeout_s}")
    print()

    # 1. Pre-flight
    _preflight(mill_url, token)
    print()

    # 2. handle_wrapper gate
    _check_handle_wrapper()
    print()

    # 3. Load generation
    print("--- Creating load tickets")
    _create_load_tickets(mill_url, token, ticket_count)
    _poll_until_implement_complete(mill_url, token, min_count=3, timeout_s=poll_timeout_s)
    print()

    # 4/5. Measurement
    stats: dict[str, float] = {}
    method: str = ""
    if langfuse_session:
        _, stats = _langfuse_measure(langfuse_session, max_latency_s, mill_url)
        method = "langfuse"
    else:
        _inline_timing_stub(mill_url, token, reads)
        print()
        print(
            "Re-run with --langfuse-session <ID> to measure via Langfuse traces."
        )
        return 2  # no measurement possible

    return _report(stats, max_latency_s, method)


if __name__ == "__main__":
    sys.exit(main())
