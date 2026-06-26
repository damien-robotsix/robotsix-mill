(function() {
  "use strict";

  // ---------------------------------------------------------------------------
  // The canonical source of truth for ticket ``kind`` values is the Python
  // ``TicketKind`` StrEnum in ``src/robotsix_mill/core/models.py``:
  //
  //   class TicketKind(StrEnum):
  //       TASK = "task"
  //       INQUIRY = "inquiry"
  //       EPIC = "epic"
  //
  // The string literals "task", "inquiry", "epic" used throughout this file
  // MUST stay in sync with that enum.
  // ---------------------------------------------------------------------------

  // =========================================================================
  // State variables
  // =========================================================================
  const SHOW_CLOSED_KEY = "robotsix-mill:show-closed";
  let showClosed = (function() {
    try { return localStorage.getItem(SHOW_CLOSED_KEY) === "true"; }
    catch (_e) { return false; }
  })();
  let sel = null;
  let runsOpen = false;
  let refreshSeq = 0;
  let activeMap = {};
  let gatesCache = {};
  let reposCache = null;
  let currentRepoId = null;
  let mergeLoading = new Set();
  let candidatesOpen = false;
  let _runsLastSig = null;
  const _detailLast = {};
  let wsReconnectTimer = null;
  let wsActive = false;
  let wsReconnectDelay = 2000;
  let wsKeepaliveTimer = null;
  const WS_RECONNECT_MAX = 30000;
  const WS_RECONNECT_BASE = 2000;

  // =========================================================================
  // Constants
  // =========================================================================
  const ACTIVE_LABEL = {
    refine: "refining…",
    implement: "implementing…",
    document: "documenting…",
    review: "reviewing…",
    deliver: "delivering…",
    merge: "merging…",
    ci_fix: "fixing CI…",
    retrospect: "retrospecting…"
  };

  const AGENT_COLORS = {
    audit: '#059669',
    health: '#0d9488',
    test_gap: '#7c3aed',
    trace_health: '#0ea5e9',
    langfuse_cleanup: '#14b8a6',
    agent_check: '#db2777',
    survey: '#f59e0b',
    bc_check: '#84cc16',
    completeness_check: '#84cc16',
    run_health: '#3b82f6',
    config_sync: '#6366f1',
    member_sync: '#0891b2',
    roadmap_sync: '#9333ea',
    trace_review: '#0ea5e9',
    module_curator: '#f97316',
    forge_parity: '#a78bfa',
    copy_paste: '#ec4899',
    state_sync: '#0891b2',
    env_doc_sync: '#7c3aed',
    frontend_sync: '#06b6d4',
    meta: '#a855f7',
  };

  const SOURCE_CLASS = {
    retrospect: "retrospect",
    audit: "audit",
    config_sync: "config-sync",
    member_sync: "member-sync",
    "trace-health": "trace-health",
    health: "health",
    test_gap: "test-gap",
    agent: "agent",
    survey: "survey",
    ci: "ci",
    agent_check: "agent-check",
    bc_check: "bc-check",
    completeness_check: "completeness-check",
    "trace-review": "trace-review",
    roadmap_sync: "roadmap-sync",
    forge_parity: "forge-parity",
    frontend_sync: "frontend-sync",
    module_curator: "module-curator",
    copy_paste: "copy-paste",
    state_sync: "state-sync",
    env_doc_sync: "env-doc-sync",
    data_dir_audit: "data-dir-audit",
    meta: "meta",
    "run-health": "run-health",
    ci_fix_dependency: "ci-fix-dependency",
    dependabot_alerts: "dependabot-alerts",
    implement_baseline_dependency: "implement-baseline-dependency",
  };

  const STATE_ARTIFACT = {
    human_issue_approval: "draft-original.md",
    ready: "file_map.json",
    code_review: "review.md",
    documenting: "",
    implement_complete: "deliver.md",
    human_mr_approval: "merge.md",
    waiting_auto_merge: "merge.md",
    fixing_ci: "ci_fix.md",
    rebasing: "merge.md",
    done: "merge.md",
    closed: "retrospect.md",
    answered: "question-original.md",
  };

  const STEP_LABEL = [
    ["implement:",          "implement",          "implement.md"],
    ["scope-triage EXPAND", "scope-triage",       ""],
    ["scope-triage REJECT", "scope-triage",       ""],
    ["scope-triage ESCAL",  "scope-triage",       ""],
    ["doc_classifier:",     "doc_classifier",     ""],
    ["merge:",              "merge",              "merge.md"],
    ["review:",             "review",             "review.md"],
    ["epic-breakdown",      "epic-breakdown",     ""],
  ];

  const STATE_TRACE = {
    ready: "refine",
    human_issue_approval: "refine",
    code_review: "review",
    documenting: "document",
    deliverable: "deliver",
    implement_complete: "deliver",
    maintenance: "maintenance",
    human_mr_approval: "merge",
    addressing_review: "merge",
    waiting_auto_merge: "merge",
    fixing_ci: "ci_fix",
    rebasing: "rebase",
    done: "merge",
    closed: "retrospect",
    answered: "answer",
  };

  // =========================================================================
  // Helpers
  // =========================================================================
  function esc(s) {
    return (s || "").replace(/[&<>]/g, function(c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c];
    });
  }

  function jsq(s) {
    return esc(JSON.stringify(String(s == null ? "" : s))).replace(/"/g, "&quot;");
  }

  function renderMD(s) {
    if (!s) return "";
    return marked.parse(s);
  }

  function srcClass(s) {
    return SOURCE_CLASS[s] || "user";
  }

  function matchStep(note) {
    if (!note) return null;
    for (var i = 0; i < STEP_LABEL.length; i++) {
      var pfx = STEP_LABEL[i][0];
      var label = STEP_LABEL[i][1];
      var art = STEP_LABEL[i][2];
      if (note.startsWith(pfx)) return { label: label, art: art };
    }
    return null;
  }

  // Extract the stage name from a Langfuse trace breadcrumb note, which
  // the worker writes as "🔍 [Trace: <stage>](url)". These notes reuse
  // the ticket's current state, so without this they'd show as a
  // repeated state chip (e.g. "ready, ready, ready" for 3 implement
  // passes) instead of naming the stage that actually ran.
  function parseTraceStage(note) {
    if (!note) return null;
    var m = note.match(/\[Trace:\s*([^\]]+)\]/);
    return m ? m[1].trim() : null;
  }

  // Resolve the chip label/class/artifact for one history event. Prefers
  // the *stage that ran* over the raw destination state: the first event
  // is "created"; side-band notes (trace breadcrumbs, scope-triage, etc.)
  // show the stage name; real transitions show "stage → state" when the
  // producing stage is known, else the bare state.
  function eventChip(e, prev, idx) {
    var artFor = function(step) {
      return (step && step.art) ? step.art : (STATE_ARTIFACT[e.state] || "");
    };
    if (idx === 0) {
      return { label: "created", cls: "s-" + e.state, art: artFor(null) };
    }
    var isStep = prev && prev.state === e.state;
    var step = matchStep(e.note);
    var traceStage = parseTraceStage(e.note);
    if (isStep) {
      var stepLabel = step ? step.label : (traceStage || e.state);
      return { label: stepLabel, cls: "ev-step", art: artFor(step) };
    }
    // Real transition: name the producing stage if we can derive one.
    var stage = (step && step.label) || traceStage || STATE_TRACE[e.state] || null;
    var label = stage ? (stage + " → " + e.state) : e.state;
    return { label: label, cls: "s-" + e.state, art: artFor(step) };
  }

  function eventAgentName(event, isStep, step) {
    return isStep ? (step ? step.label : null) : STATE_TRACE[event.state];
  }

  function buildEventTraceMap(events, traces) {
    var map = {};
    var sortedTraces = (traces || []).slice().sort(function(a, b) {
      return new Date(a.at).getTime() - new Date(b.at).getTime();
    });
    for (var ti = 0; ti < sortedTraces.length; ti++) {
      var trace = sortedTraces[ti];
      var tts = new Date(trace.at).getTime();
      for (var i = 0; i < events.length; i++) {
        if (map[i]) continue;
        var e = events[i];
        var prev = events[i - 1];
        var isStep = !!prev && prev.state === e.state;
        var step = isStep ? matchStep(e.note) : null;
        var name = eventAgentName(e, isStep, step);
        if (name !== trace.name) continue;
        var ets = new Date(e.at).getTime();
        if (ets < tts - 5000) continue;
        map[i] = trace;
        break;
      }
    }
    return map;
  }

  function fmtRelative(iso) {
    var d = (new Date(iso)).getTime() - Date.now();
    if (d <= 0) return "now";
    var s = Math.round(d / 1000);
    if (s < 60) return "in " + s + "s";
    var m = Math.round(s / 60);
    if (m < 60) return "in " + m + "m";
    return new Date(iso).toLocaleTimeString();
  }

  // HTTP helpers built on XMLHttpRequest
  function jget(u) {
    return new Promise(function(res) {
      var x = new XMLHttpRequest();
      x.open("GET", u, true);
      x.onload = function() {
        if (x.status >= 200 && x.status < 300) {
          try { res(JSON.parse(x.responseText)); } catch (e) { res(null); }
        } else { res(null); }
      };
      x.onerror = function() { res(null); };
      x.send();
    });
  }

  function _xhr(method, u, body) {
    return new Promise(function(res) {
      var x = new XMLHttpRequest();
      x.open(method, u, true);
      if (body != null) x.setRequestHeader("Content-Type", "application/json");
      var wrap = function() {
        return {
          ok: x.status >= 200 && x.status < 300,
          status: x.status,
          text: function() { return Promise.resolve(x.responseText || ""); },
          json: function() {
            try { return Promise.resolve(JSON.parse(x.responseText || "null")); }
            catch (e) { return Promise.reject(e); }
          }
        };
      };
      x.onload = function() { res(wrap()); };
      x.onerror = function() {
        res({
          ok: false, status: 0,
          text: function() { return Promise.resolve("network error"); },
          json: function() { return Promise.resolve(null); }
        });
      };
      x.send(body != null ? JSON.stringify(body) : null);
    });
  }

  function jpost(u, body) { return _xhr("POST", u, body); }
  function jdel(u) { return _xhr("DELETE", u, null); }

  // =========================================================================
  // Agent color helpers
  // =========================================================================
  function agentColor(kind) {
    var k = String(kind || '').replace(/-/g, '_');
    return AGENT_COLORS[k] || '#6b7280';
  }

  function applyAgentColors() {
    document.querySelectorAll('.agents-menu button[data-agent]').forEach(function(b) {
      b.style.setProperty('--agent-color', agentColor(b.dataset.agent));
    });
  }

  // =========================================================================
  // Repo selector
  // =========================================================================
  function getRepoId() {
    if (currentRepoId !== null) return currentRepoId;
    var params = new URLSearchParams(window.location.search);
    currentRepoId = params.get("repo") || localStorage.getItem("robotsix-mill:repo-id") || "all";
    return currentRepoId;
  }

  function onRepoChange(value) {
    currentRepoId = value;
    localStorage.setItem("robotsix-mill:repo-id", value);
    var url = new URL(window.location);
    if (value === "all") url.searchParams.delete("repo");
    else url.searchParams.set("repo", value);
    window.history.replaceState({}, "", url);
    toggleMetaOnlyButtons();
    updateAgentsMenu();
    refresh();
  }

  async function fetchRepos() {
    if (reposCache) return reposCache;
    var data = await jget("/repos");
    reposCache = data || [];
    var selEl = document.getElementById("repo-selector");
    if (!selEl) return reposCache;
    var cur = getRepoId();
    if (reposCache.length <= 1) {
      selEl.innerHTML = reposCache.map(function(r) {
        return '<option value="' + esc(r.repo_id) + '">' + esc(r.repo_id) + '</option>';
      }).join("");
      if (reposCache.length === 1) onRepoChange(reposCache[0].repo_id);
      selEl.value = currentRepoId;
    } else {
      selEl.innerHTML = '<option value="all">All repos</option>' +
        reposCache.map(function(r) {
          return '<option value="' + esc(r.repo_id) + '">' + esc(r.repo_id) + '</option>';
        }).join("");
      selEl.value = cur === "all" || !reposCache.some(function(r) { return r.repo_id === cur; }) ? "all" : cur;
    }
    return reposCache;
  }

  function repoIdForBoardId(boardId) {
    if (!reposCache || !boardId) return boardId;
    var r = reposCache.find(function(r) { return r.board_id === boardId; });
    return r ? r.repo_id : boardId;
  }

  // =========================================================================
  // Gates
  // =========================================================================
  async function fetchGates() {
    window.robotsixBoardSetGateEndpoint('/gates');
    var repoId = getRepoId();
    var gatesUrl = repoId !== "all" ? "/gates?repo_id=" + encodeURIComponent(repoId) : "/gates";
    var g = await jget(gatesUrl);
    if (!g) return;
    gatesCache = g;
    if (window.robotsixBoardSetGate) window.robotsixBoardSetGate(g);
    var gatesEl = document.getElementById("gates");
    if (!gatesEl) return;
    gatesEl.innerHTML = [
      { key: "auto_approve", label: "auto-approve", on: g.auto_approve,
        yaml: "gates.auto_approve_enabled",
        tip: "Cheap-LLM auto-approves safe refined specs; when off, every ticket pauses at human_issue_approval" },
      { key: "review", label: "review", on: g.review,
        yaml: "gates.review_enabled",
        tip: "Dual-model code review before deliver; when off, tickets skip code_review" },
      { key: "auto_merge", label: "auto-merge", on: g.auto_merge,
        yaml: "gates.auto_merge_enabled",
        tip: "Auto-merge green PRs after review approves; when off, tickets stop at waiting_auto_merge" },
      { key: "require_approval", label: "require-approval", on: g.require_approval,
        yaml: "gates.require_approval",
        tip: "Human approval gate on refine output; when off, tickets skip human_issue_approval" }
    ].map(function(p) {
      return '<span class="gate-pill ' + (p.on ? "gate-on" : "gate-off") +
        '" title="' + esc(p.yaml) + ' — ' + esc(p.tip) + '">' +
        esc(p.label) + ' ' + (p.on ? "✓" : "✗") + '</span>';
    }).join("");
  }

  // =========================================================================
  // Langfuse status
  // =========================================================================
  async function fetchLangfuseStatus() {
    var s = await jget("/langfuse-status");
    if (!s) return;
    var banner = document.getElementById("lf-status");
    if (!banner) return;
    if (!s.count) {
      banner.style.display = "none";
      banner.innerHTML = "";
      return;
    }
    var last = s.failures[s.failures.length - 1];
    banner.style.display = "block";
    banner.innerHTML =
      '<span class="lf-badge">⚠ Langfuse export issues</span> ' +
      s.count + ' recent failure(s). Latest: ' + esc(last.project || "?") + ' — ' +
      '<code>' + esc((last.error || "").slice(0, 200)) + '</code> ' +
      '<button onclick="dismissLfStatus()" class="lf-dismiss">dismiss</button>';
  }

  async function dismissLfStatus() {
    await jpost("/langfuse-status/clear", {});
    fetchLangfuseStatus();
  }

  // =========================================================================
  // Credit status
  // =========================================================================
  async function fetchCreditStatus() {
    var s = await jget("/credit-status");
    if (!s) return;
    var banner = document.getElementById("credit-status");
    if (!banner) return;
    if (!s.low) {
      banner.style.display = "none";
      banner.innerHTML = "";
      return;
    }
    var balance = s.balance_usd != null ? "$" + Number(s.balance_usd).toFixed(2) : "?";
    var threshold = s.threshold_usd != null ? "$" + Number(s.threshold_usd).toFixed(2) : "?";
    banner.style.display = "block";
    banner.innerHTML =
      '<span class="lf-badge">⚠ Low OpenRouter credit</span> ' +
      'Balance: ' + balance + ' (threshold ' + threshold + '). ' +
      '<a href="https://openrouter.ai/credits" target="_blank" style="color:#fbbf24;text-decoration:underline">Top up</a>' +
      (s.last_402_at ? ' · Last 402: ' + esc(s.last_402_at) : '') +
      ' <button onclick="dismissCreditStatus()" class="lf-dismiss">dismiss</button>';
  }

  async function dismissCreditStatus() {
    await jpost("/credit-status/clear", {});
    fetchCreditStatus();
  }

  // =========================================================================
  // Active labels
  // =========================================================================
  async function fetchActive() {
    var repoId = getRepoId();
    var activeUrl = repoId !== "all" ? "/active?repo_id=" + encodeURIComponent(repoId) : "/active";
    var activeList = await jget(activeUrl);
    var active = {};
    if (activeList) activeList.forEach(function(a) { active[a.ticket_id] = a; });
    activeMap = active;
  }

  function applyActiveLabels() {
    document.querySelectorAll('.board-card').forEach(function(card) {
      var ticketId = card.dataset.cardId;
      if (!ticketId) return;
      // Remove existing live-badge
      var existing = card.querySelector('.live-badge');
      if (existing) existing.remove();
      var a = activeMap[ticketId];
      if (!a) return;
      var col = card.closest('.col');
      var colState = col ? col.dataset.state : '';
      var label = colState === 'rebasing' ? 'rebasing…' : (ACTIVE_LABEL[a.stage] || a.stage + '…');
      var badge = document.createElement('span');
      badge.className = 'live-badge';
      badge.innerHTML = '<span class="live-spinner"></span> ' + label;
      card.appendChild(badge);
    });
    hideEmptyColumns();
  }

  function applyMoveButtons() {
    document.querySelectorAll('.board-card').forEach(function(card) {
      var ticketId = card.dataset.cardId;
      if (!ticketId) return;
      var existing = card.querySelector('.move-btn');
      if (existing) return;
      var btn = document.createElement('button');
      btn.className = 'move-btn';
      btn.textContent = 'Move to board…';
      btn.title = 'Move ticket to another board';
      btn.setAttribute('style', 'background:#374151;color:#cfd3db;border:1px solid #4b5563;border-radius:4px;padding:2px 6px;font-size:10px;cursor:pointer;margin-top:4px');
      btn.onclick = function(e) {
        e.stopPropagation();
        e.preventDefault();
        moveToBoard(ticketId);
      };
      card.appendChild(btn);
    });
  }

  // Hide board columns that currently hold no cards. robotsix-board's
  // board.js renders every configured column (22 of them) whether or
  // not it has tickets; the mill only wants populated columns visible.
  // Uses an inline display toggle so it composes with board.js's
  // "Show closed" control (which hides via the .hidden class):
  // style.display="" on a non-empty column lets that class still hide
  // it, while style.display="none" wins for empty columns.
  function hideEmptyColumns() {
    var cols = document.querySelectorAll('#board .board-column');
    for (var i = 0; i < cols.length; i++) {
      var n = cols[i].querySelectorAll(
        '.board-column-cards > .board-card'
      ).length;
      cols[i].style.display = n === 0 ? 'none' : '';
    }
    applyClosedVisibility();
  }

  // Show or hide the terminal columns (closed + epic_closed) according to
  // the current `showClosed` state. Uses the `.hidden` class (which
  // robotsix-board's board.css renders as `display:none`) so it composes
  // with hideEmptyColumns' inline `style.display` toggle: an empty
  // closed column stays hidden via the inline rule, while a non-empty one
  // is hidden only when `.hidden` is present.
  function applyClosedVisibility() {
    var sels = '#board .board-column[data-status="closed"],' +
      ' #board .board-column[data-status="epic_closed"]';
    var cols = document.querySelectorAll(sels);
    for (var i = 0; i < cols.length; i++) {
      cols[i].classList.toggle('hidden', !showClosed);
    }
  }

  // Flip the show/hide-closed state, persist the preference, update the
  // button label, and re-apply column visibility. refresh() re-runs the
  // board fetch/render; applyClosedVisibility (re-invoked via
  // hideEmptyColumns) then hides/shows the closed + epic_closed columns.
  function toggleClosed() {
    showClosed = !showClosed;
    try { localStorage.setItem(SHOW_CLOSED_KEY, String(showClosed)); }
    catch (_e) { /* localStorage may be unavailable */ }
    var btn = document.getElementById("toggle-closed-btn");
    if (btn) btn.textContent = showClosed ? "Hide closed" : "Show closed";
    applyClosedVisibility();
    refresh();
  }

  // Strip the redundant "Show closed" checkbox (#board-closed-toggle) that
  // robotsix-board's attachClosedToggle() injects before #board. The mill
  // header button #toggle-closed-btn is the canonical control (it toggles
  // both the closed and epic_closed columns and persists under the mill
  // localStorage key), so the upstream checkbox is a stale duplicate. No-op
  // when the element is absent.
  function removeDuplicateClosedToggle() {
    var el = document.getElementById("board-closed-toggle");
    if (el) el.remove();
  }

  // =========================================================================
  // WebSocket
  // =========================================================================
  function connectWebSocket() {
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    var proto = window.location.protocol === "https:" ? "wss" : "ws";
    var qs = "show_closed=" + (showClosed ? "true" : "false");
    var url = proto + "://" + window.location.host + "/ws/board?" + qs;
    var sock = new WebSocket(url);
    sock.onopen = function() {
      wsActive = true;
      wsReconnectDelay = WS_RECONNECT_BASE;
      if (wsKeepaliveTimer) clearInterval(wsKeepaliveTimer);
      wsKeepaliveTimer = setInterval(refresh, 30000);
    };
    sock.onmessage = function(evt) {
      try {
        var msg = JSON.parse(evt.data);
        if (msg.type === "ticket_list") {
          refresh();
        } else if (msg.type === "ticket_update") {
          window.robotsixBoardRefresh();
          setTimeout(function() { fetchActive().then(applyActiveLabels).then(applyMoveButtons); }, 500);
        }
      } catch (e) { /* ignore malformed messages */ }
    };
    sock.onclose = function() {
      wsActive = false;
      if (wsKeepaliveTimer) { clearInterval(wsKeepaliveTimer); wsKeepaliveTimer = null; }
      wsReconnectTimer = setTimeout(connectWebSocket, wsReconnectDelay);
      wsReconnectDelay = Math.min(wsReconnectDelay * 2, WS_RECONNECT_MAX);
    };
    sock.onerror = function() {
      sock.close();
    };
  }

  // =========================================================================
  // History & threads rendering (for drawer)
  // =========================================================================
  async function toggleEvent(summaryEl) {
    var wrap = summaryEl.parentElement;
    var detail = wrap.querySelector(".ev-detail");
    var arrow = summaryEl.querySelector(".ev-arrow");
    var open = wrap.dataset.open === "1";
    if (!open) {
      detail.style.display = "block";
      wrap.dataset.open = "1";
      if (arrow && arrow.textContent === "▶") arrow.textContent = "▼";
      var art = wrap.dataset.art;
      var tid = wrap.dataset.tid;
      var aEl = wrap.querySelector(".ev-artifact");
      if (art && aEl && aEl.dataset.loaded === "0") {
        aEl.dataset.loaded = "1";
        try {
          var r = await jget("/tickets/" + encodeURIComponent(tid) + "/artifacts/" + encodeURIComponent(art));
          if (r && r.content) {
            aEl.innerHTML = '<details open><summary class="muted" style="cursor:pointer;font-size:11px">📄 ' + esc(art) + '</summary><div class="md-body" style="margin-top:6px">' + renderMD(r.content) + '</div></details>';
          } else {
            aEl.innerHTML = '<span class="muted" style="font-size:11px">(' + esc(art) + ' not yet written)</span>';
          }
        } catch (_) {
          aEl.innerHTML = '<span class="muted" style="font-size:11px">(' + esc(art) + ' not yet written)</span>';
        }
      }
    } else {
      detail.style.display = "none";
      wrap.dataset.open = "0";
      if (arrow && arrow.textContent === "▼") arrow.textContent = "▶";
    }
  }

  function renderHistoryHtml(history, ticketId, traces) {
    var events = history || [];

    // --- merge trace-link breadcrumbs into their matching transition ---
    // A stage run writes two events: a breadcrumb note (🔍 [Trace: <stage>]) and a
    // transition.  Pair them here so renderHistoryHtml produces one row per action.
    var absorbed = new Set();   // original indices of breadcrumb events to skip
    var bcForIdx = {};          // transition index → { event, idx } of the absorbed breadcrumb

    for (var bi = 0; bi < events.length; bi++) {
      var be = events[bi];
      var bcStage = parseTraceStage(be.note);
      if (!bcStage) continue;
      var bPrev = events[bi - 1];
      if (!bPrev || bPrev.state !== be.state) continue;  // must be a step
      var next = events[bi + 1];
      if (!next) continue;                                 // nothing to pair with
      if (next.state === be.state) continue;               // not a state transition
      var nextStep = matchStep(next.note);
      var nextTraceStage = parseTraceStage(next.note);
      var transitionStage = (nextStep && nextStep.label) || nextTraceStage || STATE_TRACE[next.state] || null;
      if (transitionStage === bcStage) {
        absorbed.add(bi);
        bcForIdx[bi + 1] = { event: be, idx: bi };
      }
    }

    var costByIndex = buildEventTraceMap(events, traces || []);
    var claimed = new Set(Object.values(costByIndex).map(function(t) { return t.trace_id; }));
    var orphanRows = (traces || [])
      .filter(function(t) { return !claimed.has(t.trace_id) && (t.latency === undefined || t.latency > 0); })
      .map(function(t) {
        return { __orphan: true, at: t.at, name: t.name, cost: t.cost, trace_id: t.trace_id };
      });
    var merged = [];
    events.forEach(function(e, i) { merged.push(Object.assign({}, e, { __idx: i })); });
    orphanRows.forEach(function(o) { merged.push(o); });
    merged.sort(function(a, b) {
      var ta = new Date(a.at).getTime();
      var tb = new Date(b.at).getTime();
      return ta - tb;
    });
    // Dedupe repeated implement_complete (deliver.md) cards from ci_fix
    // loops: only the last such event gets its artifact rendered.
    // Also suppress the artifact placeholder for fixing_ci rows so the
    // drawer doesn't show "(ci_fix.md not yet written)" noise.
    var suppressArt = new Set();
    var deliverLast = -1;
    for (var si = 0; si < events.length; si++) {
      if (absorbed.has(si)) continue;
      if (events[si].state === "implement_complete") deliverLast = si;
    }
    for (var si = 0; si < events.length; si++) {
      if (absorbed.has(si)) continue;
      if (events[si].state === "implement_complete" && si !== deliverLast) {
        suppressArt.add(si);
      }
      if (events[si].state === "fixing_ci") {
        suppressArt.add(si);
      }
    }
    return '<h3>History</h3>' + merged.map(function(item) {
      if (item.__orphan) {
        return '<div class="ev ev-is-step ev-orphan" data-tid="' + esc(ticketId) + '" data-art="" data-open="0">' +
          '<div class="ev-summary" onclick="toggleEvent(this)">' +
          '<span class="ev-arrow">·</span>' +
          '<span class="ev-at muted">' + item.at + '</span>' +
          '<b class="ev-state ev-step" title="No history event matched this trace — probably an interrupted run">interrupted: ' + esc(item.name) + '</b>' +
          '<span class="ev-cost" title="Langfuse trace ' + esc(item.trace_id) + '">$' + item.cost.toFixed(4) + '</span>' +
          '</div>' +
          '<div class="ev-detail" style="display:none">' +
          '<div class="muted" style="font-size:11px">Langfuse trace ' + esc(item.trace_id) + ' ran at ' + item.at + ' (' + esc(item.name) + ', $' + item.cost.toFixed(4) + ') but no history event was written — the stage was interrupted before its transition committed.</div>' +
          '</div>' +
          '</div>';
      }
      var e = item;
      var i = item.__idx;
      if (absorbed.has(i)) return '';
      var bcData = bcForIdx[i];
      var bcEvent = bcData ? bcData.event : null;
      var bcIdx = bcData ? bcData.idx : -1;
      var prev = events[i - 1];
      var isStep = prev && prev.state === e.state;
      var chip = eventChip(e, prev, i);
      var chipLabel = chip.label;
      var chipClass = chip.cls;
      var art = suppressArt.has(i) ? "" : chip.art;
      var hasDetail = !!(e.note || art || (bcEvent && bcEvent.note));
      var trace = costByIndex[i] || (bcIdx >= 0 ? costByIndex[bcIdx] : null);
      var cost = trace ? '<span class="ev-cost" title="Langfuse trace ' + esc(trace.trace_id) + '">$' + trace.cost.toFixed(4) + '</span>' : "";
      return '<div class="ev' + (isStep ? " ev-is-step" : "") + '" data-tid="' + esc(ticketId) + '" data-art="' + esc(art) + '" data-open="0">' +
        '<div class="ev-summary" onclick="toggleEvent(this)">' +
        '<span class="ev-arrow">' + (hasDetail ? "▶" : "·") + '</span>' +
        '<span class="ev-at muted">' + e.at + '</span>' +
        '<b class="ev-state ' + chipClass + '">' + esc(chipLabel) + '</b>' +
        cost +
        '</div>' +
        '<div class="ev-detail" style="display:none">' +
        (e.note ? '<div class="ev-note">' + renderMD(e.note) + '</div>' : "") +
        (bcEvent && bcEvent.note ? '<div class="ev-note">' + renderMD(bcEvent.note) + '</div>' : "") +
        (art ? '<div class="ev-artifact" data-loaded="0"><span class="muted">Click expand for ' + esc(art) + '…</span></div>' : "") +
        '</div>' +
        '</div>';
    }).join("");
  }

  function renderMergeInfo(mi) {
    var ciHtml = "";
    if (mi.ci_conclusion === "success") ciHtml = '<span class="mi-ok">✓</span> CI passing';
    else if (mi.ci_conclusion === "failure") {
      var names = mi.ci_failing.map(function(f) { return esc(f.name); }).join(", ");
      ciHtml = '<span class="mi-bad">✗</span> CI failing';
      if (names) ciHtml += ' — ' + names;
    } else if (mi.ci_conclusion === "pending") ciHtml = '<span class="mi-pending">◷</span> CI pending…';
    else ciHtml = '<span class="mi-unknown">—</span> CI unknown';

    var mgHtml = "";
    if (mi.mergeable === true) mgHtml = '<span class="mi-ok">✓</span> No conflicts';
    else if (mi.mergeable === false) mgHtml = '<span class="mi-bad">✗</span> Conflicts detected';
    else mgHtml = '<span class="mi-unknown">—</span> Checking conflicts…';

    var filesHtml = "";
    if (mi.files && mi.files.length) {
      filesHtml = '<div class="mi-files-header">' + mi.files.length + ' file' + (mi.files.length !== 1 ? "s" : "") + ' changed</div>';
      filesHtml += mi.files.map(function(f) {
        var a = "", d = "";
        if (f.additions) a = '<span class="mi-add">+' + f.additions + '</span> ';
        if (f.deletions) d = '<span class="mi-del">−' + f.deletions + '</span> ';
        return '<div class="mi-file">' + a + d + '<span class="mi-path">' + esc(f.path) + '</span> <span class="mi-status">' + esc(f.status) + '</span></div>';
      }).join("");
    } else {
      filesHtml = '<div class="mi-files-header muted">(no file info available)</div>';
    }

    return '<div class="mi-section">' +
     '<h3>Merge Info</h3>' +
     '<div class="mi-row">' + ciHtml + '</div>' +
     '<div class="mi-row">' + mgHtml + '</div>' +
     filesHtml +
     '</div>';
  }

  function renderThreads(cs) {
    var threads = cs.filter(function(c) { return c.parent_id === null; });
    var askUserThreads = threads.filter(function(t) { return t.body && t.body.startsWith("[ASK_USER]") && t.closed_at === null; });
    var normalThreads = threads.filter(function(t) { return !askUserThreads.includes(t); });
    var replies = cs.filter(function(c) { return c.parent_id !== null; });
    var replyMap = {};
    replies.forEach(function(r) { (replyMap[r.parent_id] = replyMap[r.parent_id] || []).push(r); });

    function renderOneThread(t) {
      var isClosed = t.closed_at !== null;
      var children = replyMap[t.id] || [];
      var replyHtml = children.map(function(r) {
        return '<div class="ev reply-ev"><b class="muted">' + r.created_at + '</b> · <b>' + esc(r.author) + '</b>' +
          (r.author === "scope-triage" ? ' <span class="triage-badge">🤖 triage</span>' : '') +
          '<br>' + renderMD(r.body) + '</div>';
      }).join("");
      return '<div class="thread' + (isClosed ? ' thread-closed' : '') + '">' +
       '<div class="ev"><b class="muted">' + t.created_at + '</b> · <b>' + esc(t.author) + '</b>' +
         (t.author === "scope-triage" ? ' <span class="triage-badge">🤖 triage</span>' : '') +
         (isClosed ? ' <span class="closed-badge">🔒 Closed</span>' : '') +
         '<br>' + renderMD(t.body) + '</div>' +
       replyHtml +
       '<div class="thread-actions">' +
        '<button class="add-comment-btn" onclick="replyToThread(' + jsq(t.id) + ',' + jsq(t.ticket_id) + ')">↩ Reply</button>' +
        (isClosed
          ? '<button class="add-comment-btn" onclick="reopenThread(' + jsq(t.id) + ',' + jsq(t.ticket_id) + ')">🔓 Reopen</button>'
          : '<button class="add-comment-btn" onclick="closeThread(' + jsq(t.id) + ',' + jsq(t.ticket_id) + ')">🔒 Close</button>') +
       '</div>' +
      '</div>';
    }

    var html = "";
    if (askUserThreads.length > 0) {
      html += '<div class="ask-user-cta"><strong>🙋 This ticket is waiting on your reply.</strong> Reply to the question below and close the thread to resume the ticket.</div>';
      html += '<div class="ask-user-threads">' + askUserThreads.map(renderOneThread).join("") + '</div>';
    }
    html += normalThreads.map(renderOneThread).join("");
    return html;
  }

  // =========================================================================
  // Ticket action buttons (header area)
  // =========================================================================
  function _actionButtonsHtml(t) {
    if (!t) return "";
    var redraftable = ['draft', 'human_issue_approval', 'closed', 'answered', 'epic_closed', 'epic_open', 'done'].indexOf(t.state) === -1;
    var prioLabel = t.priority ? "⚡ Priority on" : "⚡ Set priority";
    var prioClass = t.priority ? "prio-btn prio-btn-on" : "prio-btn";
    return (t.state === "human_issue_approval" ?
      '<button class="approve-btn" onclick="event.stopPropagation();approve(' + jsq(t.id) + ')">Approve</button>' +
      '<button class="reject-btn" title="Send back to draft with a comment" onclick="event.stopPropagation();requestChanges(' + jsq(t.id) + ')">Request Changes</button>' : "") +
      (redraftable ?
        '<button class="redraft-btn" title="Send back to draft" onclick="event.stopPropagation();redraft(' + jsq(t.id) + ')">Redraft</button>' : "") +
      '<button class="' + prioClass + '" title="Pulled from the queue ahead of non-priority tickets" onclick="event.stopPropagation();togglePriority(' + jsq(t.id) + ',' + (t.priority ? "false" : "true") + ')">' + prioLabel + '</button>' +
      (t.kind === "inquiry" && t.state === "answered" ?
        '<button class="redraft-btn" title="Turn this Q&A into an actionable task" onclick="event.stopPropagation();convertToTicket(' + jsq(t.id) + ')">Convert to ticket</button>' : "") +
      '<button class="move-btn" title="Move ticket to another board" style="background:#374151;color:#cfd3db;border:1px solid #4b5563;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;margin-left:4px;margin-top:5px;display:inline-block" onclick="event.stopPropagation();moveToBoard(' + jsq(t.id) + ',' + jsq(t.board_id) + ')">Move to board…</button>' +
      '<button class="del-btn" title="Delete ticket" style="position:static;opacity:1;margin-left:4px;margin-top:5px;display:inline-block" onclick="event.stopPropagation();del_(' + jsq(t.id) + ')">✕</button>';
  }

  // In-flight button locking. Wraps an action so the clicked button AND
  // its sibling action buttons are greyed out until the request settles.
  // Locking the whole group (not just the clicked button) is deliberate:
  // for opposing pairs (Approve/Reject, Close/Reopen) a slow POST must
  // not let the user fire the opposite action mid-flight. The clicked
  // button is resolved from window.event — the inline onclick handlers
  // don't pass `this` — so it MUST be captured synchronously, before the
  // first await. When no event is available (programmatic call, tests)
  // the action runs unlocked.
  function _actionGroupButtons() {
    var ev = window.event;
    var t = ev && ev.target;
    var btn = t && t.closest ? t.closest("button") : null;
    if (!btn) return { btn: null, all: [] };
    var group =
      btn.closest(".pa-buttons") ||
      btn.closest("#ticket-action-buttons") ||
      btn.parentElement;
    var all = group && group.querySelectorAll
      ? Array.prototype.slice.call(group.querySelectorAll("button"))
      : [btn];
    return { btn: btn, all: all.length ? all : [btn] };
  }

  async function lockWhile(fn) {
    var g = _actionGroupButtons();
    var prev = g.all.map(function (b) { return b.disabled; });
    g.all.forEach(function (b) { b.disabled = true; });
    if (g.btn && g.btn.classList) g.btn.classList.add("btn-busy");
    try {
      return await fn();
    } finally {
      // If the action re-rendered the panel these nodes are detached and
      // the restore is a harmless no-op on the old elements.
      if (g.btn && g.btn.classList) g.btn.classList.remove("btn-busy");
      g.all.forEach(function (b, i) { b.disabled = prev[i]; });
    }
  }

  async function togglePriority(id, want) {
    await lockWhile(async function () {
      var r = await jpost("/tickets/" + id + "/priority", { priority: want === "true" || want === true });
      if (!r.ok) { var e = await r.text(); alert("priority toggle failed: " + e); return; }
      refresh();
      if (sel === id) open_(id);
    });
  }

  async function convertToTicket(id) {
    await lockWhile(async function () {
      var comment = prompt("Add a comment to guide the new ticket (optional):");
      if (comment === null) return;
      var r = await jpost("/tickets/" + id + "/convert-to-task", { comment: comment.trim() });
      if (!r.ok) { var e = await r.text(); alert("convert to ticket failed: " + e); return; }
      var nt = await r.json();
      refresh();
      if (nt && nt.id) open_(nt.id);
      else if (sel === id) open_(id);
    });
  }

  async function generateChildren(id) {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Generating…';
    try {
      var r = await jpost("/tickets/" + id + "/generate-children");
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Epic breakdown started — child tickets will appear below after the agent finishes.");
      setTimeout(refresh, 3000);
    } catch (e) {
      alert("Generate children failed: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Generate Tickets';
    }
  }

  function toggleBody(btn) {
    var body = document.getElementById("ticket-body");
    if (!body) return;
    if (body.style.display === "none") {
      body.style.display = "";
      btn.textContent = "▲ Hide";
    } else {
      body.style.display = "none";
      btn.textContent = "▼ Show";
    }
  }

  // =========================================================================
  // Drawer: open / close / refresh
  // =========================================================================
  function setMergeLoading(id, loading) {
    var btns = document.querySelectorAll('.merge-btn[data-ticket-id="' + id.replace(/[\\"]/g, '') + '"]');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      if (b.hasAttribute('title')) continue;
      if (loading) {
        b.disabled = true;
        b.classList.add('merging');
        b.innerHTML = '<span class="live-spinner"></span> Merging…';
      } else {
        b.disabled = false;
        b.classList.remove('merging');
        b.textContent = 'Merge';
      }
    }
  }

  async function open_(id) {
    sel = id;
    runsOpen = false;
    candidatesOpen = false;

    document.getElementById("drawer").classList.add("open");

    var afterBody = gatesCache.comments_after_body;
    var skW = function(w, h) { return '<div class="sk-block" style="width:' + w + ';height:' + h + '"></div>'; };
    document.getElementById("d").innerHTML =
      '<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>' +
      '<div class="drawer-skeleton">' +
      skW('70%', '18px') + skW('30%', '12px') + skW('90%', '12px') +
      '<div class="sk-label"></div>' + skW('100%', '14px') + skW('80%', '14px') +
      '<div class="sk-label"></div>' + skW('90%', '10px') + skW('70%', '10px') +
      '</div>';

    var tP = jget("/tickets/" + id);
    var hP = jget("/tickets/" + id + "/history");
    var dP = jget("/tickets/" + id + "/description");
    var csP = jget("/tickets/" + id + "/comments");
    var rtP = jget("/tickets/" + id + "/retrospect");
    var chP = jget("/tickets/" + id + "/children");
    var cbP = jget("/tickets/" + id + "/cost-breakdown");
    var miP = jget("/tickets/" + id + "/merge-info");
    var mrP = jget("/tickets/" + id + "/merge-reason");
    var msP = jget("/tickets/" + id + "/merge-status");

    var tData = null, _ch, _h, _d, _cs, _rt, _mi, _mr, _ms, _cb;

    function updateMergeButton() {
      if (!tData || tData.state !== "human_mr_approval" || _ms === undefined) return;
      var ba = document.getElementById("ticket-merge-btn-area");
      if (!ba) return;
      ba.innerHTML =
        (_ms && _ms.can_merge === false ?
          '<button class="merge-btn" disabled title="' + esc(_ms.reason || '') + '">Merge</button>' +
          '<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ ' + esc(_ms.reason || 'not mergeable') + '</p>' :
          '<button class="merge-btn" onclick="event.stopPropagation();mergePR(' + jsq(tData.id) + ')">Merge</button>'
        ) +
        (_mr && _mr.reason ? '<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ auto-merge not eligible: ' + esc(_mr.reason) + '</p>' : "");
    }

    function flushChildren() {
      if (_ch === undefined) return;
      var el = document.getElementById("ticket-children");
      if (!el) return;
      el.innerHTML = (_ch && _ch.length ? '<h3>Children (' + _ch.length + ')</h3><div class="children-list">' +
        _ch.map(function(c) {
          return '<div class="child-ticket" onclick="open_(' + jsq(c.id) + ')"><span class="child-state s-' + c.state + '">' + c.state + '</span> <span class="child-title">' + esc(c.title) + '</span> <span class="child-id muted">' + c.id + '</span></div>';
        }).join("") +
        '</div>' : "");
    }

    function flushHistory() {
      flushApprovalReason();
      if (_h === undefined) return;
      var el = document.getElementById("ticket-history");
      if (!el) return;
      var traces = (_cb && _cb.traces) || [];
      el.innerHTML = renderHistoryHtml(_h, id, traces);
    }

    function flushApprovalReason() {
      if (_h === undefined) return;
      if (!tData) return;
      var el = document.getElementById("ticket-approval-reason");
      if (!el) return;
      var st = tData.state;
      if (st !== "human_issue_approval" && st !== "human_mr_approval") { el.innerHTML = ""; return; }
      var hist = Array.isArray(_h) ? _h : [];
      var note = "";
      for (var i = hist.length - 1; i >= 0; i--) {
        var e = hist[i];
        if (e && e.state === st && typeof e.note === "string" && e.note.trim()) { note = e.note; break; }
      }
      if (!note) {
        note = st === "human_issue_approval"
          ? "Awaiting human approval of the refined spec."
          : "Awaiting human merge approval.";
      }
      el.innerHTML = '<div style="margin-top:8px;padding:6px 10px;border-left:3px solid #f59e0b;background:#1f1b12;border-radius:4px"><b style="color:#f59e0b;font-size:11px">Why this is awaiting approval:</b><div class="md-body" style="font-size:12px;margin-top:2px">' + renderMD(note) + '</div></div>';
    }

    function flushDescription() {
      if (_d === undefined) return;
      var el = document.getElementById("ticket-body-area");
      if (!el) return;
      if (afterBody) {
        el.innerHTML = '<h3>description.md <button class="toggle-body-btn" onclick="toggleBody(this)" style="font-size:11px;margin-left:8px">▲ Hide</button></h3><div class="md-body" id="ticket-body">' + renderMD((_d && _d.description) || "") + '</div>';
      } else {
        el.innerHTML = '<h3>description.md</h3><div class="md-body">' + renderMD((_d && _d.description) || "") + '</div>';
      }
    }

    function flushRetrospect() {
      if (_rt === undefined) return;
      var el = document.getElementById("ticket-retrospect");
      if (!el) return;
      el.innerHTML = (_rt && _rt.retrospect ? '<h3>retrospect.md</h3><div class="md-body">' + renderMD(_rt.retrospect) + '</div>' : "");
    }

    function flushComments() {
      if (_cs === undefined) return;
      var el = document.getElementById("ticket-comments");
      if (!el) return;
      el.innerHTML = '<h3>Comments <button class="add-comment-btn" onclick="addComment(' + jsq(id) + ')">+ Add</button></h3>' +
        ((_cs && _cs.length) ? renderThreads(_cs) : '<div class="muted" style="font-size:11px">No comments yet.</div>');
    }

    function flushMerge() {
      updateMergeButton();
      var mel = document.getElementById("ticket-merge");
      if (mel && _mi !== undefined) mel.innerHTML = (tData && tData.state === "human_mr_approval" && _mi ? renderMergeInfo(_mi) : "");
    }

    function flushAllSections() {
      flushChildren();
      flushHistory();
      flushApprovalReason();
      flushDescription();
      flushRetrospect();
      flushComments();
      flushMerge();
    }

    tP.then(function(t) {
      if (sel !== id) return;
      if (!t) { document.getElementById("d").innerHTML = '<div class="muted">Ticket not found</div>'; return; }
      tData = t;
      document.getElementById("d").innerHTML =
        '<div class="drawer-sticky-head">' +
        '<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>' +
        '<div id="ticket-header">' +
        '<h3>' + esc(t.title) + '</h3>' +
        '<div class="muted">' + t.id + '</div>' +
        '<p>state <b class="s-' + t.state + '" style="border-left:3px solid var(--c);padding-left:6px">' + t.state + '</b>' +
        (t.kind === "inquiry" ? ' <span class="inquiry-badge">🔍 inquiry</span>' : "") +
        (t.kind === "epic" ? ' <span class="epic-badge">📋 epic</span>' : "") +
        ' · branch ' + esc(t.branch || "—") + '<br>' +
        (t.board_id ? 'repo <span class="repo-badge">' + esc(repoIdForBoardId(t.board_id)) + '</span> · ' : "") +
        'source <span class="src-badge src-' + srcClass(t.source) + '">' + esc(t.source || "user") + '</span>' +
        (t.origin_session_url ? ' · origin <a href="' + esc(t.origin_session_url) + '" target="_blank" rel="noopener" class="origin-link">' + esc(t.origin_session) + '</a>' :
          t.origin_session ? ' · origin <span class="muted">' + esc(t.origin_session) + '</span>' : "") +
        (t.pr_url ? ' · <a href="' + esc(t.pr_url) + '" target="_blank" rel="noopener" class="pr-link">🔗 PR</a>' : "") +
        '<span id="ticket-merge-btn-area">' +
        (t.state === "human_mr_approval" ? '<span class="sk-inline" style="width:60px;height:22px;vertical-align:middle"></span>' : "") +
        '</span>' +
        '<br>· cost <b>$' + (t.cost_usd || 0).toFixed(4) + '</b>' +
        (t.pre_redraft_cost_usd > 0 ? '<br>· total (incl. pre-redraft) <b>$' + ((t.cost_usd || 0) + (t.pre_redraft_cost_usd || 0)).toFixed(4) + '</b>' : "") +
        (t.cumulative_cost && t.cumulative_cost > t.cost_usd ? '<br>· cumulative (incl. children) <b>$' + t.cumulative_cost.toFixed(4) + '</b>' : "") +
        '<br>created ' + t.created_at + ' · updated ' + t.updated_at + '</p>' +
        (t.dependencies && t.dependencies.length ?
          '<div style="margin:6px 0"><b>depends on:</b><ul style="margin:4px 0 0 18px;padding:0;list-style:none">' +
          t.dependencies.map(function(d) {
            var st = d.state || "?";
            var terminal = { "closed": 1, "done": 1, "epic_closed": 1 };
            var blocked = { "blocked": 1, "errored": 1 };
            var awaiting = { "awaiting_user_reply": 1, "human_issue_approval": 1, "human_mr_approval": 1 };
            var icon = terminal[st] ? "✅" : blocked[st] ? "⛔" : awaiting[st] ? "⏸" : "⏳";
            var color = terminal[st] ? "#10b981" : blocked[st] ? "#ef4444" : awaiting[st] ? "#a855f7" : "#f59e0b";
            var title = d.title ? esc(d.title) : "(unknown)";
            var shortId = esc(d.id.slice(0, 8) + "…" + d.id.slice(-4));
            return '<li style="margin:2px 0"><span style="color:' + color + '">' + icon + '</span> <span style="color:' + color + ';font-family:monospace;font-size:11px;text-transform:uppercase">' + esc(st) + '</span> · <a href="#" onclick="event.preventDefault();open_(' + jsq(d.id) + ')" title="' + esc(d.id) + '">' + title + '</a> <span style="color:#888;font-family:monospace;font-size:11px">' + shortId + '</span></li>';
          }).join("") +
          '</ul></div>' : "") +
        (t.unmet_deps && t.unmet_deps.length ? '<p style="color:#f59e0b;font-weight:bold">⏳ waiting on ' + t.unmet_deps.length + ' unfinished dep' + (t.unmet_deps.length > 1 ? "s" : "") + '</p>' : "") +
        (t.parent_id ? '<p><b>Part of epic:</b> <span class="epic-ref">📋 ' + esc(t.parent_title || t.parent_id) + '</span></p>' : "") +
        (t.kind === "epic" ? '<p><button class="add-comment-btn" style="background:#9333ea;color:#fff" onclick="generateChildren(' + jsq(t.id) + ')">Generate Tickets</button> <button class="add-comment-btn" style="background:#2563eb;color:#fff" onclick="newChildTicket(' + jsq(t.id) + ')">Add Ticket</button></p>' : "") +
        '<div id="ticket-approval-reason"></div>' +
        '<div id="ticket-action-buttons">' + _actionButtonsHtml(t) + '</div>' +
        '</div>' +
        '</div>' +
        '<div id="ticket-children" class="detail-section"><div class="sk-label"></div>' + skW('60%', '12px') + '</div>' +
        '<div id="ticket-history" class="detail-section"><div class="sk-label"></div>' + skW('90%', '10px') + skW('70%', '10px') + '</div>' +
        (afterBody ?
          '<div id="ticket-body-area" class="detail-section">' + skW('100%', '40px') + skW('80%', '12px') + '</div><div id="ticket-retrospect" class="detail-section"></div><div id="ticket-comments" class="detail-section"><div class="sk-label"></div>' + skW('100%', '24px') + skW('80%', '24px') + '</div>' :
          '<div id="ticket-comments" class="detail-section"><div class="sk-label"></div>' + skW('100%', '24px') + skW('80%', '24px') + '</div><div id="ticket-retrospect" class="detail-section"></div><div id="ticket-body-area" class="detail-section">' + skW('100%', '40px') + skW('80%', '12px') + '</div>'
        ) +
        '<div id="ticket-merge" class="detail-section"></div>';
      flushAllSections();
    });

    chP.then(function(ch) { if (sel !== id) return; _ch = ch; flushChildren(); });
    hP.then(function(h) { if (sel !== id) return; _h = h; flushHistory(); });
    cbP.then(function(cb) { if (sel !== id) return; _cb = cb; flushHistory(); });
    dP.then(function(d) { if (sel !== id) return; _d = d; flushDescription(); });
    rtP.then(function(rt) { if (sel !== id) return; _rt = rt; flushRetrospect(); });
    csP.then(function(cs) { if (sel !== id) return; _cs = cs; flushComments(); });
    Promise.all([miP, mrP, msP]).then(function(_ref) {
      if (sel !== id) return;
      _mi = _ref[0]; _mr = _ref[1]; _ms = _ref[2];
      flushMerge();
    });
  }

  function close_() {
    sel = null;
    runsOpen = false;
    candidatesOpen = false;
    document.getElementById("drawer").classList.remove("open");
  }

  async function refreshDetail(id) {
    if (!document.getElementById("ticket-header")) return;
    var results = await Promise.all([
      jget("/tickets/" + id), jget("/tickets/" + id + "/children"),
      jget("/tickets/" + id + "/history"), jget("/tickets/" + id + "/description"),
      jget("/tickets/" + id + "/retrospect"), jget("/tickets/" + id + "/comments"),
      jget("/tickets/" + id + "/merge-info"), jget("/tickets/" + id + "/merge-reason"),
      jget("/tickets/" + id + "/merge-status"),
      jget("/tickets/" + id + "/cost-breakdown"),
    ]);
    var t = results[0], ch = results[1], h = results[2], d = results[3],
        rt = results[4], cs = results[5], mi = results[6], mr = results[7],
        ms = results[8], cb = results[9];
    if (sel !== id || !t) return;
    var swap = function(elId, html) {
      var el = document.getElementById(elId);
      if (!el) return;
      var key = elId + ":" + id;
      if (_detailLast[key] === html) return;
      _detailLast[key] = html;
      el.innerHTML = html;
    };
    var stateBadge = document.querySelector("#ticket-header b.s-" + t.state) || document.querySelector("#ticket-header b[class^='s-']");
    if (stateBadge && stateBadge.textContent !== t.state) {
      stateBadge.className = "s-" + t.state;
      stateBadge.textContent = t.state;
    }
    swap("ticket-action-buttons", _actionButtonsHtml(t));
    swap("ticket-children", ch && ch.length ? '<h3>Children (' + ch.length + ')</h3><div class="children-list">' +
      ch.map(function(c) {
        return '<div class="child-ticket" onclick="open_(' + jsq(c.id) + ')"><span class="child-state s-' + c.state + '">' + c.state + '</span> <span class="child-title">' + esc(c.title) + '</span> <span class="child-id muted">' + c.id + '</span></div>';
      }).join("") + '</div>' : "");

    var histEl = document.getElementById("ticket-history");
    var wasOpen = new Set();
    if (histEl) {
      histEl.querySelectorAll(".ev[data-open='1']").forEach(function(w) {
        var at = w.querySelector(".ev-at");
        var st = w.querySelector(".ev-state");
        if (at && st) wasOpen.add(at.textContent + "|" + st.textContent);
      });
    }
    var newHistHtml = renderHistoryHtml(h, id, (cb && cb.traces) || []);
    swap("ticket-history", newHistHtml);
    if (wasOpen.size > 0) {
      var el2 = document.getElementById("ticket-history");
      if (el2) {
        el2.querySelectorAll(".ev").forEach(function(w) {
          if (w.dataset.open === "1") return;
          var at = w.querySelector(".ev-at");
          var st = w.querySelector(".ev-state");
          if (at && st && wasOpen.has(at.textContent + "|" + st.textContent)) {
            var sum = w.querySelector(".ev-summary");
            if (sum) toggleEvent(sum);
          }
        });
      }
    }
    var afterBody = gatesCache.comments_after_body;
    swap("ticket-body-area", afterBody ?
      '<h3>description.md <button class="toggle-body-btn" onclick="toggleBody(this)" style="font-size:11px;margin-left:8px">▲ Hide</button></h3><div class="md-body" id="ticket-body">' + renderMD((d && d.description) || "") + '</div>' :
      '<h3>description.md</h3><div class="md-body">' + renderMD((d && d.description) || "") + '</div>');
    swap("ticket-retrospect", rt && rt.retrospect ? '<h3>retrospect.md</h3><div class="md-body">' + renderMD(rt.retrospect) + '</div>' : "");
    swap("ticket-comments", '<h3>Comments <button class="add-comment-btn" onclick="addComment(' + jsq(id) + ')">+ Add</button></h3>' +
      ((cs && cs.length) ? renderThreads(cs) : '<div class="muted" style="font-size:11px">No comments yet.</div>'));
    var ba = document.getElementById("ticket-merge-btn-area");
    if (ba) {
      var baHtml = t.state === "human_mr_approval" ? (
        (ms && ms.can_merge === false ?
          '<button class="merge-btn" disabled title="' + esc(ms.reason || '') + '">Merge</button>' +
          '<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ ' + esc(ms.reason || 'not mergeable') + '</p>' :
          '<button class="merge-btn" onclick="event.stopPropagation();mergePR(' + jsq(t.id) + ')">Merge</button>'
        ) +
        (mr && mr.reason ? '<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ auto-merge not eligible: ' + esc(mr.reason) + '</p>' : "")
      ) : "";
      var k = "ticket-merge-btn-area:" + id;
      if (_detailLast[k] !== baHtml) { _detailLast[k] = baHtml; ba.innerHTML = baHtml; }
    }
    swap("ticket-merge", t.state === "human_mr_approval" && mi ? renderMergeInfo(mi) : "");
  }

  // =========================================================================
  // Ticket actions
  // =========================================================================
  async function approve(id) {
    await lockWhile(async function () {
      var r = await jpost("/tickets/" + id + "/approve");
      if (!r.ok) { var e = await r.text(); alert("approve failed: " + e); }
      else refresh();
    });
  }

  async function mergePR(id) {
    if (mergeLoading.has(id)) return;
    mergeLoading.add(id);
    setMergeLoading(id, true);
    var r = await jpost("/tickets/" + id + "/merge-now");
    if (!r.ok) { var e = await r.text(); mergeLoading.delete(id); setMergeLoading(id, false); alert("merge failed: " + e); }
    else { mergeLoading.delete(id); refresh(); }
  }

  async function requestChanges(id) {
    await lockWhile(async function () {
      var body = prompt("Send this ticket back to draft. What needs to change?\n(your comment goes to the refine agent so it can re-process with this feedback.)");
      if (body === null) return;
      if (!body.trim()) {
        var existing = await jget("/tickets/" + id + "/comments");
        if (!existing || !existing.length) { alert("A comment is required when requesting changes"); return; }
      }
      var r = await jpost("/tickets/" + id + "/request-changes", { body: body.trim() });
      if (!r.ok) { var e = await r.text(); alert("request-changes failed: " + e); }
      else { refresh(); if (sel === id) open_(id); }
    });
  }

  async function redraft(id) {
    await lockWhile(async function () {
      var body = prompt("Start this ticket over from scratch? Branch, comments, and history will be discarded and folded into a clean draft. Add a note (optional):");
      if (body === null) return;
      var r = await jpost("/tickets/" + id + "/redraft", { body: body.trim() });
      if (!r.ok) { var e = await r.text(); alert("redraft failed: " + e); }
      else { refresh(); if (sel === id) open_(id); }
    });
  }

  async function del_(id) {
    await lockWhile(async function () {
      if (!confirm("Delete ticket " + id + "? This is irreversible (row, history, workspace).")) return;
      var r = await jdel("/tickets/" + id);
      if (!r.ok && r.status !== 204) { var e = await r.text(); alert("delete failed: " + e); }
      else refresh();
    });
  }

  async function moveToBoard(id, boardId) {
    // Fetch ticket to get current board_id when not passed (card-level call).
    if (boardId === undefined) {
      var t = await jget("/tickets/" + id);
      if (!t) { alert("ticket not found"); return; }
      boardId = t.board_id;
    }
    var backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    var modal = document.createElement("div");
    modal.className = "modal";
    modal.innerHTML =
      '<h2>Move Ticket</h2>' +
      '<p style="color:#9ca3af;font-size:12px;margin:0 0 12px 0">Ticket <code>' + esc(id) + '</code></p>' +
      '<label class="modal-label">Target board <span class="modal-req">*</span></label>' +
      '<select class="modal-input" id="modal-board" style="width:100%">' +
        '<option value="">Select board…</option>' +
        (reposCache || []).filter(function(r) { return r.board_id !== boardId; }).map(function(r) {
          return '<option value="' + esc(r.repo_id) + '">' + esc(r.repo_id) + (r.board_id === "meta" ? " (meta)" : "") + '</option>';
        }).join("") +
      '</select>' +
      '<label class="modal-label" style="margin-top:12px">Note (optional)</label>' +
      '<input type="text" class="modal-input" id="modal-note" placeholder="Why is this ticket moving?" autocomplete="off">' +
      '<div class="modal-buttons">' +
       '<span class="modal-submit-error" id="modal-submit-err"></span>' +
       '<button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>' +
       '<button type="button" class="modal-btn-create" id="modal-move">Move</button>' +
      '</div>';
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    var boardEl = document.getElementById("modal-board");
    var noteEl = document.getElementById("modal-note");
    var submitErr = document.getElementById("modal-submit-err");
    var moveBtn = document.getElementById("modal-move");

    function close() { document.body.removeChild(backdrop); }
    function showSubmitErr(msg) { submitErr.textContent = msg; }
    function clearSubmitErr() { submitErr.textContent = ""; }

    async function doSubmit() {
      var repoId = boardEl.value;
      if (!repoId) { showSubmitErr("Select a target board"); boardEl.focus(); return; }
      clearSubmitErr();
      await lockWhile(async function () {
        moveBtn.disabled = true; moveBtn.textContent = "Moving…";
        var r = await jpost("/tickets/" + id + "/migrate", { repo_id: repoId, note: noteEl.value.trim() || undefined });
        if (!r.ok) {
          var e = await r.text();
          if (r.status === 404) { alert("ticket not found"); close(); return; }
          showSubmitErr(e || "migrate failed");
          moveBtn.disabled = false; moveBtn.textContent = "Move";
        } else {
          close();
          if (sel === id) close_();
          refresh();
        }
      });
    }

    backdrop.addEventListener("click", function(e) { if (e.target === backdrop) close(); });
    document.getElementById("modal-cancel").addEventListener("click", close);
    moveBtn.addEventListener("click", doSubmit);
    modal.addEventListener("keydown", function(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSubmit(); return; }
    });
  }

  async function addComment(id) {
    await lockWhile(async function () {
      var body = prompt("Add a comment to this ticket:");
      if (body === null) return;
      if (!body.trim()) return;
      var r = await jpost("/tickets/" + id + "/comments", { body: body.trim() });
      if (!r.ok) { var e = await r.text(); alert("add comment failed: " + e); }
      else if (sel === id) open_(id);
    });
  }

  async function replyToThread(threadId, ticketId) {
    await lockWhile(async function () {
      var body = prompt("Reply to this thread:");
      if (body === null) return;
      if (!body.trim()) return;
      var r = await jpost("/tickets/" + ticketId + "/comments", { body: body.trim(), parent_id: threadId });
      if (!r.ok) { var e = await r.text(); alert("reply failed: " + e); }
      else if (sel === ticketId) open_(ticketId);
    });
  }

  async function closeThread(commentId, ticketId) {
    await lockWhile(async function () {
      var tid = ticketId || sel;
      var url = "/comments/" + commentId + "/close" + (tid ? "?ticket_id=" + encodeURIComponent(tid) : "");
      var r = await jpost(url);
      if (!r.ok) { var e = await r.text(); alert("close thread failed: " + e); }
      else if (tid) open_(tid);
    });
  }

  async function reopenThread(commentId, ticketId) {
    await lockWhile(async function () {
      var tid = ticketId || sel;
      var url = "/comments/" + commentId + "/reopen" + (tid ? "?ticket_id=" + encodeURIComponent(tid) : "");
      var r = await jpost(url);
      if (!r.ok) { var e = await r.text(); alert("reopen thread failed: " + e); }
      else if (tid) open_(tid);
    });
  }

  // =========================================================================
  // New ticket / epic / inquiry / child ticket
  // =========================================================================
  async function newTicket() {
    var backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    var modal = document.createElement("div");
    modal.className = "modal";
    var repoId = getRepoId();
    var repoField = repoId === "all"
      ? '<label class="modal-label">Repo <span class="modal-req">*</span></label>' +
        '<select class="modal-input" id="modal-repo" style="width:100%">' +
          (reposCache || []).map(function(r) { return '<option value="' + esc(r.repo_id) + '">' + esc(r.repo_id) + '</option>'; }).join("") +
        '</select>'
      : '<label class="modal-label">Repo</label>' +
        '<select class="modal-input" id="modal-repo" style="width:100%">' +
          (reposCache || []).map(function(r) { return '<option value="' + esc(r.repo_id) + '"' + (r.repo_id === repoId ? ' selected' : '') + '>' + esc(r.repo_id) + '</option>'; }).join("") +
        '</select>';
    modal.innerHTML =
      '<h2>New Ticket</h2>' +
      '<label class="modal-label">Title <span class="modal-req">*</span></label>' +
      '<input type="text" class="modal-input" id="modal-title" placeholder="What needs doing?" autocomplete="off">' +
      '<div class="modal-field-error" id="modal-title-err"></div>' +
      '<label class="modal-label">Description</label>' +
      '<textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints… (optional)"></textarea>' +
      '<label class="modal-label">Screenshot</label>' +
      '<input type="file" class="modal-input" id="modal-screenshot" accept="image/png,image/jpeg,image/gif,image/webp">' +
      repoField +
      '<div class="modal-field-error" id="modal-repo-err"></div>' +
      '<div class="modal-buttons">' +
       '<span class="modal-submit-error" id="modal-submit-err"></span>' +
       '<button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>' +
       '<button type="button" class="modal-btn-create" id="modal-create">Create</button>' +
      '</div>';
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    var titleEl = document.getElementById("modal-title");
    var titleErr = document.getElementById("modal-title-err");
    var descEl = document.getElementById("modal-desc");
    var screenshotEl = document.getElementById("modal-screenshot");
    var submitErr = document.getElementById("modal-submit-err");
    var createBtn = document.getElementById("modal-create");

    function close() { document.body.removeChild(backdrop); }
    function showTitleErr(msg) { titleErr.textContent = msg; }
    function clearTitleErr() { titleErr.textContent = ""; }
    function showSubmitErr(msg) { submitErr.textContent = msg; }
    function clearSubmitErr() { submitErr.textContent = ""; }

    // Optional: paste an image straight into the description to attach it.
    descEl.addEventListener("paste", function(e) {
      var items = (e.clipboardData && e.clipboardData.items) || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].type && items[i].type.indexOf("image/") === 0) {
          var blob = items[i].getAsFile();
          if (blob && screenshotEl.files.length === 0) {
            var dt = new DataTransfer();
            dt.items.add(blob);
            screenshotEl.files = dt.files;
          }
          break;
        }
      }
    });

    // Mirror the server's _SCREENSHOT_MEDIA_TYPES / _MAX_SCREENSHOT_BYTES so
    // invalid files fail fast without a round-trip.
    var SCREENSHOT_MEDIA_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
    var MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024;

    async function uploadScreenshot(id, file) {
      if (file.type && SCREENSHOT_MEDIA_TYPES.indexOf(file.type) === -1) {
        throw new Error("Unsupported image format — use PNG, JPEG, GIF, or WebP.");
      }
      if (file.size > MAX_SCREENSHOT_BYTES) {
        throw new Error("Screenshot is too large (max 10 MiB).");
      }
      var fd = new FormData();
      fd.append("file", file);
      var resp;
      try {
        resp = await fetch("/tickets/" + encodeURIComponent(id) + "/screenshots", { method: "POST", body: fd });
      } catch (netErr) {
        throw new Error("Network error — check your connection and retry.");
      }
      if (!resp.ok) {
        var detail = "";
        try { var j = await resp.json(); detail = j && j.detail; } catch (parseErr) { detail = ""; }
        if (detail) { throw new Error(detail); }
        if (resp.status === 413) { throw new Error("Screenshot is too large (max 10 MiB)."); }
        if (resp.status === 400) { throw new Error("Unsupported image format — use PNG, JPEG, GIF, or WebP."); }
        throw new Error("Upload failed (HTTP " + resp.status + ").");
      }
    }

    // The ticket already exists on the backend; present Retry / Skip so a
    // successfully-created ticket is never stranded by a screenshot failure.
    function showUploadRecovery(id, file, msg) {
      submitErr.innerHTML =
        '<span class="modal-submit-error-msg"></span> ' +
        '<button type="button" class="modal-btn-cancel" id="modal-ss-retry">Retry</button> ' +
        '<button type="button" class="modal-btn-cancel" id="modal-ss-skip">Skip &amp; keep ticket</button>';
      submitErr.querySelector(".modal-submit-error-msg").textContent =
        "ticket created, but screenshot upload failed: " + msg;
      createBtn.disabled = true; createBtn.textContent = "Create";
      document.getElementById("modal-ss-retry").addEventListener("click", async function() {
        clearSubmitErr();
        createBtn.disabled = true; createBtn.textContent = "Uploading…";
        try {
          await uploadScreenshot(id, file);
          close(); refresh();
        } catch (err) {
          showUploadRecovery(id, file, err.message);
        }
      });
      document.getElementById("modal-ss-skip").addEventListener("click", function() {
        close(); refresh();
      });
    }

    async function doSubmit() {
      var title = titleEl.value.trim();
      if (!title) { showTitleErr("Title is required"); titleEl.focus(); return; }
      clearTitleErr(); clearSubmitErr();
      createBtn.disabled = true; createBtn.textContent = "Creating…";
      var r = await jpost("/tickets", { title: title, description: descEl.value, repo_id: document.getElementById("modal-repo").value });
      if (!r.ok) { var e = await r.text(); showSubmitErr("create failed: " + e);
        createBtn.disabled = false; createBtn.textContent = "Create"; return; }
      var file = screenshotEl.files && screenshotEl.files[0];
      if (file) {
        var body = await r.json();
        try {
          await uploadScreenshot(body.id, file);
        } catch (err) {
          showUploadRecovery(body.id, file, err.message);
          return;
        }
      }
      close(); refresh();
    }

    backdrop.addEventListener("click", function(e) { if (e.target === backdrop) close(); });
    document.getElementById("modal-cancel").addEventListener("click", close);
    createBtn.addEventListener("click", doSubmit);
    modal.addEventListener("keydown", function(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSubmit(); return; }
      if (e.key === "Enter" && e.target === titleEl) { e.preventDefault(); descEl.focus(); return; }
    });
    titleEl.focus();
  }

  async function newInquiry() {
    var backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    var modal = document.createElement("div");
    modal.className = "modal";
    var repoId = getRepoId();
    var repoField = repoId === "all"
      ? '<label class="modal-label">Repo <span class="modal-req">*</span></label>' +
        '<select class="modal-input" id="modal-repo" style="width:100%">' +
          (reposCache || []).map(function(r) { return '<option value="' + esc(r.repo_id) + '">' + esc(r.repo_id) + '</option>'; }).join("") +
        '</select>'
      : '<input type="hidden" id="modal-repo" value="' + esc(repoId) + '">';
    modal.innerHTML =
      '<h2>New Inquiry</h2>' +
      '<label class="modal-label">Question / investigation prompt <span class="modal-req">*</span></label>' +
      '<input type="text" class="modal-input" id="modal-title" placeholder="What do you want to know?" autocomplete="off">' +
      '<div class="modal-field-error" id="modal-title-err"></div>' +
      '<label class="modal-label">Context / background</label>' +
      '<textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints… (optional)"></textarea>' +
      repoField +
      '<div class="modal-field-error" id="modal-repo-err"></div>' +
      '<div class="modal-buttons">' +
       '<span class="modal-submit-error" id="modal-submit-err"></span>' +
       '<button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>' +
       '<button type="button" class="modal-btn-create" id="modal-create">Create</button>' +
      '</div>';
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    var titleEl = document.getElementById("modal-title");
    var titleErr = document.getElementById("modal-title-err");
    var descEl = document.getElementById("modal-desc");
    var submitErr = document.getElementById("modal-submit-err");
    var createBtn = document.getElementById("modal-create");

    function close() { document.body.removeChild(backdrop); }
    function showTitleErr(msg) { titleErr.textContent = msg; }
    function clearTitleErr() { titleErr.textContent = ""; }
    function showSubmitErr(msg) { submitErr.textContent = msg; }
    function clearSubmitErr() { submitErr.textContent = ""; }

    async function doSubmit() {
      var title = titleEl.value.trim();
      if (!title) { showTitleErr("Question is required"); titleEl.focus(); return; }
      clearTitleErr(); clearSubmitErr();
      createBtn.disabled = true; createBtn.textContent = "Creating…";
      var r = await jpost("/tickets", { title: title, description: descEl.value, kind: "inquiry", repo_id: document.getElementById("modal-repo").value });
      if (!r.ok) { var e = await r.text(); showSubmitErr("create failed: " + e);
        createBtn.disabled = false; createBtn.textContent = "Create"; }
      else { close(); refresh(); }
    }

    backdrop.addEventListener("click", function(e) { if (e.target === backdrop) close(); });
    document.getElementById("modal-cancel").addEventListener("click", close);
    createBtn.addEventListener("click", doSubmit);
    modal.addEventListener("keydown", function(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSubmit(); return; }
      if (e.key === "Enter" && e.target === titleEl) { e.preventDefault(); descEl.focus(); return; }
    });
    titleEl.focus();
  }

  async function newEpic() {
    var backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    var modal = document.createElement("div");
    modal.className = "modal";
    var repoId = getRepoId();
    var repoField = repoId === "all"
      ? '<label class="modal-label">Repo <span class="modal-req">*</span></label>' +
        '<select class="modal-input" id="modal-repo" style="width:100%">' +
          (reposCache || []).map(function(r) { return '<option value="' + esc(r.repo_id) + '">' + esc(r.repo_id) + '</option>'; }).join("") +
        '</select>'
      : '<label class="modal-label">Repo</label>' +
        '<select class="modal-input" id="modal-repo" style="width:100%">' +
          (reposCache || []).map(function(r) { return '<option value="' + esc(r.repo_id) + '"' + (r.repo_id === repoId ? ' selected' : '') + '>' + esc(r.repo_id) + '</option>'; }).join("") +
        '</select>';
    modal.innerHTML =
      '<h2>New Epic</h2>' +
      '<label class="modal-label">Title <span class="modal-req">*</span></label>' +
      '<input type="text" class="modal-input" id="modal-title" placeholder="Epic title / goal" autocomplete="off">' +
      '<div class="modal-field-error" id="modal-title-err"></div>' +
      '<label class="modal-label">Description</label>' +
      '<textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Scope, outcome, notes… (optional)"></textarea>' +
      repoField +
      '<div class="modal-buttons">' +
       '<span class="modal-submit-error" id="modal-submit-err"></span>' +
       '<button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>' +
       '<button type="button" class="modal-btn-create" id="modal-create">Create</button>' +
      '</div>';
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    var titleEl = document.getElementById("modal-title");
    var titleErr = document.getElementById("modal-title-err");
    var descEl = document.getElementById("modal-desc");
    var submitErr = document.getElementById("modal-submit-err");
    var createBtn = document.getElementById("modal-create");

    function close() { document.body.removeChild(backdrop); }
    function showTitleErr(msg) { titleErr.textContent = msg; }
    function clearTitleErr() { titleErr.textContent = ""; }
    function showSubmitErr(msg) { submitErr.textContent = msg; }
    function clearSubmitErr() { submitErr.textContent = ""; }

    async function doSubmit() {
      var title = titleEl.value.trim();
      if (!title) { showTitleErr("Title is required"); titleEl.focus(); return; }
      clearTitleErr(); clearSubmitErr();
      createBtn.disabled = true; createBtn.textContent = "Creating…";
      var r = await jpost("/epics", { title: title, description: descEl.value, repo_id: document.getElementById("modal-repo").value });
      if (!r.ok) { var e = await r.text(); showSubmitErr("create failed: " + e);
        createBtn.disabled = false; createBtn.textContent = "Create"; }
      else { close(); refresh(); }
    }

    backdrop.addEventListener("click", function(e) { if (e.target === backdrop) close(); });
    document.getElementById("modal-cancel").addEventListener("click", close);
    createBtn.addEventListener("click", doSubmit);
    modal.addEventListener("keydown", function(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSubmit(); return; }
      if (e.key === "Enter" && e.target === titleEl) { e.preventDefault(); descEl.focus(); return; }
    });
    titleEl.focus();
  }

  async function newChildTicket(epicId) {
    var backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    var modal = document.createElement("div");
    modal.className = "modal";
    modal.innerHTML =
      '<h2>Add Ticket to Epic</h2>' +
      '<label class="modal-label">Title <span class="modal-req">*</span></label>' +
      '<input type="text" class="modal-input" id="modal-title" placeholder="What needs doing?" autocomplete="off">' +
      '<div class="modal-field-error" id="modal-title-err"></div>' +
      '<label class="modal-label">Description</label>' +
      '<textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints…"></textarea>' +
      '<div class="modal-buttons">' +
       '<span class="modal-submit-error" id="modal-submit-err"></span>' +
       '<button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>' +
       '<button type="button" class="modal-btn-create" id="modal-create">Create</button>' +
      '</div>';
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    var titleEl = document.getElementById("modal-title");
    var titleErr = document.getElementById("modal-title-err");
    var descEl = document.getElementById("modal-desc");
    var submitErr = document.getElementById("modal-submit-err");
    var createBtn = document.getElementById("modal-create");

    function close() { document.body.removeChild(backdrop); }
    function showTitleErr(msg) { titleErr.textContent = msg; }
    function clearTitleErr() { titleErr.textContent = ""; }
    function showSubmitErr(msg) { submitErr.textContent = msg; }
    function clearSubmitErr() { submitErr.textContent = ""; }

    async function doSubmit() {
      var title = titleEl.value.trim();
      if (!title) { showTitleErr("Title is required"); titleEl.focus(); return; }
      clearTitleErr(); clearSubmitErr();
      createBtn.disabled = true; createBtn.textContent = "Creating…";
      var r = await jpost("/tickets", { title: title, description: descEl.value, parent_id: epicId, kind: "task" });
      if (!r.ok) { var e = await r.text(); showSubmitErr("create failed: " + e);
        createBtn.disabled = false; createBtn.textContent = "Create"; }
      else { close(); open_(epicId); }
    }

    backdrop.addEventListener("click", function(e) { if (e.target === backdrop) close(); });
    document.getElementById("modal-cancel").addEventListener("click", close);
    createBtn.addEventListener("click", doSubmit);
    modal.addEventListener("keydown", function(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSubmit(); return; }
      if (e.key === "Enter" && e.target === titleEl) { e.preventDefault(); descEl.focus(); return; }
    });
    titleEl.focus();
  }

  // =========================================================================
  // Runs view
  // =========================================================================
  function _runElapsed(r) {
    var s = Date.parse(r.started_at);
    var f = r.finished_at ? Date.parse(r.finished_at) : null;
    var e = f ? f : Date.now();
    var ms = e - s;
    var sec = Math.floor(ms / 1000);
    var min = Math.floor(sec / 60);
    var sss = sec % 60;
    return f ? (min + 'm ' + sss + 's') : 'running…';
  }

  function _runRowHtml(r, elapsed) {
    var kc = agentColor(r.kind);
    var sc = r.status === 'running' ? '#eab308' : r.status === 'ok' ? '#22c55e' : '#ef4444';
    var st = r.status === 'running' ? 'running…' : r.status;
    var repoTag = (getRepoId() === 'all' && r.repo_id) ?
      '<span class="repo-badge" style="margin-right:6px">' + esc(r.repo_id) + '</span>' : '';
    return '<div data-run-id="' + esc(r.id || '') + '" data-run-status="' + esc(r.status || '') + '" style="padding:8px 0;border-bottom:1px solid #262b36">' +
      repoTag + '<span style="display:inline-block;padding:1px 6px;border-radius:4px;background:' + kc + ';color:#fff;font-size:10px;margin-right:6px">' + r.kind + '</span>' +
      '<span style="display:inline-block;padding:1px 6px;border-radius:4px;background:' + sc + ';color:#fff;font-size:10px">' + st + '</span>' +
      '<span style="color:#7d828c;font-size:10px;margin-left:6px">' + r.started_at + '</span>' +
      '<span class="run-elapsed" style="color:#7d828c;font-size:10px;margin-left:3px">' + elapsed + '</span>' +
      '<div style="font-size:11px;color:#aab0bd;margin-top:3px;white-space:pre-wrap">' + esc(r.summary || '') + '</div>' +
      (r.error ? '<div style="font-size:11px;color:#f87171;margin-top:2px">' + esc(r.error) + '</div>' : '') +
      '</div>';
  }

  async function renderRuns() {
    var repoId = getRepoId();
    var runsUrl = repoId !== "all" ? "/runs?repo_id=" + encodeURIComponent(repoId) : "/runs";
    var rs = await jget(runsUrl);
    var sig = rs ? JSON.stringify(rs.map(function(r) { return [r.id || r.started_at, r.status, r.finished_at, r.summary, r.error]; })) : "null";
    var d = document.getElementById("d");
    var domHasRuns = d.querySelector("[data-run-id]") !== null || (rs && !rs.length);
    if (sig === _runsLastSig && domHasRuns) {
      if (rs && rs.length) {
        var rows = d.querySelectorAll("[data-run-id]");
        rows.forEach(function(row, i) {
          if (!rs[i]) return;
          var el = row.querySelector(".run-elapsed");
          if (el) el.textContent = _runElapsed(rs[i]);
        });
      }
      return;
    }
    _runsLastSig = sig;
    d.innerHTML = '<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>' +
      (rs && rs.length ?
        rs.map(function(r) { return _runRowHtml(r, _runElapsed(r)); }).join("")
        : '<div class="muted">No runs yet. Click Run Audit or Trace Health to start one.</div>');
  }

  async function toggleRuns() {
    if (runsOpen) { close_(); return; }
    if (sel) { close_(); }
    await renderRuns();
    runsOpen = true;
    document.getElementById("drawer").classList.add("open");
  }

  // =========================================================================
  // Cost dashboard
  // =========================================================================
  // =========================================================================
  // Agents menu
  // =========================================================================
  function toggleAgentsMenu(ev) {
    ev.stopPropagation();
    var menu = document.getElementById("agents-menu");
    if (menu) menu.classList.toggle("open");
  }

  function closeAgentsMenu() {
    var menu = document.getElementById("agents-menu");
    if (menu) menu.classList.remove("open");
  }

  function toggleMetaOnlyButtons() {
    var onMeta = getRepoId() === "meta";
    document.querySelectorAll(".meta-only").forEach(function(el) { el.style.display = onMeta ? "" : "none"; });
  }

  async function fetchEnabledAgents() {
    var repoId = getRepoId();
    if (repoId === "all") return new Set();
    var list = await jget("/agents?repo_id=" + encodeURIComponent(repoId));
    return new Set(Array.isArray(list) ? list : []);
  }

  async function updateAgentsMenu() {
    var dd = document.querySelector(".agents-dropdown");
    var repoId = getRepoId();
    if (repoId === "all") { if (dd) dd.style.display = "none"; return; }
    if (dd) dd.style.display = "";
    var onMeta = repoId === "meta";
    var enabled = await fetchEnabledAgents();
    if (getRepoId() !== repoId) return;
    document.querySelectorAll("#agents-menu button[data-agent]").forEach(function(btn) {
      var metaOnly = btn.classList.contains("meta-only");
      var show = metaOnly ? onMeta : enabled.has(btn.dataset.agent);
      btn.style.display = show ? "" : "none";
    });
  }

  async function runAudit() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var auditUrl = repoId !== "all" ? "/audit?repo_id=" + encodeURIComponent(repoId) : "/audit";
      var r = await jpost(auditUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Audit started — it runs for a few minutes; new draft tickets will appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Audit failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Audit';
    }
  }

  async function runTraceHealth() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var thUrl = repoId !== "all" ? "/trace-health?repo_id=" + encodeURIComponent(repoId) : "/trace-health";
      var r = await jpost(thUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Trace-health check started — new draft tickets will appear on the board if unsessioned traces are found.");
      setTimeout(refresh, 3000);
    } catch (e) {
      alert("Trace-health check failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Trace Health';
    }
  }

  async function runLangfuseCleanup() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var lcUrl = repoId !== "all" ? "/langfuse-cleanup?repo_id=" + encodeURIComponent(repoId) : "/langfuse-cleanup";
      var r = await jpost(lcUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Langfuse cleanup started — excess traces will be purged.");
      setTimeout(refresh, 3000);
    } catch (e) {
      alert("Langfuse cleanup failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Langfuse Cleanup';
    }
  }

  async function runHealth() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var hUrl = repoId !== "all" ? "/health-check?repo_id=" + encodeURIComponent(repoId) : "/health-check";
      var r = await jpost(hUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Health check started — new draft tickets will appear on the board if issues are found.");
      setTimeout(refresh, 3000);
    } catch (e) {
      alert("Health check failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Health Check';
    }
  }

  async function runTestGap() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var tgUrl = repoId !== "all" ? "/test-gap?repo_id=" + encodeURIComponent(repoId) : "/test-gap";
      var r = await jpost(tgUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Test-gap inspection started — new draft tickets will appear on the board if gaps are found.");
      setTimeout(refresh, 3000);
    } catch (e) {
      alert("Test-gap check failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Test Gaps';
    }
  }

  async function runAgentCheck() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var acUrl = repoId !== "all" ? "/agent-check?repo_id=" + encodeURIComponent(repoId) : "/agent-check";
      var r = await jpost(acUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Agent-check started — it inspects every agent's prompt/tools for coherence gaps. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Agent-check failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Agent Check';
    }
  }

  async function runSurvey() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var sUrl = repoId !== "all" ? "/survey?repo_id=" + encodeURIComponent(repoId) : "/survey";
      var r = await jpost(sUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Survey started — it discovers similar OSS projects and proposes improvements. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Survey failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Survey';
    }
  }

  async function runModuleCurator() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var mcUrl = repoId !== "all" ? "/module-curator?repo_id=" + encodeURIComponent(repoId) : "/module-curator";
      var r = await jpost(mcUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Module Curator started — it checks the directory tree against docs/modules.yaml and files drafts for unclassified files / stale paths / new modules. New drafts appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Module Curator failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Module Curator';
    }
  }

  async function runForgeParity() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var url = repoId !== "all" ? "/forge-parity?repo_id=" + encodeURIComponent(repoId) : "/forge-parity";
      var r = await jpost(url);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Forge Parity started — it compares forge adapter implementations against the Forge ABC, flags drift, and files at most 3 draft tickets per pass. New drafts appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Forge Parity failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Forge Parity';
    }
  }

  async function runCopyPaste() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var url = repoId !== "all" ? "/copy-paste?repo_id=" + encodeURIComponent(repoId) : "/copy-paste";
      var r = await jpost(url);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Copy-paste detection started — new draft tickets will appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Copy-paste detection failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Copy Paste';
    }
  }

  async function runBcCheck() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var bcUrl = repoId !== "all" ? "/bc-check?repo_id=" + encodeURIComponent(repoId) : "/bc-check";
      var r = await jpost(bcUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("BC-check started — it scans for backward-compat shims and dead-code branches ripe for removal. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("BC-check failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'BC Check';
    }
  }

  async function runCompletenessCheck() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var ccUrl = repoId !== "all" ? "/completeness-check?repo_id=" + encodeURIComponent(repoId) : "/completeness-check";
      var r = await jpost(ccUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Completeness-check started — it scans for half-wired features and files draft tickets for discovered gaps. New drafts appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Completeness-check failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Completeness';
    }
  }

  async function runRunHealth() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var r = await jpost("/run-health");
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Run-health started — it analyzes recent run outcomes and files health drafts to the mill board.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Run-health failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Run Health';
    }
  }

  async function runConfigSync() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var csUrl = repoId !== "all" ? "/config-sync?repo_id=" + encodeURIComponent(repoId) : "/config-sync";
      var r = await jpost(csUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Config-sync started — it scans for config ↔ .env ↔ docs drift. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Config-sync failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Config Sync';
    }
  }

  async function runMemberSync() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var msUrl = repoId !== "all" ? "/member-sync?repo_id=" + encodeURIComponent(repoId) : "/member-sync";
      var r = await jpost(msUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Member-sync started — it reconciles workspace members against the configured roster. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Member-sync failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Member Sync';
    }
  }

  async function runStateSync() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var ssUrl = repoId !== "all" ? "/state-sync?repo_id=" + encodeURIComponent(repoId) : "/state-sync";
      var r = await jpost(ssUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("State-sync started — it inspects board state consistency. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("State-sync failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'State Sync';
    }
  }

  async function runEnvDocSync() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var edUrl = repoId !== "all" ? "/env-doc-sync?repo_id=" + encodeURIComponent(repoId) : "/env-doc-sync";
      var r = await jpost(edUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Env-doc-sync started — it inspects env-var documentation consistency. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Env-doc-sync failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Env Doc Sync';
    }
  }

  async function runFrontendSync() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var fsUrl = repoId !== "all" ? "/frontend-sync?repo_id=" + encodeURIComponent(repoId) : "/frontend-sync";
      var r = await jpost(fsUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Frontend-sync started — it aligns the front-end with backend API definitions. New draft tickets appear on the board when it finishes.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Frontend-sync failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Frontend Sync';
    }
  }

  async function runTraceReview() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var trUrl = repoId !== "all" ? "/trace-review?repo_id=" + encodeURIComponent(repoId) : "/trace-review";
      var r = await jpost(trUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Trace review started — scans Langfuse traces since the last run, flags outliers, runs the cheap flash inspector on flagged ones, files draft tickets per finding.");
      setTimeout(refresh, 4000);
    } catch (e) {
      alert("Trace review failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Trace Review';
    }
  }

  async function runRoadmapSync() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var repoId = getRepoId();
      var rsUrl = repoId !== "all" ? "/roadmap-sync?repo_id=" + encodeURIComponent(repoId) : "/roadmap-sync";
      var r = await jpost(rsUrl);
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Roadmap-sync started — it reconciles ROADMAP.md against the board's epics. New epics + a marker-PR appear when it finishes.");
      setTimeout(refresh, 3000);
    } catch (e) {
      alert("Roadmap-sync failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Roadmap Sync';
    }
  }

  async function runMeta() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = 'Running...';
    try {
      var r = await jpost("/meta");
      if (!r.ok) { throw new Error(await r.text()); }
      alert("Meta-agent pass started — new extraction and alignment draft tickets will appear on the board when it finishes.");
      setTimeout(refresh, 3000);
    } catch (e) {
      alert("Meta pass failed to start: " + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Meta';
    }
  }

  // =========================================================================
  // AGENT.md candidates
  // =========================================================================
  async function refreshCandidateBadge() {
    var btn = document.getElementById("agentmd-btn");
    var badge = document.getElementById("agentmd-badge");
    if (!btn || !badge) return;
    var reset = function() { badge.style.display = "none"; badge.textContent = ""; btn.style.borderColor = "#3a2a4b"; btn.style.boxShadow = ""; };
    var repo = getRepoId();
    if (repo === "meta") { btn.style.display = "none"; reset(); return; }
    btn.style.display = "";
    try {
      var candsUrl = (!repo || repo === "all")
        ? "/candidates?repo_id=all"
        : "/candidates?repo_id=" + encodeURIComponent(repo);
      var cands = await jget(candsUrl);
      if (!Array.isArray(cands)) return;
      if (cands.length > 0) {
        badge.textContent = "⚠ " + cands.length;
        badge.style.display = "";
        btn.style.borderColor = "#f59e0b";
        btn.style.boxShadow = "0 0 0 1px #f59e0b";
      } else reset();
    } catch (e) { /* silently leave badge unchanged on error */ }
  }

  async function openCandidates() {
    if (candidatesOpen) { close_(); return; }
    if (sel || runsOpen) close_();
    candidatesOpen = true;
    document.getElementById("drawer").classList.add("open");
    await renderCandidatesList();
  }

  async function renderCandidatesList() {
    var repo = getRepoId();
    var escLocal = function(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; };
    var drawer = document.getElementById("d");
    var isAll = !repo || repo === "all";
    var headerLabel = isAll ? "All repos" : escLocal(repo);
    drawer.innerHTML = '<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>' +
      '<h3>AGENT.md candidates · ' + headerLabel + '</h3>' +
      '<div class="muted" style="margin-bottom:10px;font-size:11px">' +
      'Retrospect proposes rules for the audited repo\'s <code>AGENT.md</code>. ' +
      'Validate to file a draft ticket that edits <code>AGENT.md</code> on this repo; ' +
      'reject to dismiss.</div>' +
      '<div id="candidates-list">loading…</div>';
    var cands;
    var candsUrl = isAll
      ? "/candidates?repo_id=all"
      : "/candidates?repo_id=" + encodeURIComponent(repo);
    try { cands = await jget(candsUrl); }
    catch (e) {
      document.getElementById("candidates-list").innerHTML =
        '<div class="muted" style="padding:12px 0;color:#f87171">failed to load candidates: ' + escLocal(String(e)) + '</div>';
      return;
    }
    if (!Array.isArray(cands) || !cands.length) {
      document.getElementById("candidates-list").innerHTML =
        '<div class="muted" style="padding:12px 0">No pending candidates. Retrospect appends new entries as it runs.</div>';
      return;
    }
    var html = '';
    cands.forEach(function(c) {
      html += '<div class="candidate-card" id="cand-' + escLocal(c.candidate_id) + '" data-repo="' + escLocal(c.repo_id) + '" style="border:1px solid #2c313d;border-radius:6px;padding:10px 12px;margin-bottom:10px;background:#1d212c">' +
       '<div style="font-size:11px;color:#9ca3af;margin-bottom:4px">' +
       (isAll ? '<span class="repo-badge">' + escLocal(c.repo_id) + '</span> · ' : '') +
       escLocal(c.section) + ' · proposed ' + escLocal(c.proposed_at) + '</div>' +
       '<blockquote style="margin:4px 0 8px 0;padding:6px 10px;border-left:3px solid #7c3aed;background:#1a1d27;color:#e2e4eb;font-size:13px;line-height:1.4">' +
       escLocal(c.rule) + '</blockquote>' +
       '<div style="font-size:11px;color:#9ca3af;margin-bottom:8px"><strong>Rationale:</strong> ' + escLocal(c.rationale) + '</div>' +
       '<div style="font-size:10px;color:#6b7280;margin-bottom:8px">From ticket <code>' + escLocal(c.source_ticket) + '</code></div>' +
       '<div style="display:flex;gap:6px">' +
        '<button onclick="validateCandidate(' + jsq(c.candidate_id) + ')" style="font-size:11px;padding:4px 12px;background:#059669;color:#fff;border:none;border-radius:4px;cursor:pointer">' +
        '✓ Validate &amp; file ticket</button>' +
        '<button onclick="rejectCandidate(' + jsq(c.candidate_id) + ')" style="font-size:11px;padding:4px 12px;background:#374151;color:#cfd3db;border:none;border-radius:4px;cursor:pointer">' +
        '✕ Reject</button>' +
       '</div>' +
      '</div>';
    });
    document.getElementById("candidates-list").innerHTML = html;
  }

  async function validateCandidate(cid) {
    var card = document.getElementById("cand-" + cid);
    var repo = card ? card.getAttribute("data-repo") : getRepoId();
    if (card) { card.style.opacity = '0.5'; card.querySelectorAll("button").forEach(function(b) { b.disabled = true; }); }
    try {
      var r = await fetch("/candidates/" + encodeURIComponent(cid) + "/validate?repo_id=" + encodeURIComponent(repo), { method: "POST" });
      if (!r.ok) {
        var txt = await r.text();
        alert("Validate failed: " + txt);
        if (card) { card.style.opacity = ''; card.querySelectorAll("button").forEach(function(b) { b.disabled = false; }); }
        return;
      }
      await renderCandidatesList();
      refreshCandidateBadge();
    } catch (e) {
      alert("Validate error: " + e);
      if (card) { card.style.opacity = ''; card.querySelectorAll("button").forEach(function(b) { b.disabled = false; }); }
    }
  }

  async function rejectCandidate(cid) {
    if (!confirm("Reject this candidate? It stays in the file as audit trail but won't be surfaced again.")) return;
    var card = document.getElementById("cand-" + cid);
    var repo = card ? card.getAttribute("data-repo") : getRepoId();
    try {
      var r = await fetch("/candidates/" + encodeURIComponent(cid) + "/reject?repo_id=" + encodeURIComponent(repo), { method: "POST" });
      if (!r.ok) { alert("Reject failed: " + await r.text()); return; }
      await renderCandidatesList();
      refreshCandidateBadge();
    } catch (e) { alert("Reject error: " + e); }
  }

  // =========================================================================
  // Status bar
  // =========================================================================
  function updateMeta() {
    var meta = document.getElementById("meta");
    if (!meta) return;
    var n = document.querySelectorAll("#board .board-card").length;
    meta.textContent = n + " tickets · " + new Date().toLocaleTimeString();
  }

  // =========================================================================
  // Main refresh — delegates board rendering to robotsix-board
  // =========================================================================
  async function refresh() {
    var wantClosed = showClosed;
    var tok = ++refreshSeq;
    await fetchRepos();
    var repoId = getRepoId();
    toggleMetaOnlyButtons();
    updateAgentsMenu();
    fetchGates();
    fetchLangfuseStatus();
    fetchCreditStatus();
    refreshCandidateBadge();

    // Update the board refresh URL to include the repo filter
    if (window.robotsixBoardSetRefreshUrl) {
      if (repoId !== "all") {
        window.robotsixBoardSetRefreshUrl(
          "/board/cards?repo_id=" + encodeURIComponent(repoId)
        );
      } else {
        window.robotsixBoardSetRefreshUrl("/board/cards");
      }
    }

    // Delegate board rendering to robotsix-board
    window.robotsixBoardRefresh();
    updateMeta();
    // After a short delay, fetch active labels and apply to cards
    setTimeout(function() {
      if (refreshSeq === tok) {
        fetchActive().then(applyActiveLabels).then(applyMoveButtons);
      }
    }, 600);
  }

  // =========================================================================
  // Bootstrap
  // =========================================================================
  function millBootstrap() {
    // Configure robotsix-board gate endpoint once
    if (window.robotsixBoardSetGateEndpoint) {
      window.robotsixBoardSetGateEndpoint('/gates');
    }

    // Reflect any persisted show-closed preference in the toggle label
    var closedBtn = document.getElementById("toggle-closed-btn");
    if (closedBtn) closedBtn.textContent = showClosed ? "Hide closed" : "Show closed";

    // Remove robotsix-board's duplicate show-closed checkbox if it has
    // already been injected by the time bootstrap runs.
    removeDuplicateClosedToggle();

    // Apply repo filter to the initial board render
    var repoId = getRepoId();
    if (window.robotsixBoardSetRefreshUrl && repoId !== "all") {
      window.robotsixBoardSetRefreshUrl(
        "/board/cards?repo_id=" + encodeURIComponent(repoId)
      );
    }

    // Initial data fetch
    fetchRepos();
    fetchGates();
    fetchLangfuseStatus();
    fetchCreditStatus();
    refreshCandidateBadge();
    applyAgentColors();

    // Connect WebSocket for live updates
    connectWebSocket();

    // Initial board render via robotsix-board, then active labels
    window.robotsixBoardRefresh();
    setTimeout(function() { fetchActive().then(applyActiveLabels).then(applyMoveButtons); }, 600);

    // Intercept clicks on board cards BEFORE robotsix-board's handler
    var board = document.getElementById("board");
    if (board) {
      board.addEventListener('click', function(evt) {
        // Only intercept .board-card, not .board-card-move forms
        var card = evt.target.closest('.board-card');
        if (!card) return;
        if (evt.target.closest('.board-card-move')) return;
        if (evt.target.closest('.move-btn')) return;
        evt.stopPropagation();
        evt.preventDefault();
        var ticketId = card.dataset.cardId;
        if (ticketId) open_(ticketId);
      }, true); // capture phase ensures mill wins over robotsix-board
    }

    // Agents menu global click-to-close
    document.addEventListener("click", function(ev) {
      var menu = document.getElementById("agents-menu");
      if (!menu || !menu.classList.contains("open")) return;
      if (menu.contains(ev.target)) return;
      var trigger = document.querySelector(".agents-trigger");
      if (trigger && trigger.contains(ev.target)) return;
      menu.classList.remove("open");
    });

    // Escape key closes agents menu
    document.addEventListener("keydown", function(ev) {
      if (ev.key === "Escape") closeAgentsMenu();
    });

    // 1s tick: refresh drawer content when open, also periodically refresh active labels
    setInterval(function() {
      hideEmptyColumns();
      removeDuplicateClosedToggle();
      updateMeta();
      if (runsOpen) renderRuns();
      else if (sel) refreshDetail(sel);
      // Refresh active labels on the board every 5s when drawer is closed
      if (!sel && !runsOpen && !candidatesOpen) {
        fetchActive().then(applyActiveLabels).then(applyMoveButtons);
      }
    }, 1000);
  }

  // Run the mill bootstrap as soon as the DOM is ready. Mirror the
  // shared robotsix-board init guard so the filter setup (and therefore
  // the robotsixBoardSetRefreshUrl call) runs even when board-mill.js
  // evaluates after DOMContentLoaded has already fired (cached assets /
  // bfcache / fast reload) — otherwise the filtered refresh URL is never
  // set and the board polls /board/cards unfiltered.
  if (document.readyState === "loading") {
    document.addEventListener('DOMContentLoaded', millBootstrap);
  } else {
    millBootstrap();
  }

  // =========================================================================
  // Expose functions called from inline HTML onclick handlers
  // =========================================================================
  window.open_ = open_;
  window.close_ = close_;
  window.toggleEvent = toggleEvent;
  window.approve = approve;
  window.mergePR = mergePR;
  window.requestChanges = requestChanges;
  window.redraft = redraft;
  window.del_ = del_;
  window.addComment = addComment;
  window.replyToThread = replyToThread;
  window.closeThread = closeThread;
  window.reopenThread = reopenThread;
  window.togglePriority = togglePriority;
  window.convertToTicket = convertToTicket;
  window.moveToBoard = moveToBoard;
  window.generateChildren = generateChildren;
  window.newChildTicket = newChildTicket;
  window.toggleAgentsMenu = toggleAgentsMenu;
  window.closeAgentsMenu = closeAgentsMenu;
  window.dismissLfStatus = dismissLfStatus;
  window.dismissCreditStatus = dismissCreditStatus;
  window.validateCandidate = validateCandidate;
  window.rejectCandidate = rejectCandidate;
  window.toggleBody = toggleBody;
  window.toggleRuns = toggleRuns;
  window.openCandidates = openCandidates;
  window.newTicket = newTicket;
  window.newEpic = newEpic;
  window.newInquiry = newInquiry;
  window.onRepoChange = onRepoChange;
  window.toggleClosed = toggleClosed;
  window.fetchRepos = fetchRepos;
  window.connectWebSocket = connectWebSocket;
  window.refresh = refresh;
  window.updateMeta = updateMeta;
  window.runAudit = runAudit;
  window.runTraceHealth = runTraceHealth;
  window.runLangfuseCleanup = runLangfuseCleanup;
  window.runHealth = runHealth;
  window.runTestGap = runTestGap;
  window.runAgentCheck = runAgentCheck;
  window.runSurvey = runSurvey;
  window.runModuleCurator = runModuleCurator;
  window.runForgeParity = runForgeParity;
  window.runCopyPaste = runCopyPaste;
  window.runStateSync = runStateSync;
  window.runEnvDocSync = runEnvDocSync;
  window.runFrontendSync = runFrontendSync;
  window.runBcCheck = runBcCheck;
  window.runCompletenessCheck = runCompletenessCheck;
  window.runRunHealth = runRunHealth;
  window.runConfigSync = runConfigSync;
  window.runMemberSync = runMemberSync;
  window.runTraceReview = runTraceReview;
  window.runRoadmapSync = runRoadmapSync;
  window.runMeta = runMeta;

})();
