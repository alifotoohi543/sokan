"""Source packaging + deploy pipeline (same flow as the official CLI: tar.gz → sources → releases → build-logs)."""
from __future__ import annotations

import asyncio
import fnmatch
import tarfile
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from . import config, store
from .liara import LiaraError, app_url, client

DEFAULT_IGNORES = [".git", ".idea", ".vscode", ".next", "*.*~", "liara.json", "node_modules", "bower_components",
                   ".DS_Store", ".venv", "venv", "__pycache__", "*.pyc", ".env", ".pytest_cache", "data/state.db"]


def _ignore_patterns(root: Path) -> list[str]:
    pats = list(DEFAULT_IGNORES)
    for name in (".liaraignore", ".dockerignore", ".gitignore"):
        f = root / name
        if f.exists():
            pats += [ln.strip().rstrip("/") for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if ln.strip() and not ln.startswith("#") and not ln.startswith("!")]
            break
    return pats


def _ignored(rel: str, pats: list[str]) -> bool:
    parts = rel.split("/")
    for p in pats:
        p = p.lstrip("/")
        if fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False


def create_archive(src: str, app: str) -> tuple[Path, int, float]:
    root = Path(src).expanduser().resolve()
    pats = _ignore_patterns(root)
    out = config.ARCHIVE_DIR / f"{app}-{int(time.time())}.tar.gz"
    count = 0
    with tarfile.open(out, "w:gz") as tar:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if _ignored(rel, pats) or not p.is_file():
                continue
            tar.add(p, arcname=rel, recursive=False)
            count += 1
    return out, count, out.stat().st_size / 1e6


async def probe(app: str, path: str = "/", timeout: float = 15) -> dict:
    lc = client()
    if getattr(lc, "mode", "") == "mock":
        return await lc.probe(app)
    url = app_url(app) + path
    t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.get(url)
        # Liara's edge serves its own "Application Error" page (404/503) while a container is not routed yet.
        edge = "Liara | Application Error" in r.text[:2000]
        return {"url": url, "ok": r.status_code < 500 and not edge, "status": r.status_code, "edge_error": edge,
                "latency_s": round(time.perf_counter() - t, 2), "body": r.text[:300]}
    except httpx.HTTPError as e:
        return {"url": url, "ok": False, "status": None, "latency_s": round(time.perf_counter() - t, 2),
                "error": type(e).__name__}


async def probe_until_up(app: str, timeout: float = 150) -> dict:
    """After a release the edge answers 404/502/503/504 (its own error page) until the container is routed — wait that out."""
    t0 = time.time()
    while True:
        await asyncio.sleep(config.POLL_S)
        pr = await probe(app)
        warming = pr.get("status") in (None, 502, 503, 504) or pr.get("edge_error")
        if not warming or time.time() - t0 > timeout:
            pr["waited_s"] = round(time.time() - t0)
            return pr


async def deploy(app: str, *, path: str | None = None, source_id: str | None = None, port: int,
                 platform: str, message: str = "", envs: dict | None = None) -> AsyncIterator[dict]:
    """Yield progress events; final event has type 'done' with ok True/False."""
    lc = client()
    archive = ""
    try:
        if source_id is None:
            assert path, "path or source_id required"
            yield {"type": "progress", "stage": "package", "label": "بسته‌بندی سورس (با رعایت .gitignore/.liaraignore)"}
            arc, n, mb = await asyncio.to_thread(create_archive, path, app)
            archive = str(arc)
            yield {"type": "progress", "stage": "package", "label": f"{n} فایل، {mb:.1f} مگابایت", "done": True}
            yield {"type": "progress", "stage": "upload", "label": "آپلود سورس به لیارا"}
            source_id = await lc.upload_source(app, arc)
            yield {"type": "progress", "stage": "upload", "label": "آپلود شد", "done": True}
        body: dict[str, Any] = {"sourceID": source_id, "port": port, "type": platform,
                                "message": message or "deploy via Liara Copilot", "build": {"cache": True}}
        if envs:
            body["envs"] = envs
        release_id = await lc.create_release(app, body)
        store.record_deployment(release_id, app, source_id, archive, port, platform, message, "BUILDING")
        yield {"type": "progress", "stage": "build", "label": f"ساخت ایمیج شروع شد (release {release_id[:8]})",
               "release_id": release_id}

        since: str | None = None
        tail: list[str] = []
        deploying_sent = False
        start = time.time()
        while time.time() - start < 20 * 60:
            await asyncio.sleep(config.POLL_S / 2)
            try:
                data = await lc.build_logs(release_id, since)
            except LiaraError as e:
                if e.status is None:
                    continue
                raise
            release = data.get("release", {}) or {}
            outputs = data.get("buildOutput", []) or []
            for o in outputs:
                line = (o.get("line") or "").rstrip()
                tail = (tail + [line])[-40:]
                if o.get("stream") not in (None, "STDOUT"):
                    store.update_deployment_state(release_id, "FAILED")
                    yield {"type": "done", "ok": False, "stage": "build", "release_id": release_id,
                           "error": "build_failed", "log_tail": tail}
                    return
                yield {"type": "build_log", "line": line}
            if outputs:
                since = outputs[-1].get("createdAt") or since
            state = release.get("state")
            if state == "DEPLOYING" and not deploying_sent:
                deploying_sent = True
                yield {"type": "progress", "stage": "build", "label": "ساخت ایمیج تمام شد", "done": True}
                yield {"type": "progress", "stage": "release", "label": "راه‌اندازی کانتینر و بررسی سلامت"}
            if state in ("FAILED", "UNHEALTHY", "CANCELED", "TIMEDOUT"):
                store.update_deployment_state(release_id, state)
                yield {"type": "done", "ok": False, "stage": "release", "release_id": release_id,
                       "error": state, "reason": release.get("failReason"), "log_tail": tail}
                return
            if state == "READY":
                store.update_deployment_state(release_id, "READY")
                yield {"type": "progress", "stage": "release", "label": "نسخه‌ی جدید فعال شد", "done": True}
                yield {"type": "progress", "stage": "verify", "label": f"تست دسترسی {app_url(app)}"}
                pr = await probe_until_up(app)
                yield {"type": "progress", "stage": "verify",
                       "label": f"پاسخ HTTP {pr.get('status')} در {pr.get('latency_s')} ثانیه", "done": True, "ok": pr["ok"]}
                yield {"type": "done", "ok": pr["ok"], "release_ready": True, "release_id": release_id,
                       "url": app_url(app), "probe": pr}
                return
        yield {"type": "done", "ok": False, "error": "timeout", "release_id": release_id, "log_tail": tail}
    except LiaraError as e:
        yield {"type": "done", "ok": False, "error": e.message, "detail": e.to_dict()}
