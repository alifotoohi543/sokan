"""Parsing of real Liara response shapes (captured 2026-10-22)."""
import asyncio

from app.liara import LiaraClient


def _client(payload):
    c = LiaraClient(token="x", team_id="")

    async def fake(method, path, **kw):
        return payload
    c._request = fake  # type: ignore[assignment]
    return c


def test_metric_uses_newest_container_not_sum():
    old = {"applet": "a-old", "values": [[100, "400000000"], [130, "400000000"], [160, "400000000"]]}
    new = {"applet": "a-new", "values": [[130, "30000000"], [160, "31000000"]]}
    pts = asyncio.run(_client({"result": [new, old]}).metric("x", "memory", 0))
    assert pts == [(100.0, 4e8), (130.0, 3e7), (160.0, 3.1e7)]


def test_logs_unwrap_json_and_filter_by_start():
    vals = [["1792622593787110972", '{"type":"stderr","_entry":"Listening at: http://0.0.0.0:8000"}'],
            ["1792600000000000000", '{"type":"stdout","_entry":"old line"}']]
    lines = asyncio.run(_client({"data": [{"metaData": {"releaseId": "r"}, "values": vals}]}).logs("x", 1792610000))
    assert [l["line"] for l in lines] == ["Listening at: http://0.0.0.0:8000"]
    assert lines[0]["stream"] == "stderr"
