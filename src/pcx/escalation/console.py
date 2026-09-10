"""A minimal but real operator console.

Scope, stated honestly: this is not a co-browsing product. There is no video
stream, no multi-operator queue, no auth, and it binds to loopback. What it
*does* do is real, and it is the part that matters -- an operator sees the live
screen of the run that got stuck, takes control, clicks and types on **that same
browser session**, and hands control back so the automation can finish. Nothing
is replayed into a fresh session and nothing is simulated.

The control-transfer rules are enforced in :class:`~pcx.escalation.broker.SessionBroker`,
not here, so a second front end (a real console, a Slack approval bot, a ticket
queue) can be built against the same broker without re-deriving the semantics.
Every operator action is echoed into the run's evidence log with
``actor="human"``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from ..evidence.recorder import EvidenceRecorder
from .broker import ControlDenied, SessionBroker


class ClaimBody(BaseModel):
    request_id: str
    operator: str = "operator"


class ActBody(BaseModel):
    kind: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None


class ReleaseBody(BaseModel):
    request_id: str
    disposition: str = "resume"
    note: str = ""


def build_app(broker: SessionBroker, recorder: EvidenceRecorder | None = None) -> FastAPI:
    app = FastAPI(title="pcx operator console", docs_url=None, redoc_url=None)

    def log(kind: str, /, **fields: Any) -> None:
        if recorder is not None:
            recorder.event(kind, actor="human", **fields)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return CONSOLE_HTML

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(broker.snapshot())

    @app.get("/api/frame.png")
    async def frame() -> Response:
        if broker.surface is None:
            raise HTTPException(503, "no live session")
        try:
            png = await broker.surface.screenshot_bytes()
        except Exception as exc:
            raise HTTPException(503, f"surface unavailable: {exc}") from exc
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/claim")
    async def claim(body: ClaimBody) -> JSONResponse:
        try:
            request = broker.claim(body.request_id, body.operator)
        except (KeyError, ControlDenied) as exc:
            raise HTTPException(409, str(exc)) from exc
        log("handoff_claimed", request_id=request.id, operator=body.operator)
        return JSONResponse({"ok": True, "request": request.model_dump(mode="json")})

    @app.post("/api/act")
    async def act(body: ActBody) -> JSONResponse:
        try:
            broker.assert_human_may_act()
        except ControlDenied as exc:
            raise HTTPException(409, str(exc)) from exc
        surface = broker.surface
        if surface is None:
            raise HTTPException(503, "no live session")

        if body.kind == "click" and body.x is not None and body.y is not None:
            await surface.human_click(body.x, body.y)
            detail = f"click ({int(body.x)},{int(body.y)})"
        elif body.kind == "type" and body.text is not None:
            await surface.human_type(body.text)
            detail = f"type {len(body.text)} chars"
        elif body.kind == "key" and body.key:
            await surface.human_press(body.key)
            detail = f"key {body.key}"
        else:
            raise HTTPException(400, f"unsupported operator action {body.kind!r}")

        broker.record_human_action(body.kind, detail)
        log("human_action", action=body.kind, detail=detail)
        return JSONResponse({"ok": True, "detail": detail})

    @app.post("/api/release")
    async def release(body: ReleaseBody) -> JSONResponse:
        try:
            result = broker.release(body.request_id, body.disposition, body.note)  # type: ignore[arg-type]
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        log(
            "handoff_released",
            request_id=body.request_id,
            disposition=body.disposition,
            note=body.note,
            actions=len(result.human_actions),
        )
        return JSONResponse({"ok": True, "disposition": result.disposition})

    return app


async def serve(app: FastAPI, host: str = "127.0.0.1", port: int = 8765):
    """Run the console inside the caller's event loop, alongside the automation."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    return server, task


