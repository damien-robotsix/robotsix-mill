const ST=["draft","awaiting_approval","ready","deliverable","in_review","rebasing","fixing_ci","done","closed","blocked","errored","asked","answered"];
const LBL={ready:"implementing"};   // display label only; state value stays "ready"
let showClosed=false;               // empty cols hidden; CLOSED also hidden unless toggled
let sel=null;
let runsOpen=false;
let refreshSeq=0;                    // serialize concurrent refresh() calls
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const srcClass=s=>(s==="retrospect"?"retrospect":s==="audit"?"audit":s==="scout"?"scout":s==="trace-health"?"trace-health":s==="health"?"health":s==="agent"?"agent":s==="deep-review"?"deep-review":"user");
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
 // Skip loading reviewed (closed/done) tickets by default — they dominate
 // the row count and each costs a session_cost lookup. Fetch them only
 // when the user toggles "show closed".
 // Race guard: each refresh() captures a seq token and the showClosed
 // it was started with; a later call bumps refreshSeq, so when this
 // call's await resolves it can tell it's stale and skip rendering.
 // (The auto-5s tick + the toggle's onchange refresh otherwise race,
 // and the last response to land wins — making "show closed" flicker.)
 const wantClosed=showClosed;
 const tok=++refreshSeq;
 const url=wantClosed?"/tickets":"/tickets?include_closed=false";
 const ts=await jget(url); if(!ts)return;
 if(tok!==refreshSeq)return;        // a newer refresh started — drop stale
 const by={}; ST.forEach(s=>by[s]=[]);
 ts.forEach(t=>(by[t.state]=by[t.state]||[]).push(t));
 document.getElementById("meta").textContent=
   ts.length+" tickets · "+new Date().toLocaleTimeString();
 document.getElementById("board").innerHTML=ST.filter(s=>by[s].length>0&&(s!=="closed"||wantClosed)).map(s=>`<div class="col">
  <h2>${LBL[s]||s}<span class="n">${by[s].length}</span></h2><div class="cards">`+
  by[s].map(t=>`<div class="card s-${t.state}" onclick="open_('${t.id}')">
   <button class="del-btn" title="Delete ticket" onclick="event.stopPropagation();del_('${t.id}')">✕</button>
   <div class="t">${esc(t.title)}</div><div class="id">${t.id}</div>
   ${t.kind==="inquiry"?`<span class="inquiry-badge">🔍 inquiry</span>`:""}
   <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span><span class="cost">$${(t.cost_usd||0).toFixed(4)}</span>`+
   (s==="awaiting_approval"?
    `<button class="approve-btn" onclick="event.stopPropagation();approve('${t.id}')">Approve</button>`+
    `<button class="reject-btn" title="Send back to draft with a comment" onclick="event.stopPropagation();requestChanges('${t.id}')">Request Changes</button>`:"")+
   `</div>`)
  .join("")+`</div></div>`).join("");
}
async function approve(id){
 const r=await jpost("/tickets/"+id+"/approve");
 if(!r.ok){const e=await r.text();alert("approve failed: "+e)}else refresh()
}
async function requestChanges(id){
 const body=prompt("Send this ticket back to draft. What needs to change?\n(your comment goes to the refine agent so it can re-process with this feedback.)");
 if(body===null)return;
 if(!body.trim()){alert("A comment is required when requesting changes");return}
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
async function newTicket(){
 const title=prompt("New ticket title:");
 if(title===null)return;
 if(!title.trim()){alert("Title is required");return}
 const description=prompt("Description / rough idea (optional):")||"";
 const r=await jpost("/tickets",{title:title.trim(),description:description});
 if(!r.ok){const e=await r.text();alert("create failed: "+e)}else refresh()
}
async function del_(id){
 if(!confirm("Delete ticket "+id+"? This is irreversible (row, history, workspace)."))return;
 const r=await jdel("/tickets/"+id);
 if(!r.ok&&r.status!==204){const e=await r.text();alert("delete failed: "+e)}else refresh()
}
async function runAudit(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/audit");
   if(!r.ok){throw new Error(await r.text())}
   alert("Audit started — it runs for a few minutes; new draft tickets will appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Audit failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Run Audit';
 }
}
async function runScout(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/scout");
   if(!r.ok){throw new Error(await r.text())}
   alert("Scout started — it runs for a few minutes; new draft tickets will appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Scout failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Run Scout';
 }
}
async function runTraceHealth(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/trace-health");
   if(!r.ok){throw new Error(await r.text())}
   alert("Trace-health check started — new draft tickets will appear on the board if unsessioned traces are found.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Trace-health check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Trace Health';
 }
}
async function runHealth(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/health-check");
   if(!r.ok){throw new Error(await r.text())}
   alert("Health check started — new draft tickets will appear on the board if issues are found.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Health check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Run Health Check';
 }
}
async function runAgentCheck(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/agent-check");
   if(!r.ok){throw new Error(await r.text())}
   alert("Agent-check started — it inspects every agent's prompt/tools for coherence gaps. New draft tickets appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Agent-check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Run Agent Check';
 }
}
async function open_(id){
 sel=id;
 const [t,h,d,cs,rt]=await Promise.all([jget("/tickets/"+id),
   jget("/tickets/"+id+"/history"),jget("/tickets/"+id+"/description"),
   jget("/tickets/"+id+"/comments"),jget("/tickets/"+id+"/retrospect")]);
 if(!t)return;
 document.getElementById("d").innerHTML=
  `<h3>${esc(t.title)}</h3>
   <div class="muted">${t.id}</div>
   <p>state <b class="s-${t.state}" style="border-left:3px solid var(--c);
      padding-left:6px">${t.state}</b>${t.kind==="inquiry"?` <span class="inquiry-badge">🔍 inquiry</span>`:""} · branch ${esc(t.branch)||"—"}<br>
   source <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span>`+
   (t.origin_session_url?` · origin <a href="${esc(t.origin_session_url)}" target="_blank" rel="noopener" class="origin-link">${esc(t.origin_session)}</a>`:
    t.origin_session?` · origin <span class="muted">${esc(t.origin_session)}</span>`:"")+
   (t.pr_url?` · <a href="${esc(t.pr_url)}" target="_blank" rel="noopener" class="pr-link">🔗 PR</a>`:"")+
   `<br>
   · cost <b>$${(t.cost_usd||0).toFixed(4)}</b><br>
   created ${t.created_at} · updated ${t.updated_at}</p>`+
   (t.depends_on?`<p><b>depends on:</b> ${esc(t.depends_on)}</p>`:"")+
   (t.unmet_deps&&t.unmet_deps.length?`<p style="color:#f59e0b;font-weight:bold">⏳ waiting on ${t.unmet_deps.map(esc).join(", ")}</p>`:"")+
   `<h3>History</h3>`+
   (h||[]).map(e=>`<div class="ev"><b>${e.state}</b> ${e.at}
     ${e.note?"<br>"+esc(e.note):""}</div>`).join("")+
   `<h3>Comments <button class="add-comment-btn" onclick="addComment('${t.id}')">+ Add</button></h3>`+
   ((cs&&cs.length)?cs.map(c=>`<div class="ev"><b class="muted">${c.created_at}</b><br>${esc(c.body)}</div>`).join("")
                   :`<div class="muted" style="font-size:11px">No comments yet.</div>`)+
   ((rt&&rt.retrospect)?`<h3>retrospect.md</h3><pre>${esc(rt.retrospect)}</pre>`:"")+
   `<h3>description.md</h3><pre>${esc((d&&d.description)||"")}</pre>`;
 document.getElementById("drawer").classList.add("open");
}
function close_(){sel=null;runsOpen=false;
 if(deepReviewPollTimer){clearInterval(deepReviewPollTimer);deepReviewPollTimer=null}
 deepReviewOpen=false;deepReviewTraceId=null;deepReviewFindings=[];
 document.getElementById("drawer").classList.remove("open")}
async function renderRuns(){
 const rs=await jget("/runs");
 document.getElementById("d").innerHTML=rs&&rs.length?
  rs.map(r=>{
   const s=Date.parse(r.started_at);
   const f=r.finished_at?Date.parse(r.finished_at):null;
   const e=f?f:(Date.now());
   const ms=e-s;
   const sec=Math.floor(ms/1000);
   const min=Math.floor(sec/60);
   const sss=sec%60;
   const elapsed=f?(min+'m '+sss+'s'):'running…';
   const kc=r.kind==='audit'?'#059669':r.kind==='scout'?'#7c3aed':r.kind==='trace-health'?'#0ea5e9':r.kind==='health'?'#0d9488':r.kind==='agent_check'?'#db2777':r.kind==='deep-review'?'#1a2a3b':'#6b7280';
   const sc=r.status==='running'?'#eab308':r.status==='ok'?'#22c55e':'#ef4444';
   const st=r.status==='running'?'running…':r.status;
   return `<div style="padding:8px 0;border-bottom:1px solid #262b36">
    <span style="display:inline-block;padding:1px 6px;border-radius:4px;
     background:${kc};color:#fff;font-size:10px;margin-right:6px">${r.kind}</span>
    <span style="display:inline-block;padding:1px 6px;border-radius:4px;
     background:${sc};color:#fff;font-size:10px">${st}</span>
    <span style="color:#7d828c;font-size:10px;margin-left:6px">${r.started_at}</span>
    <span style="color:#7d828c;font-size:10px;margin-left:3px">${elapsed}</span>
    <div style="font-size:11px;color:#aab0bd;margin-top:3px">${esc(r.summary||'')}</div>
    ${r.error?`<div style="font-size:11px;color:#f87171;margin-top:2px">${esc(r.error)}</div>`:''}
   </div>`
  }).join("")
  :`<div class="muted">No runs yet. Click Run Audit, Run Scout, or Trace Health to start one.</div>`;
}
async function toggleRuns(){
 if(runsOpen){close_();return}
 if(sel){close_()}
 await renderRuns();
 runsOpen=true;
 document.getElementById("drawer").classList.add("open");
}
// -- deep review --------------------------------------------------------
let deepReviewOpen=false;
let deepReviewTraceId=null;
let deepReviewPollTimer=null;
let deepReviewPollCount=0;
let deepReviewFindings=[];  // [{category, text}] for ticket creation
async function openDeepReview(){
 if(deepReviewOpen){close_();return}
 if(sel){close_()}
 deepReviewOpen=true; deepReviewTraceId=null; deepReviewPollCount=0;
 deepReviewFindings=[];
 document.getElementById("drawer").classList.add("open");
 // reset filter inputs to defaults
 const lim=document.getElementById("dr-limit");
 const minc=document.getElementById("dr-min-cost");
 const maxc=document.getElementById("dr-max-cost");
 if(lim)lim.value='10';
 if(minc)minc.value='';
 if(maxc)maxc.value='';
 await renderTraceList();
}
async function renderTraceList(){
 const lim=document.getElementById("dr-limit");
 const minc=document.getElementById("dr-min-cost");
 const maxc=document.getElementById("dr-max-cost");
 const limit=lim?parseInt(lim.value)||10:10;
 const minCost=minc&&minc.value!==''?minc.value:null;
 const maxCost=maxc&&maxc.value!==''?maxc.value:null;
 let url='/traces/recent?limit='+limit;
 if(minCost!==null)url+='&min_cost='+encodeURIComponent(minCost);
 if(maxCost!==null)url+='&max_cost='+encodeURIComponent(maxCost);
 document.getElementById("d").innerHTML='<h3>Deep Review</h3>'+
  '<div class="dr-filters">'+
   '<label>Show <input type="number" id="dr-limit" value="'+limit+'" min="1" max="50" style="width:4em"></label>'+
   '<label>Min cost $ <input type="number" id="dr-min-cost" value="'+(minCost!==null?minCost:'')+'" step="0.0001" placeholder="0.0000" style="width:7em"></label>'+
   '<label>Max cost $ <input type="number" id="dr-max-cost" value="'+(maxCost!==null?maxCost:'')+'" step="0.0001" placeholder="—" style="width:7em"></label>'+
  '</div><div id="trace-list">loading traces…</div>';
 // bind filter-change handlers
 ['dr-limit','dr-min-cost','dr-max-cost'].forEach(function(id){
  const el=document.getElementById(id);if(el){el.oninput=renderTraceList;el.onchange=renderTraceList}
 });
 const traces=await jget(url);
 const costFilterActive=minCost!==null||maxCost!==null;
 if(!traces||!traces.length){
  document.getElementById("trace-list").innerHTML=
   '<div class="muted" style="padding:12px 0">'+
   (costFilterActive?'No traces match your cost filter.':'No recent traces available — check Langfuse connectivity.')+
   '</div>';
  return;
 }
 const escT=s=>{const d=document.createElement("div");d.textContent=s;return d.innerHTML};
 let html='';
 traces.forEach(t=>{
  const cost=t.totalCost!=null?'$'+Number(t.totalCost).toFixed(4):'—';
  const ts=t.timestamp?new Date(t.timestamp).toLocaleString():'—';
  const sid=t.sessionId||'—';
  html+=`<div class="trace-row${deepReviewTraceId===t.id?' sel':''}" onclick="selectTrace('${escT(t.id)}')" id="tr-${escT(t.id)}">
   <div class="trace-name">${escT(t.name||'(unnamed)')}</div>
   <div class="trace-meta">session: ${escT(sid)} · cost: ${cost} · ${ts}</div>
  </div>`;
 });
 html+=`<div style="margin-top:12px"><button id="start-dr-btn" class="dr-btn"${deepReviewTraceId?'':' disabled'}`+
   ` onclick="startDeepReview()" style="font-size:11px;padding:5px 14px;background:#1a2a3b;color:#60c0fa;border:1px solid #2a3a4b;border-radius:4px;cursor:pointer">`+
   `Start Deep Review</button></div>`;
 document.getElementById("trace-list").innerHTML=html;
}
function selectTrace(tid){
 deepReviewTraceId=tid;
 // highlight
 document.querySelectorAll(".trace-row").forEach(r=>r.classList.remove("sel"));
 const row=document.getElementById("tr-"+tid);
 if(row)row.classList.add("sel");
 // enable button
 const btn=document.getElementById("start-dr-btn");
 if(btn){btn.disabled=false;btn.style.cursor="pointer"}
}
async function startDeepReview(){
 if(!deepReviewTraceId)return;
 const btn=document.getElementById("start-dr-btn");
 btn.disabled=true; btn.textContent='Reviewing…'; btn.style.cursor='default';
 const r=await jpost("/traces/"+deepReviewTraceId+"/deep-review");
 if(!r||r.status==="unavailable"){
  document.getElementById("trace-list").innerHTML=
   '<div class="muted" style="color:#f87171;padding:12px 0">Langfuse is not configured — cannot start deep review.</div>';
  return;
 }
 if(!r||r.status!=="started"){
  btn.disabled=false; btn.textContent='Start Deep Review'; btn.style.cursor='pointer';
  alert("Failed to start deep review");
  return;
 }
 deepReviewPollCount=0;
 deepReviewPollTimer=setInterval(pollDeepReviewResult,2000);
 pollDeepReviewResult(); // immediate first poll
}
async function pollDeepReviewResult(){
 deepReviewPollCount++;
 if(deepReviewPollCount>15){ // ~30s
  clearInterval(deepReviewPollTimer); deepReviewPollTimer=null;
  document.getElementById("d").innerHTML=
   '<h3>Deep Review</h3><div class="muted" style="color:#f87171;padding:12px 0">Review timed out — the trace may be too large or the agent is busy.</div>';
  return;
 }
 const res=await jget("/deep-review/"+deepReviewTraceId);
 if(!res){return}
 if(res.status==="running"){return} // still going
 clearInterval(deepReviewPollTimer); deepReviewPollTimer=null;
 // result ready
 renderDeepReviewResult(res);
}
function renderDeepReviewResult(res){
 const escT=s=>{const d=document.createElement("div");d.textContent=s;return d.innerHTML};
 let html=`<h3>Deep Review: ${escT(deepReviewTraceId)}</h3>`;
 if(res.status==="error"){
  html+=`<div class="muted" style="color:#f87171;padding:12px 0">${escT(res.error||'Unknown error')}</div>`;
  document.getElementById("d").innerHTML=html;
  return;
 }
 const toolErrors=res.tool_errors||[];
 const limitations=res.agent_limitations||[];
 const optimizations=res.optimizations||[];
 deepReviewFindings=[];
 if(!toolErrors.length&&!limitations.length&&!optimizations.length){
  html+=`<div class="muted" style="padding:12px 0">(no issues found in this trace)</div>`;
  document.getElementById("d").innerHTML=html;
  return;
 }
 function renderSection(title,items,cls){
  if(!items.length)return'';
  let h=`<div class="dr-section ${cls}"><h4>${title} (${items.length})</h4>`;
  items.forEach((item,i)=>{
   const idx=deepReviewFindings.length;
   deepReviewFindings.push({category:title,text:item});
   h+=`<div class="dr-finding"><span>${escT(item)}</span>`+
    `<button class="dr-ticket-btn" onclick="createTicketFromFinding(${idx},event)"`+
    ` style="font-size:10px;padding:2px 8px;background:#2563eb;color:#fff;border:none;border-radius:3px;cursor:pointer;margin-left:8px;flex-shrink:0">+ Ticket</button></div>`;
  });
  h+=`</div>`;
  return h;
 }
 html+=renderSection("Tool Errors",toolErrors,"dr-tool-errors");
 html+=renderSection("Agent Limitations",limitations,"dr-limitations");
 html+=renderSection("Optimizations",optimizations,"dr-optimizations");
 html+=`<div style="margin-top:16px"><button onclick="openDeepReview()"`+
   ` style="font-size:11px;padding:3px 10px;background:#2a2f3a;color:#aab0bd;border:1px solid #3a3f4a;border-radius:4px;cursor:pointer">← Back to traces</button></div>`;
 document.getElementById("d").innerHTML=html;
}
function createTicketFromFinding(idx,event){
 if(event)event.stopPropagation();
 const finding=deepReviewFindings[idx];
 if(!finding)return;
 const itemText=finding.text;
 const title=prompt("Ticket title:","Deep review: "+itemText.substring(0,80));
 if(title===null)return;
 if(!title.trim()){alert("Title is required");return}
 const desc=prompt("Description:",
  "Finding from deep review of trace "+deepReviewTraceId+":\n\n["+finding.category+"] "+itemText);
 if(desc===null)return;
 (async()=>{
  const r=await jpost("/tickets",{title:title.trim(),description:desc,source:"deep-review"});
  if(!r.ok){const e=await r.text();alert("create ticket failed: "+e)}else refresh()
 })();
}
// -- end deep review ----------------------------------------------------
refresh();setInterval(()=>{refresh();if(runsOpen)renderRuns();else if(sel)open_(sel);if(deepReviewOpen&&deepReviewPollTimer){}/* poll active */},5000);
