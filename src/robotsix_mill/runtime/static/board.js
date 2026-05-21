const ST=["draft","awaiting_approval","ready","deliverable","in_review","rebasing","fixing_ci","done","closed","blocked","errored"];
const LBL={ready:"implementing"};   // display label only; state value stays "ready"
let showClosed=false;               // empty cols hidden; CLOSED also hidden unless toggled
let sel=null;
let runsOpen=false;
let refreshSeq=0;                    // serialize concurrent refresh() calls
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const srcClass=s=>(s==="retrospect"?"retrospect":s==="audit"?"audit":s==="scout"?"scout":s==="trace-health"?"trace-health":s==="health"?"health":s==="agent"?"agent":"user");
async function jget(u){const r=await fetch(u);return r.ok?r.json():null}
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
   <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span><span class="cost">$${(t.cost_usd||0).toFixed(4)}</span>`+
   (s==="awaiting_approval"?
    `<button class="approve-btn" onclick="event.stopPropagation();approve('${t.id}')">Approve</button>`+
    `<button class="reject-btn" title="Send back to draft with a comment" onclick="event.stopPropagation();requestChanges('${t.id}')">Request Changes</button>`:"")+
   `</div>`)
  .join("")+`</div></div>`).join("");
}
async function approve(id){
 const r=await fetch("/tickets/"+id+"/approve",{method:"POST"});
 if(!r.ok){const e=await r.text();alert("approve failed: "+e)}else refresh()
}
async function requestChanges(id){
 const body=prompt("Send this ticket back to draft. What needs to change?\n(your comment goes to the refine agent so it can re-process with this feedback.)");
 if(body===null)return;
 if(!body.trim()){alert("A comment is required when requesting changes");return}
 const r=await fetch("/tickets/"+id+"/request-changes",{method:"POST",
  headers:{"Content-Type":"application/json"},
  body:JSON.stringify({body:body.trim()})});
 if(!r.ok){const e=await r.text();alert("request-changes failed: "+e)}else{refresh();if(sel===id)open_(id)}
}
async function addComment(id){
 const body=prompt("Add a comment to this ticket:");
 if(body===null)return;
 if(!body.trim())return;
 const r=await fetch("/tickets/"+id+"/comments",{method:"POST",
  headers:{"Content-Type":"application/json"},
  body:JSON.stringify({body:body.trim()})});
 if(!r.ok){const e=await r.text();alert("add comment failed: "+e)}else if(sel===id)open_(id)
}
async function newTicket(){
 const title=prompt("New ticket title:");
 if(title===null)return;
 if(!title.trim()){alert("Title is required");return}
 const description=prompt("Description / rough idea (optional):")||"";
 const r=await fetch("/tickets",{method:"POST",
  headers:{"Content-Type":"application/json"},
  body:JSON.stringify({title:title.trim(),description:description})});
 if(!r.ok){const e=await r.text();alert("create failed: "+e)}else refresh()
}
async function del_(id){
 if(!confirm("Delete ticket "+id+"? This is irreversible (row, history, workspace)."))return;
 const r=await fetch("/tickets/"+id,{method:"DELETE"});
 if(!r.ok&&r.status!==204){const e=await r.text();alert("delete failed: "+e)}else refresh()
}
async function runAudit(){
 const btn=event.target;
 btn.disabled=true; btn.textContent='Running...';
 try {
   const r=await fetch("/audit",{method:"POST"});
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
   const r=await fetch("/scout",{method:"POST"});
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
   const r=await fetch("/trace-health",{method:"POST"});
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
   const r=await fetch("/health-check",{method:"POST"});
   if(!r.ok){throw new Error(await r.text())}
   alert("Health check started — new draft tickets will appear on the board if issues are found.");
   setTimeout(refresh,3000);
 } catch(e) {
   alert("Health check failed to start: "+e);
 } finally {
   btn.disabled=false; btn.textContent='Run Health Check';
 }
}
async function open_(id){
 sel=id;
 const [t,h,d,cs]=await Promise.all([jget("/tickets/"+id),
   jget("/tickets/"+id+"/history"),jget("/tickets/"+id+"/description"),
   jget("/tickets/"+id+"/comments")]);
 if(!t)return;
 document.getElementById("d").innerHTML=
  `<h3>${esc(t.title)}</h3>
   <div class="muted">${t.id}</div>
   <p>state <b class="s-${t.state}" style="border-left:3px solid var(--c);
      padding-left:6px">${t.state}</b> · branch ${esc(t.branch)||"—"}<br>
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
   `<h3>description.md</h3><pre>${esc((d&&d.description)||"")}</pre>`;
 document.getElementById("drawer").classList.add("open");
}
function close_(){sel=null;runsOpen=false;
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
   const kc=r.kind==='audit'?'#059669':r.kind==='scout'?'#7c3aed':'#0ea5e9';
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
refresh();setInterval(()=>{refresh();if(runsOpen)renderRuns();else if(sel)open_(sel)},5000);
