"""The HTML shell for the kanban board served at ``GET /``.

The board chrome core (columns, cards, move control, drawer, refresh loop)
is owned by robotsix-board.  Mill's own JavaScript (`board.js`) extends
this with mill-specific UI elements and behavior: ticket card display,
real-time WebSocket updates, drawer panels (runs, cost dashboard,
candidates, proposals), repo filtering via the selector dropdown, and the
closed-ticket visibility toggle.

This module provides the HTML skeleton that links to robotsix-board's
static assets and to mill-specific JS/CSS layered on top.

The ``{CONFIG_SCRIPT}`` placeholder is replaced at request time by
``render_config_script()`` from robotsix-board.  The ``{BOARD_SKELETON}``
placeholder is replaced by :func:`build_board_skeleton` — robotsix-board's
``board.js`` only diffs cards into *pre-existing* ``.board-column``
containers in JSON_HYDRATION mode, so the empty column skeleton must be
rendered server-side.
"""

from __future__ import annotations

import functools
import html as _html
import os
import time

# Process-start fallback token, captured once at import time.  Stable for
# the lifetime of a process (every request sees the same value) and fresh
# on each restart — used when ``MILL_BUILD_SHA`` is not baked into the image
# (e.g. a dev/uvicorn run).
_PROCESS_START_TOKEN = str(int(time.time()))


@functools.lru_cache(maxsize=1)
def asset_version() -> str:
    """Per-deploy cache-busting token for local static assets.

    Resolved once (cached) at startup and appended as a ``?v=<token>``
    query to every local script/css URL so browsers fetch fresh JS/CSS
    after a deploy instead of running a stale cached bundle.  Resolves, in
    order: the ``MILL_BUILD_SHA`` environment variable (a git short SHA
    baked into the image at build time), used when present and non-empty;
    otherwise a stable process-start token captured once at import time, so
    every request in a given process gets the same non-empty token and a
    process restart yields a fresh one.
    """
    return os.environ.get("MILL_BUILD_SHA", "").strip() or _PROCESS_START_TOKEN


def build_board_skeleton(columns: list[tuple[str, str]]) -> str:
    """Render the empty ``#board`` column skeleton expected by ``board.js``.

    robotsix-board's ``board.js`` in JSON_HYDRATION mode only diffs
    cards into existing ``.board-column > .board-column-cards``
    containers (see ``applyCardDiff``/``findColumnByStatus``); it never
    creates the columns itself.  The mill therefore renders the column
    skeleton here and lets ``board.js``'s refresh loop fill the cards in
    from ``/board/cards``.

    *columns* is the ordered ``(status_key, label)`` list from
    :meth:`MillBoardAdapter.columns`.
    """
    parts: list[str] = ['<div id="board" class="board">']
    for status_key, label in columns:
        key = _html.escape(status_key, quote=True)
        lbl = _html.escape(label, quote=True)
        parts.append(f'<div class="board-column" data-status="{key}">')
        parts.append('<div class="board-column-header">')
        parts.append(f'<h2 class="board-column-label">{lbl}</h2>')
        parts.append('<span class="board-column-count">0</span>')
        parts.append("</div>")  # .board-column-header
        parts.append('<div class="board-column-cards"></div>')
        parts.append("</div>")  # .board-column
    parts.append("</div>")  # #board
    return "".join(parts)


BOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>robotsix-mill</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/board.css?v={ASSET_VERSION}">
<link rel="stylesheet" href="/static/mill/board-mill.css?v={ASSET_VERSION}">
</head><body>
<header><h1>robotsix-mill</h1>
<span id="gates"></span>
<span class="muted" id="meta">loading…</span>
<select id="repo-selector" onchange="onRepoChange(this.value)" style="font-size:11px;background:#1d212c;border:1px solid #2c313d;color:#cfd3db;border-radius:4px;padding:3px 6px">
  <option value="all">All repos</option>
</select>
<button id="add-repo-btn" onclick="addRepo()" style="font-size:11px;padding:3px 10px;
background:#374151;color:#cfd3db;border:1px solid #4b5563;border-radius:4px;cursor:pointer;
margin-left:4px" title="Register a new repo">
  + Repo
