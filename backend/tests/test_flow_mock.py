"""End-to-end loop on the in-memory simulator: diagnose → approval → execute → verify. Never touches real Liara."""
import asyncio
import os

os.environ["LIARA_MODE"] = "mock"
os.environ["POLL_S"] = "0"
os.environ["MONITOR_ENABLED"] = "0"

import pytest  # noqa: E402

from app import actions, config, liara, store  # noqa: E402
from app.mock_liara import MockLiara  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path)
    store.init()
    m = MockLiara()
    liara.set_client(m)
    assert liara.client().mode == "mock"  # safety: tests must never hit the live API
    yield m


def run(coro):
    return asyncio.run(coro)


async def drain(gen):
    return [ev async for ev in gen]


def test_missing_env_diagnosed_and_fixed(isolated):
    d = run(actions.diagnose("shop-demo"))
    top = d["hypotheses"][0]
    assert top["id"] == "missing_env"
    assert "DATABASE_URL" in top["extra"]["missing"]
    assert any(f["action"] == "set_env" for f in top["fixes"])

    req = run(actions.request("set_env", {"app": "shop-demo", "variables": {"DATABASE_URL": "postgres://u:p@db/x"}, "reason": "t"}))
    assert req["status"] == "pending_approval"
    assert ("envs", "shop-demo") not in isolated.calls  # nothing executed before approval
    assert "••" in req["impact"]["changes"][0]["value"]  # secret masked in the card

    evs = run(drain(actions.execute(req["approval_id"])))
    assert evs[-1]["ok"] is True
    keys = {e["key"] for e in isolated.apps["shop-demo"]["envs"]}
    assert keys == {"SHOP_NAME", "DATABASE_URL"}  # existing variables preserved
    assert run(actions.diagnose("shop-demo"))["healthy"]
    assert store.get_approval(req["approval_id"])["status"] == "executed"


def test_memory_leak_detected(isolated):
    d = run(actions.diagnose("blog-demo"))
    ids = [h["id"] for h in d["hypotheses"][:3]]
    assert "memory_leak" in ids or "memory_pressure" in ids


def test_rejected_action_never_runs(isolated):
    req = run(actions.request("restart_app", {"app": "blog-demo", "reason": "t"}))
    assert actions.reject(req["approval_id"])["ok"]
    evs = run(drain(actions.execute(req["approval_id"])))
    assert evs[-1]["ok"] is False
    assert ("restart", "blog-demo") not in isolated.calls


def test_create_and_deploy(isolated, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "requirements.txt").write_text("flask\n")
    (proj / "app.py").write_text("print('hi')\n")
    (proj / ".env").write_text("SECRET=1\n")
    a = run(actions.analyze_project(str(proj)))
    assert a["platform"] == "flask" and a["recommendation"]["ok"]
    req = run(actions.request("create_app", {"name": "new-site", "plan_id": "small-g2", "bundle_plan_id": "basic", "platform": "flask"}))
    assert run(drain(actions.execute(req["approval_id"])))[-1]["ok"]
    req = run(actions.request("deploy", {"app": "new-site", "path": str(proj), "port": 80, "platform": "flask"}))
    evs = run(drain(actions.execute(req["approval_id"])))
    assert evs[-1]["ok"], evs[-1]
    import tarfile
    arc = store.deployments("new-site")[0]["archive"]
    names = tarfile.open(arc).getnames()
    assert "app.py" in names and ".env" not in names  # secrets never uploaded
