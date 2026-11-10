"""Delete end-to-end test data (emails @e2e.test) from the configured DATABASE_URL.

    python scripts/live_cleanup.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from brightsmile.store import Db  # noqa: E402


async def main():
    db = Db()
    leads = await db.run("SELECT count(*) AS n FROM leads WHERE email LIKE ?", ("%@e2e.test",), fetch=True)
    books = await db.run("SELECT count(*) AS n FROM bookings WHERE email LIKE ?", ("%@e2e.test",), fetch=True)
    await db.run("DELETE FROM leads WHERE email LIKE ?", ("%@e2e.test",))
    await db.run("DELETE FROM bookings WHERE email LIKE ?", ("%@e2e.test",))
    left = await db.run("SELECT count(*) AS n FROM leads", fetch=True)
    print(f"removed {leads[0]['n']} test leads and {books[0]['n']} test bookings ({db.kind}); {left[0]['n']} leads remain")


asyncio.run(main())
