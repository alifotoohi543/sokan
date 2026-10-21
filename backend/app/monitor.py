"""Background watchdog: periodic health checks, alerts, and opt-in auto-remediation policies."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

from . import config, store
from . import diagnose as diag
from .deploy import probe
from .liara import LiaraError, client

log = logging.getLogger(__name__)
_fail_streak: dict[str, int] = defaultdict(int)
_last_auto: dict[str, float] = {}
listeners: set[asyncio.Queue] = set()
state: dict[str, Any] = {"last_run": None, "running": False, "error": None}

POLICIES = {
    "auto_restart_down": "اگر سایت ۳ بار پشت‌سرهم پاسخ نداد یا خطای 5xx داد، خودکار ری‌استارت شود (حداکثر هر ۳۰ دقیقه یک‌بار)",
    "auto_restart_ram": "اگر RAM بالای ۹۵٪ سقف پلن رفت، خودکار ری‌استارت شود (حداکثر هر ۳۰ دقیقه یک‌بار)",
}


def _broadcast(ev: dict) -> None:
    for q in list(listeners):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


async def check_app(app: dict) -> dict:
    name = app.get("project_id")
    lc = client()
    if app.get("scale") == 0:
        store.resolve_alerts(name)
        return {"app": name, "skipped": "stopped"}
    since = int(time.time() - 10 * 60)
    http, mem, cpu, logs = await asyncio.gather(
        probe(name), diag._safe(lc.metric(name, "memory", since), []), diag._safe(lc.metric(name, "cpu", since), []),
        diag._safe(lc.logs(name, int(time.time() - config.MONITOR_INTERVAL_S - 5)), []))
    limits = await diag._safe(lc.plan_limits(app.get("planID", "")), None) or {}
    mem = mem if isinstance(mem, list) else []
    cpu = cpu if isinstance(cpu, list) else []
    logs = logs if isinstance(logs, list) else []
    ram = diag._gb(mem[-1][1]) if mem else None
    cpu_now = cpu[-1][1] if cpu else None
    errors = sum(1 for l in logs if any(k in l["line"] for k in ("Error", "Exception", "Traceback", "FATAL", "Killed")))
    ok = bool(http.get("ok"))
    store.add_health(name, ok, http.get("status"), http.get("latency_s"), cpu_now, ram, limits.get("ram_gb"), errors)

    active: set[str] = set()

    def alert(kind: str, sev: str, title: str, detail: str):
        active.add(kind)
        if store.raise_alert(name, sev, kind, title, detail):
            _broadcast({"type": "alert", "app": name, "severity": sev, "title": title, "detail": detail})

    if not ok:
        _fail_streak[name] += 1
        alert("down", "critical", f"{name} پاسخ نمی‌دهد" if http.get("status") is None else f"{name} خطای {http.get('status')} می‌دهد",
              f"{http.get('url')} → {http.get('status') or http.get('error')}")
    else:
        _fail_streak[name] = 0
    if ok and http.get("latency_s", 0) > config.SLOW_RESPONSE_S:
        alert("slow", "warning", f"{name} کند شده است", f"زمان پاسخ {http['latency_s']} ثانیه")
    if ram and limits.get("ram_gb") and ram / limits["ram_gb"] >= config.RAM_WARN_RATIO:
        alert("ram", "warning", f"RAM برنامه‌ی {name} نزدیک سقف است", f"{ram*1024:.0f}MB از {limits['ram_gb']*1024:.0f}MB")
    if cpu_now and limits.get("cpu") and cpu_now / limits["cpu"] >= config.CPU_WARN_RATIO:
        alert("cpu", "warning", f"CPU برنامه‌ی {name} در سقف است", f"{cpu_now:.2f} از {limits['cpu']} هسته")
    if errors >= 5:
        alert("errors", "warning", f"افزایش خطا در لاگ‌های {name}", f"{errors} خطا در {int(config.MONITOR_INTERVAL_S)} ثانیه‌ی اخیر")
    store.resolve_alerts(name, keep=active)

    await _policies(name, ok, ram, limits)
    return {"app": name, "ok": ok, "status": http.get("status"), "latency": http.get("latency_s"), "ram_gb": ram,
            "cpu": cpu_now, "errors": errors}


async def _policies(name: str, ok: bool, ram: float | None, limits: dict) -> None:
    pol = store.policies(name)
    if time.time() - _last_auto.get(name, 0) < 1800:
        return
    reason = None
    if pol.get("auto_restart_down") and _fail_streak[name] >= 3:
        reason = "سایت ۳ بار متوالی پاسخ سالم نداد"
    elif pol.get("auto_restart_ram") and ram and limits.get("ram_gb") and ram / limits["ram_gb"] >= 0.95:
        reason = f"RAM به {ram / limits['ram_gb']:.0%} سقف رسید"
    if not reason:
        return
    _last_auto[name] = time.time()
    try:
        await client().restart(name)
        store.audit("policy", name, "restart_app", "sensitive", {"policy": True, "reason": reason}, "success")
        _broadcast({"type": "auto_action", "app": name, "title": f"ری‌استارت خودکار {name}", "detail": reason})
    except LiaraError as e:
        store.audit("policy", name, "restart_app", "sensitive", {"policy": True, "reason": reason}, "failed", e.to_dict())


async def run_once() -> list[dict]:
    apps = await client().list_apps()
    results = await asyncio.gather(*(check_app(a) for a in apps), return_exceptions=True)
    out = [r if isinstance(r, dict) else {"error": str(r)} for r in results]
    state["last_run"] = time.time()
    _broadcast({"type": "health", "results": out, "ts": state["last_run"]})
    return out


async def loop() -> None:
    state["running"] = True
    while True:
        try:
            await run_once()
            state["error"] = None
        except LiaraError as e:
            state["error"] = e.message
        except Exception as e:  # noqa: BLE001
            log.exception("monitor failed")
            state["error"] = str(e)
        await asyncio.sleep(config.MONITOR_INTERVAL_S)
