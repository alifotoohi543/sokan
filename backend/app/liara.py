"""Thin async client over the documented Liara REST API (https://openapi.liara.ir).

Every method maps to one real endpoint. Errors are converted into ``LiaraError`` with a
Persian explanation so the agent and UI can show them to the user verbatim.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)

STATUS_FA = {
    400: "درخواست نامعتبر بود",
    401: "توکن API نامعتبر یا منقضی است",
    402: "اعتبار حساب کافی نیست",
    403: "دسترسی به این منبع مجاز نیست",
    404: "پیدا نشد",
    409: "تداخل: این مورد از قبل وجود دارد یا عملیات دیگری در جریان است",
    422: "داده‌ی ارسالی معتبر نیست",
    429: "تعداد درخواست‌ها بیش از حد مجاز است؛ کمی صبر کنید",
}


class LiaraError(Exception):
    def __init__(self, status: int | None, message: str, body: Any = None, endpoint: str = ""):
        super().__init__(message)
        self.status, self.message, self.body, self.endpoint = status, message, body, endpoint

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "endpoint": self.endpoint,
                "detail": self.body if isinstance(self.body, (str, dict, list)) else None}


def _token() -> str | None:
    return config.read_secret("LIARA_API_TOKEN", config.LIARA_TOKEN_FILE)


class LiaraClient:
    """Live client. ``team_id`` is appended to every call like the official CLI does."""

    mode = "live"

    def __init__(self, token: str | None = None, team_id: str | None = None):
        self.token = token or _token()
        self.team_id = team_id if team_id is not None else config.LIARA_TEAM_ID
        self._me_cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------------ transport
    async def _request(self, method: str, path: str, *, base: str | None = None, params: dict | None = None,
                       json: Any = None, files: Any = None, timeout: float = 30, retries: int = 3) -> Any:
        if not self.token:
            raise LiaraError(401, "توکن API لیارا تنظیم نشده است", endpoint=path)
        params = dict(params or {})
        if self.team_id:
            params["teamID"] = self.team_id
        url = f"{(base or config.LIARA_API).rstrip('/')}/{path.lstrip('/')}"
        last: Exception | None = None
        # Only idempotent reads are retried automatically; writes are attempted once.
        attempts = retries if method == "GET" else 1
        for i in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.request(method, url, params=params, json=json, files=files,
                                        headers={"Authorization": f"Bearer {self.token}"})
            except httpx.HTTPError as e:
                last = e
                await asyncio.sleep(1.5 * (i + 1))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text[:500]
                msg = body.get("message") if isinstance(body, dict) else None
                fa = STATUS_FA.get(r.status_code, f"خطای سرور لیارا ({r.status_code})")
                raise LiaraError(r.status_code, f"{fa}" + (f" — {msg}" if msg else ""), body, path)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}
        raise LiaraError(None, f"اتصال به API لیارا برقرار نشد ({type(last).__name__}). شبکه/VPN را بررسی کنید.",
                         endpoint=path)

    # ------------------------------------------------------------------ account
    async def me(self, fresh: bool = False) -> dict:
        if not fresh and self._me_cache and time.time() - self._me_cache[0] < 600:
            return self._me_cache[1]
        data = await self._request("GET", "v1/me")
        self._me_cache = (time.time(), data)
        return data

    async def billing(self) -> dict:
        return await self._request("GET", "v1/billing")

    async def teams(self) -> list[dict]:
        return (await self._request("GET", "v2/teams")).get("teams", [])

    # ------------------------------------------------------------------ apps
    async def list_apps(self) -> list[dict]:
        return (await self._request("GET", "v1/projects")).get("projects", [])

    async def get_app(self, name: str) -> dict:
        return (await self._request("GET", f"v1/projects/{name}")).get("project", {})

    async def create_app(self, name: str, plan_id: str, platform: str, bundle_plan_id: str,
                         network: str | None = None) -> dict:
        body = {"name": name, "planID": plan_id, "platform": platform, "bundlePlanID": bundle_plan_id}
        if network:
            body["network"] = network
        return await self._request("POST", "v1/projects/", json=body)

    async def networks(self) -> list[dict]:
        return (await self._request("GET", "v1/networks")).get("networks", [])

    async def create_network(self, name: str) -> dict:
        return await self._request("POST", "v1/networks", json={"networkName": name})

    async def delete_app(self, name: str) -> dict:
        return await self._request("DELETE", f"v1/projects/{name}")

    async def restart(self, name: str) -> dict:
        return await self._request("POST", f"v1/projects/{name}/actions/restart")

    async def scale(self, name: str, scale: int) -> dict:
        return await self._request("POST", f"v1/projects/{name}/actions/scale", json={"scale": scale})

    async def resize(self, name: str, plan_id: str, bundle_plan_id: str | None = None) -> dict:
        body: dict[str, Any] = {"planID": plan_id}
        if bundle_plan_id:
            body["bundlePlanID"] = bundle_plan_id
        return await self._request("POST", f"v1/projects/{name}/resize", json=body)

    async def update_envs(self, name: str, variables: list[dict]) -> dict:
        """Liara REPLACES the full env set, so callers must pass the merged list."""
        return await self._request("POST", "v1/projects/update-envs",
                                   json={"project": name, "variables": variables})

    async def set_zero_downtime(self, app_id: str, enable: bool) -> dict:
        return await self._request("POST", f"v1/projects/{app_id}/zero-downtime/{'enable' if enable else 'disable'}")

    # ------------------------------------------------------------------ observability
    async def logs(self, name: str, since_unix: int, last_lines: bool = False, release_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"start": since_unix, "direction": "forward", "last_lines": str(last_lines).lower()}
        if release_id:
            params["releaseId"] = release_id
        data = await self._request("GET", f"v2/projects/{name}/logs", params=params, timeout=25)
        lines: list[dict] = []
        for group in (data or {}).get("data", []) or []:
            rid = (group.get("metaData") or {}).get("releaseId")
            for ts, line in group.get("values") or []:
                stream = None
                # Liara wraps each line as JSON: {"type": "stderr", "_entry": "..."}
                if isinstance(line, str) and line.startswith("{"):
                    try:
                        obj = _json.loads(line)
                        stream, line = obj.get("type"), obj.get("_entry", line)
                    except ValueError:
                        pass
                lines.append({"ts": int(str(ts)[:13].ljust(13, "0")) / 1000, "line": str(line), "stream": stream, "release": rid})
        # The API does not strictly honour `start` → filter client-side so "after the fix" really means after.
        lines = [l for l in lines if l["ts"] >= since_unix]
        lines.sort(key=lambda x: x["ts"])
        return lines

    async def applets(self, name: str) -> list[dict]:
        return (await self._request("GET", f"v1/projects/{name}/applets")).get("applets", [])

    async def releases(self, name: str, count: int = 10) -> dict:
        return await self._request("GET", f"v1/projects/{name}/releases", params={"page": 1, "count": count})

    async def events(self, name: str, start: int | None = None, end: int | None = None) -> list:
        params = {k: v for k, v in {"start": start, "end": end}.items() if v}
        return (await self._request("GET", f"v1/projects/{name}/events", params=params)).get("data", [])

    async def metrics_summary(self, name: str) -> dict:
        return await self._request("GET", f"v1/projects/{name}/metrics/summary")

    async def metric(self, name: str, kind: str, since_unix: int) -> list[tuple[float, float]]:
        """kind: cpu | memory | network-receive | network-transmit → [(ts, value)] of the *current* container.

        Liara reports one series per container instance ("applet"); after a restart/deploy the old instance keeps
        reporting for a while, so summing would double-count. At each timestamp we take the newest instance.
        """
        data = await self._request("GET", f"v1/projects/{name}/metrics/{kind}", params={"since": since_unix})
        per: list[tuple[float, dict[float, float]]] = []
        for res in (data or {}).get("result", []) or []:
            pts: dict[float, float] = {}
            for ts, val in res.get("values") or []:
                try:
                    pts[float(ts)] = float(val)
                except (TypeError, ValueError):
                    continue
            if pts:
                per.append((min(pts), pts))
        per.sort(key=lambda x: x[0])  # oldest instance first → newer ones overwrite
        series: dict[float, float] = {}
        for _, pts in per:
            series.update(pts)
        return sorted(series.items())

    # ------------------------------------------------------------------ deploy
    async def upload_source(self, name: str, archive: Path) -> str:
        with archive.open("rb") as fh:
            data = await self._request("POST", f"v2/projects/{name}/sources",
                                       files={"file": (archive.name, fh, "application/gzip")}, timeout=300)
        return data["sourceID"]

    async def create_release(self, name: str, body: dict) -> str:
        data = await self._request("POST", f"v2/projects/{name}/releases", json=body, timeout=60)
        return data["releaseID"]

    async def build_logs(self, release_id: str, since: str | None = None) -> dict:
        params = {"since": since} if since else {}
        return await self._request("GET", f"v2/releases/{release_id}/build-logs", params=params, timeout=30)

    async def cancel_release(self, release_id: str) -> dict:
        return await self._request("POST", f"v2/releases/{release_id}/cancel")

    # ------------------------------------------------------------------ domains & DNS
    async def domains(self) -> list[dict]:
        return (await self._request("GET", "v1/domains")).get("domains", [])

    async def add_domain(self, domain: str, app: str) -> dict:
        return await self._request("POST", "v1/domains", json={"name": domain, "project": app, "type": "PROJECT"})

    async def check_domain(self, domain_id: str) -> dict:
        return await self._request("GET", f"v1/domains/{domain_id}/check")

    async def enable_ssl(self, domain: str) -> dict:
        return await self._request("POST", "v1/domains/provision-ssl-certs", json={"domain": domain})

    async def dns_zones(self) -> list[dict]:
        return (await self._request("GET", "api/v1/zones", base=config.LIARA_DNS_API)).get("data", [])

    async def create_zone(self, zone: str) -> dict:
        return (await self._request("POST", "api/v1/zones", base=config.LIARA_DNS_API, json={"name": zone})).get("data", {})

    async def get_zone(self, zone: str) -> dict:
        return (await self._request("GET", f"api/v1/zones/{zone}", base=config.LIARA_DNS_API)).get("data", {})

    async def check_zone_ns(self, zone: str) -> dict:
        return await self._request("PUT", f"api/v1/zones/{zone}/check", base=config.LIARA_DNS_API)

    async def dns_records(self, zone: str) -> list[dict]:
        return (await self._request("GET", f"api/v1/zones/{zone}/dns-records", base=config.LIARA_DNS_API)).get("data", [])

    async def create_dns_record(self, zone: str, record: dict) -> dict:
        return await self._request("POST", f"api/v1/zones/{zone}/dns-records", base=config.LIARA_DNS_API, json=record)

    # ------------------------------------------------------------------ helpers
    async def plans(self) -> dict:
        """Available Iran-region app plans + feature bundles, normalized."""
        me = await self.me()
        plans = me.get("plans", {})
        out = []
        for pid, p in (plans.get("projects") or {}).items():
            if not p.get("available") or p.get("region") != "iran":
                continue
            out.append({"id": pid, "ram_gb": p["RAM"]["amount"], "cpu": p["CPU"]["amount"],
                        "disk_gb": p.get("volume"), "monthly_toman": round(p.get("price", 0) * 720),
                        "g2": pid.endswith("-g2")})
        out.sort(key=lambda x: (not x["g2"], x["ram_gb"]))
        bundles = {}
        for fam, items in (plans.get("projectBundlePlans") or {}).items():
            bundles[fam] = {k: {"available": v.get("available"), "monthly_toman": v.get("displayPrice"),
                                "logs_days": v.get("maxLogsRetention"), "zero_downtime": v.get("zeroDowntime"),
                                "rollback": v.get("rollback"), "build_timeout_min": v.get("buildTimeout"),
                                "max_source_mb": v.get("maxSourceSize")}
                            for k, v in items.items()}
        return {"plans": out, "bundles": bundles}

    async def plan_limits(self, plan_id: str) -> dict | None:
        me = await self.me()
        p = (me.get("plans", {}).get("projects") or {}).get(plan_id)
        if not p:
            return None
        return {"ram_gb": p["RAM"]["amount"], "cpu": p["CPU"]["amount"], "disk_gb": p.get("volume"),
                "monthly_toman": round(p.get("price", 0) * 720)}


def app_url(name: str) -> str:
    return f"https://{name}.{config.APP_DOMAIN_SUFFIX}"


_client: Any = None


def client() -> Any:
    global _client
    if _client is None:
        if config.LIARA_MODE == "mock":
            from .mock_liara import MockLiara
            _client = MockLiara()
        else:
            _client = LiaraClient()
    return _client


def set_client(c: Any) -> None:
    global _client
    _client = c
