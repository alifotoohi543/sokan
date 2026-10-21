"""SQLite state: audit trail, approvals, alerts, deployments, chat sessions, LLM spend, policies."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, app TEXT, action TEXT, risk TEXT,
  params TEXT, status TEXT, result TEXT, approval_id TEXT);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, ts REAL, session TEXT, app TEXT, action TEXT, params TEXT, risk TEXT,
  title TEXT, impact TEXT, status TEXT, decided_ts REAL, result TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, app TEXT, severity TEXT, kind TEXT, title TEXT,
  detail TEXT, resolved INTEGER DEFAULT 0, last_seen REAL);
CREATE TABLE IF NOT EXISTS deployments (
  release_id TEXT PRIMARY KEY, ts REAL, app TEXT, source_id TEXT, archive TEXT, port INTEGER,
  platform TEXT, message TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, ts REAL, messages TEXT);
CREATE TABLE IF NOT EXISTS llm_spend (usd REAL, calls INTEGER);
CREATE TABLE IF NOT EXISTS policies (app TEXT, kind TEXT, enabled INTEGER, PRIMARY KEY(app, kind));
CREATE TABLE IF NOT EXISTS health (ts REAL, app TEXT, ok INTEGER, status INTEGER, latency REAL,
  cpu REAL, ram REAL, ram_limit REAL, errors INTEGER);
"""


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(config.STATE_DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with db() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM llm_spend").fetchone()[0] == 0:
            c.execute("INSERT INTO llm_spend VALUES (0, 0)")


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- audit
def audit(actor: str, app: str | None, action: str, risk: str, params: Any, status: str,
          result: Any = None, approval_id: str | None = None) -> None:
    with db() as c:
        c.execute("INSERT INTO audit(ts,actor,app,action,risk,params,status,result,approval_id) VALUES(?,?,?,?,?,?,?,?,?)",
                  (time.time(), actor, app, action, risk, _j(params), status, _j(result)[:4000], approval_id))


def audit_log(limit: int = 100, app: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM audit", []
    if app:
        q, args = q + " WHERE app=?", [app]
    with db() as c:
        rows = c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) | {"params": json.loads(r["params"] or "null")} for r in rows]


# ---------------------------------------------------------------- approvals
def create_approval(session: str, app: str | None, action: str, params: dict, risk: str,
                    title: str, impact: dict) -> dict:
    aid = uuid.uuid4().hex[:12]
    with db() as c:
        c.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, time.time(), session, app, action, _j(params), risk, title, _j(impact), "pending", None, None))
    return get_approval(aid)  # type: ignore[return-value]


def get_approval(aid: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    if not r:
        return None
    return dict(r) | {"params": json.loads(r["params"]), "impact": json.loads(r["impact"]),
                      "result": json.loads(r["result"]) if r["result"] else None}


def decide_approval(aid: str, status: str, result: Any = None) -> None:
    with db() as c:
        c.execute("UPDATE approvals SET status=?, decided_ts=?, result=? WHERE id=?",
                  (status, time.time(), _j(result) if result is not None else None, aid))


def pending_approvals() -> list[dict]:
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM approvals WHERE status='pending' ORDER BY ts DESC")]
    return [a for a in (get_approval(i) for i in ids) if a]


# ---------------------------------------------------------------- alerts
def raise_alert(app: str, severity: str, kind: str, title: str, detail: str) -> bool:
    """Insert or refresh an open alert; returns True when it is new."""
    now = time.time()
    with db() as c:
        r = c.execute("SELECT id FROM alerts WHERE app=? AND kind=? AND resolved=0", (app, kind)).fetchone()
        if r:
            c.execute("UPDATE alerts SET last_seen=?, detail=?, title=?, severity=? WHERE id=?",
                      (now, detail, title, severity, r[0]))
            return False
        c.execute("INSERT INTO alerts(ts,app,severity,kind,title,detail,last_seen) VALUES(?,?,?,?,?,?,?)",
                  (now, app, severity, kind, title, detail, now))
        return True


def resolve_alerts(app: str, kinds: list[str] | None = None, keep: set[str] | None = None) -> None:
    with db() as c:
        rows = c.execute("SELECT id, kind FROM alerts WHERE app=? AND resolved=0", (app,)).fetchall()
        for r in rows:
            if (kinds is None or r["kind"] in kinds) and not (keep and r["kind"] in keep):
                c.execute("UPDATE alerts SET resolved=1 WHERE id=?", (r["id"],))


def alerts(open_only: bool = True, app: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM alerts WHERE 1=1", []
    if open_only:
        q += " AND resolved=0"
    if app:
        q, args = q + " AND app=?", [app]
    with db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY ts DESC LIMIT 50", args)]


# ---------------------------------------------------------------- deployments
def record_deployment(release_id: str, app: str, source_id: str, archive: str, port: int,
                      platform: str, message: str, state: str) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO deployments VALUES(?,?,?,?,?,?,?,?,?)",
                  (release_id, time.time(), app, source_id, archive, port, platform, message, state))


def update_deployment_state(release_id: str, state: str) -> None:
    with db() as c:
        c.execute("UPDATE deployments SET state=? WHERE release_id=?", (state, release_id))


def deployments(app: str) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM deployments WHERE app=? ORDER BY ts DESC", (app,))]


# ---------------------------------------------------------------- sessions
def load_session(sid: str) -> list[dict]:
    with db() as c:
        r = c.execute("SELECT messages FROM sessions WHERE id=?", (sid,)).fetchone()
    return json.loads(r[0]) if r else []


def save_session(sid: str, messages: list[dict]) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (sid, time.time(), _j(messages)))


# ---------------------------------------------------------------- llm spend
def llm_spent() -> dict:
    with db() as c:
        usd, calls = c.execute("SELECT usd, calls FROM llm_spend").fetchone()
    return {"usd": round(usd, 5), "calls": calls, "cap": config.LLM_BUDGET_USD, "model": config.LLM_MODEL}


def add_llm_spend(usd: float) -> None:
    with db() as c:
        c.execute("UPDATE llm_spend SET usd = usd + ?, calls = calls + 1", (usd,))


# ---------------------------------------------------------------- policies (standing approvals)
def set_policy(app: str, kind: str, enabled: bool) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO policies VALUES(?,?,?)", (app, kind, int(enabled)))


def policies(app: str) -> dict[str, bool]:
    with db() as c:
        return {r["kind"]: bool(r["enabled"]) for r in c.execute("SELECT * FROM policies WHERE app=?", (app,))}


# ---------------------------------------------------------------- health history
def add_health(app: str, ok: bool, status: int | None, latency: float | None, cpu: float | None,
               ram: float | None, ram_limit: float | None, errors: int) -> None:
    with db() as c:
        c.execute("INSERT INTO health VALUES(?,?,?,?,?,?,?,?,?)",
                  (time.time(), app, int(ok), status, latency, cpu, ram, ram_limit, errors))
        c.execute("DELETE FROM health WHERE ts < ?", (time.time() - 3 * 86400,))


def health_history(app: str, since_s: float = 3600) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM health WHERE app=? AND ts>? ORDER BY ts",
                                           (app, time.time() - since_s))]
