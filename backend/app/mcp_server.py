"""MCP server exposing Sokan's Liara tools to any MCP client (Claude Desktop, Cursor, ...).

Safety model: read tools run directly. Sensitive tools only *create an approval request*; nothing changes until a
human approves it in the Sokan web UI (http://127.0.0.1:8010). An AI client can never approve its own request.

Run:  python -m app.mcp_server        (stdio transport, from the backend/ folder)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import actions, store
from .liara import LiaraError, client

mcp = MCPServer("sokan-liara", instructions=(
    "Liara PaaS DevOps tools. Read tools are safe. Tools marked [APPROVAL] only queue a request that a human must "
    "approve in the Sokan web UI; tell the user to open it, then poll get_approval_status."))


async def _ready() -> None:
    store.init()
    lc = client()
    if getattr(lc, "mode", "") != "mock" and not lc.team_id:
        try:
            for t in await lc.teams():
                lc.team_id = t["_id"]
                if ((await lc.billing()).get("team") or {}).get("balance", 0) > 0:
                    return
            lc.team_id = ""
        except LiaraError:
            lc.team_id = ""


async def _read(name: str, **args: Any) -> str:
    await _ready()
    res = await actions.run_read(name, {k: v for k, v in args.items() if v not in (None, "")}, actor="mcp")
    if isinstance(res, dict):
        res.pop("series", None)
    return json.dumps(res, ensure_ascii=False, default=str)[:20000]


async def _request(name: str, **args: Any) -> str:
    await _ready()
    res = await actions.request(name, {k: v for k, v in args.items() if v not in (None, "")}, session="mcp", actor="mcp")
    if "approval_id" in res:
        res["next_step"] = "A human must approve this in the Sokan web UI (http://127.0.0.1:8010). Poll get_approval_status."
    return json.dumps(res, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ read tools
@mcp.tool()
async def account_overview() -> str:
    """Credit balance, apps with status, open alerts."""
    return await _read("account_overview")


@mcp.tool()
async def app_status(app: str) -> str:
    """Status, plan limits, containers, releases, env var names and a live HTTP probe of the site."""
    return await _read("app_status", app=app)


@mcp.tool()
async def get_logs(app: str, minutes: int = 30, contains: str = "") -> str:
    """Recent runtime logs, optionally filtered by substring."""
    return await _read("get_logs", app=app, minutes=minutes, contains=contains)


@mcp.tool()
async def get_metrics(app: str, minutes: int = 60) -> str:
    """CPU/RAM usage vs plan limits with trend."""
    return await _read("get_metrics", app=app, minutes=minutes)


@mcp.tool()
async def diagnose(app: str, minutes: int = 60) -> str:
    """Evidence-based root-cause analysis: ranked hypotheses with evidence and suggested fixes."""
    return await _read("diagnose", app=app, minutes=minutes)


@mcp.tool()
async def analyze_project(path: str, traffic: str = "low") -> str:
    """Inspect a local project folder and recommend the cheapest fitting Liara plan."""
    return await _read("analyze_project", path=path, traffic=traffic)


@mcp.tool()
async def list_plans() -> str:
    """Available Liara plans with monthly prices."""
    return await _read("list_plans")


@mcp.tool()
async def api_capabilities() -> str:
    """What can / cannot be done through the Liara API, with workarounds."""
    return await _read("api_capabilities")


# ------------------------------------------------------------------ approval-gated tools
@mcp.tool()
async def restart_app(app: str, reason: str) -> str:
    """[APPROVAL] Request a restart of the app."""
    return await _request("restart_app", app=app, reason=reason)


@mcp.tool()
async def set_env(app: str, variables: dict[str, str], reason: str = "") -> str:
    """[APPROVAL] Request adding/updating env vars (others are preserved)."""
    return await _request("set_env", app=app, variables=variables, reason=reason)


@mcp.tool()
async def resize_app(app: str, plan_id: str, reason: str) -> str:
    """[APPROVAL] Request a plan change (RAM/CPU)."""
    return await _request("resize_app", app=app, plan_id=plan_id, reason=reason)


@mcp.tool()
async def deploy(app: str, path: str, port: int, platform: str, message: str = "") -> str:
    """[APPROVAL] Request deploying a local folder as a new release."""
    return await _request("deploy", app=app, path=path, port=port, platform=platform, message=message)


@mcp.tool()
async def rollback(app: str, reason: str = "") -> str:
    """[APPROVAL] Request a rollback to the last healthy release."""
    return await _request("rollback", app=app, reason=reason)


@mcp.tool()
async def get_approval_status(approval_id: str) -> str:
    """Status (pending / running / executed / failed / rejected) and result of an approval request."""
    store.init()
    a = store.get_approval(approval_id)
    if not a:
        return json.dumps({"error": "not found"})
    a.get("params", {}).pop("variables", None)  # never echo secrets
    return json.dumps({k: a.get(k) for k in ("id", "title", "status", "result", "impact")}, ensure_ascii=False, default=str)


if __name__ == "__main__":
    mcp.run()
