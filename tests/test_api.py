"""End-to-end through HTTP with a real SQLite DB, plus LLM integration with a mocked Groq transport."""
import asyncio
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from smilebot import clinic as C
from smilebot import flow
from smilebot.llm import GroqLLM
from smilebot.state import new_state
from smilebot.store import Db, DbScheduler, Store

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=C.TZ)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return Db()


@pytest.fixture
def client(db, monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, "build_deps",
                        lambda: flow.Deps(scheduler=DbScheduler(db), store=Store(db), now=lambda: NOW))
    monkeypatch.setattr(C, "now", lambda: NOW)
    with TestClient(appmod.app) as c:
        yield c


def say(client, state, msg):
    r = client.post("/api/chat", json={"state": state, "message": msg})
    assert r.status_code == 200, r.text
    j = r.json()
    return j["state"], j["reply"]


def test_full_booking_over_http_and_dashboard(client):
    s, r = say(client, None, "")
    assert "Welcome" in r["text"] or "welcome" in r["text"]
    for m in ["Priya Sharma", "9876543210", "priya@example.com", "teeth whitening", "Veneers", "tomorrow 4pm"]:
        s, r = say(client, s, m)
    assert r["card"]["slot"] == "Tue 10 Nov, 4:00 PM"
    s, r = say(client, s, "yes")
    ref = r["booking"]["ref"]
    assert r["links"]["ics"].endswith(".ics")
    ics = client.get(r["links"]["ics"])
    assert ics.status_code == 200 and "BEGIN:VCALENDAR" in ics.text
    assert client.get("/api/leads").status_code == 401
    leads = client.get("/api/leads", headers={"x-admin-token": "demo"}).json()["leads"]
    assert leads[0]["ref"] == ref and leads[0]["summary"]["lead_score"] == "hot" and leads[0]["transcript"]


def test_tampered_state_rejected(client):
    s, _ = say(client, None, "")
    r = client.post("/api/chat", json={"state": s[:-3] + "abc", "message": "hi"})
    assert r.status_code == 400


def test_message_too_long_rejected(client):
    assert client.post("/api/chat", json={"message": "x" * 501}).status_code == 422


async def test_db_unique_index_blocks_concurrent_double_booking(db):
    sched = DbScheduler(db)
    slot = datetime(2026, 11, 10, 16, 0, tzinfo=C.TZ)
    lead = {"name": "A", "phone": "+919876543210", "email": "a@x.com", "purpose": "checkup", "details": {}, "urgent": False}
    res = await asyncio.gather(*[sched.book(slot, lead) for _ in range(5)], return_exceptions=True)
    ok = [r for r in res if isinstance(r, dict)]
    assert len(ok) == 1 and all(type(r).__name__ == "SlotTaken" for r in res if not isinstance(r, dict))
    assert slot in await sched.busy(NOW, datetime(2026, 11, 11, tzinfo=C.TZ))


# ---------------- LLM path (mocked Groq) ----------------
def groq_mock(extract: dict, phrase: str = "AI reply?", fail: bool = False):
    def handler(req: httpx.Request):
        if fail:
            return httpx.Response(503, json={"error": "down"})
        body = json.loads(req.content)
        sys = body["messages"][0]["content"]
        if "extract structured data" in sys:
            content = json.dumps(extract)
        elif "CRM notes" in sys:
            content = json.dumps({"summary": "s", "sentiment": "calm??", "lead_score": "hot", "urgency": "low", "next_action": "x"})
        else:
            content = phrase
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return GroqLLM("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_llm_extraction_is_validated_not_trusted():
    llm = groq_mock({"name": "Rahul Sharma", "phone": "12", "email": "not-an-email", "purpose": "spaceship",
                     "date": "2026-11-10", "time": "16:00"})
    d = flow.Deps(scheduler=__import__("smilebot.booking", fromlist=["x"]).MemoryScheduler(), llm=llm, now=lambda: NOW)
    st = new_state()
    await flow.turn(st, "", d)
    r = await flow.turn(st, "hey it's rahul, call me on 12, mail not-an-email, tue 4", d)
    assert st["slots"] == {"name": "Rahul Sharma"}  # bad phone/email/purpose dropped
    assert r.text == "AI reply?" and r.used_ai


async def test_llm_outage_falls_back_to_rules():
    llm = groq_mock({}, fail=True)
    d = flow.Deps(scheduler=__import__("smilebot.booking", fromlist=["x"]).MemoryScheduler(), llm=llm, now=lambda: NOW)
    st = new_state()
    await flow.turn(st, "", d)
    r = await flow.turn(st, "Priya Sharma", d)
    assert st["slots"]["name"] == "Priya Sharma" and "Nice to meet you" in r.text and not r.used_ai


async def test_llm_summary_sanitised():
    llm = groq_mock({})
    out = await llm.summarize([{"role": "user", "text": "hi"}], {"name": "A"})
    assert out["sentiment"] is None and out["lead_score"] == "hot"
