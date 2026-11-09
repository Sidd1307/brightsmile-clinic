"""Google Calendar integration against a mocked REST API (no real credentials needed)."""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from smilebot import clinic as C
from smilebot.booking import SlotTaken
from smilebot.gcal import GoogleCalendar, patient_links
from smilebot.store import Db, DbScheduler


class _Creds:
    valid, token = True, "t"


def fake_gcal(busy_periods):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == "Bearer t"
        if req.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"clinic@x": {"busy": busy_periods}}})
        if req.method == "POST":
            return httpx.Response(200, json={"id": "ev1", "htmlLink": "https://cal/ev1"})
        return httpx.Response(204)

    g = GoogleCalendar.__new__(GoogleCalendar)
    g.creds, g.cal, g._lock = _Creds(), "clinic@x", asyncio.Lock()
    g.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return g, calls


async def test_busy_period_expands_to_slots():
    g, _ = fake_gcal([{"start": "2026-11-10T04:30:00Z", "end": "2026-11-10T05:45:00Z"}])  # 10:00–11:15 IST
    busy = await g.busy(datetime(2026, 11, 10, tzinfo=C.TZ), datetime(2026, 11, 11, tzinfo=C.TZ))
    assert busy == {datetime(2026, 11, 10, h, m, tzinfo=C.TZ) for h, m in [(10, 0), (10, 30), (11, 0)]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "g.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return Db()


LEAD = {"name": "A", "phone": "+919876543210", "email": "a@x.com", "purpose": "checkup", "details": {}, "urgent": False}


async def test_booking_creates_event_and_cancel_deletes(db):
    g, calls = fake_gcal([])
    s = DbScheduler(db, g)
    b = await s.book(datetime(2026, 11, 10, 16, tzinfo=C.TZ), LEAD)
    assert b["event_link"] == "https://cal/ev1"
    await s.cancel(b["ref"])
    assert ("DELETE", "/calendar/v3/calendars/clinic%40x/events/ev1") in calls or any(m == "DELETE" for m, _ in calls)


async def test_slot_blocked_in_calendar_is_refused(db):
    g, _ = fake_gcal([{"start": "2026-11-10T10:30:00Z", "end": "2026-11-10T11:00:00Z"}])  # 16:00 IST
    with pytest.raises(SlotTaken):
        await DbScheduler(db, g).book(datetime(2026, 11, 10, 16, tzinfo=C.TZ), LEAD)


def test_patient_links():
    l = patient_links(datetime(2026, 11, 10, 16, tzinfo=C.TZ), "BSD-ABCDE", "Dr. Meera Rao")
    assert "dates=20261110T103000Z/20261110T110000Z" in l["google"]
    assert "DTSTART:20261110T103000Z" in l["ics"] and "UID:BSD-ABCDE@smilebot" in l["ics"]
