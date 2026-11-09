"""Conversation eval: messy, realistic scripts → did the bot end in the right state?

    python scripts/eval.py          # rules-only engine
    python scripts/eval.py --ai     # with Groq (needs GROQ_API_KEY)

Each scenario lists the user's turns (ending with a confirmation) and a check on the final state.
Scenarios run concurrently — each has its own state, sharing one scheduler like real users would.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from smilebot import clinic as C, flow  # noqa: E402
from smilebot.booking import MemoryScheduler  # noqa: E402
from smilebot.state import new_state  # noqa: E402

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=C.TZ)  # Monday morning


def booked(**want):
    def check(st):
        if st["stage"] != "done":
            return f"ended in stage {st['stage']!r}"
        for k, v in want.items():
            got = st["urgent"] if k == "urgent" else st["slots"].get(k)
            if k == "slot":
                got = got and got[:16]
            if got != v:
                return f"{k}: expected {v!r}, got {got!r}"
        return None
    return check


S = [
    ("one-shot everything", ["Hi I'm Neha Gupta, 98111 22333, neha.g@example.com — need a cleaning", "about a year ago",
                             "tomorrow 11am", "yes"], booked(name="Neha Gupta", purpose="cleaning", slot="2026-11-10T11:00")),
    ("purpose first, lowercase name", ["my 6 year old needs a checkup", "arjun nair", "9822012345", "arjun@example.com",
                                       "6", "friday at 10:30 am", "yes"], booked(name="Arjun Nair", purpose="kids")),
    ("bad phone then good", ["Sara Khan", "12345", "call me on 99887 76655", "sara@example.com", "whitening",
                             "Teeth whitening", "wed 3pm", "yes"], booked(phone="+919988776655", purpose="cosmetic")),
    ("email typo accepted", ["Vikram Singh", "9876501234", "vikram@gmial.com", "yes", "braces", "Clear aligners",
                             "thursday 4 pm", "yes"], booked(email="vikram@gmail.com")),
    ("spoken email", ["Meena Iyer", "9845098450", "meena dot iyer at gmail dot com", "check-up", "never",
                      "tomorrow at 2:30pm", "yes"], booked(email="meena.iyer@gmail.com")),
    ("severe pain is urgent", ["I'm Karan Mehta", "9000012345", "karan@example.com", "my tooth is killing me", "9",
                               "no", "the first one", "yes"], booked(urgent=True, slot="2026-11-09T11:00")),
    ("swelling is urgent", ["Pooja Das", "9123412345", "pooja@example.com", "toothache", "3", "yes, my cheek is swollen",
                            "first", "yes"], booked(urgent=True)),
    ("sunday then valid", ["Rohit Jain", "9811198111", "rohit@example.com", "cleaning", "6 months ago",
                           "sunday 11am", "monday 11am", "yes"], booked(slot="2026-11-16T11:00")),
    ("lunch hour rejected", ["Rohit Jain", "9811198111", "rohit@example.com", "cleaning", "6 months ago",
                             "tomorrow 1:30pm", "tomorrow 2pm", "yes"], booked(slot="2026-11-10T14:00")),
    ("question mid-flow", ["Anil Kapoor", "how much is a consultation?", "9812312345", "anil@example.com", "checkup",
                           "last year", "do you take insurance?", "tomorrow 5pm", "yes"], booked(slot="2026-11-10T17:00")),
    ("correction at confirm", ["Lata Rao", "9876598765", "lata@example.com", "implant", "Missing tooth", "tomorrow 10am",
                               "actually my number is 9123498765", "yes"], booked(phone="+919123498765")),
    ("change slot at confirm", ["Lata Rao", "9876598765", "lata@example.com", "implant", "Crown / bridge", "tomorrow 10am",
                                "Change something", "Date & time", "friday 3pm", "yes"], booked(slot="2026-11-13T15:00")),
    ("date only then pick", ["Imran Ali", "9988001122", "imran@example.com", "cleaning", "2 years ago",
                             "next saturday morning", "the second one", "yes"], booked(slot="2026-11-14T09:30")),
    ("gibberish recovery", ["asdf ???", "Priya Nair", "phone is 98450 12345", "priya.n@example.com", "check up",
                            "never", "tomorrow 4pm", "yes"], booked(name="Priya Nair")),
    ("international number", ["Tom Baker", "+44 7700 900123", "tom@example.co.uk", "cleaning", "last month",
                              "tomorrow 9am", "yes"], booked(phone="+447700900123")),
]


async def run_one(name, turns, check, llm):
    # each scenario gets its own calendar so expected slots are deterministic
    # (the shared-calendar race is covered by tests/test_flow.py + tests/test_api.py)
    deps = flow.Deps(scheduler=MemoryScheduler(), llm=llm, now=lambda: NOW)
    st = new_state()
    await flow.turn(st, "", deps)
    for t in turns:
        await flow.turn(st, t, deps)
    return name, check(st), len(turns)


async def main(use_ai: bool):
    llm = None
    if use_ai:
        from smilebot.llm import GroqLLM
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            sys.exit("GROQ_API_KEY not set")
        llm = GroqLLM(key)
    t0 = time.perf_counter()
    if use_ai:  # sequential to stay under free-tier rate limits
        results = [await run_one(*s, llm) for s in S]
    else:  # all conversations concurrently
        results = await asyncio.gather(*(run_one(*s, llm) for s in S))
    secs = time.perf_counter() - t0
    passed = 0
    for name, err, n in results:
        passed += err is None
        print(f"  {'PASS' if err is None else 'FAIL'}  {name:<32} {n:>2} turns  {err or ''}")
    print(f"\n{passed}/{len(S)} scenarios passed ({'Groq' if use_ai else 'rules'} engine) in {secs:.1f}s")
    return passed == len(S)


if __name__ == "__main__":
    ok = asyncio.run(main("--ai" in sys.argv))
    sys.exit(0 if ok else 1)
