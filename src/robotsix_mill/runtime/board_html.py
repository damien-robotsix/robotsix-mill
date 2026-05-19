"""The HTML shell for the kanban board served at ``GET /``.

CSS and JavaScript are served as static files from ``static/board.css``
and ``static/board.js``.  This module only contains the HTML skeleton
that links to them.
"""

BOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>robotsix-mill</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/board.css"></head><body>
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
<script src="/static/board.js"></script></body></html>"""
