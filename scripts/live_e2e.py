"""End-to-end checks against a deployed BrightSmile Clinic (real AI, real database).

    python scripts/live_e2e.py https://brightsmile-clinic-app.vercel.app [admin_token]

Assertions use state (stage, card, booking), not AI wording, which varies run to run.
All test patients use emails @e2e.test; run scripts/live_cleanup.py afterwards to delete them.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import hashlib
import hmac
import json
import re
import sys
import time
import zlib

import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://brightsmile-clinic-app.vercel.app"
ADMIN = sys.argv[2] if len(sys.argv) > 2 else "demo"
http = httpx.Client(timeout=40)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


class Chat:
    def __init__(self):
        self.state, self.log, self.last = None, [], None

    def say(self, msg: str) -> dict:
        try:
            r = http.post(f"{BASE}/api/chat", json={"state": self.state, "message": msg})
        except httpx.TransportError:  # a pooled keep-alive connection the edge already closed: retry once
            r = http.post(f"{BASE}/api/chat", json={"state": self.state, "message": msg})
        if r.status_code == 429:  # our own per-IP limiter: wait and retry once
            time.sleep(30)
            r = http.post(f"{BASE}/api/chat", json={"state": self.state, "message": msg})
        r.raise_for_status()
        j = r.json()
        self.state, self.last = j["state"], j["reply"]
        self.log.append((msg, j["reply"]["text"], j["latency_ms"]))
        time.sleep(0.8)  # be gentle with the free-tier LLM
        return j["reply"]

    def run(self, msgs: list[str]) -> dict:
        self.say("")
        for m in msgs:
            self.say(m)
        return self.last

    def transcript(self) -> str:
        return "\n".join(f"      U: {u}\n      A: {a[:160]}  [{ms}ms]" for u, a, ms in self.log)


def scenario(name, msgs, test):
    c = Chat()
    try:
        r = c.run(msgs)
        ok, why = test(c, r)
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"{type(e).__name__}: {e}"
    check(name, ok, why if not ok else f"{len(msgs)} turns")
    if not ok:
        print(c.transcript())
    return c


# ------------------------------------------------------------------ 1. infrastructure
print(f"\n== Infrastructure: {BASE}")
h = http.get(f"{BASE}/api/health").json()
check("health: AI on", h.get("ai") is True, json.dumps(h))
check("health: Postgres (persistent) DB", h.get("db") == "postgres")
check("branding: BrightSmile Clinic", h.get("clinic") == "BrightSmile Clinic")
page = http.get(BASE + "/").text
check("home page serves the app", "<title>BrightSmile Clinic" in page and 'id="steps"' in page)
for asset in ("/app.js", "/app.css", "/dashboard.html"):
    check(f"asset {asset}", http.get(BASE + asset).status_code == 200)


def forge(secret: bytes) -> str:
    st = {"v": 1, "stage": "collect", "expecting": "slot", "slots": {"name": "X"}, "details": {}, "fails": {},
          "offered": [], "pending_email": None, "urgent": False, "transcript": [], "booking": None, "lead_id": None}
    body = base64.urlsafe_b64encode(zlib.compress(json.dumps(st, separators=(",", ":")).encode())).decode()
    return body + "." + hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:32]


r = http.post(f"{BASE}/api/chat", json={"state": forge(b"dev-only-secret-change-me"), "message": "hi"})
check("SESSION_SECRET set (state signed with public default is rejected)", r.status_code == 400, f"HTTP {r.status_code}")
r = http.post(f"{BASE}/api/chat", json={"state": forge(b"change-me"), "message": "hi"})
check("SESSION_SECRET not the .env.example placeholder", r.status_code == 400, f"HTTP {r.status_code}")

# ------------------------------------------------------------------ 2. security / input limits
print("\n== Security & limits")
c = Chat(); c.say("")
r = http.post(f"{BASE}/api/chat", json={"state": c.state[:-4] + "abcd", "message": "hi"})
check("tampered state rejected", r.status_code == 400, f"HTTP {r.status_code}")
check("message > 500 chars rejected", http.post(f"{BASE}/api/chat", json={"message": "x" * 501}).status_code == 422)
check("dashboard API needs a code", http.get(f"{BASE}/api/leads").status_code == 401)
check("dashboard API rejects a wrong code", http.get(f"{BASE}/api/leads", headers={"x-admin-token": "nope"}).status_code == 401)

# ------------------------------------------------------------------ 3. conversations (real AI)
print("\n== Conversations (live AI)")
U = lambda n: f"{n}@e2e.test"  # noqa: E731


def booked(c, r, **want):
    if r.get("stage") != "done" or not r.get("booking"):
        return False, f"ended at stage {r.get('stage')}"
    card = r.get("card") or {}
    for k, v in want.items():
        if v not in str(card.get(k)):
            return False, f"card.{k}={card.get(k)!r}, wanted {v!r}"
    return True, ""


A = scenario("happy path: everything in one message",
             ["Hi, I'm Neha Gupta, 98111 22333, " + U("neha") + ", I need a cleaning", "about a year ago",
              "20 Nov at 11am", "yes"],
             lambda c, r: booked(c, r, name="Neha Gupta", slot="Fri 20 Nov, 11:00 AM", purpose="Cleaning"))

scenario("urgent: severe pain + swelling flagged, earliest slots offered",
         ["my tooth is killing me", "Karan Mehta", "9000012345", U("karan"), "9", "yes my cheek is swollen"],
         lambda c, r: (r.get("urgent") is True and r.get("slot_chips") and len(r["chips"]) == 3,
                       f"urgent={r.get('urgent')} chips={r.get('chips')}"))

scenario("invalid phone, then email typo suggestion",
         ["Sara Khan", "12345", "99887 76655", "sara@gmial.com"],
         lambda c, r: ("gmail.com" in " ".join(r["chips"]) and "gmial" in c.log[-1][0], f"chips={r['chips']}"))

scenario("symptom question: no diagnosis",
         ["I have a yellow tongue, is that normal?"],
         lambda c, r: (not re.search(r"(?i)\b(indicat|caused by|due to|infection|deficien|bacteria|dehydrat)", r["text"])
                       and re.search(r"(?i)dentist|diagnos", r["text"]) is not None, r["text"][:120]))

scenario("clinic FAQ mid-flow: fees answered from clinic facts",
         ["Asha Rao", "how much is a consultation?"],
         lambda c, r: ("500" in r["text"], r["text"][:120]))

scenario("Sunday rejected with alternatives",
         ["Rohit Jain", "9811198111", U("rohit"), "cleaning", "6 months ago", "this sunday at 11am"],
         lambda c, r: (r.get("slot_chips") and re.search(r"(?i)sunday|closed", r["text"]) is not None, r["text"][:120]))

scenario("small talk handled, no error",
         ["hi, how are you?"],
         lambda c, r: ("didn't quite catch" not in r["text"] and re.search(r"(?i)name", r["text"]) is not None, r["text"][:120]))

scenario("child visit with follow-up",
         ["my 6 year old needs a check up", "Arjun Nair", "9822012345", U("arjun")],
         lambda c, r: (re.search(r"(?i)old|age", r["text"]) is not None or r.get("slot_chips"), r["text"][:120]))

scenario("correction at confirmation updates the card",
         ["Lata Rao", "9876598765", U("lata"), "implant", "Missing tooth", "23 Nov at 10am",
          "actually my email is " + U("lata.r")],
         lambda c, r: (r.get("card") and r["card"]["email"] == U("lata.r") and r["stage"] == "confirm",
                       f"card={r.get('card')}"))

# cancel + reschedule on booking A
if A.last and A.last.get("booking"):
    ref = A.last["booking"]["ref"]
    ics = http.get(f"{BASE}/api/booking/{ref}.ics")
    check("calendar .ics download", ics.status_code == 200 and "BEGIN:VEVENT" in ics.text and ref in ics.text)
    rs = Chat()
    r = rs.run([f"I need to reschedule {ref}", "98111 22333", "24 Nov at 3pm", "yes"])
    ok = r.get("stage") == "done" and r.get("booking") and r["booking"]["ref"] != ref
    check("reschedule by reference (phone verified)", ok, "" if ok else r["text"][:140])
    if not ok:
        print(rs.transcript())
    new_ref = r["booking"]["ref"] if ok else ref
    old_gone = http.get(f"{BASE}/api/booking/{ref}.ics").status_code == 404
    check("old booking released after reschedule", old_gone)
    cx = Chat()
    r1 = cx.run([f"cancel {new_ref}", "9000000000"])
    check("cancel refuses the wrong phone number", re.search(r"(?i)match|verify|couldn't|can't", r1["text"]) is not None,
          r1["text"][:120])
    r2 = cx.say("98111 22333"); r3 = cx.say("yes")
    ok = http.get(f"{BASE}/api/booking/{new_ref}.ics").status_code == 404 and "cancel" in r3["text"].lower()
    check("cancel with the right phone number", ok, r3["text"][:120])

# ------------------------------------------------------------------ 4. concurrency: same slot, same moment
print("\n== Concurrency")
pair = []
for who, ph in (("Bina Das", "9123456780"), ("Chetan Rao", "9123456781")):
    c = Chat()
    c.run([who, ph, U(who.split()[0].lower()), "checkup", "never", "27 Nov at 3pm"])
    pair.append(c)
ok_setup = all(c.last.get("stage") == "confirm" for c in pair)
check("both patients reached confirmation for 27 Nov 3 PM", ok_setup)
if ok_setup:
    with cf.ThreadPoolExecutor(2) as ex:
        outs = list(ex.map(lambda c: c.say("yes"), pair))
    stages = sorted(o["stage"] for o in outs)
    loser = next((o for o in outs if o["stage"] != "done"), None)
    check("exactly one booking wins the race", stages == ["collect", "done"], f"stages={stages}")
    check("the other patient is offered alternatives", bool(loser and loser.get("slot_chips")),
          loser["text"][:100] if loser else "")

# ------------------------------------------------------------------ 5. dashboard
print("\n== Staff dashboard")
leads = http.get(f"{BASE}/api/leads", headers={"x-admin-token": ADMIN})
check("dashboard API with code", leads.status_code == 200, f"HTTP {leads.status_code}")
if leads.status_code == 200:
    mine = [l for l in leads.json()["leads"] if str(l.get("email", "")).endswith("@e2e.test")]
    check("bookings appear on the dashboard", len(mine) >= 2, f"{len(mine)} test bookings")
    check("AI summaries generated", any(l.get("ai") and (l.get("summary") or {}).get("summary") for l in mine),
          f"{sum(1 for l in mine if l.get('ai'))}/{len(mine)} AI-written")
    check("cancelled bookings marked", any(l["status"] == "cancelled" for l in mine))

lat = [ms for c in [A] for _, _, ms in c.log]
fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} checks passed" + (f"\nFAILED: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
