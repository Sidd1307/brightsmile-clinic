"""Persistence: SQLite locally, Postgres when DATABASE_URL is set (Vercel).

The `bookings` table has a partial UNIQUE index on confirmed slots: that index — not application
code — is what makes double-booking impossible when two requests race.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from . import clinic as C
from .booking import SlotTaken, new_ref

log = logging.getLogger("brightsmile.store")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bookings (
        ref TEXT PRIMARY KEY, slot TEXT NOT NULL, status TEXT NOT NULL, name TEXT, phone TEXT, email TEXT,
        purpose TEXT, details TEXT, urgent INTEGER, event_id TEXT, event_link TEXT, created_at TEXT)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_slot ON bookings(slot) WHERE status = 'confirmed'",
    """CREATE TABLE IF NOT EXISTS leads (
        ref TEXT PRIMARY KEY, created_at TEXT, name TEXT, phone TEXT, email TEXT, purpose TEXT, details TEXT,
        urgent INTEGER, slot TEXT, status TEXT, summary TEXT, transcript TEXT, ai INTEGER)""",
]


class Db:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("DATABASE_URL")
        self.kind = "postgres" if self.url and self.url.startswith("postgres") else "sqlite"
        default = "/tmp/brightsmile.db" if os.environ.get("VERCEL") else "brightsmile.db"
        self.path = os.environ.get("SQLITE_PATH", default)
        self._ready = False
        self._init_lock = threading.Lock()

    def _connect(self):
        if self.kind == "postgres":
            import psycopg
            # prepare_threshold=None: Neon's pooled (PgBouncer) endpoint doesn't support prepared statements
            return psycopg.connect(self.url, autocommit=True, connect_timeout=5, prepare_threshold=None)
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def _init(self, con):
        # schema + journal-mode changes need exclusive locks: do them exactly once, not per racing request
        with self._init_lock:
            if self._ready:
                return
            if self.kind == "sqlite":
                con.execute("PRAGMA journal_mode=WAL")
            for s in SCHEMA:
                con.execute(s)
            self._ready = True

    def _run(self, sql: str, params: tuple = (), fetch: bool = False):
        if self.kind == "postgres":
            sql = sql.replace("?", "%s")
        con = self._connect()
        try:
            if not self._ready:
                self._init(con)
            cur = con.execute(sql, params)
            if fetch:
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            return None
        finally:
            con.close()

    async def run(self, sql: str, params: tuple = (), fetch: bool = False):
        return await asyncio.to_thread(self._run, sql, params, fetch)

    def is_unique_violation(self, e: Exception) -> bool:
        return isinstance(e, sqlite3.IntegrityError) or type(e).__name__ == "UniqueViolation"


def _iso(dt: datetime) -> str:
    return dt.astimezone(C.TZ).isoformat()


class DbScheduler:
    """Scheduler backed by the DB, optionally mirrored to Google Calendar."""

    def __init__(self, db: Db, gcal=None):
        self.db, self.gcal = db, gcal

    async def busy(self, start, end):
        rows_task = self.db.run("SELECT slot FROM bookings WHERE status='confirmed' AND slot >= ? AND slot < ?",
                                (_iso(start), _iso(end)), fetch=True)
        if self.gcal:  # query DB and Google Calendar concurrently
            rows, cal = await asyncio.gather(rows_task, self.gcal.busy(start, end))
        else:
            rows, cal = await rows_task, set()
        return {datetime.fromisoformat(r["slot"]) for r in rows} | cal

    async def book(self, slot, lead):
        if self.gcal and slot in await self.gcal.busy(slot, slot + timedelta(minutes=C.SLOT_MINUTES)):
            raise SlotTaken(slot)  # clinic blocked it in Google Calendar since we suggested it
        ref = new_ref()
        try:
            await self.db.run(
                "INSERT INTO bookings (ref, slot, status, name, phone, email, purpose, details, urgent, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ref, _iso(slot), "confirmed", lead["name"], lead["phone"], lead["email"], lead["purpose"],
                 json.dumps(lead["details"]), int(lead["urgent"]), _iso(C.now())))
        except Exception as e:
            if self.db.is_unique_violation(e):
                raise SlotTaken(slot) from e
            raise
        booking = {"ref": ref, "slot": slot, "status": "confirmed", **lead}
        if self.gcal:
            try:
                ev = await self.gcal.create(slot, ref, lead)
                booking.update(event_id=ev["id"], event_link=ev.get("htmlLink"))
                await self.db.run("UPDATE bookings SET event_id=?, event_link=? WHERE ref=?", (ev["id"], ev.get("htmlLink"), ref))
            except Exception as e:  # booking stands; calendar sync is best-effort
                log.error("calendar create failed for %s: %s", ref, e)
        return booking

    async def find(self, ref):
        rows = await self.db.run("SELECT * FROM bookings WHERE ref=? AND status='confirmed'", (ref,), fetch=True)
        if not rows:
            return None
        b = rows[0]
        b["slot"] = datetime.fromisoformat(b["slot"])
        b["details"] = json.loads(b["details"] or "{}")
        return b

    async def cancel(self, ref):
        b = await self.find(ref)
        if not b:
            return None
        await self.db.run("UPDATE bookings SET status='cancelled' WHERE ref=?", (ref,))
        if self.gcal and b.get("event_id"):
            try:
                await self.gcal.delete(b["event_id"])
            except Exception as e:
                log.error("calendar delete failed for %s: %s", ref, e)
        return b



def rule_summary(lead: dict) -> dict:
    """Used when no LLM is configured, so the dashboard is always populated."""
    p = lead["purpose"]
    score = "hot" if p in ("implant", "braces", "cosmetic") or lead["urgent"] else "warm"
    yn = {True: "yes", False: "no"}
    det = ", ".join(f"{k.replace('_', ' ')}: {yn[v] if isinstance(v, bool) else v}" for k, v in lead["details"].items())
    return {
        "summary": f"{lead['name']} booked for {p}" + (f" ({det})" if det else "") + ".",
        "sentiment": "anxious" if lead["urgent"] else "neutral",
        "lead_score": score, "urgency": "high" if lead["urgent"] else "low",
        "next_action": "Call before the appointment to triage" if lead["urgent"] else "Send reminder 24h before",
    }


class Store:
    def __init__(self, db: Db):
        self.db = db

    async def save_lead(self, st: dict, booking: dict, summary: dict | None):
        s = st["slots"]
        lead = {"name": s["name"], "purpose": s["purpose"], "urgent": st["urgent"],
                "details": {k: v for k, v in st["details"].items() if k not in ("urgent_noted", "pref")}}
        summ = summary or rule_summary(lead)
        await self.db.run(
            "INSERT INTO leads (ref, created_at, name, phone, email, purpose, details, urgent, slot, status, summary, transcript, ai) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (booking["ref"], _iso(C.now()), s["name"], s["phone"], s["email"], s["purpose"], json.dumps(lead["details"]),
             int(st["urgent"]), _iso(booking["slot"]), "confirmed", json.dumps(summ), json.dumps(st["transcript"]),
             int(summary is not None)))

    async def mark_cancelled(self, ref: str):
        await self.db.run("UPDATE leads SET status='cancelled' WHERE ref=?", (ref,))

    async def leads(self, limit: int = 200) -> list[dict]:
        rows = await self.db.run("SELECT * FROM leads ORDER BY created_at DESC LIMIT ?", (limit,), fetch=True)
        for r in rows:
            for k in ("details", "summary", "transcript"):
                r[k] = json.loads(r[k] or "null")
            r["urgent"] = bool(r["urgent"])
            r["ai"] = bool(r["ai"])
        return rows
