"""The HTML shell for the kanban board served at ``GET /``.

CSS and JavaScript are served as static files from ``static/board.css``
and ``static/board.js``.  This module only contains the HTML skeleton
that links to them.
"""

BOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>robotsix-mill</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/board.css"></head><body>
<header><h1>robotsix-mill</h1>
<span id="gates"></span>
<span class="muted" id="meta">loading…</span>
<select id="repo-selector" onchange="onRepoChange(this.value)" style="font-size:11px;background:#1d212c;border:1px solid #2c313d;color:#cfd3db;border-radius:4px;padding:3px 6px">
  <option value="all">All repos</option>
</select>
<label class="muted" style="margin-left:auto">
  <input type="checkbox" onchange="showClosed=this.checked;refresh()"> show closed</label>
<span class="muted">auto-refresh 5s</span>
<button onclick="runAudit()" style="font-size:11px;padding:3px 10px;
background:#059669;color:#fff;border:none;border-radius:4px;cursor:pointer">
  Audit
</button>
<button onclick="runHealth()" style="font-size:11px;padding:3px 10px;
background:#0d9488;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Health Check
</button>
<button onclick="runTestGap()" style="font-size:11px;padding:3px 10px;
background:#7c3aed;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Test Gaps
</button>
<button onclick="runTraceHealth()" style="font-size:11px;padding:3px 10px;
background:#0ea5e9;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Trace Health
</button>
<button onclick="runAgentCheck()" style="font-size:11px;padding:3px 10px;
background:#db2777;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Agent Check
</button>
<button onclick="runSurvey()" style="font-size:11px;padding:3px 10px;
background:#f59e0b;color:#000;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Survey
</button>
<button onclick="runBcCheck()" style="font-size:11px;padding:3px 10px;
background:#84cc16;color:#000;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  BC Check
</button>
<button onclick="runCompletenessCheck()" style="font-size:11px;padding:3px 10px;
background:#84cc16;color:#000;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Completeness
</button>
<button onclick="openDeepReview()" style="font-size:11px;padding:3px 10px;
background:#1a2a3b;color:#60c0fa;border:1px solid #2a3a4b;border-radius:4px;cursor:pointer;
margin-left:4px">
  Deep Review
</button>
<span style="border-left:1px solid #2a2e37;align-self:stretch;margin-left:8px"></span>
<button onclick="newInquiry()" style="font-size:11px;padding:3px 10px;
background:#0891b2;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  + Ask
</button>
<button onclick="newEpic()" style="font-size:11px;padding:3px 10px;
background:#9333ea;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  + Epic
</button>
<button onclick="newTicket()" style="font-size:11px;padding:3px 10px;
background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  + New Ticket
</button>
<span style="border-left:1px solid #2a2e37;align-self:stretch;margin-left:8px"></span>
<button onclick="toggleRuns()" style="font-size:11px;padding:3px 10px;
background:#6b7280;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Runs
</button>
<button onclick="openCostDashboard()" style="font-size:11px;padding:3px 10px;
background:#0d9488;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  💰 Cost
</button>
</header>
<div id="board"></div>
<div id="drawer"><span class="x" onclick="close_()">&times;</span><div id="d"></div></div>
<script src="https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js"></script>
<script>const ST={ST_STATES};</script>
<script src="/static/board.js"></script></body></html>"""
