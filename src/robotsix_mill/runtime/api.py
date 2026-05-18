"""FastAPI app = the management-plane service.

It owns the DB, the in-process worker, and the HTTP surface the CLI (and
a future web frontend) use. Emitting a ticket enqueues it; the worker
picks it up immediately and chains it through the pipeline.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..config import Settings
from ..core import db
from ..core.models import (
    Ticket,
    TicketCreate,
    TicketEvent,
    TicketRead,
    TicketTransition,
)
from ..core.service import TicketService, TransitionError
from ..core.states import STAGE_FOR_STATE, State
from ..stages import StageContext
from . import tracing
from .worker import Worker


_BOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>robotsix-mill</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}body{margin:0;font:13px/1.4 ui-monospace,monospace;
background:#0f1115;color:#d6d9df}
header{padding:10px 14px;border-bottom:1px solid #2a2e37;display:flex;
gap:14px;align-items:baseline}
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
.approve-btn{font-size:11px;margin-top:5px;padding:3px 8px;background:#3b82f6;
color:#fff;border:none;border-radius:4px;cursor:pointer}
.approve-btn:hover{background:#2563eb}
#drawer{position:fixed;top:0;right:0;width:min(560px,92vw);height:100vh;
background:#11141b;border-left:1px solid #2a2e37;transform:translateX(100%);
transition:transform .15s;overflow-y:auto;padding:16px}
#drawer.open{transform:none}#drawer h3{margin:.2em 0;color:#fff}
#drawer .x{float:right;cursor:pointer;color:#7d828c;font-size:18px}
pre{white-space:pre-wrap;background:#0c0e13;border:1px solid #262b36;
border-radius:6px;padding:10px;overflow-x:auto}
.ev{border-left:2px solid #333a47;padding:2px 0 2px 9px;margin:4px 0}
.ev b{color:#cfd3db}.s-draft{--c:#6b7280}.s-awaiting_approval{--c:#f59e0b}
.s-ready{--c:#3b82f6}.s-in_review{--c:#a855f7}.s-deliverable{--c:#eab308}
.s-done{--c:#22c55e}.s-closed{--c:#14b8a6}.s-blocked{--c:#f97316}
.s-errored{--c:#ef4444}
</style></head><body>
<header><h1>robotsix-mill</h1>
<span class="muted" id="meta">loading…</span>
<span class="muted" style="margin-left:auto">auto-refresh 5s</span></header>
<div id="board"></div>
<div id="drawer"><span class="x" onclick="close_()">&times;</span><div id="d"></div></div>
<script>
const ST=["draft","awaiting_approval","ready","deliverable","in_review","done","closed","blocked","errored"];
let sel=null;
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function jget(u){const r=await fetch(u);return r.ok?r.json():null}
async function refresh(){
 const ts=await jget("/tickets"); if(!ts)return;
 const by={}; ST.forEach(s=>by[s]=[]);
 ts.forEach(t=>(by[t.state]=by[t.state]||[]).push(t));
 document.getElementById("meta").textContent=
   ts.length+" tickets · "+new Date().toLocaleTimeString();
 document.getElementById("board").innerHTML=ST.map(s=>`<div class="col">
  <h2>${s}<span class="n">${by[s].length}</span></h2><div class="cards">`+
  by[s].map(t=>`<div class="card s-${t.state}" onclick="open_('${t.id}')">
   <div class="t">${esc(t.title)}</div><div class="id">${t.id}</div>`+
   (s==="awaiting_approval"?
    `<button class="approve-btn" onclick="event.stopPropagation();approve('${t.id}')">Approve</button>`:"")+
   `</div>`)
  .join("")+`</div></div>`).join("");
}
async function approve(id){
 const r=await fetch("/tickets/"+id+"/approve",{method:"POST"});
 if(!r.ok){const e=await r.text();alert("approve failed: "+e)}else refresh()
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(settings)
        tracing.init(settings)
        service = TicketService(settings)
        ctx = StageContext(settings=settings, service=service)
        worker = Worker(ctx)
        app.state.settings = settings
        app.state.service = service
        app.state.worker = worker
        worker.start()
        worker.requeue_unfinished()  # resume anything left mid-pipeline
        try:
            yield
        finally:
            await worker.stop()

    app = FastAPI(title="robotsix-mill", lifespan=lifespan)

    def _svc() -> TicketService:
        return app.state.service

    def _maybe_enqueue(ticket: Ticket) -> None:
        if ticket.state in STAGE_FOR_STATE:
            app.state.worker.enqueue(ticket.id)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def board() -> str:
        return _BOARD_HTML

    @app.post("/tickets", response_model=TicketRead, status_code=201)
    def create_ticket(body: TicketCreate) -> Ticket:
        ticket = _svc().create(body.title, body.description)
        _maybe_enqueue(ticket)  # "directly taken in charge"
        return ticket

    @app.get("/tickets", response_model=list[TicketRead])
    def list_tickets(state: State | None = None) -> list[Ticket]:
        return _svc().list(state=state)

    @app.get("/tickets/{ticket_id}", response_model=TicketRead)
    def get_ticket(ticket_id: str) -> Ticket:
        ticket = _svc().get(ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        return ticket

    @app.get("/tickets/{ticket_id}/history", response_model=list[TicketEvent])
    def get_history(ticket_id: str) -> list[TicketEvent]:
        if _svc().get(ticket_id) is None:
            raise HTTPException(404, "ticket not found")
        return _svc().history(ticket_id)

    @app.get("/tickets/{ticket_id}/description")
    def get_description(ticket_id: str) -> dict:
        ticket = _svc().get(ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        return {"description": _svc().workspace(ticket).read_description()}

    @app.post("/tickets/{ticket_id}/transition", response_model=TicketRead)
    def transition(ticket_id: str, body: TicketTransition) -> Ticket:
        try:
            ticket = _svc().transition(ticket_id, body.state, body.note)
        except KeyError:
            raise HTTPException(404, "ticket not found") from None
        except TransitionError as e:
            raise HTTPException(409, str(e)) from None
        _maybe_enqueue(ticket)  # human unblock re-triggers the chain
        return ticket

    @app.post("/tickets/{ticket_id}/approve", response_model=TicketRead)
    def approve_ticket(ticket_id: str) -> Ticket:
        try:
            ticket = _svc().transition(
                ticket_id, State.READY, note="approved by human"
            )
        except KeyError:
            raise HTTPException(404, "ticket not found") from None
        except TransitionError as e:
            raise HTTPException(409, str(e)) from None
        _maybe_enqueue(ticket)  # implement picks it up from ready
        return ticket

    return app
