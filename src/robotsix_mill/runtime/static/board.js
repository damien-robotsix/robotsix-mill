let showClosed=false;               // empty cols hidden; CLOSED and EPIC_CLOSED also hidden unless toggled
let sel=null;
let runsOpen=false;
let costDashboardOpen=false;
let costLookbackHours=24;
let refreshSeq=0;                    // serialize concurrent refresh() calls
let activeMap={};
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
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const renderMD = s => { if (!s) return ""; return marked.parse(s); };
const srcClass=s=>(s==="retrospect"?"retrospect":s==="audit"?"audit":s==="trace-health"?"trace-health":s==="health"?"health":s==="test_gap"?"test-gap":s==="agent"?"agent":s==="deep-review"?"deep-review":"user");
function fmtRelative(iso){
 const d=(new Date(iso)).getTime()-Date.now();
 if(d<=0)return"now";
 const s=Math.round(d/1000);
 if(s<60)return"in "+s+"s";
 const m=Math.round(s/60);
 if(m<60)return"in "+m+"m";
 return new Date(iso).toLocaleTimeString();
}
// -- gate pills (pipeline behaviour flags surfaced in the header) -----
async function fetchGates() {
  const g = await jget("/gates");
  if (!g) return;
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
 // (The auto-5s tick + the toggle's onchange refresh otherwise race,
 // and the last response to land wins — making "show closed" flicker.)
 const wantClosed=showClosed;
 const tok=++refreshSeq;
 const url=wantClosed?"/tickets":"/tickets?include_closed=false";
 fetchGates();
 const [ts, activeList]=await Promise.all([jget(url), jget("/active")]);
 if(!ts)return;
 const active={};
 if(activeList) activeList.forEach(a=>{ active[a.ticket_id]=a; });
 activeMap=active;
 if(tok!==refreshSeq)return;        // a newer refresh started — drop stale
 const by={}; ST.forEach(s=>by[s]=[]);
 ts.forEach(t=>(by[t.state]=by[t.state]||[]).push(t));
 // Terminal-ish columns get reverse-chronological ordering so the
 // most recently closed/done ticket is on top — useful when scanning
 // "what just finished" without scrolling past stale items. CLOSED is
 // terminal, so its updated_at IS its closed_at; DONE is the
 // retrospect-in-flight window. Active columns keep creation-order
 // (oldest queued at top — natural FIFO of work).
 ["closed","done","epic_closed"].forEach(s=>{
  if(by[s]) by[s].sort((a,b)=>(b.updated_at||"").localeCompare(a.updated_at||""));
 });
 document.getElementById("meta").textContent=
   ts.length+" tickets · "+new Date().toLocaleTimeString();
 document.getElementById("board").innerHTML=ST.filter(s=>by[s].length>0&&(s!=="closed"&&s!=="epic_closed"||wantClosed)).map(s=>`<div class="col">
  <h2>${s}<span class="n">${by[s].length}</span></h2><div class="cards">`+
  by[s].map(t=>`<div class="card s-${t.state}" onclick="open_('${t.id}')">
   <button class="del-btn" title="Delete ticket" onclick="event.stopPropagation();del_('${t.id}')">✕</button>
   <div class="t">${esc(t.title)}</div><div class="id">${t.id}</div>
   ${t.kind==="inquiry"?`<span class="inquiry-badge">🔍 inquiry</span>`:""}
   ${t.kind==="epic"?`<span class="epic-badge">📋 epic</span>`:""}
   ${t.parent_id?`<span class="epic-ref">📋 ${esc(t.parent_title||t.parent_id.slice(0,8)+"…")}</span>`:""}
   <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span><span class="cost">$${(t.cost_usd||0).toFixed(4)}</span>${t.cumulative_cost&&t.cumulative_cost>t.cost_usd?`<span class="cost-cumulative">/$${t.cumulative_cost.toFixed(4)}</span>`:""}${t.retry_attempt>0?`<span class="retry-chip" title="${esc(t.last_transient_error||'')}">retry ${t.retry_attempt}${t.next_retry_at?` · next ${fmtRelative(t.next_retry_at)}`:''}</span>`:''}`+
   `${activeMap[t.id] ? `<span class="live-badge"><span class="live-spinner"></span> ${s==="rebasing" ? "rebasing…" : (ACTIVE_LABEL[activeMap[t.id].stage] || activeMap[t.id].stage + "…")}</span>` : ""}`+
   (s==="human_mr_approval"?
    `<button class="merge-btn" onclick="event.stopPropagation();mergePR('${t.id}')">Merge</button>`:"")+
   (s==="human_issue_approval"?
    `<button class="approve-btn" onclick="event.stopPropagation();approve('${t.id}')">Approve</button>`+
    `<button class="reject-btn" title="Send back to draft with a comment" onclick="event.stopPropagation();requestChanges('${t.id}')">Request Changes</button>`:"")+
   (!['draft','human_issue_approval','closed','answered','epic_closed','epic_open'].includes(s)?
    `<button class="redraft-btn" title="Send back to draft" onclick="event.stopPropagation();redraft('${t.id}')">Redraft</button>`:"")+
   `</div>`)
  .join("")+`</div></div>`).join("");
}
async function approve(id){
 const r=await jpost("/tickets/"+id+"/approve");
 if(!r.ok){const e=await r.text();alert("approve failed: "+e)}else refresh()
}
async function mergePR(id){
 const r=await jpost("/tickets/"+id+"/merge-now");
 if(!r.ok){const e=await r.text();alert("merge failed: "+e)}else refresh()
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
 const replies=cs.filter(c=>c.parent_id!==null);
 const replyMap={};
 replies.forEach(r=>{(replyMap[r.parent_id]||=[]).push(r);});
 return threads.map(t=>{
  const isClosed=t.closed_at!==null;
  const children=replyMap[t.id]||[];
  const replyHtml=children.map(r=>
   `<div class="ev reply-ev"><b class="muted">${r.created_at}</b> · <b>${esc(r.author)}</b><br>${renderMD(r.body)}</div>`
  ).join("");
  return `<div class="thread${isClosed?' thread-closed':''}">
   <div class="ev"><b class="muted">${t.created_at}</b> · <b>${esc(t.author)}</b>${isClosed?' <span class="closed-badge">🔒 Closed</span>':''}<br>${renderMD(t.body)}</div>
   ${replyHtml}
   <div class="thread-actions">
    <button class="add-comment-btn" onclick="replyToThread('${t.id}','${t.ticket_id}')">↩ Reply</button>
    ${isClosed
     ?`<button class="add-comment-btn" onclick="reopenThread('${t.id}')">🔓 Reopen</button>`
     :`<button class="add-comment-btn" onclick="closeThread('${t.id}')">🔒 Close</button>`}
   </div>
  </div>`;
 }).join("");
}
async function replyToThread(threadId,ticketId){
 const body=prompt("Reply to this thread:");
 if(body===null)return;
 if(!body.trim())return;
 const r=await jpost("/tickets/"+ticketId+"/comments",{body:body.trim(),parent_id:threadId});
 if(!r.ok){const e=await r.text();alert("reply failed: "+e)}else if(sel===ticketId)open_(ticketId)
}
async function closeThread(commentId){
 const tid=sel;
 const r=await jpost("/comments/"+commentId+"/close");
 if(!r.ok){const e=await r.text();alert("close thread failed: "+e)}else if(tid)open_(tid)
}
async function reopenThread(commentId){
 const tid=sel;
 const r=await jpost("/comments/"+commentId+"/reopen");
 if(!r.ok){const e=await r.text();alert("reopen thread failed: "+e)}else if(tid)open_(tid)
}
async function newTicket(){
 // Build modal DOM
 const backdrop=document.createElement("div");
 backdrop.className="modal-backdrop";
 const modal=document.createElement("div");
 modal.className="modal";
 modal.innerHTML=
  `<h2>New Ticket</h2>
   <label class="modal-label">Title <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="What needs doing?" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Description</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints… (optional)"></textarea>
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
  const r=await jpost("/tickets",{title:title,description:descEl.value});
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
 modal.innerHTML=
  `<h2>New Inquiry</h2>
   <label class="modal-label">Question / investigation prompt <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="What do you want to know?" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Context / background</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Rough idea, context, constraints… (optional)"></textarea>
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
  const r=await jpost("/tickets",{title:title,description:descEl.value,kind:"inquiry"});
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
 modal.innerHTML=
  `<h2>New Epic</h2>
   <label class="modal-label">Title <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="Epic title / goal" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Description</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8" placeholder="Scope, outcome, notes… (optional)"></textarea>
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
  const r=await jpost("/epics",{title:title,description:descEl.value});
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
async function resumeRetry(id){
 const r=await jpost("/tickets/"+id+"/resume-blocked");
 if(!r.ok){const e=await r.text();alert("resume failed: "+e);return}
 refresh();if(sel===id)open_(id);
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
   btn.disabled=false; btn.textContent='Audit';
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
   btn.disabled=false; btn.textContent='Health Check';
 }
}
async function runTestGap(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/test-gap");
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
   const r=await jpost("/agent-check");
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
   const r=await jpost("/survey");
   if(!r.ok){throw new Error(await r.text())}
   alert("Survey started — it discovers similar OSS projects and proposes improvements. New draft tickets appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Survey failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Survey';
 }
}
async function runBcCheck(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await jpost("/bc-check");
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
   const r=await jpost("/completeness-check");
   if(!r.ok){throw new Error(await r.text())}
   alert("Completeness-check started — it scans for half-wired features and files draft tickets for discovered gaps. New drafts appear on the board when it finishes.");
   setTimeout(refresh,4000);
 } catch(e) {
   alert("Completeness-check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Completeness';
 }
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
async function open_(id){
 sel=id;
 const [t,h,d,cs,rt,ch,mi,mr]=await Promise.all([jget("/tickets/"+id),
   jget("/tickets/"+id+"/history"),jget("/tickets/"+id+"/description"),
   jget("/tickets/"+id+"/comments"),jget("/tickets/"+id+"/retrospect"),
   jget("/tickets/"+id+"/children"),jget("/tickets/"+id+"/merge-info"),
   jget("/tickets/"+id+"/merge-reason")]);
 if(!t)return;
 document.getElementById("d").innerHTML=
  `<h3>${esc(t.title)}</h3>
   <div class="muted">${t.id}</div>
   <p>state <b class="s-${t.state}" style="border-left:3px solid var(--c);
      padding-left:6px">${t.state}</b>${t.kind==="inquiry"?` <span class="inquiry-badge">🔍 inquiry</span>`:""}${t.kind==="epic"?` <span class="epic-badge">📋 epic</span>`:""} · branch ${esc(t.branch)||"—"}<br>
   source <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span>`+
   (t.origin_session_url?` · origin <a href="${esc(t.origin_session_url)}" target="_blank" rel="noopener" class="origin-link">${esc(t.origin_session)}</a>`:
    t.origin_session?` · origin <span class="muted">${esc(t.origin_session)}</span>`:"")+
   (t.pr_url?` · <a href="${esc(t.pr_url)}" target="_blank" rel="noopener" class="pr-link">🔗 PR</a>`:"")+
   (t.state==="human_mr_approval"?
    `<button class="merge-btn" onclick="event.stopPropagation();mergePR('${t.id}')">Merge</button>`:"")+
   (mr&&mr.reason?
    `<p style="color:#f59e0b;font-size:11px;margin-top:4px">⚠ auto-merge not eligible: ${esc(mr.reason)}</p>`:"")+
   (t.state==="human_mr_approval"&&mi?renderMergeInfo(mi):"")+
   `<br>
   · cost <b>$${(t.cost_usd||0).toFixed(4)}</b>`+
   (t.cumulative_cost&&t.cumulative_cost>t.cost_usd?`<br>· cumulative (incl. children) <b>$${t.cumulative_cost.toFixed(4)}</b>`:"")+
   (t.retry_attempt>0?`<br><button class="retry-now-btn" onclick="event.stopPropagation();resumeRetry('${t.id}')">Retry now</button>`:"")+
   `<br>
   created ${t.created_at} · updated ${t.updated_at}</p>`+
   (t.depends_on?`<p><b>depends on:</b> ${esc(t.depends_on)}</p>`:"")+
   (t.unmet_deps&&t.unmet_deps.length?`<p style="color:#f59e0b;font-weight:bold">⏳ waiting on ${t.unmet_deps.map(esc).join(", ")}</p>`:"")+
   (t.parent_id?`<p><b>Part of epic:</b> <span class="epic-ref">📋 ${esc(t.parent_title||t.parent_id)}</span></p>`:"")+
   (ch&&ch.length?`<h3>Children (${ch.length})</h3><div class="children-list">${ch.map(c=>`<div class="child-ticket" onclick="open_('${c.id}')"><span class="child-state s-${c.state}">${c.state}</span> <span class="child-title">${esc(c.title)}</span> <span class="child-id muted">${c.id}</span></div>`).join("")}</div>`:"")+
   (t.kind==="epic"?`<p><button class="add-comment-btn" style="background:#9333ea;color:#fff" onclick="generateChildren('${t.id}')">Generate Tickets</button> <button class="add-comment-btn" style="background:#2563eb;color:#fff" onclick="newChildTicket('${t.id}')">Add Ticket</button></p>`:"")+
   `<h3>History</h3>`+
   (h||[]).map(e=>`<div class="ev"><b>${e.state}</b> ${e.at}
     ${e.note?"<br>"+renderMD(e.note):""}</div>`).join("")+
   `<h3>Comments <button class="add-comment-btn" onclick="addComment('${t.id}')">+ Add</button></h3>`+
   ((cs&&cs.length)?renderThreads(cs)
                   :`<div class="muted" style="font-size:11px">No comments yet.</div>`)+
   ((rt&&rt.retrospect)?`<h3>retrospect.md</h3><div class="md-body">${renderMD(rt.retrospect)}</div>`:"")+
   `<h3>description.md</h3><div class="md-body">${renderMD((d&&d.description)||"")}</div>`;
 document.getElementById("drawer").classList.add("open");
}
function close_(){sel=null;runsOpen=false;costDashboardOpen=false;
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
   const kc=r.kind==='audit'?'#059669':r.kind==='trace-health'?'#0ea5e9':r.kind==='health'?'#0d9488':r.kind==='agent_check'?'#db2777':r.kind==='deep-review'?'#1a2a3b':r.kind==='survey'?'#f59e0b':'#6b7280';
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
  :`<div class="muted">No runs yet. Click Run Audit or Trace Health to start one.</div>`;
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
 const selOpt=lookback=>lookback==costLookbackHours?' selected':'';
 document.getElementById("d").innerHTML='<h3>💰 Cost Dashboard</h3>'+
  '<div class="cost-lookback">'+
   '<label>Last <select id="cost-lookback" onchange="costLookbackHours=parseInt(this.value);renderCostDashboard()">'+
    '<option value="1"'+selOpt(1)+'>1 hour</option>'+
    '<option value="6"'+selOpt(6)+'>6 hours</option>'+
    '<option value="24"'+selOpt(24)+'>24 hours</option>'+
    '<option value="72"'+selOpt(72)+'>3 days</option>'+
    '<option value="168"'+selOpt(168)+'>7 days</option>'+
   '</select></label>'+
  '</div>'+
  '<canvas id="cost-sparkline" style="display:none"></canvas>'+
  '<div id="cost-chart">loading…</div>'+
  '<div id="cost-highlights"></div>';

 const trendUrl="/costs/trend?lookback_hours="+costLookbackHours;
 const baseUrl="/costs/by-agent?lookback_hours="+costLookbackHours;
 const ticketUrl="/costs/most-expensive-ticket?lookback_hours="+costLookbackHours;
 const traceUrl="/costs/most-expensive-trace?lookback_hours="+costLookbackHours;
 const [trendData, data, topTicket, topTrace]=await Promise.all([
  jget(trendUrl), jget(baseUrl), jget(ticketUrl), jget(traceUrl)
 ]);

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
  ctx.fillText("No trend data available for this period.",rect.width/2,rect.height/2);
 }

 // -- per-agent bar chart -----------------------------------------------
 if(!data||!data.length){
  document.getElementById("cost-chart").innerHTML='<div class="muted">No cost data available for this period.</div>';
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
 highlightsHtml+='<div class="cost-bar-row">'+
  '<div class="cost-bar-label">'+
   '<span class="cost-bar-name">Most Expensive Ticket</span>'+
  '</div>';
 if(topTicket){
  highlightsHtml+=
   '<div class="cost-bar-track" style="display:flex;align-items:center;gap:8px">'+
    '<a href="#" onclick="open_(\''+esc(topTicket.ticket_id)+'\');return false" style="color:#8bb4f8">'+esc(topTicket.title)+'</a>'+
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
 highlightsHtml+='<div class="cost-bar-row">'+
  '<div class="cost-bar-label">'+
   '<span class="cost-bar-name">Most Expensive Run</span>'+
  '</div>';
 if(topTrace){
  highlightsHtml+=
   '<div class="cost-bar-track" style="display:flex;align-items:center;gap:8px">'+
    '<span>'+esc(topTrace.name)+'</span>'+
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
// -- deep review --------------------------------------------------------
let deepReviewOpen=false;
let deepReviewTraceId=null;
let deepReviewPollTimer=null;
let deepReviewPollCount=0;
let deepReviewPollStart=0;
let deepReviewFindings=[];  // [{category, text}] for ticket creation
let lastReviewsCache=[];
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
 // lazy-load last-reviews count
 jget("/deep-review").then(function(arr){
  if(Array.isArray(arr)){lastReviewsCache=arr;var el=document.getElementById("lr-count");if(el)el.textContent=arr.length}
 }).catch(function(){var el=document.getElementById("lr-count");if(el)el.textContent='?'});
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
  '<button onclick="renderLastReviewsList()" class="dr-btn"'+
  ' style="font-size:11px;padding:3px 10px;background:#1a2a3b;color:#60c0fa;'+
  'border:1px solid #2a3a4b;border-radius:4px;cursor:pointer;margin-bottom:10px">'+
  'Last reviews (<span id="lr-count">…</span>)</button>'+
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
async function renderLastReviewsList(){
 const arr=await jget("/deep-review");
 if(Array.isArray(arr))lastReviewsCache=arr; else lastReviewsCache=[];
 const escT=s=>{const d=document.createElement("div");d.textContent=s;return d.innerHTML};
 let html='<h3>Last Deep Reviews</h3><div id="last-reviews-list">';
 if(!lastReviewsCache.length){
  html+='<div class="muted" style="padding:12px 0">No completed deep reviews yet. Run one from the trace picker.</div>';
 } else {
  lastReviewsCache.forEach(function(entry){
   const finished=entry.finished_at?new Date(entry.finished_at).toLocaleString():'—';
   const n_te=(entry.findings||[]).filter(f=>f.category==="tool_error").length;
   const n_al=(entry.findings||[]).filter(f=>f.category==="agent_limitation").length;
   const n_opt=(entry.findings||[]).filter(f=>f.category==="optimization").length;
   const status=entry.status||'ok';
   const statusHtml=status==='error'?
    '<span style="color:#f87171">error</span>':
    '<span class="src-badge src-deep-review">'+escT(status)+'</span>';
   html+='<div class="trace-row" onclick="viewStoredReview(\''+escT(entry.trace_id)+'\')">'+
    '<div class="trace-name">'+escT(entry.source_trace_name||entry.trace_id)+'</div>'+
    '<div class="trace-meta">'+
    finished+' · '+n_te+' T / '+n_al+' L / '+n_opt+' O · '+
    statusHtml+
    '</div></div>';
  });
 }
 html+='</div>'+
  '<button onclick="openDeepReview()"'+
  ' style="font-size:11px;padding:3px 10px;background:#2a2f3a;color:#aab0bd;'+
  'border:1px solid #3a3f4a;border-radius:4px;cursor:pointer;margin-top:12px">'+
  '← Back to trace picker</button>';
 document.getElementById("d").innerHTML=html;
}
function viewStoredReview(traceId){
 const entry=lastReviewsCache.find(function(e){return e.trace_id===traceId});
 if(!entry)return;
 deepReviewTraceId=entry.trace_id;
 renderDeepReviewResult(entry, renderLastReviewsList);
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
 // jpost returns a fetch-shaped wrapper {ok, status, text(), json()}.
 // ``r.status`` is the HTTP status code (number), NOT the response
 // body's "status" string. We have to parse the JSON body to read
 // the route's signal of "started" vs "unavailable" vs an error.
 const r=await jpost("/traces/"+deepReviewTraceId+"/deep-review");
 if(!r||!r.ok){
  btn.disabled=false; btn.textContent='Start Deep Review'; btn.style.cursor='pointer';
  alert("Failed to start deep review (HTTP "+(r?r.status:"unreachable")+")");
  return;
 }
 const body=await r.json();
 if(body&&body.status==="unavailable"){
  document.getElementById("trace-list").innerHTML=
   '<div class="muted" style="color:#f87171;padding:12px 0">Langfuse is not configured — cannot start deep review.</div>';
  return;
 }
 if(!body||body.status!=="started"){
  btn.disabled=false; btn.textContent='Start Deep Review'; btn.style.cursor='pointer';
  alert("Failed to start deep review (unexpected body: "+JSON.stringify(body)+")");
  return;
 }
 deepReviewPollCount=0;
 // Backoff scheduling rather than fixed 2s interval. A real deep
 // review on a long implement-run trace can take 60-120s; the old
 // 30s hard cutoff was both wrong and operator-hostile (gave up
 // before the answer arrived, then the user had no way to recover
 // because the result is in-process state only).
 //   first 60s: poll every 2s (catches quick results)
 //   60-300s:  poll every 5s
 //   >300s:    show "still running" but stop polling (5 min ceiling)
 deepReviewPollStart=Date.now();
 schedulePollDeepReview(2000);
 pollDeepReviewResult();  // immediate first poll
}
function schedulePollDeepReview(ms){
 if(deepReviewPollTimer){clearTimeout(deepReviewPollTimer)}
 deepReviewPollTimer=setTimeout(pollDeepReviewResult, ms);
}
async function pollDeepReviewResult(){
 deepReviewPollCount++;
 const elapsed=Math.round((Date.now()-deepReviewPollStart)/1000);
 const HARD_CAP_S=300; // 5 min
 const res=await jget("/deep-review/"+deepReviewTraceId);
 if(res && res.status && res.status!=="running"){
  // Result ready (ok OR error)
  if(deepReviewPollTimer){clearTimeout(deepReviewPollTimer);deepReviewPollTimer=null}
  renderDeepReviewResult(res);
  return;
 }
 // Still running — update the UI with elapsed time and keep polling
 // unless we've hit the hard cap.
 const stillRunningHtml=
  '<h3>Deep Review</h3>'+
  '<div class="muted" style="padding:12px 0">'+
  '⏳ Still analysing… <span style="color:#aab0bd">('+elapsed+'s elapsed)</span>'+
  '<div style="font-size:11px;margin-top:6px">Deep reviews can take 1-3 minutes on large traces; this window will update when the result arrives.</div>'+
  '</div>';
 document.getElementById("d").innerHTML=stillRunningHtml;
 if(elapsed >= HARD_CAP_S){
  // Stop polling but DON'T claim failure — the backend may still
  // finish. The result will land in app.state.deep_review_results
  // and can be re-fetched later (e.g. via a "Last Deep Review"
  // panel — separate ticket).
  if(deepReviewPollTimer){clearTimeout(deepReviewPollTimer);deepReviewPollTimer=null}
  document.getElementById("d").innerHTML=
   '<h3>Deep Review</h3>'+
   '<div class="muted" style="color:#eab308;padding:12px 0">'+
   '⏱ Still running after '+Math.floor(elapsed/60)+'m '+(elapsed%60)+'s — stopped polling but the analysis may still complete in the background.'+
   '<div style="font-size:11px;margin-top:6px">Click below to keep waiting.</div>'+
   '<button onclick="resumeDeepReviewPolling()" style="font-size:11px;margin-top:8px;padding:3px 10px;background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer">Keep polling</button>'+
   '</div>';
  return;
 }
 // Backoff: first 60s every 2s, after that every 5s.
 schedulePollDeepReview(elapsed<60 ? 2000 : 5000);
}
function resumeDeepReviewPolling(){
 deepReviewPollStart=Date.now()-300000; // reset elapsed window
 schedulePollDeepReview(2000);
 pollDeepReviewResult();
}
function renderDeepReviewResult(res, backFn){
 const escT=s=>{const d=document.createElement("div");d.textContent=s;return d.innerHTML};
 let html=`<h3>Deep Review: ${escT(deepReviewTraceId)}</h3>`;
 if(res.status==="error"){
  html+=`<div class="muted" style="color:#f87171;padding:12px 0">${escT(res.error||'Unknown error')}</div>`;
  document.getElementById("d").innerHTML=html;
  return;
 }
 // New schema: res.findings is a list of {category, symptom, root_cause,
 // proposed_solution, confidence}.
 let findings=Array.isArray(res.findings)?res.findings:[];
 deepReviewFindings=findings.slice();
 if(!findings.length){
  html+=`<div class="muted" style="padding:12px 0">(no issues found in this trace)</div>`;
  document.getElementById("d").innerHTML=html;
  return;
 }
 const sectionTitles={"tool_error":"Tool Errors","agent_limitation":"Agent Limitations","optimization":"Optimizations"};
 const sectionCls={"tool_error":"dr-tool-errors","agent_limitation":"dr-limitations","optimization":"dr-optimizations"};
 const confColor={"high":"#22c55e","medium":"#eab308","low":"#7d828c"};
 function renderFinding(f,idx){
  const conf=f.confidence||"medium";
  return `<div class="dr-finding-card"><div class="dr-finding-head">`+
   `<span class="dr-conf" style="background:${confColor[conf]||"#7d828c"};color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;text-transform:uppercase">${escT(conf)}</span>`+
   `<button class="dr-ticket-btn" onclick="createTicketFromFinding(${idx},event)" style="font-size:10px;padding:2px 8px;background:#2563eb;color:#fff;border:none;border-radius:3px;cursor:pointer;margin-left:auto;flex-shrink:0">+ Ticket</button>`+
   `</div>`+
   `<div class="dr-finding-symptom"><b>Symptom.</b> ${escT(f.symptom||"")}</div>`+
   (f.root_cause?`<div class="dr-finding-rc muted"><b>Root cause.</b> ${escT(f.root_cause)}</div>`:"")+
   (f.proposed_solution?`<div class="dr-finding-sol"><b>Proposed solution.</b> ${escT(f.proposed_solution)}</div>`:`<div class="muted" style="font-size:11px;font-style:italic">(no proposed solution)</div>`)+
   `</div>`;
 }
 function renderSectionStructured(cat,items){
  if(!items.length)return"";
  const title=sectionTitles[cat]||cat;
  const cls=sectionCls[cat]||"";
  let h=`<div class="dr-section ${cls}"><h4>${title} (${items.length})</h4>`;
  items.forEach(f=>{const idx=deepReviewFindings.indexOf(f);h+=renderFinding(f,idx)});
  h+=`</div>`;
  return h;
 }
 const byCat={"tool_error":[],"agent_limitation":[],"optimization":[]};
 findings.forEach(f=>{(byCat[f.category]=byCat[f.category]||[]).push(f)});
 ["tool_error","agent_limitation","optimization"].forEach(cat=>{html+=renderSectionStructured(cat,byCat[cat]||[])});
 const back=backFn || openDeepReview;
 html+=`<div style="margin-top:16px"><button onclick="${back.name}()"`+
   ` style="font-size:11px;padding:3px 10px;background:#2a2f3a;color:#aab0bd;border:1px solid #3a3f4a;border-radius:4px;cursor:pointer">← Back to traces</button></div>`;
 document.getElementById("d").innerHTML=html;
}
function createTicketFromFinding(idx,event){
 if(event)event.stopPropagation();
 const finding=deepReviewFindings[idx];
 if(!finding)return;
 const itemText=finding.symptom||finding.text||"";
 const category=finding.category||"";

 // Build modal DOM
 const backdrop=document.createElement("div");
 backdrop.className="modal-backdrop";
 const modal=document.createElement("div");
 modal.className="modal";
 modal.innerHTML=
  `<h2>New Ticket from Deep Review</h2>
   <span class="dr-source-badge">🔍 deep review</span>
   <label class="modal-label">Title <span class="modal-req">*</span></label>
   <input type="text" class="modal-input" id="modal-title" placeholder="What needs doing?" autocomplete="off">
   <div class="modal-field-error" id="modal-title-err"></div>
   <label class="modal-label">Description (auto-generated from findings)</label>
   <textarea class="modal-textarea" id="modal-desc" rows="8"></textarea>
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

 titleEl.value="Deep review: "+itemText.substring(0,80);
 descEl.value="**Symptom:** "+(finding.symptom||"")+"\n\n**Root cause:** "+(finding.root_cause||"")+"\n\n**Proposed solution:** "+(finding.proposed_solution||"")+"\n\n**Confidence:** "+(finding.confidence||"medium")+"\n\n**Source trace:** "+deepReviewTraceId;

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
  const r=await jpost("/tickets",{title:title,description:descEl.value,source:"deep-review"});
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
// -- end deep review ----------------------------------------------------
refresh();setInterval(()=>{refresh();if(runsOpen)renderRuns();else if(sel)open_(sel);if(deepReviewOpen&&deepReviewPollTimer){}/* poll active */},5000);
