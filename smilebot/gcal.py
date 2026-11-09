"""Google Calendar over its REST API (async httpx; google-auth only mints the service-account token).

Setup: create a service account, share your test calendar with its email ("Make changes to events"),
then set GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON or base64) and GOOGLE_CALENDAR_ID.
Service accounts can't invite attendees without Workspace domain-wide delegation, so the patient gets an
"Add to my calendar" link / .ics instead of an invite.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from . import clinic as C

SCOPES = ["https://www.googleapis.com/auth/calendar"]
API = "https://www.googleapis.com/calendar/v3"


class GoogleCalendar:
    def __init__(self, info: dict, calendar_id: str, client: httpx.AsyncClient | None = None):
        from google.oauth2 import service_account

        self.creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        self.cal = calendar_id
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "GoogleCalendar | None":
        raw, cal = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"), os.environ.get("GOOGLE_CALENDAR_ID")
        if not raw or not cal:
            return None
        if not raw.lstrip().startswith("{"):
            raw = base64.b64decode(raw).decode()
        return cls(json.loads(raw), cal)

    async def _headers(self) -> dict:
        async with self._lock:  # one refresh even if many requests need a token at once
            if not self.creds.valid:
                from google.auth.transport.requests import Request
                await asyncio.to_thread(self.creds.refresh, Request())
        return {"Authorization": f"Bearer {self.creds.token}"}

    async def _req(self, method: str, path: str, **kw) -> dict:
        r = await self.client.request(method, API + path, headers=await self._headers(), **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def busy(self, start: datetime, end: datetime) -> set[datetime]:
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": str(C.TZ), "items": [{"id": self.cal}]}
        periods = (await self._req("POST", "/freeBusy", json=body))["calendars"][self.cal].get("busy", [])
        out: set[datetime] = set()
        for p in periods:  # expand busy periods into the 30-min slots they overlap
            s = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).astimezone(C.TZ)
            e = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).astimezone(C.TZ)
            t = s.replace(minute=0 if s.minute < 30 else 30, second=0, microsecond=0)
            while t < e:
                out.add(t)
                t += timedelta(minutes=C.SLOT_MINUTES)
        return out

    async def create(self, slot: datetime, ref: str, lead: dict) -> dict:
        details = "\n".join(f"{k.replace('_', ' ')}: {v}" for k, v in lead["details"].items())
        body = {
            "summary": f"{'URGENT · ' if lead['urgent'] else ''}{lead['name']} ({lead['purpose']}) {ref}",
            "description": f"Phone: {lead['phone']}\nEmail: {lead['email']}\n{details}\n\nBooked by SmileBot",
            "start": {"dateTime": slot.isoformat(), "timeZone": str(C.TZ)},
            "end": {"dateTime": (slot + timedelta(minutes=C.SLOT_MINUTES)).isoformat(), "timeZone": str(C.TZ)},
            "colorId": "11" if lead["urgent"] else "9",
        }
        return await self._req("POST", f"/calendars/{quote(self.cal)}/events", json=body)

    async def delete(self, event_id: str) -> None:
        await self._req("DELETE", f"/calendars/{quote(self.cal)}/events/{quote(event_id)}")


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def patient_links(slot: datetime, ref: str, doctor: str) -> dict:
    end = slot + timedelta(minutes=C.SLOT_MINUTES)
    title = f"Dental appointment at {C.CLINIC_NAME}"
    detail = f"Booking ref {ref} with {doctor}. Please arrive 10 minutes early."
    google = ("https://calendar.google.com/calendar/render?action=TEMPLATE"
              f"&text={quote(title)}&dates={_utc(slot)}/{_utc(end)}&details={quote(detail)}&location={quote(C.FACTS['location'])}")
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SmileBot//EN", "BEGIN:VEVENT", f"UID:{ref}@smilebot",
        f"DTSTAMP:{_utc(C.now())}", f"DTSTART:{_utc(slot)}", f"DTEND:{_utc(end)}", f"SUMMARY:{title}",
        f"DESCRIPTION:{detail}", "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", "DESCRIPTION:Dental appointment",
        "END:VALARM", "END:VEVENT", "END:VCALENDAR", ""])
    return {"google": google, "ics": ics}
