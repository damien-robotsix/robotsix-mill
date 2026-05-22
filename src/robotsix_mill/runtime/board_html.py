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
<button onclick="runTraceHealth()" style="font-size:11px;padding:3px 10px;
background:#0ea5e9;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Trace Health
</button>
<button onclick="runAgentCheck()" style="font-size:11px;padding:3px 10px;
background:#db2777;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Run Agent Check
</button>
<button onclick="runSurvey()" style="font-size:11px;padding:3px 10px;
background:#f59e0b;color:#000;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Survey
</button>
<button onclick="openDeepReview()" style="font-size:11px;padding:3px 10px;
background:#1a2a3b;color:#60c0fa;border:1px solid #2a3a4b;border-radius:4px;cursor:pointer;
margin-left:4px">
  Deep Review
</button>
<button onclick="toggleRuns()" style="font-size:11px;padding:3px 10px;
background:#6b7280;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Runs
</button>
<button onclick="newInquiry()" style="font-size:11px;padding:3px 10px;
background:#0891b2;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  + Ask
</button>
<button onclick="newTicket()" style="font-size:11px;padding:3px 10px;
background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  + New Ticket
</button>
<button onclick="openCostDashboard()" style="font-size:11px;padding:3px 10px;
background:#0d9488;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  💰 Cost
</button>
</header>
<div id="board"></div>
<div id="drawer"><span class="x" onclick="close_()">&times;</span><div id="d"></div></div>
<script src="/static/board.js"></script></body></html>"""
