"""HTTP API + SSE streams for the Sokan web UI."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import actions, agent, capabilities, config, monitor, store
from .liara import LiaraError, client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sokan")
workspace: dict[str, Any] = {"team_id": config.LIARA_TEAM_ID, "team_name": None, "teams": []}


async def detect_workspace() -> None:
    """Liara credit often lives on a team, not the personal account → pick the funded team automatically."""
    lc = client()
    if getattr(lc, "mode", "") == "mock" or config.LIARA_TEAM_ID:
        return
    try:
        lc.team_id = ""
        teams = await lc.teams()
        workspace["teams"] = [{"id": t.get("_id"), "name": t.get("name")} for t in teams]
        best = None
        for t in teams:
            lc.team_id = t["_id"]
            bal = ((await lc.billing()).get("team") or {}).get("balance", 0)
            if best is None or bal > best[1]:
                best = (t, bal)
        lc.team_id = ""
        if best and best[1] > 0:
            lc.team_id = best[0]["_id"]
            workspace.update(team_id=best[0]["_id"], team_name=best[0].get("name"))
            log.info("Using Liara team %s (balance %s)", best[0].get("name"), best[1])
    except LiaraError as e:
        log.warning("workspace detection failed: %s", e.message)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    await detect_workspace()
    task = asyncio.create_task(monitor.loop()) if config.MONITOR_ENABLED else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Sokan — Liara Copilot", lifespan=lifespan)


def sse(gen: AsyncIterator[dict]) -> StreamingResponse:
    async def body():
        try:
            async for ev in gen:
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        except LiaraError as e:
            yield f"data: {json.dumps({'type': 'error', 'text': e.message}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            log.exception("stream failed")
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"end\"}\n\n"
    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _guard(coro):
    try:
        return await coro
    except LiaraError as e:
        raise HTTPException(e.status if e.status and 400 <= e.status < 600 else 502, e.message) from e


# ------------------------------------------------------------------ basics
@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/overview")
async def overview():
    ov = await _guard(actions.account_overview())
    return ov | {"mode": getattr(client(), "mode", "live"), "workspace": workspace, "llm": store.llm_spent(),
                 "monitor": monitor.state, "policies_catalog": monitor.POLICIES}


class WorkspaceIn(BaseModel):
    team_id: str = ""


@app.post("/api/workspace")
async def set_workspace(body: WorkspaceIn):
    client().team_id = body.team_id
    workspace["team_id"] = body.team_id
    workspace["team_name"] = next((t["name"] for t in workspace["teams"] if t["id"] == body.team_id), None)
    return workspace


@app.get("/api/capabilities")
async def caps():
    return capabilities.CAPABILITIES


# ------------------------------------------------------------------ app data
@app.get("/api/apps/{name}/status")
async def app_status(name: str):
    return await _guard(actions.app_status(name))


@app.get("/api/apps/{name}/metrics")
async def app_metrics(name: str, minutes: int = 60):
    return await _guard(actions.get_metrics(name, minutes))


@app.get("/api/apps/{name}/logs")
async def app_logs(name: str, minutes: int = 30, contains: str = ""):
    return await _guard(actions.get_logs(name, minutes, contains, 300))


@app.get("/api/apps/{name}/diagnose")
async def app_diagnose(name: str, minutes: int = 60):
    return await _guard(actions.diagnose(name, minutes))


@app.get("/api/apps/{name}/health")
async def app_health(name: str, hours: float = 1):
    return {"history": store.health_history(name, hours * 3600), "alerts": store.alerts(app=name),
            "policies": store.policies(name)}


class AnalyzeIn(BaseModel):
    path: str
    traffic: str = "low"


@app.post("/api/analyze")
async def analyze(body: AnalyzeIn):
    return await _guard(actions.analyze_project(body.path, body.traffic))


# ------------------------------------------------------------------ chat
class ChatIn(BaseModel):
    text: str
    session: str = ""
    app: str = ""  # app currently open in the dashboard (context hint)


@app.post("/api/chat")
async def chat(body: ChatIn):
    sid = body.session or uuid.uuid4().hex[:12]

    async def gen():
        yield {"type": "session", "session": sid}
        async for ev in agent.chat(sid, body.text, body.app):
            yield ev
    return sse(gen())


# ------------------------------------------------------------------ approvals / human-in-the-loop
@app.get("/api/approvals")
async def approvals():
    return {"pending": store.pending_approvals()}


class DecisionIn(BaseModel):
    confirm_text: str = ""
    session: str = ""


@app.post("/api/approvals/{aid}/approve")
async def approve(aid: str, body: DecisionIn):
    appr = store.get_approval(aid)
    if not appr:
        raise HTTPException(404, "درخواست پیدا نشد")
    if appr["risk"] == actions.CRITICAL and body.confirm_text.strip() != (appr.get("app") or ""):
        raise HTTPException(400, "برای عملیات بحرانی باید نام برنامه را دقیقاً تایپ کنید")

    async def gen():
        final: dict = {}
        async for ev in actions.execute(aid):
            if ev.get("type") == "done":
                final = ev
            yield {"type": "exec", "approval_id": aid, "event": ev}
        text = await agent.summarize_execution(appr, final)
        sess = body.session or appr.get("session") or ""
        agent.note_outcome(sess, text)
        yield {"type": "message", "text": text, "approval_id": aid, "ok": bool(final.get("ok"))}
        yield {"type": "approval_update", "approval": store.get_approval(aid)}
        if final.get("ok"):
            yield {"type": "followup_start"}
            async for ev in agent.continue_after(sess, f"{appr['title']}: {text}"):
                yield ev
        elif final.get("release_ready"):
            # Release is live but the site is unhealthy → verify immediately (read-only, no approval needed).
            yield {"type": "followup_start"}
            async for ev in agent.continue_after(
                    sess, f"{appr['title']}: release is READY but the site answers {(final.get('probe') or {}).get('status')}. "
                          f"Call diagnose for app '{appr.get('app')}' now and explain the root cause and the fix."):
                yield ev
    return sse(gen())


@app.post("/api/approvals/{aid}/reject")
async def reject(aid: str, body: DecisionIn):
    res = actions.reject(aid)
    appr = store.get_approval(aid)
    if res["ok"]:
        agent.note_outcome(body.session or (appr or {}).get("session") or "", f"کاربر «{(appr or {}).get('title')}» را رد کرد.")
    return {"ok": res["ok"], "approval": appr}


# ------------------------------------------------------------------ automation / audit
class PolicyIn(BaseModel):
    kind: str
    enabled: bool


@app.post("/api/apps/{name}/policies")
async def set_policy(name: str, body: PolicyIn):
    if body.kind not in monitor.POLICIES:
        raise HTTPException(400, "سیاست ناشناخته")
    store.set_policy(name, body.kind, body.enabled)
    store.audit("user", name, "policy:" + body.kind, "sensitive", {"enabled": body.enabled}, "success")
    return {"policies": store.policies(name)}


@app.post("/api/monitor/run")
async def monitor_run():
    return {"results": await _guard(monitor.run_once())}


@app.get("/api/alerts")
async def get_alerts(all: bool = False):
    return {"alerts": store.alerts(open_only=not all)}


@app.get("/api/audit")
async def audit(limit: int = 100, app_name: str | None = None):
    return {"audit": store.audit_log(limit, app_name)}


@app.get("/api/events")
async def events():
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    monitor.listeners.add(q)

    async def gen():
        try:
            yield {"type": "hello"}
            while True:
                try:
                    yield await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
        finally:
            monitor.listeners.discard(q)
    return sse(gen())


# ------------------------------------------------------------------ frontend
if config.STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=config.STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        f = config.STATIC_DIR / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(config.STATIC_DIR / "index.html")
