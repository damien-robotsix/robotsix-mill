"""The HTML shell for the kanban board served at ``GET /``.

CSS and JavaScript are served as static files from ``static/board.css``
and ``static/board.js``.  This module only contains the HTML skeleton
that links to them.

Cache-busting: the link/script tags are emitted with ``?v=<sha1[:10]>``
queries derived from the actual file contents at process start. Same
content → same URL (no spurious refetch); any code edit → new URL →
the browser bypasses its cache without the user needing a hard-reload.
The user has been bitten by stale ``board.js`` more than once this
session — the static files are baked into the image, so a normal
reload after a ``docker compose up --build`` would still hit the
cached copy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def _asset_version(name: str) -> str:
    """Return a short content hash for a static asset, or ``"0"`` if the
    file is missing (the page still renders without versioning)."""
    p = Path(__file__).parent / "static" / name
    if not p.is_file():
        return "0"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:10]


_CSS_V = _asset_version("board.css")
_JS_V = _asset_version("board.js")


BOARD_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<title>robotsix-mill</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/board.css?v={_CSS_V}"></head><body>
<header><h1>robotsix-mill</h1>
<span class="muted" id="meta">loading…</span>
<label class="muted" style="margin-left:auto">
  <input type="checkbox" onchange="showClosed=this.checked;refresh()"> show closed</label>
<span class="muted">auto-refresh 5s</span>
<button onclick="runAudit()" style="font-size:11px;padding:3px 10px;
background:#059669;color:#fff;border:none;border-radius:4px;cursor:pointer">
  Run Audit
</button>
<button onclick="runHealth()" style="font-size:11px;padding:3px 10px;
background:#0d9488;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Run Health Check
</button>
<button onclick="runScout()" style="font-size:11px;padding:3px 10px;
background:#7c3aed;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Run Scout
</button>
<button onclick="runTraceHealth()" style="font-size:11px;padding:3px 10px;
background:#0ea5e9;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Trace Health
</button>
<button onclick="toggleRuns()" style="font-size:11px;padding:3px 10px;
background:#6b7280;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Runs
</button>
<button onclick="newTicket()" style="font-size:11px;padding:3px 10px;
background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  + New Ticket
</button>
</header>
<div id="board"></div>
<div id="drawer"><span class="x" onclick="close_()">&times;</span><div id="d"></div></div>
<script src="/static/board.js?v={_JS_V}"></script></body></html>"""
