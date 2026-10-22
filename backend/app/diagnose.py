"""Evidence-based troubleshooting.

1. ``collect`` gathers every signal the Liara API exposes for an app (status, applets, releases, logs,
   CPU/RAM vs. plan limits) plus an external HTTP probe.
2. ``analyze`` runs a deterministic rule library over that evidence and produces ranked hypotheses,
   each with the concrete evidence lines and a fix that maps to an executable action.

The LLM only explains/prioritises on top of this; diagnosis works with zero LLM budget.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any

from . import config
from .deploy import probe
from .liara import LiaraError, client

# ----------------------------------------------------------------------------- log rule library
# (id, regex, category, weight). Weight ~ how strongly one match points to the cause.
LOG_RULES: list[tuple[str, re.Pattern, str, float]] = [
    ("oom", re.compile(r"JavaScript heap out of memory|MemoryError|OOMKilled|Cannot allocate memory|out of memory|"
                       r"exited with code 137|signal: killed|\bKilled\b", re.I), "memory", 0.6),
    ("missing_env", re.compile(r"KeyError: ['\"]([A-Z][A-Z0-9_]{2,})['\"]|environment variable ['\"]?([A-Z][A-Z0-9_]{2,})['\"]? "
                               r"(?:is )?(?:not set|missing|required|undefined)|Missing (?:required )?env(?:ironment)? (?:var(?:iable)?)?:? ?['\"]?([A-Z][A-Z0-9_]{2,})|"
                               r"process\.env\.([A-Z][A-Z0-9_]{2,}) is undefined|([A-Z][A-Z0-9_]{2,}) (?:env )?is not defined", re.I), "config", 0.7),
    ("db_conn", re.compile(r"ECONNREFUSED|could not connect to server|Connection refused|OperationalError|"
                           r"getaddrinfo ENOTFOUND|SequelizeConnection|MongoNetworkError|Can't connect to MySQL|"
                           r"connection to server at .* failed|too many connections|P1001", re.I), "dependency", 0.55),
    ("module_missing", re.compile(r"ModuleNotFoundError: No module named ['\"]([\w\.]+)['\"]|Cannot find module ['\"]([^'\"]+)['\"]|"
                                  r"ImportError: ([^\n]{0,80})", re.I), "code", 0.75),
    ("port_bind", re.compile(r"(?:listening|running|started|serving)\b.{0,40}?(?:127\.0\.0\.1|localhost)|"
                             r"EADDRINUSE|address already in use", re.I), "config", 0.5),
    ("disk_full", re.compile(r"ENOSPC|No space left on device|disk quota exceeded", re.I), "disk", 0.8),
    ("readonly_fs", re.compile(r"Read-only file system|EROFS", re.I), "config", 0.8),
    ("timeout_upstream", re.compile(r"ETIMEDOUT|ReadTimeout|ConnectTimeout|timed out|504 Gateway|upstream timed out", re.I), "dependency", 0.35),
    ("exception", re.compile(r"Traceback \(most recent call last\)|Unhandled(?:Promise)?Rejection|"
                             r"^\s*(?:\w+\.)*\w*(?:Error|Exception):|panic:|Fatal error|Internal Server Error", re.I | re.M), "code", 0.3),
    ("http_5xx", re.compile(r"\"\s*(?:GET|POST|PUT|PATCH|DELETE|HEAD)\s[^\"]*\"\s+(5\d\d)\b|\b(?:status|code)[=: ]+(5\d\d)\b|"
                            r"\s(5\d\d)\s+(?:GET|POST|PUT|DELETE|PATCH)\s|(?:GET|POST|PUT|DELETE|PATCH)\s+\S+\s+(5\d\d)\b"), "symptom", 0.2),
    ("slow_request", re.compile(r"slow (?:query|request)|took (\d{4,})\s?ms|duration[=: ](\d{4,})ms", re.I), "performance", 0.3),
]

FA_TITLES = {
    "oom": "کمبود حافظه (RAM) / نشت حافظه",
    "missing_env": "متغیر محیطی لازم تنظیم نشده است",
    "db_conn": "اتصال به دیتابیس یا سرویس وابسته برقرار نمی‌شود",
    "module_missing": "وابستگی (پکیج) در ایمیج نصب نشده است",
    "port_bind": "برنامه روی پورت/آدرس اشتباه گوش می‌دهد",
    "disk_full": "فضای دیسک پر شده است",
    "readonly_fs": "نوشتن روی فایل‌سیستم فقط‌خواندنی",
    "timeout_upstream": "تایم‌اوت در فراخوانی سرویس بیرونی",
    "exception": "خطای کد (Exception) در برنامه",
    "cpu_saturation": "پردازنده (CPU) به سقف پلن رسیده است",
    "memory_pressure": "مصرف RAM نزدیک سقف پلن است",
    "memory_leak": "روند صعودی مداوم حافظه (الگوی نشت حافظه)",
    "app_stopped": "برنامه خاموش است",
    "not_deployed": "هنوز هیچ نسخه‌ای دیپلوی نشده است",
    "release_failed": "آخرین دیپلوی ناموفق بوده است",
    "crash_loop": "کانتینر مدام کرش و ری‌استارت می‌شود",
    "slow_response": "پاسخ‌دهی کند سایت",
    "unreachable": "سایت از بیرون در دسترس نیست",
    "starting": "برنامه هنوز در حال راه‌اندازی است",
    "slow_request": "درخواست‌های کند در لاگ برنامه",
}


def _gb(v: float) -> float:
    """Liara reports memory in bytes; tolerate MB just in case."""
    if v > 1e6:
        return v / 1024 ** 3
    if v > 64:
        return v / 1024
    return v


def _series_stats(series: list[tuple[float, float]]) -> dict | None:
    if not series:
        return None
    vals = [v for _, v in series]
    n = len(vals)
    third = max(1, n // 3)
    return {"last": vals[-1], "max": max(vals), "avg": sum(vals) / n,
            "first_third_avg": sum(vals[:third]) / third, "last_third_avg": sum(vals[-third:]) / third, "points": n}


async def _safe(coro, default=None):
    try:
        return await coro
    except LiaraError as e:
        return {"__error__": e.to_dict()} if default is None else default
    except Exception as e:  # noqa: BLE001 - evidence collection must never crash diagnosis
        return {"__error__": {"message": str(e)}} if default is None else default


async def collect(app: str, minutes: int = 60) -> dict[str, Any]:
    lc = client()
    since = int(time.time() - minutes * 60)
    details, applets, releases, logs, cpu, mem, http = await asyncio.gather(
        _safe(lc.get_app(app)), _safe(lc.applets(app), []), _safe(lc.releases(app, 5), {}),
        _safe(lc.logs(app, since), []), _safe(lc.metric(app, "cpu", since), []),
        _safe(lc.metric(app, "memory", since), []), probe(app))
    limits = None
    if isinstance(details, dict) and details.get("planID"):
        limits = await _safe(lc.plan_limits(details["planID"]), None)
    mem_gb = [(t, _gb(v)) for t, v in (mem if isinstance(mem, list) else [])]
    return {"app": app, "window_min": minutes, "details": details, "applets": applets, "releases": releases,
            "logs": logs if isinstance(logs, list) else [], "cpu": cpu if isinstance(cpu, list) else [],
            "mem_gb": mem_gb, "limits": limits, "http": http, "collected_at": time.time()}


def _fix(action: str, label: str, params: dict | None = None, kind: str = "action") -> dict:
    return {"action": action, "label": label, "params": params or {}, "kind": kind}


def analyze(ev: dict[str, Any]) -> dict[str, Any]:
    app = ev["app"]
    hyps: dict[str, dict] = {}

    def add(hid: str, score: float, evidence: str, fixes: list[dict] | None = None, extra: dict | None = None):
        h = hyps.setdefault(hid, {"id": hid, "title": FA_TITLES.get(hid, hid), "score": 0.0, "evidence": [],
                                  "fixes": [], "extra": {}})
        h["score"] = min(0.99, h["score"] + score * (1 - h["score"]))  # noisy-OR accumulation
        if evidence and evidence not in h["evidence"] and len(h["evidence"]) < 6:
            h["evidence"].append(evidence)
        for f in fixes or []:
            if f not in h["fixes"]:
                h["fixes"].append(f)
        if extra:
            h["extra"].update(extra)

    details = ev.get("details") or {}
    if isinstance(details, dict) and "__error__" in details:
        return {"app": app, "error": details["__error__"], "hypotheses": [], "summary": {}}

    # --- platform state ----------------------------------------------------------------
    if details.get("scale") == 0 or str(details.get("status", "")).upper() in ("STOPPED", "OFF"):
        add("app_stopped", 0.95, f"وضعیت برنامه در لیارا: {details.get('status')} (scale={details.get('scale')})",
            [_fix("start_app", "روشن کردن برنامه")])
    if details.get("isDeployed") is False:
        add("not_deployed", 0.95, "فیلد isDeployed در API برابر false است", [_fix("deploy", "دیپلوی اولین نسخه", kind="guide")])

    rel = (ev.get("releases") or {}).get("releases") or []
    if rel:
        last = rel[0]
        if last.get("state") in ("FAILED", "UNHEALTHY", "TIMEDOUT"):
            add("release_failed", 0.8, f"آخرین release ({last.get('tag') or last.get('_id', '')[:8]}) در وضعیت {last.get('state')}",
                [_fix("rollback", "بازگشت به آخرین نسخه‌ی سالم")])

    for a in ev.get("applets") or []:
        reason = f"{a.get('state', '')} {a.get('reason', '')}"
        if re.search(r"OOMKilled", reason, re.I):
            add("oom", 0.85, f"وضعیت کانتینر: {reason.strip()}")
        if re.search(r"CrashLoop|BackOff|Error|Terminated", reason, re.I):
            add("crash_loop", 0.6, f"وضعیت کانتینر: {reason.strip()}")

    # --- logs ----------------------------------------------------------------------------
    logs = ev.get("logs") or []
    counts: Counter = Counter()
    exc_types: Counter = Counter()
    first_seen: dict[str, float] = {}
    for item in logs:
        line = str(item.get("line", ""))[:1000]
        for rid, rx, _cat, w in LOG_RULES:
            m = rx.search(line)
            if not m:
                continue
            counts[rid] += 1
            first_seen.setdefault(rid, item.get("ts", 0))
            if counts[rid] <= 3:
                add(rid, w, line.strip()[:220])
            else:
                add(rid, w * 0.15, "")
            if rid == "missing_env":
                var = next((g for g in m.groups() if g), None)
                if var:
                    hyps["missing_env"]["extra"].setdefault("vars", [])
                    if var not in hyps["missing_env"]["extra"]["vars"]:
                        hyps["missing_env"]["extra"]["vars"].append(var)
            if rid == "module_missing":
                mod = next((g for g in m.groups() if g), None)
                if mod:
                    hyps["module_missing"]["extra"]["module"] = mod
            if rid == "exception":
                mt = re.search(r"((?:\w+\.)*\w*(?:Error|Exception))", line)
                if mt:
                    exc_types[mt.group(1)] += 1

    if "exception" in hyps and exc_types:
        hyps["exception"]["extra"]["top_exceptions"] = exc_types.most_common(3)
    # A specific root cause explains the generic exception/5xx noise -> damp the generic ones.
    specific = {"missing_env", "module_missing", "db_conn", "oom", "disk_full", "readonly_fs"} & hyps.keys()
    if specific and "exception" in hyps:
        hyps["exception"]["score"] *= 0.5

    if "missing_env" in hyps:
        env_keys = {e.get("key") for e in details.get("envs") or []}
        vars_ = [v for v in hyps["missing_env"]["extra"].get("vars", [])]
        missing = [v for v in vars_ if v not in env_keys]
        hyps["missing_env"]["extra"]["missing"] = missing
        if missing:
            add("missing_env", 0.2, f"در تنظیمات فعلی برنامه این متغیر(ها) وجود ندارد: {', '.join(missing)}",
                [_fix("set_env", f"تنظیم {', '.join(missing)}", {"keys": missing}, kind="needs_input")])
        elif (ev.get("http") or {}).get("ok"):
            # Variable is configured now and the site answers → these errors predate the fix.
            h = hyps.pop("missing_env")
            stale = {"id": "resolved_env", "title": "خطای متغیر محیطی قبلاً رفع شده است", "score": 0.05,
                     "evidence": [h["evidence"][0], f"{', '.join(vars_)} اکنون تنظیم شده و سایت سالم پاسخ می‌دهد — این خطاها مربوط به قبل از اصلاح است"],
                     "fixes": [], "extra": {}}
            hyps["resolved_env"] = stale
            if "exception" in hyps and all("KeyError" in e or "Traceback" in e for e in hyps["exception"]["evidence"]):
                hyps.pop("exception")
        else:
            hyps["missing_env"]["fixes"].append(_fix("restart_app", "ری‌استارت برای خواندن مجدد متغیرها"))
    if "module_missing" in hyps:
        mod = hyps["module_missing"]["extra"].get("module", "")
        hyps["module_missing"]["fixes"].append(_fix("code_fix", f"افزودن «{mod}» به requirements.txt / package.json و دیپلوی مجدد", kind="guide"))
    if "db_conn" in hyps:
        hyps["db_conn"]["fixes"] += [_fix("check_env", "بررسی متغیر اتصال دیتابیس (DATABASE_URL و ...)", kind="guide")]
    if "disk_full" in hyps:
        hyps["disk_full"]["fixes"].append(_fix("code_fix", "پاک‌سازی فایل‌های موقت/لاگ یا افزایش حجم دیسک از پنل", kind="guide"))
    if "readonly_fs" in hyps:
        hyps["readonly_fs"]["fixes"].append(_fix("code_fix", "نوشتن فایل‌ها روی دیسک متصل (/data) یا غیرفعال‌کردن حالت read-only", kind="guide"))
    if "port_bind" in hyps:
        hyps["port_bind"]["fixes"].append(_fix("code_fix", "برنامه باید روی 0.0.0.0 و پورت تنظیم‌شده‌ی لیارا گوش دهد", kind="guide"))

    # --- resources vs plan limits -------------------------------------------------------
    limits = ev.get("limits") or {}
    mem = _series_stats(ev.get("mem_gb") or [])
    cpu = _series_stats(ev.get("cpu") or [])
    resources: dict[str, Any] = {"limits": limits}
    if mem and limits.get("ram_gb"):
        ratio = mem["max"] / limits["ram_gb"]
        resources["ram"] = {"last_gb": round(mem["last"], 3), "max_gb": round(mem["max"], 3), "ratio": round(ratio, 2)}
        if ratio >= config.RAM_WARN_RATIO:
            add("memory_pressure", 0.55 + 0.4 * min(1, (ratio - 0.85) / 0.15),
                f"بیشینه‌ی RAM در {ev['window_min']} دقیقه‌ی اخیر: {mem['max']*1024:.0f}MB از {limits['ram_gb']*1024:.0f}MB ({ratio:.0%})",
                [_fix("restart_app", "ری‌استارت (رهاسازی موقت حافظه)"), _fix("resize_app", "ارتقای پلن به RAM بیشتر")])
        growth = (mem["last_third_avg"] - mem["first_third_avg"]) / max(limits["ram_gb"], 1e-6)
        if mem["points"] >= 6 and growth > 0.2:
            add("memory_leak", 0.35 + min(0.5, growth),
                f"میانگین حافظه از {mem['first_third_avg']*1024:.0f}MB به {mem['last_third_avg']*1024:.0f}MB رسیده (رشد پیوسته)",
                [_fix("restart_app", "ری‌استارت فوری برای کاهش ریسک کرش"),
                 _fix("code_fix", "پیدا کردن نشت حافظه در کد (کش بی‌سقف، لیست‌های سراسری، اتصال‌های بسته‌نشده)", kind="guide")])
        if "oom" in hyps:
            hyps["oom"]["fixes"] += [_fix("restart_app", "ری‌استارت"), _fix("resize_app", "ارتقای پلن")]
    elif "oom" in hyps:
        hyps["oom"]["fixes"] += [_fix("restart_app", "ری‌استارت"), _fix("resize_app", "ارتقای پلن")]
    if cpu and limits.get("cpu"):
        c_last = cpu["avg"]
        ratio = (cpu["max"] if cpu["max"] <= limits["cpu"] * 4 else cpu["max"] / 100) / limits["cpu"]
        resources["cpu"] = {"avg": round(c_last, 3), "max": round(cpu["max"], 3), "ratio": round(ratio, 2)}
        if ratio >= config.CPU_WARN_RATIO:
            add("cpu_saturation", 0.45 + 0.4 * min(1, ratio - 0.9),
                f"بیشینه‌ی CPU: {cpu['max']:.2f} از {limits['cpu']} هسته ({ratio:.0%})",
                [_fix("resize_app", "ارتقای پلن به CPU بیشتر"),
                 _fix("code_fix", "بهینه‌سازی کوئری/حلقه‌های سنگین یا کش‌کردن پاسخ‌ها", kind="guide")])

    # --- external probe -------------------------------------------------------------------
    http = ev.get("http") or {}
    if rel and (http.get("status") in (502, 503, 504) or http.get("edge_error")):
        fin = rel[0].get("finishedAt") or rel[0].get("createdAt")
        try:
            age = time.time() - datetime.fromisoformat(str(fin).replace("Z", "+00:00")).timestamp()
        except ValueError:
            age = 1e9
        if age < 300:
            add("starting", 0.6, f"آخرین نسخه {age:.0f} ثانیه پیش منتشر شده و پاسخ {http['status']} معمولاً یعنی کانتینر هنوز بالا نیامده",
                [_fix("wait", "یک دقیقه صبر و بررسی مجدد", kind="guide")])
    if http.get("status") is None and details.get("scale", 1) != 0:
        add("unreachable", 0.7, f"درخواست به {http.get('url')} پاسخی نگرفت ({http.get('error')})")
    elif (http.get("status") or 0) >= 500:
        sym = f"صفحه‌ی اصلی با کد {http['status']} پاسخ داد"
        # attach the symptom to every code/config cause; if nothing explains it, keep a generic hypothesis
        attached = False
        for hid in ("missing_env", "db_conn", "module_missing", "exception", "oom", "readonly_fs"):
            if hid in hyps:
                add(hid, 0.15, sym)
                attached = True
        if not attached:
            add("exception", 0.4, sym + " ولی لاگ واضحی پیدا نشد", [_fix("code_fix", "فعال کردن لاگ خطا (DEBUG/LOG_LEVEL) برای دیدن علت", kind="guide")])
    if http.get("latency_s") and http["latency_s"] > config.SLOW_RESPONSE_S and (http.get("status") or 0) < 500:
        add("slow_response", 0.5, f"زمان پاسخ صفحه‌ی اصلی: {http['latency_s']} ثانیه")
        for hid in ("cpu_saturation", "memory_pressure", "timeout_upstream", "memory_leak"):
            if hid in hyps:
                add(hid, 0.15, "هم‌زمان با کندی پاسخ")

    if counts.get("http_5xx") and "resolved_env" in hyps:
        hyps["resolved_env"]["evidence"].append(f"{counts['http_5xx']} درخواست 5xx در همان بازه‌ی قبل از اصلاح")
    elif counts.get("http_5xx"):
        target = next((h for h in ("missing_env", "db_conn", "module_missing", "exception") if h in hyps), "exception")
        add(target, 0.1, f"{counts['http_5xx']} درخواست با کد 5xx در لاگ‌ها")
    hyps.pop("http_5xx", None)  # a symptom, already folded into the causes above

    ranked = sorted(hyps.values(), key=lambda h: -h["score"])
    for h in ranked:
        h["score"] = round(h["score"], 2)
        h["confidence"] = "زیاد" if h["score"] >= 0.7 else "متوسط" if h["score"] >= 0.4 else "کم"

    healthy = not ranked or (ranked[0]["score"] < 0.35 and http.get("ok"))
    return {
        "app": app, "healthy": healthy, "hypotheses": ranked[:5],
        "summary": {
            "status": details.get("status"), "plan": details.get("planID"), "scale": details.get("scale"),
            "http": {k: http.get(k) for k in ("url", "status", "latency_s", "error")},
            "log_lines": len(logs), "log_hits": dict(counts), "resources": resources,
            "latest_release": (rel[0].get("state") if rel else None),
        },
        "log_sample": [l["line"][:200] for l in logs[-12:]],
    }


async def diagnose(app: str, minutes: int = 60) -> dict:
    ev = await collect(app, minutes)
    res = analyze(ev)
    res["series"] = {"cpu": ev["cpu"][-120:], "mem_gb": ev["mem_gb"][-120:]}
    return res
