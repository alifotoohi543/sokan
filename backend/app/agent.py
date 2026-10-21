"""Chat agent: LLM tool-calling loop over ``actions.TOOLS`` with a deterministic fallback.

Events streamed to the UI (one JSON object each):
  step      – a tool started/finished (visible "what am I doing" timeline)
  card      – rich result to render (diagnosis, metrics, logs, plan recommendation, ...)
  approval  – a sensitive action waiting for the user's decision
  message   – assistant text
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from . import actions, config, store
from .liara import LiaraError, client

log = logging.getLogger(__name__)
_PRICE_IN, _PRICE_OUT = 0.10, 0.50  # gpt-6-luna USD per 1M tokens (fallback when usage.cost is missing)

SYSTEM = """You are «سکان» (Sokan), a senior DevOps teammate for website owners who host on Liara (Iranian PaaS).
Always answer in Persian, short and plain; explain technical terms briefly; use bullet points.
Rules:
- Gather evidence with tools BEFORE concluding. For any problem report (500 error, slow, down, crash) call `diagnose` first.
- When presenting a diagnosis: name the most likely cause, the evidence (quote the log line or the number), confidence, then the fix.
- Fixes that change something (restart, env vars, plan, deploy, rollback, domain, create/delete) MUST go through the matching tool;
  calling it only creates an approval card for the user. Never say an action is done until you see its result.
