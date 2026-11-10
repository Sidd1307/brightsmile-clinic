"""Smoke-test the configured DATABASE_URL (e.g. Neon): schema, a 5-way booking race, cleanup.

    python scripts/check_db.py
Never prints the connection string.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from brightsmile import clinic as C  # noqa: E402
from brightsmile.booking import SlotTaken  # noqa: E402
from brightsmile.store import Db, DbScheduler  # noqa: E402

LEAD = {"name": "DB Check", "phone": "+910000000000", "email": "check@example.com", "purpose": "checkup",
        "details": {}, "urgent": False}


async def main():
    db = Db()
    if db.kind != "postgres":
        sys.exit("DATABASE_URL is not a Postgres URL; nothing to check.")
    host = db.url.split("@")[-1].split("/")[0]
    print(f"Connecting to Postgres at {host.split('.')[0]}… (credentials hidden)")
    sched = DbScheduler(db)
    slot = datetime(2030, 1, 7, 10, 0, tzinfo=C.TZ)  # far-future Monday, never a real booking
    await db.run("DELETE FROM bookings WHERE phone = ?", (LEAD["phone"],))
    res = await asyncio.gather(*[sched.book(slot, LEAD) for _ in range(5)], return_exceptions=True)
    ok = [r for r in res if isinstance(r, dict)]
    taken = [r for r in res if isinstance(r, SlotTaken)]
    other = [r for r in res if not isinstance(r, (dict, SlotTaken))]
    await db.run("DELETE FROM bookings WHERE phone = ?", (LEAD["phone"],))
    print(f"5 concurrent bookings of one slot -> {len(ok)} succeeded, {len(taken)} refused as taken, {len(other)} errors")
    if other:
        print("errors:", [f"{type(e).__name__}: {e}" for e in other])
    good = len(ok) == 1 and len(taken) == 4 and not other
    print("Postgres OK, test rows cleaned up" if good else "Postgres check FAILED")
    return good


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
