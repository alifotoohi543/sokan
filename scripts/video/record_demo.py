"""Record the Sokan demo against the REAL Liara account.

Drives the web UI with Playwright (installed Chrome, headless), captures frames via CDP screencast, and writes
scene boundaries + narration to scenes.json. build_video.py turns that into a narrated MP4.

Side effects on Liara (all intended for the demo): creates and finally deletes the temporary app `cafeland-live`,
sets its DATABASE_URL, grows + restarts memory on `cafeland-sokan`, toggles one automation policy (turned off again).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
OUT = Path(os.getenv("VIDEO_OUT", ROOT / "data" / "video"))
FRAMES = OUT / "frames"
BASE = os.getenv("SOKAN_URL", "http://127.0.0.1:8010")
TEMP_APP = os.getenv("TEMP_APP", "cafeland-live")
LEAK_APP = "cafeland-sokan"
DEMO_PATH = str(ROOT / "demo-app")
VIEW_W, VIEW_H, DSF = 1536, 864, 1.25  # → 1920x1080 frames

OVERLAY_JS = r"""
(() => {
  if (window.__rec) return;
  window.__rec = true;
  const css = `
  #rec-cap{position:fixed;left:50%;bottom:122px;transform:translateX(-50%);max-width:78%;z-index:99999;
    background:rgba(5,8,12,.86);color:#f1f5f9;border:1px solid rgba(45,212,191,.45);border-radius:14px;
    padding:12px 22px;font:500 19px/1.75 "Vazirmatn Variable",Tahoma,sans-serif;direction:rtl;text-align:center;
    box-shadow:0 10px 40px rgba(0,0,0,.5);pointer-events:none;transition:opacity .35s}
  #rec-cap:empty{opacity:0}
  #rec-chap{position:fixed;top:64px;left:50%;transform:translateX(-50%);z-index:99999;pointer-events:none;
    background:linear-gradient(90deg,#0f2e2c,#1b2040);color:#e6edf3;border:1px solid rgba(45,212,191,.5);
    border-radius:999px;padding:6px 18px;font:600 14px "Vazirmatn Variable",Tahoma,sans-serif;direction:rtl;transition:opacity .35s}
  #rec-chap:empty{opacity:0}
  #rec-cur{position:fixed;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;z-index:100000;pointer-events:none;
    background:rgba(255,255,255,.95);border:3px solid #2dd4bf;box-shadow:0 0 0 6px rgba(45,212,191,.25);
    transition:left .55s cubic-bezier(.2,.8,.2,1),top .55s cubic-bezier(.2,.8,.2,1);left:-40px;top:-40px}
  .rec-ripple{position:fixed;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;z-index:99999;pointer-events:none;
    border:3px solid #2dd4bf;animation:recr .6s ease-out forwards}
  @keyframes recr{to{transform:scale(4);opacity:0}}
  .rec-hl{outline:3px solid #2dd4bf !important;outline-offset:4px;border-radius:14px;transition:outline-color .3s}`;
  const add = () => {
    if (!document.body || document.getElementById('rec-cap')) return;
    const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s);
    for (const id of ['rec-cap','rec-chap','rec-cur']) { const d = document.createElement('div'); d.id = id; document.body.appendChild(d); }
    if (window.__recState) { document.getElementById('rec-cap').textContent = window.__recState.cap || '';
      document.getElementById('rec-chap').textContent = window.__recState.chap || ''; }
  };
  window.__recSet = (cap, chap) => { window.__recState = {cap, chap}; add();
    document.getElementById('rec-cap').textContent = cap || ''; document.getElementById('rec-chap').textContent = chap || ''; };
  window.__recMove = (x, y) => { add(); const c = document.getElementById('rec-cur'); c.style.left = x + 'px'; c.style.top = y + 'px'; };
  window.__recRipple = (x, y) => { const r = document.createElement('div'); r.className = 'rec-ripple';
    r.style.left = x + 'px'; r.style.top = y + 'px'; document.body.appendChild(r); setTimeout(() => r.remove(), 700); };
  document.addEventListener('DOMContentLoaded', add); add();
})();
"""


class Recorder:
    def __init__(self):
        self.frames: list[tuple[float, str]] = []
        self.scenes: list[dict] = []
        self.cdp = None
        self.page: Page | None = None
        self.n = 0
        self.chapter = ""

    async def attach(self, page: Page):
        if self.cdp:
            try:
                await self.cdp.send("Page.stopScreencast")
                await self.cdp.detach()
            except Exception:  # noqa: BLE001
                pass
        self.page = page
        self.cdp = await page.context.new_cdp_session(page)

        async def on_frame(p):
            self.n += 1
            fn = FRAMES / f"f{self.n:06d}.jpg"
            fn.write_bytes(base64.b64decode(p["data"]))
            self.frames.append((time.time(), fn.name))
            try:
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
            except Exception:  # noqa: BLE001
                pass

        self.cdp.on("Page.screencastFrame", lambda p: asyncio.ensure_future(on_frame(p)))
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": 1920,
                                                     "maxHeight": 1080, "everyNthFrame": 1})

    async def scene(self, sid: str, narration: str, caption: str | None = None, chapter: str | None = None):
        """Start a new scene; narration is spoken over it, caption shown on screen."""
        now = time.time()
        if self.scenes:
            self.scenes[-1]["t1"] = now
        if chapter is not None:
            self.chapter = chapter
        self.scenes.append({"id": sid, "t0": now, "t1": None, "narration": narration})
        await self.page.evaluate("([c,h]) => window.__recSet && window.__recSet(c,h)", [caption if caption is not None else narration, self.chapter])
        print(f"[{time.strftime('%H:%M:%S')}] scene {sid}", flush=True)

    def finish(self):
        if self.scenes:
            self.scenes[-1]["t1"] = time.time()
        (OUT / "scenes.json").write_text(json.dumps({"frames": self.frames, "scenes": self.scenes}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")


R = Recorder()


# ----------------------------------------------------------------------------- UI helpers
async def pause(s: float):
    await asyncio.sleep(s)


async def point(page: Page, loc, click: bool = True):
    await loc.scroll_into_view_if_needed()
    box = await loc.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.evaluate("([x,y]) => window.__recMove(x,y)", [x, y])
    await pause(0.7)
    if click:
        await page.evaluate("([x,y]) => window.__recRipple(x,y)", [x, y])
        await loc.click()
        await pause(0.3)


async def highlight(page: Page, loc, secs: float = 2.5):
    await loc.evaluate("e => { e.scrollIntoView({behavior:'smooth', block:'center'}); e.classList.add('rec-hl') }")
    await pause(secs)
    await loc.evaluate("e => e.classList.remove('rec-hl')")


async def wait_idle(page: Page, timeout: float = 240):
    """Agent turn / approval stream finished (send button not spinning)."""
    await pause(1.2)
    await page.wait_for_function("() => !document.querySelector('button[aria-label=\"ارسال\"] .animate-spin')",
                                 timeout=timeout * 1000)
    await pause(0.8)


async def ask(page: Page, text: str, timeout: float = 240):
    box = page.locator('textarea[aria-label="پیام به دستیار"]')
    await point(page, box)
    await box.type(text, delay=28)
    await pause(0.5)
    await page.keyboard.press("Enter")
    await wait_idle(page, timeout)


def pending_card(page: Page):
    return page.locator('section[aria-label^="درخواست تأیید"]').filter(has_text="منتظر تأیید شما").last


async def has_pending(page: Page) -> bool:
    return await pending_card(page).count() > 0


async def approve(page: Page, confirm: str = "", timeout: float = 600):
    card = pending_card(page)
    await highlight(page, card, 2.0)
    if confirm:
        inp = card.locator("input")
        await point(page, inp)
        await inp.type(confirm, delay=60)
        await pause(0.6)
    btn = card.get_by_role("button", name="تأیید و اجرا")
    await point(page, btn)
    await wait_idle(page, timeout)


async def reject(page: Page):
    card = pending_card(page)
    await highlight(page, card, 1.5)
    await point(page, card.get_by_role("button", name="رد کردن"))
    await pause(1.5)


async def last_text(page: Page) -> str:
    return await page.evaluate("() => document.querySelector('main').innerText.slice(-1500)")


async def scroll_bottom(page: Page):
    await page.evaluate("() => { const m = document.querySelector('main .overflow-y-auto'); if (m) m.scrollTo({top: m.scrollHeight, behavior:'smooth'}) }")
    await pause(1)


# ----------------------------------------------------------------------------- slides
SLIDE_CSS = """
body{margin:0;background:radial-gradient(1200px 600px at 70% 20%,#0f2e2c 0%,#0b0f14 55%);color:#e6edf3;
font-family:"Vazirmatn Variable",Tahoma,sans-serif;direction:rtl;height:100vh;display:grid;place-items:center}
.w{width:1100px}.t{font-size:64px;font-weight:800;margin:0}.s{font-size:26px;color:#aab6c3;margin:10px 0 36px}
.g{display:grid;grid-template-columns:1fr 1fr;gap:16px}.c{border:1px solid #253041;background:#11161dcc;border-radius:18px;padding:18px 22px}
.c b{color:#2dd4bf;font-size:20px}.c p{margin:6px 0 0;color:#aab6c3;font-size:17px;line-height:1.8}
.k{display:inline-block;background:#0f2e2c;color:#2dd4bf;border-radius:999px;padding:4px 14px;font-size:15px;margin-bottom:18px}
"""


async def slide(page: Page, html: str):
    await page.evaluate("""([css, html]) => {
      const keep = ['rec-cap','rec-chap','rec-cur'].map(id => document.getElementById(id)).filter(Boolean);
      document.body.innerHTML = `<style>${css}</style>` + html; keep.forEach(e => document.body.appendChild(e));
    }""", [SLIDE_CSS, html])


INTRO = """<div class="w"><span class="k">چالش لیارا · استارکوچ</span>
<h1 class="t">سکان</h1><p class="s">همکار فنی هوشمند صاحبان وب‌سایت روی لیارا — راه‌اندازی، پایش، عیب‌یابی و رفع مشکل، با تأیید شما</p>
<div class="g">
<div class="c"><b>۱. یکپارچگی با لیارا</b><p>ساخت برنامه، دیپلوی، متغیرها، ری‌استارت، پلن، حذف — همه با API واقعی</p></div>
<div class="c"><b>۲. هوشمندی و حل مسئله</b><p>تحلیل لاگ و منابع، علت‌یابی با شواهد و درصد اطمینان، راستی‌آزمایی نتیجه</p></div>
<div class="c"><b>۳. تجربه‌ی کاربری</b><p>گفت‌وگوی فارسی ساده، نمایش لحظه‌ای مراحل، کارت تأیید با اثر و هزینه</p></div>
<div class="c"><b>۴. پایداری و امنیت</b><p>پایش خودکار، سیاست‌های خودکار، سطح‌بندی ریسک، گزارش عملیات، اعلام محدودیت‌ها</p></div>
</div></div>"""

OUTRO = """<div class="w"><span class="k">جمع‌بندی</span><h1 class="t" style="font-size:52px">سکان: از اولین دیپلوی تا نگهداری پایدار</h1>
<p class="s">همه‌ی صحنه‌های این ویدئو روی حساب واقعی لیارا و API رسمی اجرا شد</p>
<div class="g">
<div class="c"><b>یکپارچگی</b><p>شبکه، برنامه، release، env، restart، resize، delete، لاگ، متریک، دامنه</p></div>
<div class="c"><b>هوشمندی</b><p>موتور قواعد مبتنی بر شواهد + مدل زبانی کم‌هزینه؛ بررسی نتیجه بعد از هر اقدام</p></div>
<div class="c"><b>تجربه‌ی کاربری</b><p>فارسی، راست‌به‌چپ، کارت‌های قابل‌فهم، تأیید دومرحله‌ای برای اقدامات بحرانی</p></div>
<div class="c"><b>امنیت</b><p>هیچ تغییری بدون تأیید انسان — حتی از طریق MCP؛ پوشاندن رمزها؛ audit کامل</p></div>
</div></div>"""


# ----------------------------------------------------------------------------- prep (real account)
async def prep():
    from app import liara, store
    store.init()
    lc = liara.client()
    for t in await lc.teams():
        lc.team_id = t["_id"]
        if ((await lc.billing()).get("team") or {}).get("balance", 0) > 0:
            break
    apps = [a["project_id"] for a in await lc.list_apps()]
    if TEMP_APP in apps:
        print("prep: deleting leftover", TEMP_APP)
        await lc.delete_app(TEMP_APP)
        await asyncio.sleep(15)
    for name in apps:
        store.resolve_alerts(name)  # start the recording without stale alerts
    for a in store.pending_approvals():
        store.decide_approval(a["id"], "rejected")
    for k in ("auto_restart_down", "auto_restart_ram"):
        store.set_policy(LEAK_APP, k, False)
    async with httpx.AsyncClient(timeout=30) as c:
        h = (await c.get(f"https://{LEAK_APP}.liara.run/health")).json()
    print("prep: leak app health", h)
    return h


async def leak_task():
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(9):
            try:
                r = await c.get(f"https://{LEAK_APP}.liara.run/admin/leak", params={"mb": 45})
                print("leak", r.text.strip(), flush=True)
            except httpx.HTTPError as e:
                print("leak err", e)
            await asyncio.sleep(22)


def mcp_request_restart() -> str:
    """Simulate an external MCP client (e.g. Claude Desktop) asking for a restart."""
    code = r"""
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    p = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], env={**os.environ, "PYTHONIOENCODING": "utf-8"}, cwd=".")
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("restart_app", {"app": "%s", "reason": "درخواست از Claude Desktop از طریق MCP"})
            print(res.content[0].text[:300])
asyncio.run(main())
""" % LEAK_APP
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "backend", capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=120)
    return out.stdout + out.stderr[-500:]


# ----------------------------------------------------------------------------- the demo
async def run():
    FRAMES.mkdir(parents=True, exist_ok=True)
    for f in FRAMES.glob("*.jpg"):
        f.unlink()
    await prep()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=DSF, locale="fa-IR")
        await ctx.add_init_script(OVERLAY_JS)
        page = await ctx.new_page()
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.goto(BASE)
        await page.evaluate("() => localStorage.clear()")
        await page.reload()
        await page.wait_for_selector('textarea[aria-label="پیام به دستیار"]')
        await pause(2)
        await R.attach(page)
        leak = asyncio.create_task(leak_task())

        # ---------------- intro
        await slide(page, INTRO)
        await R.scene("intro", "سلام. این «سکان» است؛ یک دستیار هوشمند که نقش همکار فنی صاحبان وب‌سایت روی لیارا را بازی می‌کند. "
                               "در این ویدئو همه‌ی قابلیت‌ها را روی حساب واقعی لیارا و بر اساس چهار معیار داوری نشان می‌دهم.",
                      caption="سکان — دستیار DevOps هوشمند برای لیارا", chapter="معرفی")
        await pause(9)
        await page.goto(BASE)
        await page.wait_for_selector('textarea[aria-label="پیام به دستیار"]')
        await R.scene("ui", "این صفحه‌ی اصلی است. بالا اعتبار واقعی تیم، وضعیت پایش خودکار و بودجه‌ی مدل زبانی دیده می‌شود. "
                            "سمت راست برنامه‌ها و هشدارها هستند و سمت چپ داشبورد برنامه‌ی انتخاب‌شده با نمودار مصرف منابع.",
                      caption="اعتبار تیم، پایش خودکار، برنامه‌ها و داشبورد — همه از API واقعی لیارا")
        await highlight(page, page.locator("header"), 3)
        await highlight(page, page.locator('aside[aria-label="برنامه‌ها"]'), 3)
        await highlight(page, page.locator('aside[aria-label="داشبورد برنامه"]'), 3)

        # ---------------- chapter 1: first deploy
        await R.scene("deploy-ask", "معیار اول، راه‌اندازی و دیپلوی. کاربر فقط می‌گوید پروژه‌اش کجاست. "
                                    "سکان پروژه را تحلیل می‌کند: داکرفایل، پورت، متغیرهای لازم؛ و ارزان‌ترین پلن مناسب را با قیمت واقعی پیشنهاد می‌دهد.",
                      caption="«این پروژه رو روی لیارا راه‌اندازی کن» → تحلیل پروژه و پیشنهاد پلن با قیمت واقعی",
                      chapter="۱ · راه‌اندازی و دیپلوی")
        await ask(page, f"این پروژه رو با اسم {TEMP_APP} روی لیارا راه‌اندازی کن: {DEMO_PATH}")
        if not await has_pending(page):
            await ask(page, f"با همون پلن پیشنهادی برنامه‌ی {TEMP_APP} رو بساز و دیپلوی کن")
        await highlight(page, page.locator("section").filter(has_text="تحلیل پروژه و پیشنهاد زیرساخت").last, 4)
        await R.scene("create-approve", "ساخت برنامه هزینه دارد، پس سکان اجرا نمی‌کند؛ یک کارت تأیید می‌سازد: چه اتفاقی می‌افتد، هزینه‌ی ماهانه و روزانه، "
                                        "و این‌که برگشت‌پذیر است. با تأیید من، برنامه روی لیارا ساخته می‌شود و دستیار خودش گام بعد، یعنی دیپلوی را پیشنهاد می‌دهد.",
                      caption="کارت تأیید: اثر، هزینه، برگشت‌پذیری — بدون تأیید هیچ تغییری اعمال نمی‌شود")
        await approve(page)
        new_app = page.locator('aside[aria-label="برنامه‌ها"] button').filter(has_text=TEMP_APP).first
        try:
            await page.evaluate("() => document.querySelector('aside button[aria-label=\"به‌روزرسانی\"]').click()")
            await new_app.wait_for(timeout=15000)
            await point(page, new_app)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        if not await has_pending(page):
            await ask(page, f"حالا پروژه رو روی {TEMP_APP} دیپلوی کن")
        await R.scene("deploy-run", "حالا دیپلوی: سورس بسته‌بندی می‌شود، بدون فایل‌های محرمانه مثل دات‌اِنو، روی لیارا آپلود و ساخته می‌شود. "
                                    "لاگ ساخت به‌صورت زنده نمایش داده می‌شود و بعد از انتشار، سکان خودِ سایت را تست می‌کند.",
                      caption="بسته‌بندی → آپلود → لاگ زنده‌ی build → انتظار برای بالا آمدن کانتینر → تست سایت")
        await approve(page, timeout=900)
        await scroll_bottom(page)

        # ---------------- chapter 2: troubleshooting
        await R.scene("diag", "معیار دوم، هوشمندی. نسخه با موفقیت منتشر شد، اما سایت خطای ۵۰۰ می‌دهد. سکان تشخیص می‌دهد مشکل از زیرساخت نیست و خودکار عیب‌یابی می‌کند: "
                              "لاگ‌ها، تنظیمات برنامه، پاسخ سایت و مصرف منابع. محتمل‌ترین علت با اطمینان بالا: متغیر DATABASE_URL تنظیم نشده؛ همراه با شواهد: خط خطا در لاگ، نبودن متغیر در تنظیمات و پاسخ ۵۰۰. مصرف پردازنده و حافظه هم رد می‌شود.",
                      caption="انتشار موفق ولی HTTP 500 → عیب‌یابی خودکار: علت‌ها با شواهد و درصد اطمینان",
                      chapter="۲ · هوشمندی و عیب‌یابی")
        diag_card = page.locator("section").filter(has_text="گزارش عیب‌یابی").last
        if await diag_card.count():
            await highlight(page, diag_card, 6)
        site = await ctx.new_page()
        await site.goto(f"https://{TEMP_APP}.liara.run/", wait_until="domcontentloaded")
        await R.attach(site)
        await R.scene("site500", "این هم خودِ سایت روی لیارا که واقعاً خطای داخلی سرور می‌دهد.",
                      caption=f"{TEMP_APP}.liara.run — Internal Server Error")
        await pause(4)
        await R.attach(page)
        await page.bring_to_front()
        await R.scene("secret", "دستیار رمز اتصال دیتابیس را حدس نمی‌زند و از کاربر می‌پرسد. مقدار را می‌دهم؛ "
                                "در کارت تأیید، مقدار محرمانه پوشانده شده و بقیه‌ی متغیرها حفظ می‌شوند.",
                      caption="مقدار محرمانه از کاربر گرفته می‌شود و در کارت تأیید پوشانده است")
        await ask(page, "مقدار DATABASE_URL اینه: postgres://cafe:demo-pass@db.internal:5432/cafeland")
        if not await has_pending(page):
            await ask(page, f"همین مقدار رو برای DATABASE_URL روی {TEMP_APP} تنظیم کن")
        await R.scene("verify", "بعد از تأیید، لیارا برنامه را با تنظیمات جدید ری‌استارت می‌کند و سکان نتیجه را دوباره بررسی می‌کند: "
                                "سایت اکنون کد ۲۰۰ برمی‌گرداند و خطای قبلی در لاگ‌های جدید تکرار نشده است.",
                      caption="اجرا → راستی‌آزمایی: پاسخ سایت + لاگ‌های بعد از اصلاح")
        await approve(page)
        await scroll_bottom(page)
        await pause(2)
        await site.reload(wait_until="domcontentloaded")
        await R.attach(site)
        await site.bring_to_front()
        await R.scene("site200", "و سایت واقعی حالا درست کار می‌کند.", caption=f"{TEMP_APP}.liara.run — سالم ✓")
        await pause(4)
        await R.attach(page)
        await page.bring_to_front()

        # ---------------- chapter 3: resources / memory leak
        await leak
        await R.scene("leak-alert", "سناریوی دوم: یک سایت که در حال کار است کم‌کم سنگین می‌شود. پایش خودکار سکان هر دقیقه سلامت سایت و مصرف منابع را بررسی می‌کند "
                                    "و وقتی حافظه به سقف پلن نزدیک شد، هشدار می‌دهد. روی هشدار کلیک می‌کنم.",
                      caption="پایش خودکار: هشدار نزدیک شدن RAM به سقف پلن", chapter="۲ · هوشمندی — منابع و کندی")
        app_btn = page.locator('aside[aria-label="برنامه‌ها"] button').filter(has_text=LEAK_APP).first
        await point(page, app_btn)
        await pause(3)
        alert = page.locator('aside[aria-label="برنامه‌ها"] button').filter(has_text="RAM").first
        for _ in range(12):
            if await alert.count():
                break
            await page.evaluate("() => fetch('/api/monitor/run', {method:'POST'})")
            await pause(10)
        if await alert.count():
            await point(page, alert)
            await wait_idle(page)
        else:
            await ask(page, f"سایت {LEAK_APP} سنگین شده، ممکنه کرش کنه؟ بررسی کن")
        await R.scene("leak-diag", "سکان روند مصرف حافظه را تحلیل می‌کند: رشد پیوسته‌ی حافظه تا ۴۴۰ مگابایت، یعنی ۸۶ درصد سقف پلن؛ الگوی نشت حافظه. "
                                   "ری‌استارت را به‌عنوان راه‌حل فوری پیشنهاد می‌دهد و صادقانه می‌گوید ریشه‌ی مشکل در کد است.",
                      caption="الگوی نشت حافظه از روی روند متریک — ری‌استارت = مسکّن، ریشه در کد")
        dc = page.locator("section").filter(has_text="گزارش عیب‌یابی").last
        if await dc.count():
            await highlight(page, dc, 5)
        if not await has_pending(page):
            await ask(page, f"{LEAK_APP} رو ری‌استارت کن")
        await R.scene("leak-fix", "تأیید می‌کنم. سکان بعد از ری‌استارت دوباره اندازه می‌گیرد و نتیجه را با عدد گزارش می‌دهد: حافظه از ۴۴۰ مگابایت به چند مگابایت برگشت و سایت سالم پاسخ می‌دهد.",
                      caption="راستی‌آزمایی با عدد: RAM قبل و بعد از ری‌استارت")
        await approve(page, timeout=300)
        await scroll_bottom(page)
        await pause(3)
        await R.scene("resize", "اگر کاربر بخواهد پلن را ارتقا دهد، کارت تأیید تفاوت منابع و هزینه‌ی ماهانه را نشان می‌دهد. "
                                "تصمیم با کاربر است؛ این بار رد می‌کنم و هیچ تغییری اعمال نمی‌شود.",
                      caption="ارتقای پلن: نمایش تفاوت منابع و هزینه — رد شد، اجرا نشد")
        await ask(page, f"پلن {LEAK_APP} رو یه پله ارتقا بده")
        if await has_pending(page):
            await reject(page)
        await scroll_bottom(page)

        # ---------------- chapter 4: ops & safety
        await R.scene("policy", "معیار چهارم، پایداری و امنیت. برای کارهای تکراری، کاربر می‌تواند سیاست خودکار تعریف کند؛ "
                                "مثلاً ری‌استارت خودکار وقتی حافظه از ۹۵ درصد سقف گذشت. این یک تأیید دائمی است که صریحاً از کاربر گرفته می‌شود.",
                      caption="خودکارسازی فقط با «تأیید دائمی» صریح کاربر", chapter="۳ · پایداری، امنیت و خودکارسازی")
        sw = page.locator('aside[aria-label="داشبورد برنامه"] button[role="switch"]').nth(1)
        await highlight(page, sw.locator("xpath=ancestor::section[1]"), 1.5)
        await point(page, sw)
        await pause(2)
        await R.scene("mcp", "همین ابزارها از طریق پروتکل MCP در دستیارهایی مثل Claude Desktop هم در دسترس‌اند. "
                             "اما یک هوش مصنوعی بیرونی هم نمی‌تواند خودش تغییری بدهد: درخواستش فقط به‌صورت کارت تأیید در سکان ظاهر می‌شود و من آن را رد می‌کنم.",
                      caption="درخواست ری‌استارت از یک کلاینت MCP → فقط انسان می‌تواند تأیید کند")
        print(mcp_request_restart())
        await page.reload()
        await page.wait_for_selector('textarea[aria-label="پیام به دستیار"]')
        await pause(2)
        if await has_pending(page):
            await reject(page)
        await R.scene("limits", "وقتی کاربر چیزی بخواهد که API لیارا اجازه نمی‌دهد، مثل خرید دامنه یا تنظیم ایرنیک، سکان شفاف می‌گوید ممکن نیست و راه جایگزین را می‌دهد.",
                      caption="اعلام شفاف محدودیت‌های API + راه جایگزین")
        await ask(page, "دامنه‌ی cafeland.ir رو برام بخر و توی ایرنیک هم تنظیمش کن")
        await scroll_bottom(page)
        await pause(3)
        await point(page, page.get_by_role("button", name="محدودیت‌های API"))
        await pause(4)
        await R.scene("audit", "و همه‌ی عملیات، چه خواندنی، چه درخواست تأیید، تأیید، رد و اجرا، با عامل و نتیجه در گزارش عملیات ثبت می‌شود؛ مقادیر محرمانه هم پوشانده‌اند.",
                      caption="گزارش عملیات: چه کسی، چه کاری، با چه نتیجه‌ای")
        await point(page, page.get_by_role("button", name="گزارش عملیات"))
        await pause(5)
        await point(page, page.get_by_role("button", name="دستیار", exact=True))
        await pause(1)
        await R.scene("delete", "در پایان، برنامه‌ی آزمایشی را حذف می‌کنم. حذف عملیات بحرانی است و غیرقابل بازگشت؛ برای همین علاوه بر تأیید، باید نام برنامه را دقیقاً تایپ کنم.",
                      caption="عملیات بحرانی: تأیید دومرحله‌ای با تایپ نام برنامه")
        await ask(page, f"برنامه‌ی آزمایشی {TEMP_APP} رو کامل حذف کن")
        if await has_pending(page):
            await approve(page, confirm=TEMP_APP, timeout=180)
        await scroll_bottom(page)
        await pause(2)

        # ---------------- outro
        await slide(page, OUTRO)
        await R.scene("outro", "سکان، زیرساخت را به یک گفت‌وگوی ساده و قابل‌اعتماد تبدیل می‌کند: از اولین دیپلوی تا تشخیص مشکل و نگهداری پایدار، "
                               "با شواهد، با راستی‌آزمایی، و همیشه با تأیید شما. ممنون از توجهتان.",
                      caption="سکان — همکار فنی شما روی لیارا", chapter="جمع‌بندی")
        await pause(8)
        R.finish()
        await browser.close()

    # leave the account tidy
    from app import store
    store.set_policy(LEAK_APP, "auto_restart_ram", False)
    print("done; frames:", len(R.frames))


if __name__ == "__main__":
    asyncio.run(run())