- Never invent secret values (passwords, DB URLs, API keys): ask the user for them.
- If the user asks for something the Liara API cannot do, say so clearly and offer the workaround (see `api_capabilities`).
- For a first deploy: analyze_project → explain recommended plan & monthly cost → create_app → deploy.
- Keep answers under ~120 words unless the user asks for detail. Do not repeat raw JSON; the UI shows cards for tool results.
Context: {context}"""

CARD_TOOLS = {"diagnose", "get_metrics", "get_logs", "app_status", "analyze_project", "account_overview", "list_plans",
              "api_capabilities", "list_releases", "domain_status", "list_apps"}


def _key() -> str | None:
    return config.read_secret("LLM_API_KEY", config.LLM_KEY_FILE)


def compact(name: str, res: Any) -> str:
    """Shrink tool results before sending them to the LLM (token budget)."""
    if not isinstance(res, dict):
        return json.dumps(res, ensure_ascii=False)[:3000]
    r = dict(res)
    r.pop("series", None)
    if name == "diagnose":
        r["log_sample"] = (r.get("log_sample") or [])[-6:]
        for h in r.get("hypotheses", []):
            h["evidence"] = h["evidence"][:3]
    if name == "get_logs":
        r["lines"] = [l["line"][:200] for l in r.get("lines", [])[-25:]]
    if name == "list_plans":
        r = {"plans": r.get("plans", [])[:9]}
    if name == "account_overview":
        r["apps"] = r.get("apps", [])[:20]
    return json.dumps(r, ensure_ascii=False, default=str)[:3500]


async def _llm(messages: list[dict]) -> dict | None:
    key = _key()
    if not key:
        return None
    if store.llm_spent()["usd"] >= config.LLM_BUDGET_USD:
        log.warning("LLM budget cap reached")
        return None
    try:
        async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S) as c:
            r = await c.post(f"{config.LLM_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                             json={"model": config.LLM_MODEL, "messages": messages, "tools": actions.openai_tools(),
                                   "reasoning_effort": "none", "max_completion_tokens": 900, "temperature": 0.2})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("LLM call failed: %s", e)
        return None
    usage = data.get("usage") or {}
    cost = float(usage.get("cost") or 0) or (usage.get("prompt_tokens", 0) * _PRICE_IN + usage.get("completion_tokens", 0) * _PRICE_OUT) / 1e6
    store.add_llm_spend(cost)
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError):
        return None


async def _context(focus: str = "") -> str:
    try:
        apps = await client().list_apps()
        names = [f"{a.get('project_id')}({a.get('type')},{a.get('status')})" for a in apps]
    except LiaraError as e:
        names = [f"<unavailable: {e.message}>"]
    alerts = [f"{a['app']}:{a['title']}" for a in store.alerts()][:6]
    ctx = f"apps={names}; open_alerts={alerts}; now={time.strftime('%Y-%m-%d %H:%M')}"
    if focus:
        ctx += f"; the user is currently looking at app '{focus}' — assume it when no app is named"
    return ctx


async def _run_tool(name: str, args: dict, session: str) -> AsyncIterator[dict]:
    """Execute one tool call and yield UI events; the last event is {'type':'_result', 'content': str}."""
    t = actions.TOOLS.get(name)
    if not t:
        yield {"type": "_result", "content": json.dumps({"error": f"unknown tool {name}"})}
        return
    sid = uuid.uuid4().hex[:8]
    if t["risk"] == actions.READ:
        yield {"type": "step", "id": sid, "tool": name, "label": t["label"], "status": "running", "args": args}
        res = await actions.run_read(name, args)
        ok = "error" not in res
        yield {"type": "step", "id": sid, "tool": name, "label": t["label"], "status": "done" if ok else "error",
               "summary": res.get("error") if not ok else None}
        if ok and name in CARD_TOOLS:
            yield {"type": "card", "kind": name, "data": res}
        yield {"type": "_result", "content": compact(name, res)}
    else:
        yield {"type": "step", "id": sid, "tool": name, "label": "آماده‌سازی درخواست تأیید: " + t["label"], "status": "running"}
        res = await actions.request(name, args, session)
        yield {"type": "step", "id": sid, "tool": name, "label": "آماده‌سازی درخواست تأیید: " + t["label"],
               "status": "done" if "error" not in res else "error", "summary": res.get("error")}
        if "approval_id" in res:
            yield {"type": "approval", "approval": store.get_approval(res["approval_id"])}
            yield {"type": "_result", "content": json.dumps({"status": "pending_approval",
                                                             "note": "Approval card shown to the user. Tell them to review it; it has NOT run yet."})}
        else:
            yield {"type": "_result", "content": json.dumps(res, ensure_ascii=False)}


async def chat(session: str, text: str, focus: str = "") -> AsyncIterator[dict]:
    history = store.load_session(session)
    history.append({"role": "user", "content": text})
    msgs = [{"role": "system", "content": SYSTEM.format(context=await _context(focus))}] + history[-14:]
    used_llm = False
    for _ in range(config.LLM_MAX_STEPS):
        m = await _llm(msgs)
        if m is None:
            break
        used_llm = True
        calls = m.get("tool_calls") or []
        if not calls:
            answer = (m.get("content") or "").strip()
            yield {"type": "message", "text": answer}
            history.append({"role": "assistant", "content": answer})
            store.save_session(session, history)
            return
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
        for call in calls:
            fn = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            content = ""
            async for ev in _run_tool(fn, args, session):
                if ev["type"] == "_result":
                    content = ev["content"]
                else:
                    yield ev
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": content})
    if used_llm:
        yield {"type": "message", "text": "بررسی‌ها انجام شد؛ نتیجه‌ها در کارت‌های بالا آمده است."}
        history.append({"role": "assistant", "content": "(نتایج در کارت‌ها نمایش داده شد)"})
        store.save_session(session, history)
        return
    # ------------------------------------------------------------ deterministic fallback (no LLM / over budget)
    async for ev in fallback(session, text, history, focus):
        yield ev


# ================================================================ rule-based fallback
INTENTS = [
    ("restart_app", r"ری.?استارت|restart|راه.?اندازی مجدد"),
    ("diagnose", r"۵۰۰|500|ارور|خطا|error|مشکل|کند|slow|down|بالا نمیاد|باز نمیشه|کرش|خراب"),
    ("get_logs", r"لاگ|log"),
    ("get_metrics", r"مصرف|رم|ram|cpu|سی.?پی.?یو|منابع|حافظه"),
    ("list_plans", r"پلن|قیمت|plan"),
    ("analyze_project", r"دیپلوی|deploy|انتشار|راه.?اندازی اولیه|[a-z]:\\\\"),
    ("api_capabilities", r"محدودیت|نمی.?تونی|limitation"),
    ("app_status", r"وضعیت|status|سلامت"),
]


async def fallback(session: str, text: str, history: list[dict], focus: str = "") -> AsyncIterator[dict]:
    try:
        apps = [a.get("project_id") for a in await client().list_apps()]
    except LiaraError:
        apps = []
    named = next((a for a in apps if a and a.lower() in text.lower()), None)
    alerting = list(dict.fromkeys(a["app"] for a in store.alerts() if a["severity"] == "critical"))
    app = named or (focus if focus in apps else None) or (alerting[0] if len(alerting) == 1 else None) or (apps[0] if len(apps) == 1 else None)
    intent = next((name for name, rx in INTENTS if re.search(rx, text, re.I)), "account_overview")
    path = re.search(r"([A-Za-z]:\\[^\s\"']+|/[\w./-]+)", text)
    args: dict[str, Any] = {}
    if intent == "analyze_project":
        if not path:
            intent = "account_overview"
        else:
            args = {"path": path.group(1)}
    elif intent in ("diagnose", "get_logs", "get_metrics", "app_status", "restart_app"):
        if not app:
            yield {"type": "message", "text": "منظورتان کدام برنامه است؟ " + (("برنامه‌ها: " + "، ".join(apps)) if apps else "هنوز برنامه‌ای ندارید.")}
            return
        args = {"app": app}
        if intent == "restart_app":
            args["reason"] = "درخواست کاربر"
    result: dict | None = None
    async for ev in _run_tool(intent, args, session):
        if ev["type"] == "_result":
            try:
                result = json.loads(ev["content"])
            except ValueError:
                result = None
        else:
            yield ev
    yield {"type": "message", "text": _template(intent, result or {}, app)}
    history.append({"role": "assistant", "content": f"[{intent}]"})
    store.save_session(session, history)


def _template(intent: str, r: dict, app: str | None) -> str:
    if r.get("error"):
        return f"⚠️ {r['error']}"
    if intent == "diagnose":
        hs = r.get("hypotheses") or []
        if not hs:
            return f"✅ مشکلی در {app} پیدا نکردم؛ سایت پاسخ می‌دهد و خطای مهمی در لاگ‌ها نیست."
        h = hs[0]
        ev = "\n".join(f"• {e}" for e in h["evidence"][:3])
        fixes = "، ".join(f["label"] for f in h["fixes"][:3]) or "—"
        return f"محتمل‌ترین علت: **{h['title']}** (اطمینان {h['confidence']})\n{ev}\nپیشنهاد: {fixes}"
    if intent == "restart_app":
        return "درخواست ری‌استارت آماده شد؛ لطفاً کارت تأیید را بررسی کنید."
    if intent == "account_overview":
        apps = r.get("apps") or []
        bal = r.get("balance_toman")
        return f"اعتبار: {bal:,} تومان — {len(apps)} برنامه." if isinstance(bal, (int, float)) else f"{len(apps)} برنامه."
    return "نتیجه در کارت بالا نمایش داده شد."


async def summarize_execution(approval: dict, final: dict) -> str:
    """Plain-language outcome of an approved action (deterministic: no LLM cost)."""
    action, app = approval["action"], approval.get("app")
    if final.get("release_ready") and not final.get("ok"):
        pr = final.get("probe") or {}
        return (f"⚠️ نسخه‌ی جدید با موفقیت ساخته و منتشر شد، اما سایت سالم پاسخ نمی‌دهد "
                f"(HTTP {pr.get('status') or 'بدون پاسخ'}). یعنی مشکل از زیرساخت نیست و از خود برنامه یا تنظیماتش است — "
                f"همین الان بررسی‌اش می‌کنم.")
    if not final.get("ok"):
        err = final.get("error") or final.get("reason") or "نامشخص"
        tail = final.get("log_tail") or final.get("errors_after") or []
        msg = f"❌ «{approval['title']}» موفق نبود: {err}"
        if tail:
            msg += "\nآخرین خطوط:\n" + "\n".join(f"  {l}" for l in tail[-4:])
        msg += "\nمی‌خواهید علت را بررسی کنم؟"
        return msg
    pr = final.get("probe") or {}
    probe_txt = f" سایت با کد {pr.get('status')} در {pr.get('latency_s')} ثانیه پاسخ داد." if pr else ""
    if action == "add_domain":
        g = final.get("guide") or {}
        steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(g.get("steps", [])))
        return f"✅ دامنه ثبت شد. کارهایی که فقط خودتان می‌توانید انجام دهید:\n{steps}"
    ram_txt = ""
    if final.get("ram_before_mb") and final.get("ram_after_mb") is not None:
        ram_txt = f" مصرف RAM از {final['ram_before_mb']}MB به {final['ram_after_mb']}MB رسید."
    return f"✅ «{approval['title']}» با موفقیت انجام و بررسی شد.{probe_txt}{ram_txt}"


async def continue_after(session: str, outcome: str) -> AsyncIterator[dict]:
    """After an approved action finishes, let the agent take the next step of the user's plan (e.g. create → deploy)."""
    if not session or not _key() or store.llm_spent()["usd"] >= config.LLM_BUDGET_USD:
        return
    history = store.load_session(session)
    history.append({"role": "user", "content": f"[system note, not from the user] The approved action finished: {outcome}\n"
                                                "If my earlier request still has remaining steps, continue with the next one now "
                                                "(sensitive steps will create a new approval card). If nothing remains, reply with ONE short sentence "
                                                "on what to check next — do not repeat the result."})
    msgs = [{"role": "system", "content": SYSTEM.format(context=await _context())}] + history[-14:]
    for _ in range(config.LLM_MAX_STEPS):
        m = await _llm(msgs)
        if m is None:
            return
        calls = m.get("tool_calls") or []
        if not calls:
            answer = (m.get("content") or "").strip()
            if answer:
                yield {"type": "message", "text": answer}
            history.append({"role": "assistant", "content": answer})
            store.save_session(session, history)
            return
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
        for call in calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            content = ""
            async for ev in _run_tool(call["function"]["name"], args, session):
                if ev["type"] == "_result":
                    content = ev["content"]
                else:
                    yield ev
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        if any(actions.TOOLS.get(c["function"]["name"], {}).get("risk") != actions.READ for c in calls):
            store.save_session(session, history)
            # stop after proposing a sensitive step; the user decides next
            m2 = await _llm(msgs)
            if m2 and m2.get("content") and not m2.get("tool_calls"):
                yield {"type": "message", "text": m2["content"].strip()}
                history.append({"role": "assistant", "content": m2["content"].strip()})
                store.save_session(session, history)
            return


def note_outcome(session: str, text: str) -> None:
    if not session:
        return
    h = store.load_session(session)
    h.append({"role": "assistant", "content": text})
    store.save_session(session, h)
