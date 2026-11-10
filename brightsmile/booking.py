"""Scheduling backends. `Scheduler` is what the conversation flow talks to."""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from typing import Protocol


class SlotTaken(Exception):
    pass


class Scheduler(Protocol):
    async def busy(self, start: datetime, end: datetime) -> set[datetime]: ...
    async def book(self, slot: datetime, lead: dict) -> dict: ...
    async def cancel(self, ref: str) -> dict | None: ...
    async def find(self, ref: str) -> dict | None: ...


def new_ref() -> str:
    return "BSD-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))


class MemoryScheduler:
    """In-process scheduler for tests. The lock makes check-and-insert atomic, like a DB unique index."""

    def __init__(self, preset_busy: set[datetime] | None = None):
        self.bookings: dict[str, dict] = {}
        self.preset = set(preset_busy or ())
        self._lock = asyncio.Lock()

    def _taken(self) -> set[datetime]:
        return self.preset | {b["slot"] for b in self.bookings.values() if b["status"] == "confirmed"}

    async def busy(self, start, end):
        return {s for s in self._taken() if start <= s < end}

    async def book(self, slot, lead):
        async with self._lock:
            if slot in self._taken():
                raise SlotTaken(slot)
            await asyncio.sleep(0)  # yield inside the critical section — proves the lock matters
            ref = new_ref()
            self.bookings[ref] = {"ref": ref, "slot": slot, "status": "confirmed", **lead}
            return self.bookings[ref]

    async def cancel(self, ref):
        b = self.bookings.get(ref)
        if b and b["status"] == "confirmed":
            b["status"] = "cancelled"
            return b
        return None

    async def find(self, ref):
        b = self.bookings.get(ref)
        return b if b and b["status"] == "confirmed" else None
