let showClosed=false;               // empty cols hidden; CLOSED and EPIC_CLOSED also hidden unless toggled
let sel=null;
let runsOpen=false;
let costDashboardOpen=false;
let costLookbackHours=24;
let costMaxTickets=20;               // default for ticket-count mode
let costMode='time';                 // 'time' | 'tickets'
let refreshSeq=0;                    // serialize concurrent refresh() calls
let costRenderSeq=0;                  // serialize concurrent renderCostDashboard() calls
let activeMap={};
let gatesCache={};                    // cached /gates response for open_() drawer ordering
let reposCache=null;                  // cached from GET /repos: [{repo_id, board_id}, …]
let currentRepoId=null;               // current selection, resolved lazily from URL → localStorage → "all"
let mergeLoading=new Set();           // ticket IDs currently awaiting merge-now POST
const ACTIVE_LABEL={
  refine: "refining…",
  implement: "implementing…",
  document: "documenting…",
  review: "reviewing…",
  deliver: "delivering…",
  merge: "merging…",
  ci_fix: "fixing CI…",
  retrospect: "retrospecting…"
};
// Single source of truth for agent→badge colors. Keyed by the
// underscore-normalized agent name; both the Runs view (via
// agentColor) and the Agents menu dots derive their color from here,
// so the two views never drift. Seeded from the Agents menu palette.
const AGENT_COLORS={
 audit:'#059669',
 health:'#0d9488',
 test_gap:'#7c3aed',
 trace_health:'#0ea5e9',
 langfuse_cleanup:'#14b8a6',
 agent_check:'#db2777',
 survey:'#f59e0b',
 bc_check:'#84cc16',
 completeness_check:'#84cc16',
 cost_reconciliation:'#6366f1',
 config_sync:'#6366f1',
 roadmap_sync:'#9333ea',
 trace_review:'#0ea5e9',
 module_curator:'#f97316',
 copy_paste:'#ec4899',
 meta:'#a855f7',
};
// Resolve an agent color from any kind spelling. Normalizes hyphens to
// underscores (the runtime RunEntry.kind mixes both) and falls back to
// grey for unknown kinds (periodic-workflow YAML stems, copy_paste, …).
function agentColor(kind){const k=String(kind||'').replace(/-/g,'_');return AGENT_COLORS[k]||'#6b7280';}
// Apply AGENT_COLORS to each Agents-menu button's --agent-color var.
// Safe to call repeatedly; a button whose key is missing from the map
// keeps the CSS grey fallback (var(--agent-color, #6b7280)).
function applyAgentColors(){
 document.querySelectorAll('.agents-menu button[data-agent]').forEach(b=>b.style.setProperty('--agent-color',agentColor(b.dataset.agent)));
}
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
// JS-string-literal escaper for values interpolated into inline event
// handlers (onclick etc.). esc() escapes HTML text/attribute content but
// NOT the quotes that delimit JS string literals, so a value containing a
// quote would break (or inject into) the generated handler. jsq() returns a
// complete, quoted JS string literal — drop it straight between the call
// parens, e.g. onclick="approveProposal("+jsq(pa.id)+")". JSON.stringify
// produces a properly-escaped, double-quoted JS literal; we then HTML-escape
// it (esc) and encode the wrapping/inner double quotes as &quot; so it is
// also safe inside a double-quoted HTML attribute.
const jsq=s=>esc(JSON.stringify(String(s==null?"":s))).replace(/"/g,"&quot;");
const renderMD = s => { if (!s) return ""; return marked.parse(s); };
const SOURCE_CLASS={retrospect:"retrospect",audit:"audit",config_sync:"config-sync","trace-health":"trace-health",health:"health",test_gap:"test-gap",agent:"agent",survey:"survey",ci:"ci",agent_check:"agent-check",bc_check:"bc-check",cost_reconciliation:"cost-reconciliation",completeness_check:"completeness-check","trace-review":"trace-review",roadmap_sync:"roadmap-sync"};const srcClass=s=>SOURCE_CLASS[s]||"user";
// History row → artifact that the stage producing this state wrote.
// Drives the collapsible-history expanded view. States without an
// entry (draft, blocked, errored, awaiting_user_reply, …) just show
// the note when the user expands them.
const STATE_ARTIFACT={
 human_issue_approval:"draft-original.md",
 ready:"file_map.json",
 // implement.md now lives on the `implement` step row above this
 // transition (see STEP_LABEL); the code_review row exposes the
 // review's feedback so the operator can see what review concluded.
 code_review:"review.md",
 documenting:"",
 implement_complete:"deliver.md",
 human_mr_approval:"merge.md",
 waiting_auto_merge:"merge.md",
 fixing_ci:"merge.md",
 rebasing:"merge.md",
 done:"merge.md",
 closed:"retrospect.md",
 answered:"question-original.md",
};
// Step-event note prefixes → (chip label, artifact). Step events are
// same-state TicketEvents emitted between transitions so the agent
// that produced them gets its own visible row (e.g. implement comes
// between ready and code_review). Recognised prefixes get a labelled
// chip + artifact mapping that override the state defaults.
const STEP_LABEL=[
 ["implement:",          "implement",          "implement.md"],
 ["scope-triage EXPAND", "scope-triage",       ""],
 ["scope-triage REJECT", "scope-triage",       ""],
 ["scope-triage ESCAL",  "scope-triage",       ""],
 ["doc_classifier:",     "doc_classifier",     ""],
 ["merge:",              "merge",              "merge.md"],
 ["review:",             "review",             "review.md"],
 // The breakdown step event is emitted from the /generate-children
 // route + the refine→promote_to_epic path. Trace name is
 // "epic-breakdown".
 ["epic-breakdown",      "epic-breakdown",     ""],
];
function matchStep(note){
 if(!note)return null;
 for(const [pfx,label,art] of STEP_LABEL){
  if(note.startsWith(pfx))return {label,art};
 }
 return null;
}
async function toggleEvent(summaryEl){
 const wrap=summaryEl.parentElement;
 const detail=wrap.querySelector(".ev-detail");
 const arrow=summaryEl.querySelector(".ev-arrow");
 // Compare against "block" rather than "none" — the inline default
 // is empty-string after we toggle once, and that === "" not "none".
 const open=wrap.dataset.open==="1";
 if(!open){
  detail.style.display="block";
  wrap.dataset.open="1";
  if(arrow&&arrow.textContent==="▶")arrow.textContent="▼";
  const art=wrap.dataset.art;
  const tid=wrap.dataset.tid;
  const aEl=wrap.querySelector(".ev-artifact");
  if(art&&aEl&&aEl.dataset.loaded==="0"){
   aEl.dataset.loaded="1";
   try{
    const r=await jget("/tickets/"+encodeURIComponent(tid)+"/artifacts/"+encodeURIComponent(art));
    if(r&&r.content){
     aEl.innerHTML=`<details open><summary class="muted" style="cursor:pointer;font-size:11px">📄 ${esc(art)}</summary><div class="md-body" style="margin-top:6px">${renderMD(r.content)}</div></details>`;
    } else {
     aEl.innerHTML=`<span class="muted" style="font-size:11px">(${esc(art)} not yet written)</span>`;
    }
   } catch(_){
    aEl.innerHTML=`<span class="muted" style="font-size:11px">(${esc(art)} not yet written)</span>`;
   }
  }
 } else {
  detail.style.display="none";
  wrap.dataset.open="0";
  if(arrow&&arrow.textContent==="▼")arrow.textContent="▶";
 }
}
// Single source of truth for collapsed-history rendering — used by
// open_()'s initial paint and the 1s refreshDetail() poll.  Without
// this shared helper the poll re-emitted the legacy single-line
// format and erased per-event expansion state.
// History event → Langfuse trace name. Used by renderHistoryHtml to
// look up which Langfuse trace's cost should appear on each row.
// State chips return the trace that PRODUCED that state; step events
// already carry the trace name in the chip label.
const STATE_TRACE={
 ready:"refine",
 human_issue_approval:"refine",
 code_review:"review",
 documenting:"document",
 deliverable:"deliver",
 implement_complete:"deliver",
 human_mr_approval:"merge",
 waiting_auto_merge:"merge",
 fixing_ci:"ci_fix",
 rebasing:"rebase",
 done:"merge",
 closed:"retrospect",
 answered:"answer",
};
// Infer which Langfuse trace name an event corresponds to.
function eventAgentName(event, isStep, step){
 return isStep ? (step ? step.label : null) : STATE_TRACE[event.state];
}
// Build an event-index → trace map. Each Langfuse trace is claimed by
// AT MOST ONE event: the earliest event after the trace started whose
// inferred agent name matches. Without this dedup pass, a refine
// trace whose result was `human_issue_approval` would show its cost
// twice — once on the `human_issue_approval` transition (refine
// produced it) and again on the later `ready` transition (after the
// operator approves), because both rows have STATE_TRACE→"refine".
function buildEventTraceMap(events, traces){
 const map={}; // event index → trace
 // Stable sort by timestamp ascending so "first match" is well-defined.
 const sortedTraces=(traces||[]).slice().sort(
  (a,b)=>new Date(a.at).getTime()-new Date(b.at).getTime(),
 );
 for(const trace of sortedTraces){
  const tts=new Date(trace.at).getTime();
  for(let i=0;i<events.length;i++){
   if(map[i])continue; // already claimed by another trace
   const e=events[i];
   const prev=events[i-1];
   const isStep=!!prev && prev.state===e.state;
   const step=isStep?matchStep(e.note):null;
   const name=eventAgentName(e, isStep, step);
   if(name!==trace.name)continue;
   const ets=new Date(e.at).getTime();
   if(ets<tts-5000)continue; // 5s grace for clock skew
   map[i]=trace;
   break;
  }
 }
 return map;
}
function renderHistoryHtml(history, ticketId, traces){
 const events=history||[];
 // Detect step events: a same-state event whose previous event had
 // the same state too (i.e. no transition between them). Those rows
 // get their chip + artifact derived from the note prefix instead
 // of the parent state — that's how implement gets its own visible
 // row between ready and code_review.
 const costByIndex=buildEventTraceMap(events, traces||[]);
 // Unclaimed traces = work that ran on Langfuse but has no matching
 // history event. Most often this is an interrupted run (server
 // restart, container kill, OOM) where the agent did N calls,
 // billed cost, then never reached the state-transition write. Show
 // them as synthetic "interrupted: <agent>" rows so the operator
 // can see that work happened (and what it cost) instead of the
 // run silently disappearing.
 //
 // Skip traces with latency == 0 — those are still running. Without
 // this guard the drawer mis-labels an in-flight stage as
 // "interrupted" the moment Langfuse ingests its first observation.
 const claimed=new Set(Object.values(costByIndex).map(t=>t.trace_id));
 const orphanRows=(traces||[])
  .filter(t=>!claimed.has(t.trace_id) && (t.latency===undefined || t.latency>0))
  .map(t=>({
   __orphan:true, at:t.at, name:t.name, cost:t.cost, trace_id:t.trace_id,
  }));
 // Merge events + orphans, sort by `at` ascending. Real events keep
 // their array index; orphan rows are tagged with __orphan.
 const merged=[];
 events.forEach((e,i)=>merged.push({...e, __idx:i}));
 orphanRows.forEach(o=>merged.push(o));
 merged.sort((a,b)=>{
  const ta=new Date(a.at).getTime();
  const tb=new Date(b.at).getTime();
  return ta-tb;
 });
 return `<h3>History</h3>`+merged.map(item=>{
  if(item.__orphan){
   return `<div class="ev ev-is-step ev-orphan" data-tid="${esc(ticketId)}" data-art="" data-open="0">`+
    `<div class="ev-summary" onclick="toggleEvent(this)">`+
     `<span class="ev-arrow">·</span>`+
     `<span class="ev-at muted">${item.at}</span>`+
     `<b class="ev-state ev-step" title="No history event matched this trace — probably an interrupted run">interrupted: ${esc(item.name)}</b>`+
     `<span class="ev-cost" title="Langfuse trace ${esc(item.trace_id)}">$${item.cost.toFixed(4)}</span>`+
    `</div>`+
    `<div class="ev-detail" style="display:none">`+
     `<div class="muted" style="font-size:11px">Langfuse trace ${esc(item.trace_id)} ran at ${item.at} (${esc(item.name)}, $${item.cost.toFixed(4)}) but no history event was written — the stage was interrupted before its transition committed.</div>`+
    `</div>`+
   `</div>`;
  }
  const e=item;
  const i=item.__idx;
  const prev=events[i-1];
  const isStep=prev && prev.state===e.state;
  const step=isStep?matchStep(e.note):null;
  const chipLabel=step?step.label:e.state;
  const chipClass=step?"ev-step":"s-"+e.state;
  const art=(step&&step.art)?step.art:(STATE_ARTIFACT[e.state]||"");
  const hasDetail=!!(e.note||art);
  const trace=costByIndex[i];
  const cost=trace?`<span class="ev-cost" title="Langfuse trace ${esc(trace.trace_id)}">$${trace.cost.toFixed(4)}</span>`:"";
  return `<div class="ev${isStep?" ev-is-step":""}" data-tid="${esc(ticketId)}" data-art="${esc(art)}" data-open="0">`+
   `<div class="ev-summary" onclick="toggleEvent(this)">`+
    `<span class="ev-arrow">${hasDetail?"▶":"·"}</span>`+
    `<span class="ev-at muted">${e.at}</span>`+
    `<b class="ev-state ${chipClass}">${esc(chipLabel)}</b>`+
    cost+
   `</div>`+
   `<div class="ev-detail" style="display:none">`+
    (e.note?`<div class="ev-note">${renderMD(e.note)}</div>`:"")+
    (art?`<div class="ev-artifact" data-loaded="0"><span class="muted">Click expand for ${esc(art)}…</span></div>`:"")+
   `</div>`+
  `</div>`;
 }).join("");
}
function fmtRelative(iso){
 const d=(new Date(iso)).getTime()-Date.now();
 if(d<=0)return"now";
 const s=Math.round(d/1000);
 if(s<60)return"in "+s+"s";
 const m=Math.round(s/60);
 if(m<60)return"in "+m+"m";
 return new Date(iso).toLocaleTimeString();
}
function renderRetryChip(t){
 if(!(t.retry_attempt>0))return"";
 const next=t.next_retry_at?new Date(t.next_retry_at).getTime():0;
 const upcoming=next>Date.now()+1000;
 const parts=[t.last_transient_error||"transient error"];
 if(upcoming)parts.push("retry "+fmtRelative(t.next_retry_at));
 return `<span class="retry-chip" title="${esc(parts.join(' — '))}">↻ ${t.retry_attempt}</span>`;
}
// -- repo selector -------------------------------------------------------
function getRepoId(){
  if(currentRepoId!==null)return currentRepoId;
  const params=new URLSearchParams(window.location.search);
  currentRepoId=params.get("repo")||localStorage.getItem("robotsix-mill:repo-id")||"all";
  return currentRepoId;
}
function onRepoChange(value){
  currentRepoId=value;
  localStorage.setItem("robotsix-mill:repo-id",value);
  const url=new URL(window.location);
  if(value==="all")url.searchParams.delete("repo");
  else url.searchParams.set("repo",value);
  window.history.replaceState({},"",url);
  toggleMetaOnlyButtons();
  updateAgentsMenu();
  refresh();
}
async function fetchRepos(){
  if(reposCache)return reposCache;
  const data=await jget("/repos");
  reposCache=data||[];
  const sel=document.getElementById("repo-selector");
  if(!sel)return reposCache;
  const cur=getRepoId();
  if(reposCache.length<=1){
    // Single repo: no "All" option.
    sel.innerHTML=reposCache.map(r=>`<option value="${esc(r.repo_id)}">${esc(r.repo_id)}</option>`).join("");
    if(reposCache.length===1)onRepoChange(reposCache[0].repo_id);
    sel.value=currentRepoId;
  } else {
    sel.innerHTML='<option value="all">All repos</option>'+
      reposCache.map(r=>`<option value="${esc(r.repo_id)}">${esc(r.repo_id)}</option>`).join("");
    sel.value=cur==="all"||!reposCache.some(r=>r.repo_id===cur)?"all":cur;
  }
  return reposCache;
}
function repoIdForBoardId(boardId){
  if(!reposCache||!boardId)return boardId;
  const r=reposCache.find(r=>r.board_id===boardId);
  return r?r.repo_id:boardId;
}
// -- gate pills (pipeline behaviour flags surfaced in the header) -----
async function fetchGates() {
  const repoId=getRepoId();
  const gatesUrl=repoId!=="all"?"/gates?repo_id="+encodeURIComponent(repoId):"/gates";
  const g = await jget(gatesUrl);
  if (!g) return;
  gatesCache = g;
  document.getElementById("gates").innerHTML = [
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
  ].map(p => `<span class="gate-pill ${p.on ? "gate-on" : "gate-off"}" title="${esc(p.yaml)} — ${esc(p.tip)}">${esc(p.label)} ${p.on ? "✓" : "✗"}</span>`).join("");
}

// Poll Langfuse export status; surface a banner when recent exports
// failed so the operator notices without watching worker logs.
async function fetchLangfuseStatus() {
  const s = await jget("/langfuse-status");
  if (!s) return;
  const banner = document.getElementById("lf-status");
  if (!banner) return;
  if (!s.count) {
    banner.style.display = "none";
    banner.innerHTML = "";
    return;
  }
  const last = s.failures[s.failures.length - 1];
  banner.style.display = "block";
  banner.innerHTML =
    `<span class="lf-badge">⚠ Langfuse export issues</span> ` +
    `${s.count} recent failure(s). Latest: ${esc(last.project || "?")} — ` +
    `<code>${esc((last.error || "").slice(0, 200))}</code> ` +
    `<button onclick="dismissLfStatus()" class="lf-dismiss">dismiss</button>`;
}

async function dismissLfStatus() {
  await jpost("/langfuse-status/clear", {});
  fetchLangfuseStatus();
}

// HTTP helpers built on XMLHttpRequest, not fetch().
// `fetch` is wrapped by SES / hardened-JS extensions (MetaMask, some
// privacy/wallet add-ons) and can fail with "NetworkError when
// attempting to fetch resource" before any request leaves the browser.
// XHR predates that interceptor surface and survives those wrappers.
// All board POST/GET/DELETE goes through these; no direct fetch() use.
function jget(u){return new Promise(res=>{
 const x=new XMLHttpRequest();x.open("GET",u,true);
 x.onload=()=>{if(x.status>=200&&x.status<300){
  try{res(JSON.parse(x.responseText))}catch{res(null)}
 }else res(null)};
 x.onerror=()=>res(null);x.send();
})}
// fetch-shaped response: {ok, status, text(), json()} so call-sites
// keep their `if(!r.ok){const e=await r.text();…}` pattern unchanged.
function _xhr(method,u,body){return new Promise(res=>{
 const x=new XMLHttpRequest();x.open(method,u,true);
 if(body!=null)x.setRequestHeader("Content-Type","application/json");
 const wrap=()=>({ok:x.status>=200&&x.status<300,status:x.status,
  text:()=>Promise.resolve(x.responseText||""),
  json:()=>{try{return Promise.resolve(JSON.parse(x.responseText||"null"))}
           catch(e){return Promise.reject(e)}}});
 x.onload=()=>res(wrap());
 x.onerror=()=>res({ok:false,status:0,
  text:()=>Promise.resolve("network error"),
  json:()=>Promise.resolve(null)});
 x.send(body!=null?JSON.stringify(body):null);
})}
function jpost(u,body){return _xhr("POST",u,body)}
function jdel(u){return _xhr("DELETE",u,null)}
async function refresh(){
 // Skip loading reviewed (closed/done/epic_closed) tickets by default — they dominate
 // the row count and each costs a session_cost lookup. Fetch them only
 // when the user toggles "show closed".
 // Race guard: each refresh() captures a seq token and the showClosed
 // it was started with; a later call bumps refreshSeq, so when this
 // call's await resolves it can tell it's stale and skip rendering.
 // (The auto-1s tick + the toggle's onchange refresh otherwise race,
 // and the last response to land wins — making "show closed" flicker.)
 const wantClosed=showClosed;
 const tok=++refreshSeq;
 await fetchRepos();
 const repoId=getRepoId();
 toggleMetaOnlyButtons();
 updateAgentsMenu();
 const ticketsBase=repoId!=="all"?"/tickets?repo_id="+encodeURIComponent(repoId):"/tickets";
 const url=wantClosed?ticketsBase:(ticketsBase+(ticketsBase.includes("?")?"&":"?")+"include_closed=false");
 const activeUrl=repoId!=="all"?"/active?repo_id="+encodeURIComponent(repoId):"/active";
 fetchGates();
 fetchLangfuseStatus();
 refreshCandidateBadge();
 const [ts, activeList]=await Promise.all([jget(url), jget(activeUrl)]);
 if(!ts)return;
 const active={};
 if(activeList) activeList.forEach(a=>{ active[a.ticket_id]=a; });
 activeMap=active;
 if(tok!==refreshSeq)return;        // a newer refresh started — drop stale
 _renderBoard(ts, wantClosed, repoId);
}

// Render just the inner HTML of one card, given the ticket data,
// the currently-selected repo dropdown, and the column's state
// (used to tweak the live-badge label for rebasing).
function renderCardInner(t,repoId,colState){
 return `<div class="t">${esc(t.title)}</div><div class="id">${t.id}</div>`+
  (t.priority?`<span class="priority-badge" title="priority — pulled from the queue ahead of non-priority tickets">⚡ priority</span>`:"")+
  (repoId==="all"&&t.board_id?`<span class="repo-badge">${esc(repoIdForBoardId(t.board_id))}</span>`:"")+
  (t.kind==="inquiry"?`<span class="inquiry-badge">🔍 inquiry</span>`:"")+
  (t.kind==="epic"?`<span class="epic-badge">📋 epic</span>`:"")+
  (t.parent_id?`<span class="epic-ref">📋 ${esc(t.parent_title||t.parent_id.slice(0,8)+"…")}</span>`:"")+
  (t.state==="awaiting_user_reply"?`<span class="needs-reply-badge">🙋 needs reply</span>`:"")+
  `<span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span>`+
  `<span class="cost">$${(t.cost_usd||0).toFixed(4)}</span>`+
  (t.cumulative_cost&&t.cumulative_cost>t.cost_usd?`<span class="cost-cumulative">/$${t.cumulative_cost.toFixed(4)}</span>`:"")+
  renderRetryChip(t)+
  (activeMap[t.id]?`<span class="live-badge"><span class="live-spinner"></span> ${colState==="rebasing"?"rebasing…":(ACTIVE_LABEL[activeMap[t.id].stage]||activeMap[t.id].stage+"…")}</span>`:"");
}

// Reconcile the .cards container against the desired ticket list.
// Existing cards are reused (preserves DOM identity → no scroll
// reset, no animation interruption); only those whose rendered
// signature changed get their innerHTML rewritten. Card order in
// the column matches the ticket array order via insertBefore.
function syncCards(col,tickets,repoId,colState){
 const cards=col.querySelector(".cards");
 const wantedIds=new Set(tickets.map(t=>t.id));
 const existing=new Map();
 cards.querySelectorAll(".card").forEach(c=>existing.set(c.dataset.id,c));
 // Drop cards no longer in this column (moved state or deleted).
 existing.forEach((card,id)=>{
  if(!wantedIds.has(id)){card.remove();existing.delete(id);}
 });
 let prevCard=null;
 tickets.forEach(t=>{
  let card=existing.get(t.id);
  if(!card){
   card=document.createElement("div");
   card.dataset.id=t.id;
   card.addEventListener("click",()=>open_(t.id));
   existing.set(t.id,card);
  }
  const wantClass=`card s-${t.state}`;
  if(card.className!==wantClass) card.className=wantClass;
  const sig=renderCardInner(t,repoId,colState);
  // Cache last signature on the node; only touch innerHTML when it
  // actually changed. Most ticks for an idle board are no-ops.
  if(card._sig!==sig){card.innerHTML=sig;card._sig=sig;}
  const expectedNext=prevCard?prevCard.nextSibling:cards.firstChild;
  if(card!==expectedNext) cards.insertBefore(card,expectedNext);
  prevCard=card;
 });
}
async function approve(id){
 const r=await jpost("/tickets/"+id+"/approve");
 if(!r.ok){const e=await r.text();alert("approve failed: "+e)}else refresh()
}
function setMergeLoading(id,loading){
 const btns=document.querySelectorAll(`.merge-btn[data-ticket-id="${id.replace(/[\\"]/g,'')}"]`);
 for(const b of btns){
  if(b.hasAttribute('title')) continue;  // pre-existing disabled drawer button
  if(loading){
   b.disabled=true;
   b.classList.add('merging');
   b.innerHTML='<span class="live-spinner"></span> Merging…';
  }else{
   b.disabled=false;
   b.classList.remove('merging');
   b.textContent='Merge';
  }
 }
}
async function mergePR(id){
 if(mergeLoading.has(id)) return;
 mergeLoading.add(id);
 setMergeLoading(id,true);
 const r=await jpost("/tickets/"+id+"/merge-now");
 if(!r.ok){const e=await r.text();mergeLoading.delete(id);setMergeLoading(id,false);alert("merge failed: "+e)}else{mergeLoading.delete(id);refresh()}
}
async function requestChanges(id){
 const body=prompt("Send this ticket back to draft. What needs to change?\n(your comment goes to the refine agent so it can re-process with this feedback.)");
 if(body===null)return;
 if(!body.trim()){
  const existing=await jget("/tickets/"+id+"/comments");
  if(!existing||!existing.length){alert("A comment is required when requesting changes");return}
 }
 const r=await jpost("/tickets/"+id+"/request-changes",{body:body.trim()});
 if(!r.ok){const e=await r.text();alert("request-changes failed: "+e)}else{refresh();if(sel===id)open_(id)}
}
async function addComment(id){
 const body=prompt("Add a comment to this ticket:");
 if(body===null)return;
 if(!body.trim())return;
 const r=await jpost("/tickets/"+id+"/comments",{body:body.trim()});
 if(!r.ok){const e=await r.text();alert("add comment failed: "+e)}else if(sel===id)open_(id)
}
function renderMergeInfo(mi){
 // CI status
 let ciHtml="";
 if(mi.ci_conclusion==="success") ciHtml=`<span class="mi-ok">✓</span> CI passing`;
 else if(mi.ci_conclusion==="failure") {
  const names=mi.ci_failing.map(f=>esc(f.name)).join(", ");
  ciHtml=`<span class="mi-bad">✗</span> CI failing`;
  if(names) ciHtml+=` — ${names}`;
 }
 else if(mi.ci_conclusion==="pending") ciHtml=`<span class="mi-pending">◷</span> CI pending…`;
 else ciHtml=`<span class="mi-unknown">—</span> CI unknown`;

 // Mergeable status
 let mgHtml="";
 if(mi.mergeable===true) mgHtml=`<span class="mi-ok">✓</span> No conflicts`;
 else if(mi.mergeable===false) mgHtml=`<span class="mi-bad">✗</span> Conflicts detected`;
 else mgHtml=`<span class="mi-unknown">—</span> Checking conflicts…`;

 // Files
 let filesHtml="";
 if(mi.files&&mi.files.length){
  filesHtml=`<div class="mi-files-header">${mi.files.length} file${mi.files.length!==1?"s":""} changed</div>`;
  filesHtml+=mi.files.map(f=>{
   let a="", d="";
   if(f.additions) a=`<span class="mi-add">+${f.additions}</span> `;
   if(f.deletions) d=`<span class="mi-del">−${f.deletions}</span> `;
   return `<div class="mi-file">${a}${d}<span class="mi-path">${esc(f.path)}</span> <span class="mi-status">${esc(f.status)}</span></div>`;
  }).join("");
 } else {
  filesHtml=`<div class="mi-files-header muted">(no file info available)</div>`;
 }

 return `<div class="mi-section">
  <h3>Merge Info</h3>
  <div class="mi-row">${ciHtml}</div>
  <div class="mi-row">${mgHtml}</div>
  ${filesHtml}
 </div>`;
}
function renderThreads(cs){
 const threads=cs.filter(c=>c.parent_id===null);
 // Separate open [ASK_USER] threads from normal threads.
 // Closed ASK_USER threads stay in the normal section.
 const askUserThreads=threads.filter(t=>t.body&&t.body.startsWith("[ASK_USER]")&&t.closed_at===null);
 const normalThreads=threads.filter(t=>!askUserThreads.includes(t));
 const replies=cs.filter(c=>c.parent_id!==null);
 const replyMap={};
 replies.forEach(r=>{(replyMap[r.parent_id]||=[]).push(r);});
 function renderOneThread(t){
  const isClosed=t.closed_at!==null;
  const children=replyMap[t.id]||[];
  const replyHtml=children.map(r=>
   `<div class="ev reply-ev"><b class="muted">${r.created_at}</b> · <b>${esc(r.author)}</b>${r.author==="scope-triage"?' <span class="triage-badge">🤖 triage</span>':''}<br>${renderMD(r.body)}</div>`
  ).join("");
  return `<div class="thread${isClosed?' thread-closed':''}">
   <div class="ev"><b class="muted">${t.created_at}</b> · <b>${esc(t.author)}</b>${t.author==="scope-triage"?' <span class="triage-badge">🤖 triage</span>':''}${isClosed?' <span class="closed-badge">🔒 Closed</span>':''}<br>${renderMD(t.body)}</div>
   ${replyHtml}
   <div class="thread-actions">
    <button class="add-comment-btn" onclick="replyToThread(${jsq(t.id)},${jsq(t.ticket_id)})">↩ Reply</button>
    ${isClosed
     ?`<button class="add-comment-btn" onclick="reopenThread(${jsq(t.id)},${jsq(t.ticket_id)})">🔓 Reopen</button>`
     :`<button class="add-comment-btn" onclick="closeThread(${jsq(t.id)},${jsq(t.ticket_id)})">🔒 Close</button>`}
   </div>
  </div>`;
 }
 let html="";
 // Call-to-action banner — only when there are open ASK_USER threads
 if(askUserThreads.length>0){
  html+=`<div class="ask-user-cta"><strong>🙋 This ticket is waiting on your reply.</strong> Reply to the question below and close the thread to resume the ticket.</div>`;
  html+=`<div class="ask-user-threads">${askUserThreads.map(renderOneThread).join("")}</div>`;
 }
 html+=normalThreads.map(renderOneThread).join("");
 return html;
}
async function replyToThread(threadId,ticketId){
 const body=prompt("Reply to this thread:");
 if(body===null)return;
 if(!body.trim())return;
 const r=await jpost("/tickets/"+ticketId+"/comments",{body:body.trim(),parent_id:threadId});
 if(!r.ok){const e=await r.text();alert("reply failed: "+e)}else if(sel===ticketId)open_(ticketId)
}
async function closeThread(commentId,ticketId){
 // Pass ticket_id so the server resolves the correct per-board DB —
 // comment ids are per-board (not globally unique), so a bare close
 // call landed on the wrong board's id=N on collisions.
 const tid=ticketId||sel;
 const url="/comments/"+commentId+"/close"+(tid?"?ticket_id="+encodeURIComponent(tid):"");
 const r=await jpost(url);
 if(!r.ok){const e=await r.text();alert("close thread failed: "+e)}else if(tid)open_(tid)
}
async function reopenThread(commentId,ticketId){
 const tid=ticketId||sel;
 const url="/comments/"+commentId+"/reopen"+(tid?"?ticket_id="+encodeURIComponent(tid):"");
 const r=await jpost(url);
 if(!r.ok){const e=await r.text();alert("reopen thread failed: "+e)}else if(tid)open_(tid)
}
async function newTicket(){
 // Build modal DOM
 const backdrop=document.createElement("div");
 backdrop.className="modal-backdrop";
 const modal=document.createElement("div");
 modal.className="modal";
 const repoId=getRepoId();
 const repoField=repoId==="all"
  ? `<label class="modal-label">Repo <span class="modal-req">*</span></label>
     <select class="modal-input" id="modal-repo" style="width:100%">
       ${(reposCache||[]).map(r=>`<option value="${esc(r.repo_id)}">${esc(r.repo_id)}</option>`).join("")}
     </select>`
  : `<label class="modal-label">Repo</label>
     <select class="modal-input" id="modal-repo" style="width:100%">
       ${(reposCache||[]).map(r=>`<option value="${esc(r.repo_id)}"${r.repo_id===repoId?' selected':''}>${esc(r.repo_id)}</option>`).join("")}
     </select>`;
 modal.innerHTML=
  `<h2>New Ticket</h2>
   <label class="modal-label">Title <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="What needs doing?" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Description</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints… (optional)"></textarea>
   ${repoField}
   <div class="modal-field-error" id="modal-repo-err"></div>
   <div class="modal-buttons">
    <span class="modal-submit-error" id="modal-submit-err"></span>
    <button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>
    <button type="button" class="modal-btn-create" id="modal-create">Create</button>
   </div>`;
 backdrop.appendChild(modal);
 document.body.appendChild(backdrop);

 const titleEl=document.getElementById("modal-title");
 const titleErr=document.getElementById("modal-title-err");
 const descEl=document.getElementById("modal-desc");
 const submitErr=document.getElementById("modal-submit-err");
 const createBtn=document.getElementById("modal-create");

 function close(){
  document.body.removeChild(backdrop);
 }

 function showTitleErr(msg){titleErr.textContent=msg}
 function clearTitleErr(){titleErr.textContent=""}
 function showSubmitErr(msg){submitErr.textContent=msg}
 function clearSubmitErr(){submitErr.textContent=""}

 async function doSubmit(){
  const title=titleEl.value.trim();
  if(!title){showTitleErr("Title is required");titleEl.focus();return}
  clearTitleErr();clearSubmitErr();
  createBtn.disabled=true;createBtn.textContent="Creating…";
  const r=await jpost("/tickets",{title:title,description:descEl.value,repo_id:document.getElementById("modal-repo").value});
  if(!r.ok){const e=await r.text();showSubmitErr("create failed: "+e);
   createBtn.disabled=false;createBtn.textContent="Create"}
  else{close();refresh()}
 }

 // Backdrop click → close
 backdrop.addEventListener("click",function(e){if(e.target===backdrop)close()});

 // Cancel button
 document.getElementById("modal-cancel").addEventListener("click",close);

 // Create button
 createBtn.addEventListener("click",doSubmit);

 // Keyboard handling
 modal.addEventListener("keydown",function(e){
  if(e.key==="Escape"){e.preventDefault();close();return}
  if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault();doSubmit();return}
  if(e.key==="Enter"&&e.target===titleEl){e.preventDefault();descEl.focus();return}
 });

 // Auto-focus title
 titleEl.focus();
}
async function newInquiry(){
 // Build modal DOM
 const backdrop=document.createElement("div");
 backdrop.className="modal-backdrop";
 const modal=document.createElement("div");
 modal.className="modal";
 const repoId=getRepoId();
 const repoField=repoId==="all"
  ? `<label class="modal-label">Repo <span class="modal-req">*</span></label>
     <select class="modal-input" id="modal-repo" style="width:100%">
       ${(reposCache||[]).map(r=>`<option value="${esc(r.repo_id)}">${esc(r.repo_id)}</option>`).join("")}
     </select>`
  : `<input type="hidden" id="modal-repo" value="${esc(repoId)}">`;
 modal.innerHTML=
  `<h2>New Inquiry</h2>
   <label class="modal-label">Question / investigation prompt <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="What do you want to know?" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Context / background</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints… (optional)"></textarea>
   ${repoField}
   <div class="modal-field-error" id="modal-repo-err"></div>
   <div class="modal-buttons">
    <span class="modal-submit-error" id="modal-submit-err"></span>
    <button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>
    <button type="button" class="modal-btn-create" id="modal-create">Create</button>
   </div>`;
 backdrop.appendChild(modal);
 document.body.appendChild(backdrop);

 const titleEl=document.getElementById("modal-title");
 const titleErr=document.getElementById("modal-title-err");
 const descEl=document.getElementById("modal-desc");
 const submitErr=document.getElementById("modal-submit-err");
 const createBtn=document.getElementById("modal-create");

 function close(){
  document.body.removeChild(backdrop);
 }

 function showTitleErr(msg){titleErr.textContent=msg}
 function clearTitleErr(){titleErr.textContent=""}
 function showSubmitErr(msg){submitErr.textContent=msg}
 function clearSubmitErr(){submitErr.textContent=""}

 async function doSubmit(){
  const title=titleEl.value.trim();
  if(!title){showTitleErr("Question is required");titleEl.focus();return}
  clearTitleErr();clearSubmitErr();
  createBtn.disabled=true;createBtn.textContent="Creating…";
  const r=await jpost("/tickets",{title:title,description:descEl.value,kind:"inquiry",repo_id:document.getElementById("modal-repo").value});
  if(!r.ok){const e=await r.text();showSubmitErr("create failed: "+e);
   createBtn.disabled=false;createBtn.textContent="Create"}
  else{close();refresh()}
 }

 // Backdrop click → close
 backdrop.addEventListener("click",function(e){if(e.target===backdrop)close()});

 // Cancel button
 document.getElementById("modal-cancel").addEventListener("click",close);

 // Create button
 createBtn.addEventListener("click",doSubmit);

 // Keyboard handling
 modal.addEventListener("keydown",function(e){
  if(e.key==="Escape"){e.preventDefault();close();return}
  if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault();doSubmit();return}
  if(e.key==="Enter"&&e.target===titleEl){e.preventDefault();descEl.focus();return}
 });

 // Auto-focus title
 titleEl.focus();
}
async function newEpic(){
 // Build modal DOM
 const backdrop=document.createElement("div");
 backdrop.className="modal-backdrop";
 const modal=document.createElement("div");
 modal.className="modal";
 // Repo field: default to currently-selected repo; show a dropdown
 // when viewing "all" so the user picks explicitly.
 const repoId=getRepoId();
 const repoField=repoId==="all"
  ? `<label class="modal-label">Repo <span class="modal-req">*</span></label>
     <select class="modal-input" id="modal-repo" style="width:100%">
       ${(reposCache||[]).map(r=>`<option value="${esc(r.repo_id)}">${esc(r.repo_id)}</option>`).join("")}
     </select>`
  : `<label class="modal-label">Repo</label>
     <select class="modal-input" id="modal-repo" style="width:100%">
       ${(reposCache||[]).map(r=>`<option value="${esc(r.repo_id)}"${r.repo_id===repoId?' selected':''}>${esc(r.repo_id)}</option>`).join("")}
     </select>`;
 modal.innerHTML=
  `<h2>New Epic</h2>
   <label class="modal-label">Title <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="Epic title / goal" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Description</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Scope, outcome, notes… (optional)"></textarea>
   ${repoField}
   <div class="modal-buttons">
    <span class="modal-submit-error" id="modal-submit-err"></span>
    <button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>
    <button type="button" class="modal-btn-create" id="modal-create">Create</button>
   </div>`;
 backdrop.appendChild(modal);
 document.body.appendChild(backdrop);

 const titleEl=document.getElementById("modal-title");
 const titleErr=document.getElementById("modal-title-err");
 const descEl=document.getElementById("modal-desc");
 const submitErr=document.getElementById("modal-submit-err");
 const createBtn=document.getElementById("modal-create");

 function close(){
  document.body.removeChild(backdrop);
 }

 function showTitleErr(msg){titleErr.textContent=msg}
 function clearTitleErr(){titleErr.textContent=""}
 function showSubmitErr(msg){submitErr.textContent=msg}
 function clearSubmitErr(){submitErr.textContent=""}

 async function doSubmit(){
  const title=titleEl.value.trim();
  if(!title){showTitleErr("Title is required");titleEl.focus();return}
  clearTitleErr();clearSubmitErr();
  createBtn.disabled=true;createBtn.textContent="Creating…";
  const r=await jpost("/epics",{title:title,description:descEl.value,repo_id:document.getElementById("modal-repo").value});
  if(!r.ok){const e=await r.text();showSubmitErr("create failed: "+e);
   createBtn.disabled=false;createBtn.textContent="Create"}
  else{close();refresh()}
 }

 // Backdrop click → close
 backdrop.addEventListener("click",function(e){if(e.target===backdrop)close()});

 // Cancel button
 document.getElementById("modal-cancel").addEventListener("click",close);

 // Create button
 createBtn.addEventListener("click",doSubmit);

 // Keyboard handling
 modal.addEventListener("keydown",function(e){
  if(e.key==="Escape"){e.preventDefault();close();return}
  if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault();doSubmit();return}
  if(e.key==="Enter"&&e.target===titleEl){e.preventDefault();descEl.focus();return}
 });

 // Auto-focus title
 titleEl.focus();
}
async function newChildTicket(epicId){
 // Build modal DOM
 const backdrop=document.createElement("div");
 backdrop.className="modal-backdrop";
 const modal=document.createElement("div");
 modal.className="modal";
 modal.innerHTML=
  `<h2>Add Ticket to Epic</h2>
   <label class="modal-label">Title <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="What needs doing?" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Description</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints…"></textarea>
   <div class="modal-buttons">
    <span class="modal-submit-error" id="modal-submit-err"></span>
    <button type="button" class="modal-btn-cancel" id="modal-cancel">Cancel</button>
    <button type="button" class="modal-btn-create" id="modal-create">Create</button>
   </div>`;
 backdrop.appendChild(modal);
 document.body.appendChild(backdrop);

 const titleEl=document.getElementById("modal-title");
 const titleErr=document.getElementById("modal-title-err");
 const descEl=document.getElementById("modal-desc");
 const submitErr=document.getElementById("modal-submit-err");
 const createBtn=document.getElementById("modal-create");

 function close(){
  document.body.removeChild(backdrop);
 }

 function showTitleErr(msg){titleErr.textContent=msg}
 function clearTitleErr(){titleErr.textContent=""}
 function showSubmitErr(msg){submitErr.textContent=msg}
 function clearSubmitErr(){submitErr.textContent=""}

 async function doSubmit(){
  const title=titleEl.value.trim();
  if(!title){showTitleErr("Title is required");titleEl.focus();return}
  clearTitleErr();clearSubmitErr();
  createBtn.disabled=true;createBtn.textContent="Creating…";
  const r=await jpost("/tickets",{title:title,description:descEl.value,parent_id:epicId,kind:"task"});
  if(!r.ok){const e=await r.text();showSubmitErr("create failed: "+e);
   createBtn.disabled=false;createBtn.textContent="Create"}
  else{close();open_(epicId)}
 }

 // Backdrop click → close
 backdrop.addEventListener("click",function(e){if(e.target===backdrop)close()});

 // Cancel button
 document.getElementById("modal-cancel").addEventListener("click",close);

 // Create button
 createBtn.addEventListener("click",doSubmit);

 // Keyboard handling
 modal.addEventListener("keydown",function(e){
  if(e.key==="Escape"){e.preventDefault();close();return}
  if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault();doSubmit();return}
  if(e.key==="Enter"&&e.target===titleEl){e.preventDefault();descEl.focus();return}
 });

 // Auto-focus title
 titleEl.focus();
}
async function redraft(id){
 const body=prompt("Send this ticket back to draft. Why? (optional)");
 if(body===null)return;
 const r=await jpost("/tickets/"+id+"/redraft",{body:body.trim()});
 if(!r.ok){const e=await r.text();alert("redraft failed: "+e)}else{refresh();if(sel===id)open_(id)}
}
async function del_(id){
 if(!confirm("Delete ticket "+id+"? This is irreversible (row, history, workspace)."))return;
 const r=await jdel("/tickets/"+id);
 if(!r.ok&&r.status!==204){const e=await r.text();alert("delete failed: "+e)}else refresh()
}
function toggleAgentsMenu(ev){
 ev.stopPropagation();
 const menu=document.getElementById("agents-menu");
 if(menu) menu.classList.toggle("open");
}
function closeAgentsMenu(){
 const menu=document.getElementById("agents-menu");
 if(menu) menu.classList.remove("open");
}
document.addEventListener("click",(ev)=>{
 const menu=document.getElementById("agents-menu");
 if(!menu||!menu.classList.contains("open"))return;
 if(menu.contains(ev.target))return;
 const trigger=document.querySelector(".agents-trigger");
 if(trigger&&trigger.contains(ev.target))return;
 menu.classList.remove("open");
});
document.addEventListener("keydown",(ev)=>{
 if(ev.key==="Escape") closeAgentsMenu();
});
async function runAudit(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const auditUrl=repoId!=="all"?"/audit?repo_id="+encodeURIComponent(repoId):"/audit";
   const r=await jpost(auditUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Audit started — it runs for a few minutes; new draft tickets will appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Audit failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Audit';
 }
}
async function runTraceHealth(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const thUrl=repoId!=="all"?"/trace-health?repo_id="+encodeURIComponent(repoId):"/trace-health";
   const r=await jpost(thUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Trace-health check started — new draft tickets will appear on the board if unsessioned traces are found.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Trace-health check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Trace Health';
 }
}
async function runLangfuseCleanup(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const lcUrl=repoId!=="all"?"/langfuse-cleanup?repo_id="+encodeURIComponent(repoId):"/langfuse-cleanup";
   const r=await jpost(lcUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Langfuse cleanup started — excess traces will be purged.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Langfuse cleanup failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Langfuse Cleanup';
 }
}
async function runHealth(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const hUrl=repoId!=="all"?"/health-check?repo_id="+encodeURIComponent(repoId):"/health-check";
   const r=await jpost(hUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Health check started — new draft tickets will appear on the board if issues are found.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Health check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Health Check';
 }
}
async function runTestGap(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const tgUrl=repoId!=="all"?"/test-gap?repo_id="+encodeURIComponent(repoId):"/test-gap";
   const r=await jpost(tgUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Test-gap inspection started — new draft tickets will appear on the board if gaps are found.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Test-gap check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Test Gaps';
 }
}
async function runAgentCheck(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const acUrl=repoId!=="all"?"/agent-check?repo_id="+encodeURIComponent(repoId):"/agent-check";
   const r=await jpost(acUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Agent-check started — it inspects every agent's prompt/tools for coherence gaps. New draft tickets appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Agent-check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Agent Check';
 }
}
async function runSurvey(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const sUrl=repoId!=="all"?"/survey?repo_id="+encodeURIComponent(repoId):"/survey";
   const r=await jpost(sUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Survey started — it discovers similar OSS projects and proposes improvements. New draft tickets appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Survey failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Survey';
 }
}
async function runModuleCurator(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const mcUrl=repoId!=="all"?"/module-curator?repo_id="+encodeURIComponent(repoId):"/module-curator";
   const r=await jpost(mcUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Module Curator started — it checks the directory tree against docs/modules.yaml and files drafts for unclassified files / stale paths / new modules. New drafts appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Module Curator failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Module Curator';
 }
}
async function runCopyPaste(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const url=repoId!=="all"?"/copy-paste?repo_id="+encodeURIComponent(repoId):"/copy-paste";
   const r=await jpost(url);
   if(!r.ok){throw new Error(await r.text())}
   alert("Copy-paste detection started — new draft tickets will appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Copy-paste detection failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Copy Paste';
 }
}
async function runBcCheck(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const bcUrl=repoId!=="all"?"/bc-check?repo_id="+encodeURIComponent(repoId):"/bc-check";
   const r=await jpost(bcUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("BC-check started — it scans for backward-compat shims and dead-code branches ripe for removal. New draft tickets appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("BC-check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='BC Check';
 }
}
async function runCompletenessCheck(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const ccUrl=repoId!=="all"?"/completeness-check?repo_id="+encodeURIComponent(repoId):"/completeness-check";
   const r=await jpost(ccUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Completeness-check started — it scans for half-wired features and files draft tickets for discovered gaps. New drafts appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Completeness-check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Completeness';
 }
}

async function runCostReconciliation(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const crUrl=repoId!=="all"?"/cost-reconciliation?repo_id="+encodeURIComponent(repoId):"/cost-reconciliation";
   const r=await jpost(crUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Cost-reconciliation started — it compares OpenRouter vs Langfuse spend and files a draft ticket if drift exceeds $1.00.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Cost-reconciliation failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Cost Recon';
 }
}

async function runConfigSync(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const csUrl=repoId!=="all"?"/config-sync?repo_id="+encodeURIComponent(repoId):"/config-sync";
   const r=await jpost(csUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Config-sync started — it scans for config ↔ .env ↔ docs drift. New draft tickets appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Config-sync failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Config Sync';
 }
}
async function runTraceReview(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const trUrl=repoId!=="all"?"/trace-review?repo_id="+encodeURIComponent(repoId):"/trace-review";
   const r=await jpost(trUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Trace review started — scans Langfuse traces since the last run, flags outliers, runs the cheap flash inspector on flagged ones, files draft tickets per finding.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Trace review failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Trace Review';
 }
}
async function runRoadmapSync(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const repoId=getRepoId();
   const rsUrl=repoId!=="all"?"/roadmap-sync?repo_id="+encodeURIComponent(repoId):"/roadmap-sync";
   const r=await jpost(rsUrl);
   if(!r.ok){throw new Error(await r.text())}
   alert("Roadmap-sync started — it reconciles ROADMAP.md against the board's epics. New epics + a marker-PR appear when it finishes.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Roadmap-sync failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Roadmap Sync';
 }
}
async function runMeta(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/meta");
   if(!r.ok){throw new Error(await r.text())}
   alert("Meta-agent pass started — new extraction and alignment draft tickets will appear on the board when it finishes.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Meta pass failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Meta';
 }
}
function toggleMetaOnlyButtons(){
 const onMeta=getRepoId()==="meta";
 document.querySelectorAll(".meta-only").forEach(el=>{el.style.display=onMeta?"":"none"});
}
// Fetch the set of periodic-agent names enabled for the current repo.
// "all" has no per-repo agents (the dropdown is hidden there anyway).
async function fetchEnabledAgents(){
 const repoId=getRepoId();
 if(repoId==="all")return new Set();
 const list=await jget("/agents?repo_id="+encodeURIComponent(repoId));
 return new Set(Array.isArray(list)?list:[]);
}
// Filter the Agents dropdown for the current repo:
//  * On the "All repos" board the per-repo run buttons make no sense
//    (each POST /<agent> targets one repo) — hide the whole dropdown.
//  * On a repo-specific board show only the agents enabled for that
//    repo (GET /agents). The meta-only Meta button is governed solely
//    by the meta board rule — meta/trace_health/roadmap_sync are
//    global/non-periodic agents the /agents endpoint never returns, so
//    the non-meta ones stay hidden on repo boards by design.
async function updateAgentsMenu(){
 const dd=document.querySelector(".agents-dropdown");
 const repoId=getRepoId();
 if(repoId==="all"){if(dd)dd.style.display="none";return}
 if(dd)dd.style.display="";
 const onMeta=repoId==="meta";
 const enabled=await fetchEnabledAgents();
 if(getRepoId()!==repoId)return;       // repo changed mid-flight — newer call wins
 document.querySelectorAll("#agents-menu button[data-agent]").forEach(btn=>{
  const metaOnly=btn.classList.contains("meta-only");
  const show=metaOnly?onMeta:enabled.has(btn.dataset.agent);
  btn.style.display=show?"":"none";
 });
}
async function generateChildren(id){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Generating…';
 try {
   const r=await jpost("/tickets/"+id+"/generate-children");
   if(!r.ok){throw new Error(await r.text())}
   alert("Epic breakdown started — child tickets will appear below after the agent finishes.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Generate children failed: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Generate Tickets';
 }
}
// State-action buttons (Approve / Request Changes / Redraft / Delete).
// Lives in the drawer header (commit 327a800 moved them off the card,
// then the progressive-fetch rewrite of open_() at 8fbd accidentally
// dropped them — restoring as a single helper used by open_() and
// refreshDetail() so they stay in sync as state changes.
function _actionButtonsHtml(t){
 if(!t)return"";
 const redraftable=!['draft','human_issue_approval','closed','answered','epic_closed','epic_open','done'].includes(t.state);
 const prioLabel=t.priority?"⚡ Priority on":"⚡ Set priority";
 const prioClass=t.priority?"prio-btn prio-btn-on":"prio-btn";
 return (t.state==="human_issue_approval"?
   `<button class="approve-btn" onclick="event.stopPropagation();approve(${jsq(t.id)})">Approve</button>`+
   `<button class="reject-btn" title="Send back to draft with a comment" onclick="event.stopPropagation();requestChanges(${jsq(t.id)})">Request Changes</button>`:"")+
  (redraftable?
   `<button class="redraft-btn" title="Send back to draft" onclick="event.stopPropagation();redraft(${jsq(t.id)})">Redraft</button>`:"")+
  `<button class="${prioClass}" title="Pulled from the queue ahead of non-priority tickets" onclick="event.stopPropagation();togglePriority(${jsq(t.id)},${t.priority?"false":"true"})">${prioLabel}</button>`+
  `<button class="del-btn" title="Delete ticket" style="position:static;opacity:1;margin-left:4px;margin-top:5px;display:inline-block" onclick="event.stopPropagation();del_(${jsq(t.id)})">✕</button>`;
}
async function togglePriority(id,want){
 const r=await jpost("/tickets/"+id+"/priority",{priority:want==="true"||want===true});
 if(!r.ok){const e=await r.text();alert("priority toggle failed: "+e);return}
 refresh();if(sel===id)open_(id);
}
async function open_(id){
 sel=id;
 // Clear every drawer-panel flag — ticket detail and the Runs/Cost/
 // Candidates/Proposals panels are mutually exclusive. Without this
 // the 1s interval re-renders a panel over the ticket detail.
 runsOpen=false;costDashboardOpen=false;candidatesOpen=false;proposalsOpen=false;
 // 1. Open drawer immediately — the 150ms slide-in starts at once
 document.getElementById("drawer").classList.add("open");
 // 2. Show skeleton placeholder while data loads
 const afterBody=gatesCache.comments_after_body;
 const skW=(w,h)=>`<div class="sk-block" style="width:${w};height:${h}"></div>`;
 document.getElementById("d").innerHTML=
  '<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+
  '<div class="drawer-skeleton">'+
  skW('70%','18px')+skW('30%','12px')+skW('90%','12px')+
  '<div class="sk-label"></div>'+skW('100%','14px')+skW('80%','14px')+
  '<div class="sk-label"></div>'+skW('90%','10px')+skW('70%','10px')+
  '</div>';
 // 3. Fire all requests in parallel — same endpoints, no extra latency
 const tP=jget("/tickets/"+id);
 const hP=jget("/tickets/"+id+"/history");
 const dP=jget("/tickets/"+id+"/description");
 const csP=jget("/tickets/"+id+"/comments");
 const rtP=jget("/tickets/"+id+"/retrospect");
 const chP=jget("/tickets/"+id+"/children");
 const cbP=jget("/tickets/"+id+"/cost-breakdown");
 const miP=jget("/tickets/"+id+"/merge-info");
 const mrP=jget("/tickets/"+id+"/merge-reason");
 const msP=jget("/tickets/"+id+"/merge-status");
 // Accumulators for sections that may arrive before the header DOM exists
 let tData=null,_ch,_h,_d,_cs,_rt,_mi,_mr,_ms,_cb;
 function updateMergeButton(){
  if(!tData||tData.state!=="human_mr_approval"||_ms===undefined)return;
  const ba=document.getElementById("ticket-merge-btn-area");
  if(!ba)return;
  ba.innerHTML=
   (_ms&&_ms.can_merge===false?
    `<button class="merge-btn" disabled title="${esc(_ms.reason||'')}">Merge</button>`+
    `<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ ${esc(_ms.reason||'not mergeable')}</p>`:
    `<button class="merge-btn" onclick="event.stopPropagation();mergePR(${jsq(tData.id)})">Merge</button>`
   )+
   (_mr&&_mr.reason?`<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ auto-merge not eligible: ${esc(_mr.reason)}</p>`:"");
 }
 function flushChildren(){
  if(_ch===undefined)return;const el=document.getElementById("ticket-children");if(!el)return;
  el.innerHTML=(_ch&&_ch.length?`<h3>Children (${_ch.length})</h3><div class="children-list">`+
   _ch.map(c=>`<div class="child-ticket" onclick="open_(${jsq(c.id)})"><span class="child-state s-${c.state}">${c.state}</span> <span class="child-title">${esc(c.title)}</span> <span class="child-id muted">${c.id}</span></div>`).join("")+
   `</div>`:"");
 }
 function flushHistory(){
  if(_h===undefined)return;const el=document.getElementById("ticket-history");if(!el)return;
  const traces=(_cb&&_cb.traces)||[];
  el.innerHTML=renderHistoryHtml(_h,id,traces);
 }
 function flushDescription(){
  if(_d===undefined)return;const el=document.getElementById("ticket-body-area");if(!el)return;
  if(afterBody){
   el.innerHTML=`<h3>description.md <button class="toggle-body-btn" onclick="toggleBody(this)" style="font-size:11px;margin-left:8px">▲ Hide</button></h3><div class="md-body" id="ticket-body">${renderMD((_d&&_d.description)||"")}</div>`;
  } else {
   el.innerHTML=`<h3>description.md</h3><div class="md-body">${renderMD((_d&&_d.description)||"")}</div>`;
  }
 }
 function flushRetrospect(){
  if(_rt===undefined)return;const el=document.getElementById("ticket-retrospect");if(!el)return;
  el.innerHTML=(_rt&&_rt.retrospect?`<h3>retrospect.md</h3><div class="md-body">${renderMD(_rt.retrospect)}</div>`:"");
 }
 function flushComments(){
  if(_cs===undefined)return;const el=document.getElementById("ticket-comments");if(!el)return;
  el.innerHTML=`<h3>Comments <button class="add-comment-btn" onclick="addComment(${jsq(id)})">+ Add</button></h3>`+
   ((_cs&&_cs.length)?renderThreads(_cs):`<div class="muted" style="font-size:11px">No comments yet.</div>`);
 }
 function flushMerge(){
  updateMergeButton();
  const mel=document.getElementById("ticket-merge");
  if(mel&&_mi!==undefined)mel.innerHTML=(tData&&tData.state==="human_mr_approval"&&_mi?renderMergeInfo(_mi):"");
 }
 function flushAllSections(){flushChildren();flushHistory();flushDescription();flushRetrospect();flushComments();flushMerge();}
 // 4. Render header as soon as ticket data resolves
 tP.then(t=>{
  if(sel!==id)return;
  if(!t){document.getElementById("d").innerHTML='<div class="muted">Ticket not found</div>';return}
  tData=t;
  document.getElementById("d").innerHTML=
   '<div class="drawer-sticky-head">'+
   '<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+
   '<div id="ticket-header">'+
   `<h3>${esc(t.title)}</h3>`+
   `<div class="muted">${t.id}</div>`+
   `<p>state <b class="s-${t.state}" style="border-left:3px solid var(--c);padding-left:6px">${t.state}</b>`+
   (t.kind==="inquiry"?` <span class="inquiry-badge">🔍 inquiry</span>`:"")+
   (t.kind==="epic"?` <span class="epic-badge">📋 epic</span>`:"")+
   ` · branch ${esc(t.branch)||"—"}<br>`+
   (t.board_id?`repo <span class="repo-badge">${esc(repoIdForBoardId(t.board_id))}</span> · `:"")+
   `source <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span>`+
   (t.origin_session_url?` · origin <a href="${esc(t.origin_session_url)}" target="_blank" rel="noopener" class="origin-link">${esc(t.origin_session)}</a>`:
    t.origin_session?` · origin <span class="muted">${esc(t.origin_session)}</span>`:"")+
   (t.pr_url?` · <a href="${esc(t.pr_url)}" target="_blank" rel="noopener" class="pr-link">🔗 PR</a>`:"")+
   `<span id="ticket-merge-btn-area">`+
   (t.state==="human_mr_approval"?`<span class="sk-inline" style="width:60px;height:22px;vertical-align:middle"></span>`:"")+
   `</span>`+
   `<br>· cost <b>$${(t.cost_usd||0).toFixed(4)}</b>`+
   (t.cumulative_cost&&t.cumulative_cost>t.cost_usd?`<br>· cumulative (incl. children) <b>$${t.cumulative_cost.toFixed(4)}</b>`:"")+
   `<br>created ${t.created_at} · updated ${t.updated_at}</p>`+
   (t.dependencies&&t.dependencies.length?
     `<div style="margin:6px 0"><b>depends on:</b><ul style="margin:4px 0 0 18px;padding:0;list-style:none">`+
     t.dependencies.map(d=>{
       const st=d.state||"?";
       const terminal={"closed":1,"done":1,"epic_closed":1};
       const blocked={"blocked":1,"errored":1};
       const awaiting={"awaiting_user_reply":1,"human_issue_approval":1,"human_mr_approval":1};
       const icon=terminal[st]?"✅":blocked[st]?"⛔":awaiting[st]?"⏸":"⏳";
       const color=terminal[st]?"#10b981":blocked[st]?"#ef4444":awaiting[st]?"#a855f7":"#f59e0b";
       const title=d.title?esc(d.title):"(unknown)";
       const shortId=esc(d.id.slice(0,8)+"…"+d.id.slice(-4));
       return `<li style="margin:2px 0"><span style="color:${color}">${icon}</span> <span style="color:${color};font-family:monospace;font-size:11px;text-transform:uppercase">${esc(st)}</span> · <a href="#" onclick="event.preventDefault();open_(${jsq(d.id)})" title="${esc(d.id)}">${title}</a> <span style="color:#888;font-family:monospace;font-size:11px">${shortId}</span></li>`;
     }).join("")+
     `</ul></div>`:"")+
   (t.unmet_deps&&t.unmet_deps.length?`<p style="color:#f59e0b;font-weight:bold">⏳ waiting on ${t.unmet_deps.length} unfinished dep${t.unmet_deps.length>1?"s":""}</p>`:"")+
   (t.parent_id?`<p><b>Part of epic:</b> <span class="epic-ref">📋 ${esc(t.parent_title||t.parent_id)}</span></p>`:"")+
   (t.kind==="epic"?`<p><button class="add-comment-btn" style="background:#9333ea;color:#fff" onclick="generateChildren(${jsq(t.id)})">Generate Tickets</button> <button class="add-comment-btn" style="background:#2563eb;color:#fff" onclick="newChildTicket(${jsq(t.id)})">Add Ticket</button></p>`:"")+
   `<div id="ticket-action-buttons">${_actionButtonsHtml(t)}</div>`+
   `</div>`+
   `</div>`+
   `<div id="ticket-children" class="detail-section"><div class="sk-label"></div>${skW('60%','12px')}</div>`+
   `<div id="ticket-history" class="detail-section"><div class="sk-label"></div>${skW('90%','10px')}${skW('70%','10px')}</div>`+
   (afterBody?
    `<div id="ticket-body-area" class="detail-section">${skW('100%','40px')}${skW('80%','12px')}</div><div id="ticket-retrospect" class="detail-section"></div><div id="ticket-comments" class="detail-section"><div class="sk-label"></div>${skW('100%','24px')}${skW('80%','24px')}</div>`:
    `<div id="ticket-comments" class="detail-section"><div class="sk-label"></div>${skW('100%','24px')}${skW('80%','24px')}</div><div id="ticket-retrospect" class="detail-section"></div><div id="ticket-body-area" class="detail-section">${skW('100%','40px')}${skW('80%','12px')}</div>`
   )+
   `<div id="ticket-merge" class="detail-section"></div>`;
  // Flush any data that arrived before the header DOM was ready
  flushAllSections();
 });
 // 5. Fire all section handlers — each stores data and tries to flush
 chP.then(ch=>{if(sel!==id)return;_ch=ch;flushChildren();});
 hP.then(h=>{if(sel!==id)return;_h=h;flushHistory();});
 cbP.then(cb=>{if(sel!==id)return;_cb=cb;flushHistory();});
 dP.then(d=>{if(sel!==id)return;_d=d;flushDescription();});
 rtP.then(rt=>{if(sel!==id)return;_rt=rt;flushRetrospect();});
 csP.then(cs=>{if(sel!==id)return;_cs=cs;flushComments();});
 Promise.all([miP,mrP,msP]).then(([mi,mr,ms])=>{
  if(sel!==id)return;
  _mi=mi;_mr=mr;_ms=ms;
  flushMerge();
 });
}
function toggleBody(btn) {
  const body = document.getElementById("ticket-body");
  if (!body) return;
  if (body.style.display === "none") {
    body.style.display = "";
    btn.textContent = "▲ Hide";
  } else {
    body.style.display = "none";
    btn.textContent = "▼ Show";
  }
}
function close_(){sel=null;runsOpen=false;costDashboardOpen=false;
 candidatesOpen=false;proposalsOpen=false;
 document.getElementById("drawer").classList.remove("open")}
// Cache the last runs payload (sans elapsed) so the 1s auto-refresh
// only rebuilds the list DOM when something material changed (new run,
// status flip, summary edit). Elapsed times are advanced in-place
// every tick so the user doesn't see the panel flash.
let _runsLastSig=null;
function _runRowHtml(r,elapsed){
 const kc=agentColor(r.kind);
 const sc=r.status==='running'?'#eab308':r.status==='ok'?'#22c55e':'#ef4444';
 const st=r.status==='running'?'running…':r.status;
 // Repo badge: only meaningful when the user is viewing All repos
 // and the run carries a repo_id tag. Otherwise the active filter
 // already implies which repo the run belongs to.
 const repoTag=(getRepoId()==='all'&&r.repo_id)?
   `<span class="repo-badge" style="margin-right:6px">${esc(r.repo_id)}</span>`:'';
 return `<div data-run-id="${esc(r.id||'')}" data-run-status="${esc(r.status||'')}" style="padding:8px 0;border-bottom:1px solid #262b36">
  ${repoTag}<span style="display:inline-block;padding:1px 6px;border-radius:4px;
   background:${kc};color:#fff;font-size:10px;margin-right:6px">${r.kind}</span>
  <span style="display:inline-block;padding:1px 6px;border-radius:4px;
   background:${sc};color:#fff;font-size:10px">${st}</span>
  <span style="color:#7d828c;font-size:10px;margin-left:6px">${r.started_at}</span>
  <span class="run-elapsed" style="color:#7d828c;font-size:10px;margin-left:3px">${elapsed}</span>
  <div style="font-size:11px;color:#aab0bd;margin-top:3px;white-space:pre-wrap">${esc(r.summary||'')}</div>
  ${r.error?`<div style="font-size:11px;color:#f87171;margin-top:2px">${esc(r.error)}</div>`:''}
 </div>`;
}
function _runElapsed(r){
 const s=Date.parse(r.started_at);
 const f=r.finished_at?Date.parse(r.finished_at):null;
 const e=f?f:(Date.now());
 const ms=e-s;
 const sec=Math.floor(ms/1000);
 const min=Math.floor(sec/60);
 const sss=sec%60;
 return f?(min+'m '+sss+'s'):'running…';
}
async function renderRuns(){
 const repoId=getRepoId();
 const runsUrl=repoId!=="all"?"/runs?repo_id="+encodeURIComponent(repoId):"/runs";
 const rs=await jget(runsUrl);
 // Signature = identity columns only, NOT elapsed (which would change
 // every tick and force a full re-render the user sees as a flash).
 const sig=rs?JSON.stringify(rs.map(r=>[r.id||r.started_at, r.status, r.finished_at, r.summary, r.error])):"null";
 const d=document.getElementById("d");
 // In-place elapsed update only when the DOM is already showing runs
 // (data-run-id markers present). Otherwise the cached signature would
 // skip a needed redraw when the drawer was showing something else.
 const domHasRuns=d.querySelector("[data-run-id]")!==null||(rs&&!rs.length);
 if(sig===_runsLastSig && domHasRuns){
  if(rs&&rs.length){
   const rows=d.querySelectorAll("[data-run-id]");
   rows.forEach((row,i)=>{
    if(!rs[i])return;
    const el=row.querySelector(".run-elapsed");
    if(el)el.textContent=_runElapsed(rs[i]);
   });
  }
  return;
 }
 _runsLastSig=sig;
 d.innerHTML='<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+
  (rs&&rs.length?
   rs.map(r=>_runRowHtml(r,_runElapsed(r))).join("")
   :`<div class="muted">No runs yet. Click Run Audit or Trace Health to start one.</div>`);
}
async function toggleRuns(){
 if(runsOpen){close_();return}
 if(sel){close_()}
 await renderRuns();
 runsOpen=true;
 document.getElementById("drawer").classList.add("open");
}
// -- cost dashboard -----------------------------------------------------
async function openCostDashboard(){
 if(costDashboardOpen){close_();return}
 if(sel){close_()}
 costDashboardOpen=true;
 document.getElementById("drawer").classList.add("open");
 await renderCostDashboard();
}
async function renderCostDashboard(){
 // Race guard: each call captures a token so stale responses (from a
 // prior selector change still in flight against Langfuse) cannot
 // overwrite the chart/highlights with the wrong max_tickets/lookback.
 const tok=++costRenderSeq;
 const selTimeOpt=lookback=>lookback===costLookbackHours?' selected':'';
 const selTickOpt=n=>n===costMaxTickets?' selected':'';
 const timeModeActive=costMode==='time';
 const repoId=getRepoId();
 const hoursLabel=costLookbackHours===1?"1 hour":costLookbackHours+" hours";
 const repoLabel=repoId==="all"?"Costs across all repos (last "+hoursLabel+")":"Costs for "+esc(repoId)+" (last "+hoursLabel+")";
 document.getElementById("d").innerHTML='<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+'<h3>💰 Cost Dashboard <span class="muted" style="font-size:11px;font-weight:normal">— '+repoLabel+'</span></h3>'+
  '<div class="cost-lookback">'+
   '<div class="cost-mode-toggle">'+
    '<button class="cost-mode-btn'+(timeModeActive?' active':'')+'" onclick="costMode=\'time\';renderCostDashboard()">⏱️ Time window</button>'+
    '<button class="cost-mode-btn'+(!timeModeActive?' active':'')+'" onclick="costMode=\'tickets\';renderCostDashboard()">🎫 Last N tickets</button>'+
   '</div>'+
   (timeModeActive?
    '<label>Last <select id="cost-lookback" onchange="costLookbackHours=parseInt(this.value);renderCostDashboard()">'+
     '<option value="1"'+selTimeOpt(1)+'>1 hour</option>'+
     '<option value="6"'+selTimeOpt(6)+'>6 hours</option>'+
     '<option value="24"'+selTimeOpt(24)+'>24 hours</option>'+
     '<option value="72"'+selTimeOpt(72)+'>3 days</option>'+
     '<option value="168"'+selTimeOpt(168)+'>7 days</option>'+
    '</select></label>'
    :
    '<label>Last <select id="cost-max-tickets" onchange="costMaxTickets=parseInt(this.value);renderCostDashboard()">'+
     '<option value="20"'+selTickOpt(20)+'>20 tickets</option>'+
     '<option value="100"'+selTickOpt(100)+'>100 tickets</option>'+
     '<option value="1000"'+selTickOpt(1000)+'>1000 tickets</option>'+
    '</select></label>')+
  '</div>'+
  '<canvas id="cost-sparkline" style="display:none"></canvas>'+
  '<div id="cost-chart">loading…</div>'+
  '<div id="cost-highlights"></div>';

 const extraParam=(timeModeActive?('lookback_hours='+costLookbackHours):('max_tickets='+costMaxTickets))+'&repo_id='+(repoId==="all"?"all":encodeURIComponent(repoId));
 const trendUrl="/costs/trend?"+extraParam;
 const baseUrl="/costs/by-agent?"+extraParam;
 const ticketUrl="/costs/most-expensive-ticket?"+extraParam;
 const traceUrl="/costs/most-expensive-trace?"+extraParam;
 const [trendData, data, topTicket, topTrace]=await Promise.all([
  jget(trendUrl), jget(baseUrl), jget(ticketUrl), jget(traceUrl)
 ]);
 if(tok!==costRenderSeq)return;       // a newer render started — drop stale results
 if(!costDashboardOpen)return;        // user closed the drawer mid-flight

 // -- sparkline ----------------------------------------------------------
 const sparkCanvas=document.getElementById("cost-sparkline");
 if(trendData&&trendData.buckets&&trendData.buckets.length>0){
  const buckets=trendData.buckets;
  sparkCanvas.style.display="block";
  // Resize to match displayed width (CSS-driven) for crisp rendering.
  const dpr=window.devicePixelRatio||1;
  const rect=sparkCanvas.getBoundingClientRect();
  sparkCanvas.width=rect.width*dpr;
  sparkCanvas.height=rect.height*dpr;
  const ctx=sparkCanvas.getContext("2d");
  ctx.scale(dpr,dpr);
  const w=rect.width, h=rect.height;
  const pad={top:4,right:4,bottom:20,left:4};
  const pw=w-pad.left-pad.right;
  const ph=h-pad.top-pad.bottom;
  const maxCost=Math.max(...buckets.map(b=>b.total_cost),0.0001);

  // Background
  ctx.fillStyle="#1a1e27";
  ctx.beginPath();
  ctx.roundRect(0,0,w,h,7);
  ctx.fill();

  if(buckets.length===1){
   // Single bucket: draw a dot
   const x=pad.left+pw/2;
   const y=pad.top+ph/2;
   ctx.fillStyle="#3b82f6";
   ctx.beginPath();
   ctx.arc(x,y,3,0,Math.PI*2);
   ctx.fill();
  } else {
   const points=[];
   buckets.forEach((b,i)=>{
    const x=pad.left+(i/(buckets.length-1))*pw;
    const y=pad.top+ph-(b.total_cost/maxCost)*ph;
    points.push({x,y,cost:b.total_cost,ts:b.ts});
   });
   // Area fill
   ctx.fillStyle="rgba(59,130,246,0.15)";
   ctx.beginPath();
   ctx.moveTo(points[0].x,pad.top+ph);
   points.forEach(p=>ctx.lineTo(p.x,p.y));
   ctx.lineTo(points[points.length-1].x,pad.top+ph);
   ctx.closePath();
   ctx.fill();
   // Line
   ctx.strokeStyle="rgba(59,130,246,0.5)";
   ctx.lineWidth=1.5;
   ctx.beginPath();
   points.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
   ctx.stroke();
   // Dots
   ctx.fillStyle="#3b82f6";
   points.forEach(p=>{
    ctx.beginPath();ctx.arc(p.x,p.y,2,0,Math.PI*2);ctx.fill();
   });
  }

  // Tooltip via title
  let title="";
  buckets.forEach(b=>{title+=b.ts+": $"+b.total_cost.toFixed(4)+" ("+b.trace_count+" traces)\n";});
  sparkCanvas.title=title.trim();
 } else {
  sparkCanvas.style.display="block";
  const dpr=window.devicePixelRatio||1;
  const rect=sparkCanvas.getBoundingClientRect();
  sparkCanvas.width=rect.width*dpr;
  sparkCanvas.height=rect.height*dpr;
  const ctx=sparkCanvas.getContext("2d");
  ctx.scale(dpr,dpr);
  ctx.fillStyle="#1a1e27";
  ctx.beginPath();
  ctx.roundRect(0,0,rect.width,rect.height,7);
  ctx.fill();
  ctx.fillStyle="#7d828c";
  ctx.font="11px ui-monospace,monospace";
  ctx.textAlign="center";
  const emptyMsg=timeModeActive?'No trend data available for this period.':'No trend data available for the last '+costMaxTickets+' tickets.';
  ctx.fillText(emptyMsg,rect.width/2,rect.height/2);
 }

 // -- per-agent bar chart -----------------------------------------------
 if(!data||!data.length){
  const emptyMsg=timeModeActive?'No cost data available for this period.':'No cost data available for the last '+costMaxTickets+' tickets.';
  document.getElementById("cost-chart").innerHTML='<div class="muted">'+emptyMsg+'</div>';
 } else {
  const colors=["#3b82f6","#8b5cf6","#22c55e","#eab308","#ef4444","#f97316","#06b6d4","#ec4899","#14b8a6","#a855f7"];
  const maxCost=Math.max(...data.map(d=>d.total_cost),0.0001);
  const grandTotal=data.reduce((s,d)=>s+d.total_cost,0);
  const totalTraceCount=data.reduce((s,d)=>s+d.trace_count,0);
  const avgTraceCost=totalTraceCount>0?grandTotal/totalTraceCount:null;
  let html='<div class="cost-summary-row">'+
   '<span class="cost-summary">'+data.length+' agents · $'+grandTotal.toFixed(4)+' total</span>'+
   '<span class="cost-summary-divider">|</span>';
  if(avgTraceCost!==null){
   html+='<span class="cost-avg-tile">Avg <span class="cost-avg-value">$'+avgTraceCost.toFixed(4)+'</span> / trace</span>';
  } else {
   html+='<span class="cost-avg-tile muted">Avg — / trace</span>';
  }
  html+='</div>';
  data.forEach((d,i)=>{
   const pct=Math.max((d.total_cost/maxCost)*100,1);
   const color=colors[i%colors.length];
   html+='<div class="cost-bar-row">'+
    '<div class="cost-bar-label">'+
     '<span class="cost-bar-name">'+esc(d.name)+'</span>'+
     '<span class="cost-bar-count">'+d.trace_count+' traces</span>'+
    '</div>'+
    '<div class="cost-bar-track">'+
     '<div class="cost-bar-fill" style="width:'+pct+'%;background:'+color+'"></div>'+
    '</div>'+
    '<div class="cost-bar-amount">$'+d.total_cost.toFixed(4)+'</div>'+
   '</div>';
  });
  document.getElementById("cost-chart").innerHTML=html;
 }

 // -- highlights section ------------------------------------------------
 let highlightsHtml='<h4 style="margin-top:16px">🔍 Highlights</h4>';

 // Most Expensive Ticket
 highlightsHtml+='<div class="cost-bar-row cost-highlight-row">'+
  '<div class="cost-bar-label">'+
   '<span class="cost-bar-name">Most Expensive Ticket</span>'+
  '</div>';
 if(topTicket){
  highlightsHtml+=
   '<div class="cost-bar-track">'+
    '<a href="#" onclick="open_('+jsq(topTicket.ticket_id)+');return false">'+esc(topTicket.title)+'</a>'+
    '<span class="cost-bar-count">'+esc(topTicket.ticket_id)+'</span>'+
   '</div>'+
   '<div class="cost-bar-amount">$'+topTicket.cost_usd.toFixed(4)+'</div>';
 } else {
  highlightsHtml+=
   '<div class="cost-bar-track"><span class="muted">No data</span></div>'+
   '<div class="cost-bar-amount"></div>';
 }
 highlightsHtml+='</div>';

 // Most Expensive Agent Run
 highlightsHtml+='<div class="cost-bar-row cost-highlight-row">'+
  '<div class="cost-bar-label">'+
   '<span class="cost-bar-name">Most Expensive Run</span>'+
  '</div>';
 if(topTrace){
  highlightsHtml+=
   '<div class="cost-bar-track">'+
    '<span style="color:#cfd3db">'+esc(topTrace.name)+'</span>'+
    '<span class="cost-bar-count">'+esc(topTrace.id)+'</span>'+
   '</div>'+
   '<div class="cost-bar-amount">$'+topTrace.total_cost.toFixed(4)+'</div>';
 } else {
  highlightsHtml+=
   '<div class="cost-bar-track"><span class="muted">No data</span></div>'+
   '<div class="cost-bar-amount"></div>';
 }
 highlightsHtml+='</div>';

 document.getElementById("cost-highlights").innerHTML=highlightsHtml;
}
let candidatesOpen=false;
let proposalsOpen=false;
// --- AGENT.md candidates ----------------------------------------------------
// Surface AGENT_CANDIDATES.md entries (written by retrospect) as actionable
// cards. Validate files an audited-repo draft ticket; reject just stamps the
// .md file. Both routes update the file in place so re-opening the drawer
// shows the candidate gone.

// Surface a warning on the 📋 AGENT.md header button when the selected
// repo has pending candidates. Per-board, so "all"/"meta" show nothing.
async function refreshCandidateBadge(){
 const btn=document.getElementById("agentmd-btn");
 const badge=document.getElementById("agentmd-badge");
 if(!btn||!badge)return;
 const reset=()=>{badge.style.display="none";badge.textContent="";btn.style.borderColor="#3a2a4b";btn.style.boxShadow=""};
 const repo=getRepoId();
 if(!repo||repo==="all"||repo==="meta"){btn.style.display="none";reset();return}
 btn.style.display="";
 try{
  const cands=await jget("/candidates?repo_id="+encodeURIComponent(repo));
  if(!Array.isArray(cands))return;       // fetch failed → leave badge unchanged
  if(cands.length>0){
   badge.textContent="⚠ "+cands.length;
   badge.style.display="";
   btn.style.borderColor="#f59e0b";
   btn.style.boxShadow="0 0 0 1px #f59e0b";
  }else reset();
 }catch(e){/* silently leave badge unchanged on error */}
}

async function openCandidates(){
 if(candidatesOpen){close_();return}
 if(sel||runsOpen||costDashboardOpen||proposalsOpen)close_();
 candidatesOpen=true;
 document.getElementById("drawer").classList.add("open");
 await renderCandidatesList();
}

async function renderCandidatesList(){
 const repo=getRepoId();
 const esc=s=>{const d=document.createElement("div");d.textContent=s;return d.innerHTML};
 const drawer=document.getElementById("d");
 if(!repo||repo==="all"){
  drawer.innerHTML='<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+
   '<h3>AGENT.md candidates</h3>'+
   '<div class="muted" style="padding:12px 0">Select a single repo (top-left selector) — candidates are per-board.</div>';
  return;
 }
 drawer.innerHTML='<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+
  '<h3>AGENT.md candidates · '+esc(repo)+'</h3>'+
  '<div class="muted" style="margin-bottom:10px;font-size:11px">'+
  'Retrospect proposes rules for the audited repo\'s <code>AGENT.md</code>. '+
  'Validate to file a draft ticket that edits <code>AGENT.md</code> on this repo; '+
  'reject to dismiss.</div>'+
  '<div id="candidates-list">loading…</div>';
 let cands;
 try{cands=await jget("/candidates?repo_id="+encodeURIComponent(repo))}
 catch(e){
  document.getElementById("candidates-list").innerHTML=
   '<div class="muted" style="padding:12px 0;color:#f87171">failed to load candidates: '+esc(String(e))+'</div>';
  return;
 }
 if(!Array.isArray(cands)||!cands.length){
  document.getElementById("candidates-list").innerHTML=
   '<div class="muted" style="padding:12px 0">No pending candidates. Retrospect appends new entries as it runs.</div>';
  return;
 }
 let html='';
 cands.forEach(function(c){
  html+='<div class="candidate-card" id="cand-'+esc(c.candidate_id)+'" style="border:1px solid #2c313d;border-radius:6px;padding:10px 12px;margin-bottom:10px;background:#1d212c">'+
   '<div style="font-size:11px;color:#9ca3af;margin-bottom:4px">'+esc(c.section)+' · proposed '+esc(c.proposed_at)+'</div>'+
   '<blockquote style="margin:4px 0 8px 0;padding:6px 10px;border-left:3px solid #7c3aed;background:#1a1d27;color:#e2e4eb;font-size:13px;line-height:1.4">'+
   esc(c.rule)+'</blockquote>'+
   '<div style="font-size:11px;color:#9ca3af;margin-bottom:8px"><strong>Rationale:</strong> '+esc(c.rationale)+'</div>'+
   '<div style="font-size:10px;color:#6b7280;margin-bottom:8px">From ticket <code>'+esc(c.source_ticket)+'</code></div>'+
   '<div style="display:flex;gap:6px">'+
    '<button onclick="validateCandidate('+jsq(c.candidate_id)+')" style="font-size:11px;padding:4px 12px;background:#059669;color:#fff;border:none;border-radius:4px;cursor:pointer">'+
    '✓ Validate &amp; file ticket</button>'+
    '<button onclick="rejectCandidate('+jsq(c.candidate_id)+')" style="font-size:11px;padding:4px 12px;background:#374151;color:#cfd3db;border:none;border-radius:4px;cursor:pointer">'+
    '✕ Reject</button>'+
   '</div>'+
  '</div>';
 });
 document.getElementById("candidates-list").innerHTML=html;
}

async function validateCandidate(cid){
 const repo=getRepoId();
 const card=document.getElementById("cand-"+cid);
 if(card){card.style.opacity='0.5';card.querySelectorAll("button").forEach(b=>b.disabled=true)}
 try{
  const r=await fetch("/candidates/"+encodeURIComponent(cid)+"/validate?repo_id="+encodeURIComponent(repo),{method:"POST"});
  if(!r.ok){
   const txt=await r.text();
   alert("Validate failed: "+txt);
   if(card){card.style.opacity='';card.querySelectorAll("button").forEach(b=>b.disabled=false)}
   return;
  }
  // Refresh the list — the validated card drops out, the new ticket
  // appears in the kanban via the regular refresh tick.
  await renderCandidatesList();
  refreshCandidateBadge();
 }catch(e){
  alert("Validate error: "+e);
  if(card){card.style.opacity='';card.querySelectorAll("button").forEach(b=>b.disabled=false)}
 }
}

async function rejectCandidate(cid){
 if(!confirm("Reject this candidate? It stays in the file as audit trail but won't be surfaced again."))return;
 const repo=getRepoId();
 try{
  const r=await fetch("/candidates/"+encodeURIComponent(cid)+"/reject?repo_id="+encodeURIComponent(repo),{method:"POST"});
  if(!r.ok){alert("Reject failed: "+await r.text());return}
  await renderCandidatesList();
  refreshCandidateBadge();
 }catch(e){alert("Reject error: "+e)}
}

// --- Proposed actions -------------------------------------------------------
// Surface pending ProposedAction records (written by periodic agents) as
// actionable cards. Approve executes the mutation against the target ticket;
// reject marks it rejected. Both re-render so the resolved card drops out of
// the pending view. Per-board: needs a single selected repo.
async function toggleProposals(){
 if(proposalsOpen){close_();return}
 if(sel||runsOpen||costDashboardOpen||candidatesOpen)close_();
 proposalsOpen=true;
 document.getElementById("drawer").classList.add("open");
 await renderProposals();
}

async function renderProposals(){
 const drawer=document.getElementById("d");
 const repo=getRepoId();
 // Per-board guard: the proposals panel shows actions for the
 // currently selected repo only (no all-repos aggregation in the UI).
 if(!repo||repo==="all"){
  drawer.innerHTML='<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+
   '<h3>Proposed actions</h3>'+
   '<div class="muted" style="padding:12px 0">Select a single repo (top-left selector) — proposed actions are per-board.</div>';
  return;
 }
 let pas;
 try{pas=await jget("/proposed-actions?status=pending&repo_id="+encodeURIComponent(repo))}
 catch(e){pas=null}
 const shell='<div class="drawer-close-row"><span class="x" onclick="close_()" title="Cancel">&times;</span></div>'+
  '<h3>Proposed actions</h3>';
 if(!Array.isArray(pas)){
  drawer.innerHTML=shell+'<div class="muted" style="padding:12px 0;color:#f87171">failed to load proposed actions.</div>';
  return;
 }
 if(!pas.length){
  drawer.innerHTML=shell+'<div class="muted">No pending proposed actions.</div>';
  return;
 }
 let html='';
 pas.forEach(function(pa){
  const at=String(pa.action_type||"").toLowerCase();
  const st=String(pa.status||"").toLowerCase();
  html+='<div class="proposal-card">'+
   '<div>'+
    '<span class="pa-source src-'+esc(srcClass(pa.source))+'">'+esc(pa.source)+'</span>'+
    '<span class="pa-action pa-action-'+esc(at)+'">'+esc(pa.action_type)+'</span>'+
    '<span class="pa-target" onclick="open_('+jsq(pa.target_ticket_id)+')">'+esc(pa.target_ticket_id)+'</span>'+
   '</div>'+
   '<div class="pa-rationale">'+esc(pa.rationale)+'</div>'+
   '<div class="pa-meta">'+esc(pa.created_at)+' · <span class="pa-status-'+esc(st)+'">'+esc(pa.status)+'</span></div>'+
   (st==="pending"?
    '<div class="pa-buttons">'+
     '<button class="approve-btn" onclick="approveProposal('+jsq(pa.id)+')">Approve</button>'+
     '<button class="reject-btn" onclick="rejectProposal('+jsq(pa.id)+')">Reject</button>'+
    '</div>':'')+
  '</div>';
 });
 drawer.innerHTML=shell+html;
}

async function approveProposal(id){
 const repo=getRepoId();
 const r=await jpost("/proposed-actions/"+encodeURIComponent(id)+"/approve?repo_id="+encodeURIComponent(repo));
 if(!r.ok){alert("Approve failed: "+await r.text());return}
 await renderProposals();
}

async function rejectProposal(id){
 const repo=getRepoId();
 const r=await jpost("/proposed-actions/"+encodeURIComponent(id)+"/reject?repo_id="+encodeURIComponent(repo));
 if(!r.ok){alert("Reject failed: "+await r.text());return}
 await renderProposals();
}

// Re-fetch detail sections WITHOUT resetting the drawer to skeleton.
// The auto-refresh tick used to call open_(sel) every 1s which reset
// the whole drawer to skeleton placeholders then progressively repainted
// — visible as a 1Hz blink. refreshDetail() re-fetches the same
// endpoints and only re-renders a section when its rendered HTML
// changed (cached in _detailLast).
const _detailLast={};
async function refreshDetail(id){
 if(!document.getElementById("ticket-header"))return; // header not yet rendered → let open_() handle
 const [t,ch,h,d,rt,cs,mi,mr,ms,cb]=await Promise.all([
  jget("/tickets/"+id), jget("/tickets/"+id+"/children"),
  jget("/tickets/"+id+"/history"), jget("/tickets/"+id+"/description"),
  jget("/tickets/"+id+"/retrospect"), jget("/tickets/"+id+"/comments"),
  jget("/tickets/"+id+"/merge-info"), jget("/tickets/"+id+"/merge-reason"),
  jget("/tickets/"+id+"/merge-status"),
  jget("/tickets/"+id+"/cost-breakdown"),
 ]);
 if(sel!==id||!t)return;
 const swap=(elId,html)=>{
  const el=document.getElementById(elId);
  if(!el)return;
  const key=elId+":"+id;
  if(_detailLast[key]===html)return;
  _detailLast[key]=html;
  el.innerHTML=html;
 };
 // Update only the volatile parts of the header: state badge + cost + updated_at + retry button + merge-btn-area + action buttons.
 const stateBadge=document.querySelector("#ticket-header b.s-"+t.state)||document.querySelector("#ticket-header b[class^='s-']");
 if(stateBadge&&stateBadge.textContent!==t.state){
  stateBadge.className="s-"+t.state;
  stateBadge.textContent=t.state;
 }
 // Action buttons depend on state — swap when state changed.
 swap("ticket-action-buttons", _actionButtonsHtml(t));
 // Children
 swap("ticket-children", ch&&ch.length?`<h3>Children (${ch.length})</h3><div class="children-list">`+
  ch.map(c=>`<div class="child-ticket" onclick="open_(${jsq(c.id)})"><span class="child-state s-${c.state}">${c.state}</span> <span class="child-title">${esc(c.title)}</span> <span class="child-id muted">${c.id}</span></div>`).join("")+`</div>`:"");
 // History — render via the shared collapsible helper. Preserve
 // expansion state across the 1s poll: any row the user had open
 // before stays open + still shows its loaded artifact.
 //
 // CRUCIAL: only re-fire toggleEvent on rows whose data-open got
 // CLEARED by the swap (meaning new HTML actually replaced the DOM).
 // When the cached-HTML guard makes swap() a no-op, the DOM still
 // carries data-open="1" — re-toggling there would CLOSE the row
 // every tick, which is exactly the "history keeps collapsing every
 // second" symptom this block exists to prevent.
 const histEl=document.getElementById("ticket-history");
 const wasOpen=new Set();
 if(histEl){
  histEl.querySelectorAll(".ev[data-open='1']").forEach(w=>{
   const at=w.querySelector(".ev-at");
   const st=w.querySelector(".ev-state");
   if(at&&st)wasOpen.add(at.textContent+"|"+st.textContent);
  });
 }
 const newHistHtml=renderHistoryHtml(h,id,(cb&&cb.traces)||[]);
 swap("ticket-history", newHistHtml);
 if(wasOpen.size>0){
  const el2=document.getElementById("ticket-history");
  if(el2){
   el2.querySelectorAll(".ev").forEach(w=>{
    if(w.dataset.open==="1")return; // swap was a no-op — leave alone
    const at=w.querySelector(".ev-at");
    const st=w.querySelector(".ev-state");
    if(at&&st&&wasOpen.has(at.textContent+"|"+st.textContent)){
     const sum=w.querySelector(".ev-summary");
     if(sum)toggleEvent(sum);
    }
   });
  }
 }
 // Description (only swap if content changed; respects the after-body layout)
 const afterBody=gatesCache.comments_after_body;
 swap("ticket-body-area", afterBody?
  `<h3>description.md <button class="toggle-body-btn" onclick="toggleBody(this)" style="font-size:11px;margin-left:8px">▲ Hide</button></h3><div class="md-body" id="ticket-body">${renderMD((d&&d.description)||"")}</div>`:
  `<h3>description.md</h3><div class="md-body">${renderMD((d&&d.description)||"")}</div>`);
 // Retrospect
 swap("ticket-retrospect", rt&&rt.retrospect?`<h3>retrospect.md</h3><div class="md-body">${renderMD(rt.retrospect)}</div>`:"");
 // Comments
 swap("ticket-comments", `<h3>Comments <button class="add-comment-btn" onclick="addComment(${jsq(id)})">+ Add</button></h3>`+
  ((cs&&cs.length)?renderThreads(cs):`<div class="muted" style="font-size:11px">No comments yet.</div>`));
 // Merge button area + merge details
 const ba=document.getElementById("ticket-merge-btn-area");
 if(ba){
  const baHtml=t.state==="human_mr_approval"?(
   (ms&&ms.can_merge===false?
    `<button class="merge-btn" disabled title="${esc(ms.reason||'')}">Merge</button>`+
    `<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ ${esc(ms.reason||'not mergeable')}</p>`:
    `<button class="merge-btn" onclick="event.stopPropagation();mergePR(${jsq(t.id)})">Merge</button>`
   )+
   (mr&&mr.reason?`<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ auto-merge not eligible: ${esc(mr.reason)}</p>`:"")
  ):"";
  const k="ticket-merge-btn-area:"+id;
  if(_detailLast[k]!==baHtml){_detailLast[k]=baHtml;ba.innerHTML=baHtml;}
 }
 swap("ticket-merge", t.state==="human_mr_approval"&&mi?renderMergeInfo(mi):"");
}
applyAgentColors();
refresh();setInterval(()=>{if(runsOpen)renderRuns();else if(proposalsOpen)renderProposals();else if(sel)refreshDetail(sel)},1000);

// Shared board rendering — used by both HTTP refresh() and the WebSocket
// ticket_list handler so the column-building logic lives in one place.
function _renderBoard(ts, wantClosed, repoId){
  const by={}; ST.forEach(s=>by[s]=[]);
  ts.forEach(t=>(by[t.state]=by[t.state]||[]).push(t));
  ["closed","done","epic_closed"].forEach(s=>{
   if(by[s]) by[s].sort((a,b)=>(b.updated_at||"").localeCompare(a.updated_at||""));
  });
  document.getElementById("meta").textContent=
    ts.length+" tickets · "+new Date().toLocaleTimeString();
  const board=document.getElementById("board");
  const visibleStates=ST.filter(s=>by[s]&&by[s].length>0&&(s!=="closed"&&s!=="epic_closed"||wantClosed));
  const visibleSet=new Set(visibleStates);
  board.querySelectorAll(".col").forEach(col=>{
   if(!visibleSet.has(col.dataset.state)) col.remove();
  });
  let prevCol=null;
  visibleStates.forEach(s=>{
   let col=board.querySelector(`.col[data-state="${s}"]`);
   if(!col){
    col=document.createElement("div");
    col.className="col";
    col.dataset.state=s;
    col.innerHTML=`<h2>${esc(s)}<span class="n"></span></h2><div class="cards"></div>`;
   }
   const expectedNext=prevCol?prevCol.nextSibling:board.firstChild;
   if(col!==expectedNext) board.insertBefore(col,expectedNext);
   col.querySelector("h2 .n").textContent=by[s].length;
   syncCards(col,by[s],repoId,s);
   prevCol=col;
  });
}

// Patch a single ticket card in the DOM from pushed WebSocket data,
// avoiding a full HTTP round-trip.  Handles state changes (moving
// the card between columns) and new tickets.
function _patchTicket(t){
  const repoId=getRepoId();
  // Ignore ticket updates from repos that don't match the selected repo filter.
  if(repoId!=="all"){
    const ticketRepoId=repoIdForBoardId(t.board_id);
    if(ticketRepoId!==repoId) return;
  }
  const newState=t.state;
  let card=document.querySelector(`.card[data-id="${t.id}"]`);
  let oldCol=card?card.closest(".col"):null;
  const oldState=oldCol?oldCol.dataset.state:null;

  if(card && oldState===newState){
    // Same column — just update the card's inner content.
    const sig=renderCardInner(t, repoId, newState);
    if(card._sig!==sig){card.innerHTML=sig;card._sig=sig;}
    const wantClass=`card s-${newState}`;
    if(card.className!==wantClass) card.className=wantClass;
    return;
  }

  // State changed or new ticket — remove from old column.
  if(card){card.remove();card._sig=null;}
  if(oldCol){
    const n=oldCol.querySelector("h2 .n");
    if(n){const c=parseInt(n.textContent)-1;n.textContent=Math.max(0,c);}
    if(!oldCol.querySelector(".card")) oldCol.remove();
  }

  // Find or create the target column in ST order.
  const board=document.getElementById("board");
  let targetCol=board.querySelector(`.col[data-state="${newState}"]`);
  if(!targetCol){
    targetCol=document.createElement("div");
    targetCol.className="col";
    targetCol.dataset.state=newState;
    targetCol.innerHTML=`<h2>${esc(newState)}<span class="n">0</span></h2><div class="cards"></div>`;
    const targetIdx=ST.indexOf(newState);
    let inserted=false;
    for(let i=targetIdx+1;i<ST.length;i++){
      const next=board.querySelector(`.col[data-state="${ST[i]}"]`);
      if(next){board.insertBefore(targetCol,next);inserted=true;break;}
    }
    if(!inserted)board.appendChild(targetCol);
  }

  // Create or reuse card.
  if(!card){
    card=document.createElement("div");
    card.dataset.id=t.id;
    card.addEventListener("click",()=>open_(t.id));
  }
  const wantClass=`card s-${newState}`;
  card.className=wantClass;
  const sig=renderCardInner(t,repoId,newState);
  card.innerHTML=sig;
  card._sig=sig;

  // Append card to target column and bump its count.
  targetCol.querySelector(".cards").appendChild(card);
  const tn=targetCol.querySelector("h2 .n");
  if(tn){tn.textContent=parseInt(tn.textContent)+1;}
}

// -- WebSocket real-time push --------------------------------------------
let wsReconnectTimer=null;
let wsActive=false;
let wsReconnectDelay=2000;
let wsKeepaliveTimer=null;
const WS_RECONNECT_MAX=30000;
const WS_RECONNECT_BASE=2000;
function connectWebSocket(){
  if(wsReconnectTimer){clearTimeout(wsReconnectTimer);wsReconnectTimer=null}
  let proto=window.location.protocol==="https:"?"wss":"ws";
  let qs="show_closed="+(showClosed?"true":"false");
  let url=proto+"://"+window.location.host+"/ws/board?"+qs;
  let sock=new WebSocket(url);
  sock.onopen=function(){
    wsActive=true;
    wsReconnectDelay=WS_RECONNECT_BASE;
    // 30s keepalive refresh as a belt-and-suspenders fallback.
    if(wsKeepaliveTimer)clearInterval(wsKeepaliveTimer);
    wsKeepaliveTimer=setInterval(refresh,30000);
  };
  sock.onmessage=function(evt){
    try{
      let msg=JSON.parse(evt.data);
      if(msg.type==="ticket_list"){
        refresh();
      } else if(msg.type==="ticket_update"){
        _patchTicket(msg.ticket);
      }
    }catch(e){/* ignore malformed messages */}
  };
  sock.onclose=function(){
    wsActive=false;
    if(wsKeepaliveTimer){clearInterval(wsKeepaliveTimer);wsKeepaliveTimer=null}
    // Exponential backoff: 2s → 4s → 8s → … → 30s max.
    wsReconnectTimer=setTimeout(connectWebSocket,wsReconnectDelay);
    wsReconnectDelay=Math.min(wsReconnectDelay*2,WS_RECONNECT_MAX);
  };
  sock.onerror=function(){
    sock.close();  // onclose handler will reconnect
  };
}
connectWebSocket();
