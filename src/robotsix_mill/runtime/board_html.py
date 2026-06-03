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
<span class="muted">auto-refresh 1s</span>
<div class="agents-dropdown">
  <button class="agents-trigger" onclick="toggleAgentsMenu(event)">🤖 Agents ▾</button>
  <div class="agents-menu" id="agents-menu" onclick="event.stopPropagation()">
    <button onclick="runAudit()" data-agent="audit" style="--agent-color:#059669">Audit</button>
    <button onclick="runHealth()" data-agent="health" style="--agent-color:#0d9488">Health Check</button>
    <button onclick="runTestGap()" data-agent="test_gap" style="--agent-color:#7c3aed">Test Gaps</button>
    <button onclick="runTraceHealth()" data-agent="trace_health" style="--agent-color:#0ea5e9">Trace Health</button>
    <button onclick="runLangfuseCleanup()" data-agent="langfuse_cleanup" style="--agent-color:#14b8a6">Langfuse Cleanup</button>
    <button onclick="runAgentCheck()" data-agent="agent_check" style="--agent-color:#db2777">Agent Check</button>
    <button onclick="runSurvey()" data-agent="survey" style="--agent-color:#f59e0b">Survey</button>
    <button onclick="runBcCheck()" data-agent="bc_check" style="--agent-color:#84cc16">BC Check</button>
    <button onclick="runCompletenessCheck()" data-agent="completeness_check" style="--agent-color:#84cc16">Completeness</button>
    <button onclick="runCostReconciliation()" data-agent="cost_reconciliation" style="--agent-color:#6366f1">Cost Recon</button>
    <button onclick="runConfigSync()" data-agent="config_sync" style="--agent-color:#6366f1">Config Sync</button>
    <button onclick="runRoadmapSync()" data-agent="roadmap_sync" style="--agent-color:#9333ea">Roadmap Sync</button>
    <button onclick="runTraceReview()" data-agent="trace_review" style="--agent-color:#0ea5e9">Trace Review</button>
    <button onclick="runModuleCurator()" data-agent="module_curator" style="--agent-color:#f97316">Module Curator</button>
    <button onclick="runMeta()" data-agent="meta" class="meta-only" style="--agent-color:#a855f7">Meta</button>
  </div>
</div>
<button id="agentmd-btn" onclick="openCandidates()" style="font-size:11px;padding:3px 10px;
background:#2a1a3b;color:#c598fb;border:1px solid #3a2a4b;border-radius:4px;cursor:pointer;
margin-left:4px" title="AGENT.md candidates from retrospect — validate to file a draft, reject to dismiss">
  📋 AGENT.md<span id="agentmd-badge" style="display:none;margin-left:5px;padding:0 5px;border-radius:8px;background:#dc2626;color:#fff;font-size:10px;font-weight:bold"></span>
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
<div id="lf-status" style="display:none;background:#3a2418;border-bottom:1px solid #6b3320;color:#e8b08a;padding:6px 12px;font-size:12px"></div>
<div id="board"></div>
<div id="drawer"><div id="d"><div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div></div></div>
<script src="https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js"></script>
<script>const ST={ST_STATES};</script>
<script src="/static/board.js"></script></body></html>"""
