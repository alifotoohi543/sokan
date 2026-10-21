"""In-memory Liara simulator — ONLY for automated tests and offline UI development (LIARA_MODE=mock).

It mirrors the response shapes of the real API and lets tests inject faults (missing env var, memory leak,
CPU saturation, crash) to exercise the full diagnose → approve → fix → verify loop without touching a real account.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from .liara import LiaraError

PLANS = {"small-g2": (0.5, 0.5, 659.72), "medium-g2": (1, 1, 1145.83), "standard-base-g2": (2, 1, 2048.61)}


class MockLiara:
    mode = "mock"

    def __init__(self):
        self.team_id = ""
        now = time.time()
        self.apps: dict[str, dict[str, Any]] = {
            "shop-demo": {"planID": "small-g2", "type": "docker", "scale": 1, "envs": [{"key": "SHOP_NAME", "value": "x"}],
                          "fault": "missing_env", "created": now - 86400, "restarted": now - 3600, "isDeployed": True},
            "blog-demo": {"planID": "small-g2", "type": "node", "scale": 1, "envs": [], "fault": "leak",
                          "created": now - 86400, "restarted": now - 3000, "isDeployed": True},
        }
        self._rels: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def _app(self, name: str) -> dict:
        if name not in self.apps:
            raise LiaraError(404, "پیدا نشد", endpoint=f"v1/projects/{name}")
        return self.apps[name]

    # --- account
    async def me(self, fresh: bool = False):
        return {"plans": {"projects": {k: {"available": True, "region": "iran", "RAM": {"amount": r}, "CPU": {"amount": c},
                                           "volume": 5, "price": p} for k, (r, c, p) in PLANS.items()},
                          "projectBundlePlans": {k: {"basic": {"available": True, "displayPrice": 175000, "maxLogsRetention": 7,
                                                               "rollback": True}} for k in PLANS}}}

    async def billing(self):
        return {"user": {"balance": 0}, "team": {"balance": 2_000_000}}

    async def teams(self):
        return []

    async def plans(self):
        from .liara import LiaraClient
        return await LiaraClient.plans(self)  # type: ignore[arg-type]

    async def plan_limits(self, plan_id: str):
        if plan_id not in PLANS:
            return None
        r, c, p = PLANS[plan_id]
        return {"ram_gb": r, "cpu": c, "disk_gb": 5, "monthly_toman": round(p * 720)}

    # --- apps
    async def list_apps(self):
        return [{"project_id": n, "type": a["type"], "status": "RUNNING" if a["scale"] else "STOPPED", "scale": a["scale"],
                 "planID": a["planID"], "isDeployed": a["isDeployed"]} for n, a in self.apps.items()]

    async def get_app(self, name: str):
        a = self._app(name)
        return {"project_id": name, "type": a["type"], "status": "RUNNING" if a["scale"] else "STOPPED", "scale": a["scale"],
                "planID": a["planID"], "envs": a["envs"], "isDeployed": a["isDeployed"], "zeroDowntime": False}

    async def create_app(self, name, plan_id, platform, bundle_plan_id, network=None):
        if name in self.apps:
            raise LiaraError(409, "تداخل", endpoint="v1/projects")
        self.apps[name] = {"planID": plan_id, "type": platform, "scale": 1, "envs": [], "fault": None,
                           "created": time.time(), "restarted": time.time(), "isDeployed": False}
        self.calls.append(("create", name))
        return {}

    async def networks(self):
        return getattr(self, "_nets", [])

    async def create_network(self, name):
        self._nets = [{"_id": "net1", "name": name}]
        return {}

    async def delete_app(self, name):
        self._app(name)
        del self.apps[name]
        return {}

    async def restart(self, name):
        a = self._app(name)
        a["restarted"] = time.time()
        self.calls.append(("restart", name))
        return {}

    async def scale(self, name, scale):
        self._app(name)["scale"] = scale
        self.calls.append(("scale", name))
        return {}

    async def resize(self, name, plan_id, bundle_plan_id=None):
        a = self._app(name)
        a["planID"] = plan_id
        a["restarted"] = time.time()
        if a["fault"] == "cpu":
            a["fault"] = None
        self.calls.append(("resize", name))
        return {}

    async def update_envs(self, name, variables):
        a = self._app(name)
        a["envs"] = [{"key": v["key"], "value": v["value"]} for v in variables]
        a["restarted"] = time.time()
        if a["fault"] == "missing_env" and any(v["key"] == "DATABASE_URL" for v in variables):
            a["fault"] = None
        self.calls.append(("envs", name))
        return {"message": "ok"}

    # --- observability
    async def logs(self, name, since_unix, last_lines=False, release_id=None):
        a = self._app(name)
        now = time.time()
        out = []
        t = max(since_unix, now - 600)
        while t < now:
            if a["fault"] == "missing_env":
                out.append({"ts": t, "line": 'Traceback (most recent call last):', "release": "r1"})
                out.append({"ts": t + 0.1, "line": "KeyError: 'DATABASE_URL'", "release": "r1"})
                out.append({"ts": t + 0.2, "line": '10.0.0.1 - "GET / HTTP/1.1" 500 21', "release": "r1"})
            elif a["fault"] == "leak":
                out.append({"ts": t, "line": '10.0.0.1 - "GET /api/posts HTTP/1.1" 200 812', "release": "r1"})
            else:
                out.append({"ts": t, "line": '10.0.0.1 - "GET / HTTP/1.1" 200 120', "release": "r1"})
            t += 60
        return out

    async def applets(self, name):
        a = self._app(name)
        return [{"state": "RUNNING" if a["scale"] else "STOPPED", "reason": "", "release": {"tag": "v1"}}]

    async def releases(self, name, count=10):
        self._app(name)
        rel = [{"_id": rid, "tag": f"v{i}", "state": r["state"], "createdAt": r["ts"]}
               for i, (rid, r) in enumerate(self._rels.items()) if r["app"] == name][::-1]
        return {"releases": rel or [{"_id": "r1", "tag": "v1", "state": "READY"}], "currentRelease": rel[0]["_id"] if rel else "r1"}

    async def events(self, name, start=None, end=None):
        return []

    async def metric(self, name, kind, since_unix):
        a = self._app(name)
        lim_ram, lim_cpu, _ = PLANS[a["planID"]]
        now = time.time()
        pts = []
        t = max(since_unix, now - 3600)
        while t <= now:
            age = max(0, t - a["restarted"])
            if kind == "memory":
                gb = 0.12 + (min(lim_ram * 0.97, 0.12 + age / 3600 * 0.5) - 0.12 if a["fault"] == "leak" else 0.0)
                pts.append((t, gb * 1024 ** 3 if t >= a["restarted"] else 0.1 * 1024 ** 3))
            elif kind == "cpu":
                pts.append((t, lim_cpu * (0.97 if a["fault"] == "cpu" else 0.08)))
            else:
                pts.append((t, 1000.0))
            t += 60
        return pts

    async def probe(self, name):
        a = self.apps.get(name)
        if not a or not a["scale"]:
            return {"url": f"https://{name}.liara.run/", "ok": False, "status": None, "latency_s": 10.0, "error": "ConnectError"}
        status = 500 if a["fault"] == "missing_env" else 200
        latency = 3.4 if a["fault"] in ("leak", "cpu") and time.time() - a["restarted"] > 1200 else 0.21
        return {"url": f"https://{name}.liara.run/", "ok": status < 500, "status": status, "latency_s": latency, "body": ""}

    # --- deploy
    async def upload_source(self, name, archive):
        self._app(name)
        return "src-" + uuid.uuid4().hex[:8]

    async def create_release(self, name, body):
        rid = uuid.uuid4().hex[:24]
        self._rels[rid] = {"app": name, "state": "BUILDING", "ts": time.time(), "polls": 0}
        return rid

    async def build_logs(self, release_id, since=None):
        r = self._rels[release_id]
        r["polls"] += 1
        if r["polls"] == 1:
            return {"release": {"state": "BUILDING"}, "buildOutput": [{"stream": "STDOUT", "line": "Step 1/5 : FROM python:3.12-slim", "createdAt": "1"}]}
        r["state"] = "READY" if r["polls"] > 2 else "DEPLOYING"
        if r["state"] == "READY":
            self.apps[r["app"]]["isDeployed"] = True
        return {"release": {"state": r["state"]}, "buildOutput": []}

    async def cancel_release(self, release_id):
        self._rels[release_id]["state"] = "CANCELED"
        return {}

    # --- domains
    async def domains(self):
        return []

    async def add_domain(self, domain, app):
        return {}
