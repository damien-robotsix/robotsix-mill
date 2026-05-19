"""The single-page kanban-board HTML served at ``GET /``.

Kept in its own module so it can be edited without touching the API file
and imported independently for testing.
"""

BOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>robotsix-mill</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}body{margin:0;font:13px/1.4 ui-monospace,monospace;
background:#0f1115;color:#d6d9df}
header{padding:10px 14px;border-bottom:1px solid #2a2e37;display:flex;
gap:14px;align-items:baseline;flex-wrap:wrap}
h1{font-size:15px;margin:0;color:#fff}.muted{color:#7d828c}
#board{display:flex;gap:10px;padding:12px;overflow-x:auto;
height:calc(100vh - 46px)}
.col{flex:0 0 220px;background:#161922;border:1px solid #262b36;
border-radius:8px;display:flex;flex-direction:column;min-height:0}
.col h2{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
margin:0;padding:9px 11px;border-bottom:1px solid #262b36;color:#aab0bd}
.col h2 .n{float:right;color:#7d828c}
.cards{padding:8px;overflow-y:auto;display:flex;flex-direction:column;gap:7px}
.card{background:#1d212c;border:1px solid #2c313d;border-left:3px solid var(--c);
border-radius:6px;padding:7px 9px;cursor:pointer}
.card:hover{background:#232836}.card .t{color:#eef0f4}
.card .id{color:#6b7280;font-size:11px;margin-top:3px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src-badge{display:inline-block;font-size:10px;padding:1px 5px;border-radius:4px;
margin-top:3px;text-transform:uppercase;letter-spacing:.04em}
.src-user{background:#1d3a5c;color:#60a5fa}
.src-retrospect{background:#3b2f1a;color:#f59e0b}
.src-audit{background:#1a3b2f;color:#34d399}
.cost{font-size:10px;color:#7d828c;margin-left:6px}
.src-scout{background:#2a1a3b;color:#c084fc}
.src-trace-health{background:#1a2a3b;color:#60c0fa}
.src-health{background:#1a3b2f;color:#34d399}
.src-agent{background:#3b1a1a;color:#f87171}
.approve-btn{font-size:11px;margin-top:5px;padding:3px 8px;background:#3b82f6;
color:#fff;border:none;border-radius:4px;cursor:pointer}
.approve-btn:hover{background:#2563eb}
.del-btn{position:absolute;top:4px;right:4px;font-size:11px;line-height:1;
padding:2px 5px;background:#3a1f1f;color:#f87171;border:1px solid #5b2a2a;
border-radius:4px;cursor:pointer;opacity:0;transition:opacity .1s}
.card{position:relative}
.card:hover .del-btn{opacity:1}
.del-btn:hover{background:#7f1d1d;color:#fff}
#drawer{position:fixed;top:0;right:0;width:min(560px,92vw);height:100vh;
background:#11141b;border-left:1px solid #2a2e37;transform:translateX(100%);
transition:transform .15s;overflow-y:auto;padding:16px}
#drawer.open{transform:none}#drawer h3{margin:.2em 0;color:#fff}
#drawer .x{float:right;cursor:pointer;color:#7d828c;font-size:18px}
pre{white-space:pre-wrap;background:#0c0e13;border:1px solid #262b36;
border-radius:6px;padding:10px;overflow-x:auto}
.ev{border-left:2px solid #333a47;padding:2px 0 2px 9px;margin:4px 0}
.ev b{color:#cfd3db}.s-draft{--c:#6b7280}.s-awaiting_approval{--c:#f59e0b}
.s-ready{--c:#3b82f6}.s-in_review{--c:#a855f7}.s-rebasing{--c:#f59e0b}
.s-deliverable{--c:#eab308}
.s-done{--c:#22c55e}.s-closed{--c:#14b8a6}.s-blocked{--c:#f97316}
.s-errored{--c:#ef4444}
</style></head><body>
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
</header>
<div id="board"></div>
<div id="drawer"><span class="x" onclick="close_()">&times;</span><div id="d"></div></div>
<script>
const ST=["draft","awaiting_approval","ready","deliverable","in_review","rebasing","done","closed","blocked","errored"];
const LBL={ready:"implementing"};   // display label only; state value stays "ready"
let showClosed=false;               // empty cols hidden; CLOSED also hidden unless toggled
let sel=null;
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const srcClass=s=>(s==="retrospect"?"retrospect":s==="audit"?"audit":s==="scout"?"scout":s==="trace-health"?"trace-health":s==="health"?"health":s==="agent"?"agent":"user");
async function jget(u){const r=await fetch(u);return r.ok?r.json():null}
async function refresh(){
 const ts=await jget("/tickets"); if(!ts)return;
 const by={}; ST.forEach(s=>by[s]=[]);
 ts.forEach(t=>(by[t.state]=by[t.state]||[]).push(t));
 document.getElementById("meta").textContent=
   ts.length+" tickets · "+new Date().toLocaleTimeString();
 document.getElementById("board").innerHTML=ST.filter(s=>by[s].length>0&&(s!=="closed"||showClosed)).map(s=>`<div class="col">
  <h2>${LBL[s]||s}<span class="n">${by[s].length}</span></h2><div class="cards">`+
  by[s].map(t=>`<div class="card s-${t.state}" onclick="open_('${t.id}')">
   <button class="del-btn" title="Delete ticket" onclick="event.stopPropagation();del_('${t.id}')">✕</button>
   <div class="t">${esc(t.title)}</div><div class="id">${t.id}</div>
   <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span><span class="cost">$${(t.cost_usd||0).toFixed(4)}</span>`+
   (s==="awaiting_approval"?
    `<button class="approve-btn" onclick="event.stopPropagation();approve('${t.id}')">Approve</button>`:"")+
   `</div>`)
  .join("")+`</div></div>`).join("");
}
async function approve(id){
 const r=await fetch("/tickets/"+id+"/approve",{method:"POST"});
 if(!r.ok){const e=await r.text();alert("approve failed: "+e)}else refresh()
}
async function del_(id){
 if(!confirm("Delete ticket "+id+"?\nThis is irreversible (row, history, workspace)."))return;
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
   const data=await r.json();
   alert("Scout complete. Created "+data.tickets_created.length+" draft(s).");
   refresh();
 } catch(e) {
   alert("Scout failed: "+e);
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
 const [t,h,d]=await Promise.all([jget("/tickets/"+id),
   jget("/tickets/"+id+"/history"),jget("/tickets/"+id+"/description")]);
 if(!t)return;
 document.getElementById("d").innerHTML=
  `<h3>${esc(t.title)}</h3>
   <div class="muted">${t.id}</div>
   <p>state <b class="s-${t.state}" style="border-left:3px solid var(--c);
      padding-left:6px">${t.state}</b> · branch ${esc(t.branch)||"—"}<br>
   source <span class="src-badge src-${srcClass(t.source)}">${esc(t.source||"user")}</span>
   · cost <b>$${(t.cost_usd||0).toFixed(4)}</b><br>
   created ${t.created_at} · updated ${t.updated_at}</p>
   <h3>History</h3>`+
   (h||[]).map(e=>`<div class="ev"><b>${e.state}</b> ${e.at}
     ${e.note?"<br>"+esc(e.note):""}</div>`).join("")+
   `<h3>description.md</h3><pre>${esc((d&&d.description)||"")}</pre>`;
 document.getElementById("drawer").classList.add("open");
}
function close_(){sel=null;
 document.getElementById("drawer").classList.remove("open")}
refresh();setInterval(()=>{refresh();if(sel)open_(sel)},5000);
</script></body></html>"""