CONSOLE_HTML = """
<!doctype html><meta charset="utf-8"><title>pcx operator console</title>
<style>
 :root{--bg:#12141a;--panel:#1b1f28;--line:#2c3340;--ink:#e6e9ef;--dim:#93a0b4;--warn:#f5a524;--ok:#3fb950;--bad:#f85149}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
   font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
 header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center}
 h1{font-size:13px;margin:0;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
 .pill{padding:2px 9px;border-radius:99px;border:1px solid var(--line);font-size:11px}
 .automation{color:var(--dim)} .pending_handoff{color:var(--warn);border-color:var(--warn)}
 .human{color:var(--ok);border-color:var(--ok)} .released{color:var(--bad)}
 main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;padding:16px;align-items:start}
 #screenwrap{background:#000;border:1px solid var(--line);border-radius:6px;overflow:hidden;position:relative}
 #screen{display:block;width:100%;cursor:crosshair}
 #screen.locked{cursor:not-allowed;opacity:.72}
 aside{display:flex;flex-direction:column;gap:12px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px}
 .card h2{margin:0 0 8px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
 .kv{display:grid;grid-template-columns:96px 1fr;gap:2px 8px;font-size:12px}
 .kv b{color:var(--dim);font-weight:400}
 button{font:inherit;background:#242a35;color:var(--ink);border:1px solid var(--line);
   border-radius:4px;padding:6px 11px;cursor:pointer}
 button:hover{border-color:#4a5568}
 button.primary{background:#1f6feb;border-color:#1f6feb} button.danger{background:#8b2c25;border-color:#8b2c25}
 button:disabled{opacity:.4;cursor:not-allowed}
 input,textarea{font:inherit;width:100%;background:#0e1116;color:var(--ink);
   border:1px solid var(--line);border-radius:4px;padding:6px}
 .row{display:flex;gap:6px} .row>*{flex:1}
 ul{margin:0;padding-left:16px} li{margin-bottom:3px}
 .muted{color:var(--dim)} .reason{color:var(--warn)}
 #log{max-height:150px;overflow:auto;font-size:11px;color:var(--dim)}
</style>
<header>
  <h1>pcx &middot; operator console</h1>
  <span id="control" class="pill automation">automation</span>
  <span id="runid" class="muted"></span>
</header>
<main>
  <div id="screenwrap"><img id="screen" alt="live session"></div>
  <aside>
    <div class="card">
      <h2>Intervention</h2>
      <div id="req" class="muted">No open request.</div>
      <div class="row" style="margin-top:10px">
        <button id="claim" class="primary" disabled>Take control</button>
        <button id="resume" disabled>Resume</button>
      </div>
      <div class="row" style="margin-top:6px">
        <button id="approve" disabled>Approve</button>
        <button id="reject" class="danger" disabled>Reject</button>
        <button id="abort" class="danger" disabled>Abort</button>
      </div>
      <textarea id="note" rows="2" placeholder="what you did / why" style="margin-top:6px"></textarea>
    </div>
    <div class="card">
      <h2>Manual input</h2>
      <p class="muted" style="margin:0 0 8px">Click the screen to click there. Then type below.</p>
      <input id="text" placeholder="text to type into the focused field">
      <div class="row" style="margin-top:6px">
        <button id="send" disabled>Type</button>
        <button id="enter" disabled>Enter</button>
        <button id="tab" disabled>Tab</button>
      </div>
    </div>
    <div class="card"><h2>Your actions</h2><div id="log">none yet</div></div>
  </aside>
</main>
<script>
const $ = id => document.getElementById(id);
let state = null, current = null;

function refreshFrame(){
  $("screen").src = "/api/frame.png?t=" + Date.now();
}
async function refreshState(){
  try{
    const r = await fetch("/api/state"); state = await r.json();
  }catch(e){ return; }
  const c = state.control;
  const pill = $("control"); pill.textContent = c; pill.className = "pill " + c;
  $("runid").textContent = state.run_id ? "run " + state.run_id : "";
  const open = state.requests.filter(r => r.status !== "resolved");
  current = open.find(r => r.id === state.current) || open[0] || null;

  if(!current){
    $("req").innerHTML = '<span class="muted">No open request.</span>';
  } else {
    const ctx = current.context || {};
    $("req").innerHTML =
      '<div class="kv">' +
      '<b>kind</b><span>' + current.kind + '</span>' +
      '<b>capability</b><span>' + (current.capability || "&mdash;") + '</span>' +
      '<b>step</b><span>' + current.step_id + ' (#' + current.step_index + ')</span>' +
      '<b>screen</b><span>' + (current.screen ? current.screen.title : "") + '</span>' +
      '<b>status</b><span>' + current.status + (current.operator ? " by " + current.operator : "") + '</span>' +
      '</div><p class="reason">' + current.reason + '</p>' +
      (ctx.hint ? '<p class="muted">' + ctx.hint + '</p>' : '') +
      (ctx.proposed_action ? '<p class="muted">proposed: <code>' + ctx.proposed_action + '</code></p>' : '') +
      (ctx.history ? '<ul class="muted">' + ctx.history.map(h => '<li>' + h + '</li>').join('') + '</ul>' : '');
    $("log").textContent = (current.human_actions || []).map(a => a.kind + " " + a.detail).join("\\n") || "none yet";
  }
  const human = c === "human";
  $("claim").disabled = !(current && current.status === "open");
  $("resume").disabled = !(human && current && current.kind !== "approval");
  $("approve").disabled = !(human && current && current.kind === "approval");
  $("reject").disabled  = !(human && current && current.kind === "approval");
  $("abort").disabled = !(human && current);
  for(const b of ["send","enter","tab"]) $(b).disabled = !human;
  $("screen").className = human ? "" : "locked";
}
async function post(path, body){
  const r = await fetch(path, {method:"POST", headers:{"content-type":"application/json"},
                              body: JSON.stringify(body)});
  if(!r.ok){ alert(await r.text()); }
  await refreshState(); refreshFrame();
}
$("screen").addEventListener("click", ev => {
  if(state?.control !== "human") return;
  const img = ev.target, rect = img.getBoundingClientRect();
  const x = (ev.clientX - rect.left) * (img.naturalWidth / rect.width);
  const y = (ev.clientY - rect.top) * (img.naturalHeight / rect.height);
  post("/api/act", {kind:"click", x, y});
});
$("claim").onclick  = () => post("/api/claim", {request_id: current.id, operator: "console-operator"});
$("send").onclick   = () => post("/api/act", {kind:"type", text: $("text").value});
$("enter").onclick  = () => post("/api/act", {kind:"key", key:"enter"});
$("tab").onclick    = () => post("/api/act", {kind:"key", key:"tab"});
$("resume").onclick = () => post("/api/release", {request_id: current.id, disposition:"resume", note: $("note").value});
$("approve").onclick= () => post("/api/release", {request_id: current.id, disposition:"approved", note: $("note").value});
$("reject").onclick = () => post("/api/release", {request_id: current.id, disposition:"rejected", note: $("note").value});
$("abort").onclick  = () => post("/api/release", {request_id: current.id, disposition:"abort", note: $("note").value});
setInterval(refreshState, 900); setInterval(refreshFrame, 900);
refreshState(); refreshFrame();
</script>
"""
