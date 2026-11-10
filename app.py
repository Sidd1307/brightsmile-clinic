"""FastAPI entrypoint (Vercel auto-detects `app` in app.py)."""
from __future__ import annotations

import hmac
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from smilebot import clinic as C  # noqa: E402
from smilebot import flow  # noqa: E402
from smilebot.gcal import GoogleCalendar, patient_links  # noqa: E402
from smilebot.llm import GroqLLM  # noqa: E402
from smilebot.state import BadState, sign, verify  # noqa: E402
from smilebot.store import Db, DbScheduler, Store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("smilebot.api")
PUBLIC = Path(__file__).parent / "public"


def build_deps() -> flow.Deps:
    db = Db()
    try:
        gcal = GoogleCalendar.from_env()
    except Exception as e:  # bad credentials shouldn't take the bot down
        log.error("google calendar disabled: %s", e)
        gcal = None
    key = os.environ.get("GROQ_API_KEY")
    return flow.Deps(scheduler=DbScheduler(db, gcal), llm=GroqLLM(key) if key else None, store=Store(db))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.deps = build_deps()
    yield


app = FastAPI(title="SmileBot", lifespan=lifespan)


# --- tiny per-IP rate limit (best-effort per instance; use Redis/Upstash for a fleet) ---
_hits: dict[str, deque] = defaultdict(deque)
RATE = int(os.environ.get("RATE_LIMIT_PER_MIN", "40"))


def _rate_ok(ip: str) -> bool:
    q, now = _hits[ip], time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE:
        return False
    q.append(now)
    return True


class ChatIn(BaseModel):
    state: str | None = Field(None, max_length=60_000)
    message: str = Field("", max_length=500)
    voice: bool = False


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if not _rate_ok(ip):
        raise HTTPException(429, "Too many messages — please slow down a little.")
    try:
        st = verify(body.state)
        st["voice"] = body.voice  # shorter, emoji-free replies when spoken aloud
    except BadState:
        raise HTTPException(400, "Conversation expired — please start again.")
    t0 = time.perf_counter()
    reply = await flow.turn(st, body.message, request.app.state.deps)
    ms = int((time.perf_counter() - t0) * 1000)
    log.info("turn stage=%s ai=%s ms=%d", reply.stage, reply.used_ai, ms)  # no PII in logs
    out = asdict(reply)
    if reply.booking:
        out["links"] = {"google": patient_links(datetime.fromisoformat(reply.booking["slot"]), reply.booking["ref"],
                                                reply.card["doctor"])["google"],
                        "ics": f"/api/booking/{reply.booking['ref']}.ics"}
    return {"state": sign(st), "reply": out, "latency_ms": ms}


@app.get("/api/booking/{ref}.ics")
async def ics(ref: str, request: Request):
    b = await request.app.state.deps.scheduler.find(ref.upper())
    if not b:
        raise HTTPException(404, "Booking not found")
    doctor = "Dr. Arjun Menon" if b["purpose"] in ("braces", "implant") else "Dr. Meera Rao"
    return Response(patient_links(b["slot"], b["ref"], doctor)["ics"], media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{b["ref"]}.ics"'})


def _admin(token: str | None):
    expected = os.environ.get("ADMIN_TOKEN", "demo")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "Invalid admin token")


@app.get("/api/leads")
async def leads(request: Request, x_admin_token: str | None = Header(None)):
    _admin(x_admin_token)
    return {"leads": await request.app.state.deps.store.leads()}


@app.get("/api/health")
async def health(request: Request):
    d = request.app.state.deps
    return {"ok": True, "ai": d.llm is not None, "calendar": getattr(d.scheduler, "gcal", None) is not None,
            "db": d.scheduler.db.kind, "clinic": C.CLINIC_NAME}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Something went wrong on our side. Please try again."}, status_code=500)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(PUBLIC / "index.html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(PUBLIC / "dashboard.html")


if PUBLIC.exists():
    app.mount("/", StaticFiles(directory=PUBLIC), name="static")
