"""Tool registry shared by the chat agent, the REST API and the MCP server.

Every tool declares a risk level:
  read       – runs automatically (only reads data)
  sensitive  – needs an explicit user approval (restart, env change, deploy, plan change, ...)
  critical   – needs approval AND typing the app name (delete)
Sensitive tools are never executed by the LLM directly: ``request`` turns the call into an approval card
with a human-readable impact statement; ``execute`` runs only after approval and then verifies the result.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from . import analyzer, capabilities, config, deploy as deploy_mod, diagnose as diag, store
from .liara import LiaraError, app_url, client

READ, SENSITIVE, CRITICAL = "read", "sensitive", "critical"
RISK_FA = {READ: "خودکار (فقط خواندن)", SENSITIVE: "نیازمند تأیید", CRITICAL: "بحرانی — تأیید دومرحله‌ای"}


def _mask(v: str) -> str:
    v = str(v)
    return v if len(v) <= 4 else v[:2] + "•" * min(8, len(v) - 4) + v[-2:]


# ============================================================================ read tools
async def account_overview() -> dict:
    lc = client()
    apps, billing = await asyncio.gather(lc.list_apps(), lc.billing())
    team = billing.get("team") or {}
    bal = team.get("balance", billing.get("user", {}).get("balance"))
    return {"balance_toman": bal, "workspace": "team" if team else "personal",
            "apps": [{"name": a.get("project_id"), "type": a.get("type"), "status": a.get("status"),
                      "plan": a.get("planID"), "scale": a.get("scale"), "deployed": a.get("isDeployed")} for a in apps],
            "open_alerts": [{"app": a["app"], "severity": a["severity"], "title": a["title"]} for a in store.alerts()],
            "pending_approvals": len(store.pending_approvals())}


async def list_apps() -> dict:
    apps = await client().list_apps()
    return {"apps": [{"name": a.get("project_id"), "type": a.get("type"), "status": a.get("status"),
                      "plan": a.get("planID"), "scale": a.get("scale"), "deployed": a.get("isDeployed"),
                      "url": app_url(a.get("project_id", ""))} for a in apps]}


async def app_status(app: str) -> dict:
    lc = client()
    d, applets, rel, http = await asyncio.gather(lc.get_app(app), diag._safe(lc.applets(app), []),
                                                 diag._safe(lc.releases(app, 3), {}), deploy_mod.probe(app))
    limits = await diag._safe(lc.plan_limits(d.get("planID", "")), None) if d.get("planID") else None
    releases = (rel or {}).get("releases") or []
    return {"name": app, "url": app_url(app), "status": d.get("status"), "scale": d.get("scale"),
            "platform": d.get("type"), "plan": d.get("planID"), "bundle": d.get("bundlePlanID"), "limits": limits,
            "deployed": d.get("isDeployed"), "zero_downtime": d.get("zeroDowntime"),
            "hourly_price": d.get("hourlyPrice"),
            "env_keys": [e.get("key") for e in d.get("envs") or []],
            "containers": [{"state": a.get("state"), "reason": a.get("reason"), "release": (a.get("release") or {}).get("tag")}
                           for a in applets or []],
            "releases": [{"id": r.get("_id"), "tag": r.get("tag"), "state": r.get("state"), "created": r.get("createdAt"),
                          "port": r.get("port")} for r in releases],
            "http": http}


async def get_logs(app: str, minutes: int = 30, contains: str = "", limit: int = 60) -> dict:
    lines = await client().logs(app, int(time.time() - minutes * 60))
    if contains:
        lines = [l for l in lines if contains.lower() in l["line"].lower()]
    errs = [l for l in lines if any(k in l["line"].lower() for k in ("error", "exception", "traceback", "fatal", "killed"))]
    return {"app": app, "minutes": minutes, "total": len(lines), "error_like": len(errs),
            "lines": [{"ts": l["ts"], "line": l["line"][:400]} for l in lines[-limit:]]}


async def get_metrics(app: str, minutes: int = 60) -> dict:
    lc = client()
    since = int(time.time() - minutes * 60)
    d = await lc.get_app(app)
    cpu, mem, rx, tx = await asyncio.gather(lc.metric(app, "cpu", since), lc.metric(app, "memory", since),
                                            diag._safe(lc.metric(app, "network-receive", since), []),
                                            diag._safe(lc.metric(app, "network-transmit", since), []))
    limits = await lc.plan_limits(d.get("planID", "")) or {}
    mem_gb = [(t, diag._gb(v)) for t, v in mem]
    cs, ms = diag._series_stats(cpu), diag._series_stats(mem_gb)
    return {"app": app, "minutes": minutes, "limits": limits,
            "cpu": {"avg": round(cs["avg"], 3), "max": round(cs["max"], 3),
                    "pct_of_limit": round(100 * cs["max"] / limits["cpu"]) if limits.get("cpu") else None} if cs else None,
            "ram": {"last_mb": round(ms["last"] * 1024), "max_mb": round(ms["max"] * 1024),
                    "pct_of_limit": round(100 * ms["max"] / limits["ram_gb"]) if limits.get("ram_gb") else None,
                    "trend": "rising" if ms["last_third_avg"] > ms["first_third_avg"] * 1.2 else "stable"} if ms else None,
            "series": {"cpu": cpu[-90:], "mem_mb": [(t, round(v * 1024, 1)) for t, v in mem_gb[-90:]],
                       "net_rx": rx[-90:] if isinstance(rx, list) else [], "net_tx": tx[-90:] if isinstance(tx, list) else []}}


async def diagnose(app: str, minutes: int = 60) -> dict:
    return await diag.diagnose(app, minutes)


async def list_plans() -> dict:
    return await client().plans()


async def analyze_project(path: str, traffic: str = "low") -> dict:
    a = analyzer.analyze(path)
    if not a.get("ok"):
        return a
    p = await client().plans()
    a["recommendation"] = analyzer.recommend_plan(a, p["plans"], p["bundles"], traffic)
    return a


async def list_releases(app: str) -> dict:
    rel = await client().releases(app, 10)
    ours = {d["release_id"]: d for d in store.deployments(app)}
    return {"current": rel.get("currentRelease"),
            "releases": [{"id": r.get("_id"), "tag": r.get("tag"), "state": r.get("state"), "created": r.get("createdAt"),
                          "rollback_possible": r.get("_id") in ours and bool(ours[r.get("_id")].get("source_id"))}
                         for r in rel.get("releases") or []]}


async def domain_status(domain: str = "") -> dict:
    doms = await client().domains()
    out = []
    for d in doms:
        if domain and d.get("name") != domain:
            continue
        out.append({"id": d.get("_id"), "name": d.get("name"), "app": (d.get("project") or {}).get("project_id"),
                    "status": d.get("status"), "ssl": d.get("certificatesStatus"), "cname_target": d.get("CNameRecord")})
    return {"domains": out}


async def api_capabilities() -> dict:
    return capabilities.CAPABILITIES


# ============================================================================ sensitive executors
async def _wait_running(app: str, timeout: float = 150) -> dict:
    """Poll until the site answers healthily or the timeout expires; return the last probe.

    After a restart/env change Liara rolls containers: the OLD instance keeps serving (e.g. its 500s) for a while,
    so the first non-5xx answer — not the first answer — is what proves the change worked.
    """
    t0 = time.time()
    pr: dict = {}
    while time.time() - t0 < timeout:
        await asyncio.sleep(config.POLL_S)
        pr = await deploy_mod.probe(app)
        if pr.get("ok"):
            break
    pr["waited_s"] = round(time.time() - t0)
    return pr


async def _ram_mb(app: str) -> float | None:
    pts = await diag._safe(client().metric(app, "memory", int(time.time() - 300)), [])
    return round(diag._gb(pts[-1][1]) * 1024) if isinstance(pts, list) and pts else None


async def ex_restart(app: str) -> AsyncIterator[dict]:
    before = await _ram_mb(app)
    yield {"type": "progress", "label": "ارسال دستور ری‌استارت به لیارا" + (f" (RAM فعلی: {before}MB)" if before else "")}
    await client().restart(app)
    yield {"type": "progress", "label": "منتظر بالا آمدن کانتینر..."}
    pr = await _wait_running(app)
    after = None
    if before:
        yield {"type": "progress", "label": "اندازه‌گیری مصرف RAM بعد از ری‌استارت"}
        t0 = time.time()
        while time.time() - t0 < 75:
            after = await _ram_mb(app)
            if after is not None and after < before * 0.7:
                break
            await asyncio.sleep(max(config.POLL_S, 0.01) * 2)
    yield {"type": "done", "ok": bool(pr.get("ok")), "probe": pr, "ram_before_mb": before, "ram_after_mb": after}


async def ex_scale(app: str, on: bool) -> AsyncIterator[dict]:
    yield {"type": "progress", "label": "روشن کردن برنامه" if on else "خاموش کردن برنامه"}
    await client().scale(app, 1 if on else 0)
    if on:
        pr = await _wait_running(app)
        yield {"type": "done", "ok": bool(pr.get("ok")), "probe": pr}
    else:
        yield {"type": "done", "ok": True}


async def ex_resize(app: str, plan_id: str, bundle_plan_id: str | None = None) -> AsyncIterator[dict]:
    yield {"type": "progress", "label": f"تغییر پلن به {plan_id}"}
    await client().resize(app, plan_id, bundle_plan_id)
    pr = await _wait_running(app, 180)
    d = await client().get_app(app)
    yield {"type": "done", "ok": bool(pr.get("ok")) and d.get("planID") == plan_id, "probe": pr, "plan_now": d.get("planID")}


async def ex_set_env(app: str, variables: dict[str, str], remove: list[str] | None = None) -> AsyncIterator[dict]:
    lc = client()
    d = await lc.get_app(app)
    merged = {e["key"]: e.get("value", "") for e in d.get("envs") or []}
    merged.update({k: str(v) for k, v in variables.items()})
    for k in remove or []:
        merged.pop(k, None)
    yield {"type": "progress", "label": f"به‌روزرسانی {len(variables)} متغیر (بقیه‌ی {len(merged) - len(variables)} متغیر حفظ می‌شوند)"}
    started = time.time()
    await lc.update_envs(app, [{"key": k, "value": v} for k, v in merged.items()])
    yield {"type": "progress", "label": "لیارا برنامه را با تنظیمات جدید ری‌استارت می‌کند..."}
    pr = await _wait_running(app, 150)
    healthy_at = time.time()
    # verify: the missing-variable error must not re-appear once the new container serves. Errors logged by the
    # old container during the rollover are expected, so only lines after the first healthy answer count.
    yield {"type": "progress", "label": "بررسی لاگ‌های جدید برای تکرار خطا"}
    if pr.get("ok"):
        await deploy_mod.probe(app)  # one more real request against the new container
    await asyncio.sleep(max(config.POLL_S, 0.01) * 2)
    new_logs = await diag._safe(lc.logs(app, int(healthy_at if pr.get("ok") else started)), [])
    still = [l["line"][:200] for l in new_logs if any(k in l["line"] for k in variables)
             and any(w in l["line"] for w in ("KeyError", "not set", "undefined", "missing", "Missing"))]
    yield {"type": "done", "ok": bool(pr.get("ok")) and not still, "probe": pr, "errors_after": still[:3]}


DEFAULT_NETWORK = "sokan-net"


async def ex_create_app(name: str, plan_id: str, bundle_plan_id: str, platform: str) -> AsyncIterator[dict]:
    lc = client()
    # New-infrastructure (g2) apps must join a private network; first-time accounts have none.
    nets = await lc.networks()
    if not nets:
        yield {"type": "progress", "label": f"ساخت شبکه‌ی خصوصی «{DEFAULT_NETWORK}» (پیش‌نیاز برنامه‌های نسل ۲، رایگان)"}
        await lc.create_network(DEFAULT_NETWORK)
        nets = await lc.networks()
    net = nets[0] if nets else {}
    yield {"type": "progress", "label": f"ساخت برنامه‌ی {name} روی پلن {plan_id} در شبکه‌ی {net.get('name', '—')}"}
    await lc.create_app(name, plan_id, platform, bundle_plan_id, network=net.get("_id"))
    d = await client().get_app(name)
    yield {"type": "done", "ok": bool(d), "app": name, "url": app_url(name)}


async def ex_deploy(app: str, path: str, port: int, platform: str, message: str = "") -> AsyncIterator[dict]:
    async for ev in deploy_mod.deploy(app, path=path, port=port, platform=platform, message=message):
        yield ev


async def ex_rollback(app: str, release_id: str | None = None) -> AsyncIterator[dict]:
    deps = [d for d in store.deployments(app) if d.get("source_id")]
    rel = await client().releases(app, 10)
    current = rel.get("currentRelease")
    target = None
    if release_id:
        target = next((d for d in deps if d["release_id"] == release_id), None)
    else:
        target = next((d for d in deps if d["state"] == "READY" and d["release_id"] != current), None)
    if not target:
        yield {"type": "done", "ok": False, "error": "نسخه‌ی سالم قبلی با سورس ذخیره‌شده پیدا نشد"}
        return
    yield {"type": "progress", "label": f"بازگشت به نسخه‌ی {target['release_id'][:8]} ({time.strftime('%H:%M', time.localtime(target['ts']))})"}
    async for ev in deploy_mod.deploy(app, source_id=target["source_id"], port=target["port"], platform=target["platform"],
                                      message=f"rollback to {target['release_id'][:8]}"):
        yield ev


async def ex_add_domain(app: str, domain: str) -> AsyncIterator[dict]:
    lc = client()
    yield {"type": "progress", "label": f"ثبت دامنه‌ی {domain} روی برنامه‌ی {app}"}
    await lc.add_domain(domain, app)
    doms = await lc.domains()
    d = next((x for x in doms if x.get("name") == domain), {})
    yield {"type": "done", "ok": bool(d), "domain": domain, "cname_target": d.get("CNameRecord"),
           "guide": capabilities.domain_guide(domain, d.get("CNameRecord"))}


async def ex_delete_app(app: str) -> AsyncIterator[dict]:
    yield {"type": "progress", "label": f"حذف برنامه‌ی {app}"}
    await client().delete_app(app)
    yield {"type": "done", "ok": True}


# ============================================================================ impact statements
async def _impact(action: str, p: dict) -> dict:
    lc = client()
    app = p.get("app")
    if action == "restart_app":
        return {"what": f"برنامه‌ی {app} ری‌استارت می‌شود.", "downtime": "حدود ۱۰ تا ۳۰ ثانیه قطعی (اگر zero-downtime فعال نباشد)",
                "cost": "بدون تغییر هزینه", "reversible": "ندارد ولی بی‌خطر است", "why": p.get("reason", "")}
    if action in ("start_app", "stop_app"):
        on = action == "start_app"
        return {"what": f"برنامه‌ی {app} {'روشن' if on else 'خاموش'} می‌شود.",
                "downtime": "—" if on else "سایت تا روشن کردن مجدد در دسترس نخواهد بود",
                "cost": "هزینه‌ی ساعتی محاسبه می‌شود" if on else "هزینه‌ی ساعتی پلن متوقف می‌شود",
                "reversible": "بله", "why": p.get("reason", "")}
    if action == "resize_app":
        d = await lc.get_app(app)
        cur = await lc.plan_limits(d.get("planID", "")) or {}
        new = await lc.plan_limits(p["plan_id"]) or {}
        return {"what": f"پلن {app} از {d.get('planID')} ({cur.get('ram_gb')}GB RAM / {cur.get('cpu')} CPU) به {p['plan_id']} "
                        f"({new.get('ram_gb')}GB RAM / {new.get('cpu')} CPU) تغییر می‌کند.",
                "downtime": "ری‌استارت کوتاه هنگام اعمال پلن", "reversible": "بله (می‌توان دوباره پلن را تغییر داد)",
                "cost": f"هزینه‌ی ماهانه‌ی پلن: {cur.get('monthly_toman', 0):,} → {new.get('monthly_toman', 0):,} تومان",
                "why": p.get("reason", "")}
    if action == "set_env":
        d = await lc.get_app(app)
        existing = {e["key"] for e in d.get("envs") or []}
        changes = [{"key": k, "value": _mask(v), "op": "تغییر" if k in existing else "افزودن"} for k, v in p["variables"].items()]
        return {"what": f"{len(changes)} متغیر محیطی روی {app} تنظیم می‌شود؛ {len(existing - set(p['variables']))} متغیر دیگر دست‌نخورده می‌ماند.",
                "changes": changes, "downtime": "لیارا برنامه را ری‌استارت می‌کند (چند ثانیه)",
                "cost": "بدون تغییر هزینه", "reversible": "بله", "why": p.get("reason", "")}
    if action == "create_app":
        new = await lc.plan_limits(p["plan_id"]) or {}
        pl = await lc.plans()
        bundle = (pl["bundles"].get(p["plan_id"]) or {}).get(p["bundle_plan_id"], {})
        total = new.get("monthly_toman", 0) + (bundle.get("monthly_toman") or 0)
        nets = await lc.networks()
        net_note = "" if nets else f" چون هنوز شبکه‌ی خصوصی ندارید، شبکه‌ی رایگان «{DEFAULT_NETWORK}» هم ساخته می‌شود."
        return {"what": f"برنامه‌ی جدید «{p['name']}» با پلتفرم {p['platform']} روی پلن {p['plan_id']} ساخته می‌شود.{net_note}",
                "cost": f"حدود {total:,} تومان در ماه (~{round(total / 30):,} تومان در روز) — ساعتی از اعتبار کسر می‌شود",
                "downtime": "—", "reversible": "بله (حذف برنامه)", "why": p.get("reason", "")}
    if action == "deploy":
        a = analyzer.analyze(p["path"])
        return {"what": f"سورس «{p['path']}» بسته‌بندی و به‌عنوان نسخه‌ی جدید {app} منتشر می‌شود (پلتفرم {p['platform']}، پورت {p['port']}).",
                "downtime": "با zero-downtime: بدون قطعی؛ در غیر این صورت چند ثانیه",
                "cost": "بدون تغییر هزینه‌ی پلن", "reversible": "بله — سورس نسخه‌ی قبل نگه داشته می‌شود و rollback ممکن است",
                "warnings": a.get("warnings", []), "why": p.get("reason", "")}
    if action == "rollback":
        return {"what": f"{app} به آخرین نسخه‌ی سالم قبلی برمی‌گردد (بازانتشار سورس ذخیره‌شده).",
                "downtime": "مثل یک دیپلوی معمولی", "cost": "بدون تغییر", "reversible": "بله", "why": p.get("reason", "")}
    if action == "add_domain":
        return {"what": f"دامنه‌ی {p['domain']} به برنامه‌ی {app} متصل می‌شود.", "cost": "رایگان (SSL رایگان)",
                "downtime": "—", "reversible": "بله", "manual_steps": "بعد از ثبت، باید رکورد DNS را در پنل ثبت دامنه تنظیم کنید (راهنما نمایش داده می‌شود)",
                "why": p.get("reason", "")}
    if action == "delete_app":
        return {"what": f"برنامه‌ی {app} و همه‌ی نسخه‌ها و دیسک‌هایش برای همیشه حذف می‌شود.",
                "downtime": "دائمی", "cost": "هزینه متوقف می‌شود", "reversible": "خیر — غیرقابل بازگشت",
                "confirm_text": app, "why": p.get("reason", "")}
    return {"what": action}


# ============================================================================ registry
ToolFn = Callable[..., Awaitable[dict]]
ExecFn = Callable[..., AsyncIterator[dict]]

TOOLS: dict[str, dict[str, Any]] = {
    # ---------------- read
    "account_overview": {"risk": READ, "fn": account_overview, "label": "بررسی حساب و برنامه‌ها",
                         "desc": "Balance, list of apps with status, open alerts. Use first when the user is vague.",
                         "params": {}},
    "list_apps": {"risk": READ, "fn": list_apps, "label": "دریافت فهرست برنامه‌ها", "desc": "List all apps.", "params": {}},
    "app_status": {"risk": READ, "fn": app_status, "label": "بررسی وضعیت برنامه",
                   "desc": "Status, plan & limits, containers, last releases, env var NAMES, live HTTP probe of the site.",
                   "params": {"app": {"type": "string"}}, "required": ["app"]},
    "get_logs": {"risk": READ, "fn": get_logs, "label": "دریافت لاگ‌ها",
                 "desc": "Recent runtime logs. Optional case-insensitive substring filter.",
                 "params": {"app": {"type": "string"}, "minutes": {"type": "integer", "default": 30},
                            "contains": {"type": "string"}}, "required": ["app"]},
    "get_metrics": {"risk": READ, "fn": get_metrics, "label": "بررسی مصرف CPU و RAM",
                    "desc": "CPU and RAM usage compared to the plan limits, with trend.",
                    "params": {"app": {"type": "string"}, "minutes": {"type": "integer", "default": 60}}, "required": ["app"]},
    "diagnose": {"risk": READ, "fn": diagnose, "label": "عیب‌یابی خودکار (لاگ + منابع + وضعیت)",
                 "desc": "Full troubleshooting: collects status, containers, releases, logs, CPU/RAM vs plan, HTTP probe and returns "
                         "ranked root-cause hypotheses with evidence and suggested fixes. Use for ANY problem report (500, slow, down).",
                 "params": {"app": {"type": "string"}, "minutes": {"type": "integer", "default": 60}}, "required": ["app"]},
    "list_plans": {"risk": READ, "fn": list_plans, "label": "دریافت پلن‌ها و قیمت‌ها", "desc": "Available Liara plans with prices.", "params": {}},
    "analyze_project": {"risk": READ, "fn": analyze_project, "label": "تحلیل پروژه و پیشنهاد پلن",
                        "desc": "Inspect a local project folder: platform, port, required env vars, risks, and recommend the cheapest fitting plan.",
                        "params": {"path": {"type": "string"}, "traffic": {"type": "string", "enum": ["low", "medium", "high"]}},
                        "required": ["path"]},
    "list_releases": {"risk": READ, "fn": list_releases, "label": "تاریخچه‌ی نسخه‌ها", "desc": "Release history.",
                      "params": {"app": {"type": "string"}}, "required": ["app"]},
    "domain_status": {"risk": READ, "fn": domain_status, "label": "وضعیت دامنه‌ها", "desc": "Domains, SSL and DNS target.",
                      "params": {"domain": {"type": "string"}}},
    "api_capabilities": {"risk": READ, "fn": api_capabilities, "label": "محدودیت‌های API لیارا",
                         "desc": "What this assistant can / cannot do through the Liara API. Use when the user asks for something unsupported.",
                         "params": {}},
    # ---------------- sensitive
    "restart_app": {"risk": SENSITIVE, "exec": ex_restart, "label": "ری‌استارت برنامه", "desc": "Restart the app.",
                    "params": {"app": {"type": "string"}, "reason": {"type": "string"}}, "required": ["app", "reason"]},
    "start_app": {"risk": SENSITIVE, "exec": lambda app, **_: ex_scale(app, True), "label": "روشن کردن برنامه",
                  "desc": "Turn the app on.", "params": {"app": {"type": "string"}, "reason": {"type": "string"}}, "required": ["app"]},
    "stop_app": {"risk": SENSITIVE, "exec": lambda app, **_: ex_scale(app, False), "label": "خاموش کردن برنامه",
                 "desc": "Turn the app off.", "params": {"app": {"type": "string"}, "reason": {"type": "string"}}, "required": ["app"]},
    "resize_app": {"risk": SENSITIVE, "exec": ex_resize, "label": "تغییر پلن منابع",
                   "desc": "Change the app plan (RAM/CPU). Use list_plans to pick plan_id (prefer *-g2).",
                   "params": {"app": {"type": "string"}, "plan_id": {"type": "string"}, "reason": {"type": "string"}},
                   "required": ["app", "plan_id", "reason"]},
    "set_env": {"risk": SENSITIVE, "exec": ex_set_env, "label": "تنظیم متغیرهای محیطی",
                "desc": "Add/update environment variables (other variables are preserved). Never invent secret values — ask the user.",
                "params": {"app": {"type": "string"}, "variables": {"type": "object", "additionalProperties": {"type": "string"}},
                           "reason": {"type": "string"}}, "required": ["app", "variables"]},
    "create_app": {"risk": SENSITIVE, "exec": ex_create_app, "label": "ساخت برنامه‌ی جدید",
                   "desc": "Create a new app (costs credit). Use analyze_project first to pick plan & platform.",
                   "params": {"name": {"type": "string"}, "plan_id": {"type": "string"}, "bundle_plan_id": {"type": "string"},
                              "platform": {"type": "string"}, "reason": {"type": "string"}},
                   "required": ["name", "plan_id", "bundle_plan_id", "platform"]},
    "deploy": {"risk": SENSITIVE, "exec": ex_deploy, "label": "انتشار نسخه‌ی جدید",
               "desc": "Package a local folder and deploy it as a new release, streaming build logs and verifying the site.",
               "params": {"app": {"type": "string"}, "path": {"type": "string"}, "port": {"type": "integer"},
                          "platform": {"type": "string"}, "message": {"type": "string"}, "reason": {"type": "string"}},
               "required": ["app", "path", "port", "platform"]},
    "rollback": {"risk": SENSITIVE, "exec": ex_rollback, "label": "بازگشت به نسخه‌ی قبل",
                 "desc": "Redeploy the last healthy release deployed through this assistant.",
                 "params": {"app": {"type": "string"}, "release_id": {"type": "string"}, "reason": {"type": "string"}},
                 "required": ["app"]},
    "add_domain": {"risk": SENSITIVE, "exec": ex_add_domain, "label": "اتصال دامنه",
                   "desc": "Attach a custom domain to an app and return the DNS steps the user must do at the registrar.",
                   "params": {"app": {"type": "string"}, "domain": {"type": "string"}, "reason": {"type": "string"}},
                   "required": ["app", "domain"]},
    "delete_app": {"risk": CRITICAL, "exec": ex_delete_app, "label": "حذف برنامه",
                   "desc": "Permanently delete an app. Only when the user explicitly asks.",
                   "params": {"app": {"type": "string"}, "reason": {"type": "string"}}, "required": ["app"]},
}


def openai_tools() -> list[dict]:
    out = []
    for name, t in TOOLS.items():
        desc = t["desc"] + ("" if t["risk"] == READ else " [REQUIRES USER APPROVAL: calling it creates an approval card; it does NOT run immediately.]")
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": t.get("params", {}), "required": t.get("required", [])}}})
    return out


async def run_read(name: str, args: dict, actor: str = "agent") -> dict:
    t = TOOLS[name]
    assert t["risk"] == READ
    try:
        res = await t["fn"](**args)
        store.audit(actor, args.get("app"), name, READ, args, "ok")
        return res
    except LiaraError as e:
        store.audit(actor, args.get("app"), name, READ, args, "error", e.to_dict())
        return {"error": e.message, "detail": e.to_dict()}
    except TypeError as e:
        return {"error": f"پارامتر نامعتبر: {e}"}


async def request(name: str, args: dict, session: str = "", actor: str = "agent") -> dict:
    """Turn a sensitive call into a pending approval with an impact statement."""
    t = TOOLS[name]
    try:
        impact = await _impact(name, args)
    except LiaraError as e:
        return {"error": e.message}
    except KeyError as e:
        return {"error": f"پارامتر لازم ارسال نشده: {e}"}
    title = t["label"] + (f" — {args['app']}" if args.get("app") else f" — {args.get('name', '')}")
    appr = store.create_approval(session, args.get("app") or args.get("name"), name, args, t["risk"], title, impact)
    store.audit(actor, args.get("app"), name, t["risk"], {k: (_mask(v) if k == "variables" else v) for k, v in args.items()},
                "approval_requested", approval_id=appr["id"])
    return {"status": "pending_approval", "approval_id": appr["id"], "title": title, "risk": t["risk"], "impact": impact}


async def execute(approval_id: str, actor: str = "user") -> AsyncIterator[dict]:
    appr = store.get_approval(approval_id)
    if not appr or appr["status"] != "pending":
        yield {"type": "done", "ok": False, "error": "این درخواست تأیید معتبر نیست یا قبلاً بررسی شده است"}
        return
    store.decide_approval(approval_id, "running")
    t = TOOLS[appr["action"]]
    params = {k: v for k, v in appr["params"].items() if k != "reason"}
    final: dict = {"type": "done", "ok": False}
    try:
        async for ev in t["exec"](**params):
            if ev.get("type") == "done":
                final = ev
            yield ev
    except LiaraError as e:
        final = {"type": "done", "ok": False, "error": e.message, "detail": e.to_dict()}
        yield final
    except Exception as e:  # noqa: BLE001
        final = {"type": "done", "ok": False, "error": str(e)}
        yield final
    store.decide_approval(approval_id, "executed" if final.get("ok") else "failed", final)
    safe_params = {k: (_mask(v) if k == "variables" else v) for k, v in appr["params"].items()}
    store.audit(actor, appr["app"], appr["action"], appr["risk"], safe_params,
                "success" if final.get("ok") else "failed", final, approval_id)


def reject(approval_id: str, actor: str = "user") -> dict:
    appr = store.get_approval(approval_id)
    if not appr or appr["status"] != "pending":
        return {"ok": False}
    store.decide_approval(approval_id, "rejected")
    store.audit(actor, appr["app"], appr["action"], appr["risk"], {}, "rejected", approval_id=approval_id)
    return {"ok": True}
