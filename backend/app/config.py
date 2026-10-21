"""Runtime configuration — every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_DB = Path(os.getenv("STATE_DB", DATA_DIR / "state.db"))
STATIC_DIR = Path(os.getenv("STATIC_DIR", ROOT / "frontend" / "dist"))
ARCHIVE_DIR = DATA_DIR / "sources"  # every uploaded source is kept here -> rollback without a rollback API
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# --- Liara -----------------------------------------------------------------
LIARA_API = os.getenv("LIARA_API", "https://api.iran.liara.ir")
LIARA_DNS_API = os.getenv("LIARA_DNS_API", "https://dns-service.iran.liara.ir")
LIARA_TOKEN_FILE = Path(os.getenv("LIARA_TOKEN_FILE", ROOT.parent / "Liara API KEy.txt"))
LIARA_TEAM_ID = os.getenv("LIARA_TEAM_ID", "")
# "live" talks to the real Liara API; "mock" runs an in-memory simulator (tests / offline development only).
LIARA_MODE = os.getenv("LIARA_MODE", "live").lower()
APP_DOMAIN_SUFFIX = os.getenv("APP_DOMAIN_SUFFIX", "liara.run")

# --- LLM (BoxAPI, OpenAI compatible) ------------------------------------------
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://ai.boxapi.ir/ai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-6-luna")
LLM_KEY_FILE = Path(os.getenv("LLM_KEY_FILE", ROOT.parent / "api key openbox.txt"))
# The BoxAPI budget is shared by three projects; this one gets a hard cap.
LLM_BUDGET_USD = float(os.getenv("LLM_BUDGET_USD", "0.4"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "40"))
LLM_MAX_STEPS = int(os.getenv("LLM_MAX_STEPS", "8"))

# --- Monitoring -------------------------------------------------------------
MONITOR_INTERVAL_S = float(os.getenv("MONITOR_INTERVAL_S", "60"))
MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "1") == "1"
RAM_WARN_RATIO = 0.85
CPU_WARN_RATIO = 0.90
SLOW_RESPONSE_S = 2.5


def read_secret(env: str, path: Path) -> str | None:
    val = os.getenv(env)
    if val:
        return val.strip()
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None

# Poll interval while verifying an action (restart/deploy). Tests set it to 0.
POLL_S = float(os.getenv("POLL_S", "5"))
