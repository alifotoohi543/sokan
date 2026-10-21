"""Static analysis of a local project: platform, port, resource needs → Liara plan recommendation."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Default ports Liara expects per platform (same table the official CLI uses).
DEFAULT_PORTS = {"node": 3000, "next": 3000, "react": 80, "vue": 80, "angular": 80, "static": 80,
                 "laravel": 80, "php": 80, "django": 80, "flask": 80, "python": 80, "dotnet": 80, "go": 80}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".idea", ".vscode"}


def _read(p: Path, limit: int = 200_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _size_mb(root: Path) -> float:
    total = 0
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            total += p.stat().st_size
    return total / 1e6


def analyze(path: str) -> dict[str, Any]:
    root = Path(path).expanduser()
    if not root.is_dir():
        return {"ok": False, "error": f"پوشه‌ی «{path}» پیدا نشد"}
    files = {p.name for p in root.iterdir()}
    evidence: list[str] = []
    warnings: list[str] = []
    platform, port, framework = "static", None, None
    heavy = 0  # rough resource score

    pkg = root / "package.json"
    req = root / "requirements.txt"
    dockerfile = root / "Dockerfile"
    liara_json = root / "liara.json"

    if dockerfile.exists():
        platform = "docker"
        text = _read(dockerfile)
        m = re.search(r"^\s*EXPOSE\s+(\d+)", text, re.M | re.I)
        if m:
            port = int(m.group(1))
            evidence.append(f"Dockerfile پورت {port} را EXPOSE کرده است")
        else:
            warnings.append("Dockerfile دستور EXPOSE ندارد؛ پورت باید دستی مشخص شود")
        evidence.append("Dockerfile موجود است → پلتفرم docker")
        if re.search(r"npm (run )?build|vite build", text):
            heavy += 1
        if re.search(r"torch|tensorflow|transformers", text, re.I):
            heavy += 3
    elif pkg.exists():
        try:
            pj = json.loads(_read(pkg))
        except ValueError:
            pj = {}
        deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
        if "next" in deps:
            platform, framework = "next", "Next.js"
            heavy += 2
        elif any(k in deps for k in ("react-scripts", "vite")) and not any(k in deps for k in ("express", "fastify", "koa", "@nestjs/core")):
            platform, framework = "react", "React (static build)"
        elif "@angular/core" in deps:
            platform, framework = "angular", "Angular"
        elif "vue" in deps and "express" not in deps:
            platform, framework = "vue", "Vue"
        else:
            platform = "node"
            framework = next((n for k, n in (("@nestjs/core", "NestJS"), ("express", "Express"),
                                             ("fastify", "Fastify"), ("koa", "Koa")) if k in deps), "Node.js")
            if "start" not in pj.get("scripts", {}):
                warnings.append("اسکریپت start در package.json تعریف نشده؛ لیارا برای اجرا به آن نیاز دارد")
        evidence.append(f"package.json → {framework}")
        if any(k in deps for k in ("puppeteer", "sharp", "@tensorflow/tfjs-node")):
            heavy += 2
            evidence.append("وابستگی سنگین (puppeteer/sharp/tfjs) → RAM بیشتر")
    elif req.exists() or (root / "pyproject.toml").exists():
        text = _read(req) + _read(root / "pyproject.toml")
        low = text.lower()
        if "django" in low:
            platform, framework = "django", "Django"
        elif "flask" in low:
            platform, framework = "flask", "Flask"
        else:
            platform, framework = "python", "Python (FastAPI/other)"
            warnings.append("برای FastAPI/پایتون عمومی، Dockerfile یا Procfile با دستور uvicorn لازم است")
        evidence.append(f"requirements → {framework}")
        if re.search(r"torch|tensorflow|transformers|pandas|scikit", low):
            heavy += 2
            evidence.append("کتابخانه‌های داده/ML → RAM بیشتر")
    elif (root / "composer.json").exists():
        platform = "laravel" if (root / "artisan").exists() else "php"
        evidence.append("composer.json → PHP")
    elif (root / "go.mod").exists():
        platform = "go"
        evidence.append("go.mod → Go")
    elif "index.html" in files:
        platform = "static"
        evidence.append("فقط index.html → سایت استاتیک")

    if port is None:
        port = DEFAULT_PORTS.get(platform, 80)
    if liara_json.exists():
        try:
            lj = json.loads(_read(liara_json))
            port = lj.get("port", port)
            evidence.append("liara.json موجود است و احترام گذاشته شد")
        except ValueError:
            warnings.append("liara.json معتبر نیست")

    env_example = next((root / n for n in (".env.example", ".env.sample", "env.example") if (root / n).exists()), None)
    required_envs: list[str] = []
    if env_example:
        required_envs = [ln.split("=", 1)[0].strip() for ln in _read(env_example).splitlines()
                         if "=" in ln and not ln.strip().startswith("#")]
        evidence.append(f"{env_example.name}: {len(required_envs)} متغیر محیطی لازم دارد")
    if (root / ".env").exists():
        warnings.append("فایل .env در پروژه هست؛ آپلود نمی‌شود — مقادیرش را به‌صورت متغیر محیطی لیارا تنظیم کنید")

    uses_db = bool(re.search(r"psycopg|pg\b|mysql|mongoose|mongodb|sqlalchemy|prisma|typeorm|sequelize",
                             _read(req) + _read(pkg) + _read(root / "pyproject.toml"), re.I))
    uses_sqlite = any(p.suffix in (".db", ".sqlite", ".sqlite3") for p in root.glob("*"))
    if uses_sqlite:
        warnings.append("دیتابیس SQLite در پوشه پیدا شد؛ فایل‌سیستم کانتینر با هر دیپلوی پاک می‌شود → از دیسک لیارا یا دیتابیس مدیریت‌شده استفاده کنید")

    size = _size_mb(root)
    if size > 120:
        warnings.append(f"حجم سورس {size:.0f}MB است؛ سقف آپلود پلن پایه ۱۲۸MB است — فایل‌های اضافه را در .liaraignore بگذارید")

    if platform in ("static", "react", "vue", "angular"):
        need_ram = 0.5
    elif heavy >= 3:
        need_ram = 2
    elif heavy >= 1 or platform in ("next", "django", "laravel"):
        need_ram = 1
    else:
        need_ram = 0.5

    return {"ok": True, "path": str(root), "platform": platform, "framework": framework, "port": port,
            "size_mb": round(size, 1), "required_envs": required_envs, "uses_external_db": uses_db,
            "uses_sqlite": uses_sqlite, "need_ram_gb": need_ram, "evidence": evidence, "warnings": warnings,
            "has_dockerfile": dockerfile.exists()}


def recommend_plan(analysis: dict, plans: list[dict], bundles: dict, traffic: str = "low") -> dict:
    """Pick the cheapest g2 plan that satisfies the RAM need (bumped for expected traffic)."""
    need = analysis.get("need_ram_gb", 0.5) * {"low": 1, "medium": 2, "high": 4}.get(traffic, 1)
    candidates = [p for p in plans if p["g2"]] or plans
    choice = next((p for p in candidates if p["ram_gb"] >= need), candidates[-1] if candidates else None)
    if not choice:
        return {"ok": False, "error": "هیچ پلن فعالی در منطقه‌ی ایران پیدا نشد"}
    fam = bundles.get(choice["id"], {})
    # "basic" keeps 7 days of logs and allows rollback — the minimum we consider safe for a real site.
    bundle_id = "basic" if fam.get("basic", {}).get("available") else next(
        (k for k, v in fam.items() if v.get("available")), None)
    bundle = fam.get(bundle_id or "", {})
    total = choice["monthly_toman"] + (bundle.get("monthly_toman") or 0)
    alt_up = next((p for p in candidates if p["ram_gb"] > choice["ram_gb"]), None)
    return {
        "ok": True, "plan": choice, "bundle_id": bundle_id, "bundle": bundle, "monthly_toman": total,
        "daily_toman": round(total / 30),
        "why": [f"نیاز تخمینی RAM: {need:g} گیگ (پلتفرم {analysis.get('framework') or analysis.get('platform')}، ترافیک {traffic})",
                f"ارزان‌ترین پلن نسل ۲ با این ظرفیت: {choice['id']} ({choice['ram_gb']}GB RAM / {choice['cpu']} CPU)",
                f"بسته‌ی امکانات {bundle_id}: نگهداری لاگ {bundle.get('logs_days')} روز، rollback: {'دارد' if bundle.get('rollback') else 'ندارد'}"],
        "upgrade_path": alt_up,
    }
