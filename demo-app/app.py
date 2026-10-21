"""«کافه‌لند» — a tiny shop used to demo Sokan on real Liara infrastructure.

Realistic failure modes, each switchable at runtime so the demo can show diagnosis + fix:
  * DATABASE_URL missing  → every page raises KeyError → HTTP 500 (fix: set the env var)
  * /admin/leak?mb=N      → keeps N MB in an unbounded "cache" → RAM climbs toward the plan limit (fix: restart / resize)
  * /admin/cpu?seconds=N  → burns CPU → slow responses (fix: resize / optimise)
"""
import os
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)
_CACHE: list[bytes] = []  # intentional unbounded cache (the "memory leak")
_START = time.time()

PRODUCTS = [
    {"id": 1, "name": "قهوه‌ی اسپرسو", "price": 95000},
    {"id": 2, "name": "کیک شکلاتی", "price": 120000},
    {"id": 3, "name": "چای ماسالا", "price": 70000},
]


def db_url() -> str:
    return os.environ["DATABASE_URL"]  # crashes with KeyError when the variable is not configured


@app.get("/")
def home():
    db_url()
    items = "".join(f"<li>{p['name']} — {p['price']:,} تومان</li>" for p in PRODUCTS)
    return f"""<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>کافه‌لند</title>
<body style="font-family:Tahoma;max-width:520px;margin:40px auto"><h1>☕ کافه‌لند</h1><ul>{items}</ul>
<small>uptime {int(time.time() - _START)}s</small></body></html>"""


@app.get("/api/products")
def products():
    db_url()
    return jsonify(PRODUCTS)


@app.get("/health")
def health():
    return {"ok": True, "uptime_s": int(time.time() - _START), "cache_mb": len(_CACHE)}


@app.get("/admin/leak")
def leak():
    mb = min(int(request.args.get("mb", 50)), 400)
    for _ in range(mb):
        _CACHE.append(b"x" * 1024 * 1024)
    print(f"cache grew to {len(_CACHE)}MB", flush=True)
    return {"cache_mb": len(_CACHE)}


@app.get("/admin/cpu")
def cpu():
    seconds = min(float(request.args.get("seconds", 60)), 600)

    def burn():
        end = time.time() + seconds
        while time.time() < end:
            sum(i * i for i in range(10_000))
    for _ in range(2):
        threading.Thread(target=burn, daemon=True).start()
    return {"burning_s": seconds}