</button>
<div class="agents-dropdown">
  <button class="agents-trigger" onclick="toggleAgentsMenu(event)">🤖 Agents ▾</button>
  <div class="agents-menu" id="agents-menu" onclick="event.stopPropagation()">
    <button onclick="runAudit()" data-agent="audit">Audit</button>
    <button onclick="runHealth()" data-agent="health">Health Check</button>
    <button onclick="runTestGap()" data-agent="test_gap">Test Gaps</button>
    <button onclick="runTraceHealth()" data-agent="trace_health">Trace Health</button>
    <button onclick="runLangfuseCleanup()" data-agent="langfuse_cleanup">Langfuse Cleanup</button>
    <button onclick="runAgentCheck()" data-agent="agent_check">Agent Check</button>
    <button onclick="runSurvey()" data-agent="survey">Survey</button>
    <button onclick="runBcCheck()" data-agent="bc_check">BC Check</button>
    <button onclick="runCompletenessCheck()" data-agent="completeness_check">Completeness</button>
    <button onclick="runRunHealth()" data-agent="run_health">Run Health</button>
    <button onclick="runConfigSync()" data-agent="config_sync">Config Sync</button>
    <button onclick="runMemberSync()" data-agent="member_sync">Member Sync</button>
    <button onclick="runRoadmapSync()" data-agent="roadmap_sync">Roadmap Sync</button>
    <button onclick="runTraceReview()" data-agent="trace_review">Trace Review</button>
    <button onclick="runModuleCurator()" data-agent="module_curator">Module Curator</button>
    <button onclick="runForgeParity()" data-agent="forge_parity">Forge Parity</button>
    <button onclick="runCopyPaste()" data-agent="copy_paste">Copy Paste</button>
    <button onclick="runStateSync()" data-agent="state_sync">State Sync</button>
    <button onclick="runFrontendSync()" data-agent="frontend_sync">Frontend Sync</button>
    <button onclick="runTriageBoilerplate()" data-agent="triage_boilerplate">Triage Boilerplate</button>
    <button onclick="runMeta()" data-agent="meta" class="meta-only">Meta</button>
  </div>
</div>
<button id="agentmd-btn" onclick="openCandidates()" style="font-size:11px;padding:3px 10px;
background:#2a1a3b;color:#c598fb;border:1px solid #3a2a4b;border-radius:4px;cursor:pointer;
margin-left:4px" title="AGENT.md candidates from retrospect">
  📋 AGENT.md<span id="agentmd-badge" style="display:none;margin-left:5px;padding:0 5px;border-radius:8px;background:#dc2626;color:#fff;font-size:10px;font-weight:bold"></span>
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
<button id="toggle-closed-btn" onclick="toggleClosed()" style="font-size:11px;padding:3px 10px;
background:#6b7280;color:#fff;border:none;border-radius:4px;cursor:pointer;
margin-left:4px">
  Show closed
</button>
</header>
<div id="lf-status" style="display:none;background:#3a2418;border-bottom:1px solid #6b3320;color:#e8b08a;padding:6px 12px;font-size:12px"></div>
<div id="credit-status" style="display:none;background:#3a2418;border-bottom:1px solid #6b3320;color:#e8b08a;padding:6px 12px;font-size:12px"></div>
{BOARD_SKELETON}
<div id="drawer"><div id="d"><div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div></div></div>
<script src="https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js"></script>
{CONFIG_SCRIPT}
<script src="/static/board.js?v={ASSET_VERSION}"></script>
<script src="/static/mill/board-mill.js?v={ASSET_VERSION}"></script></body></html>"""


def render_board_html(config_script: str, skeleton: str) -> str:
    """Render the full board HTML shell.

    Substitutes the ``{ASSET_VERSION}`` cache-busting token (see
    :func:`asset_version`), the robotsix-board ``{CONFIG_SCRIPT}``, and
    the server-rendered ``{BOARD_SKELETON}`` into :data:`BOARD_HTML`.
    """
    return (
        BOARD_HTML.replace("{ASSET_VERSION}", asset_version())
        .replace("{CONFIG_SCRIPT}", config_script)
        .replace("{BOARD_SKELETON}", skeleton)
    )
