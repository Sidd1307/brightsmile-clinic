"""Run a few free-form conversations against the real Groq LLM and print them, to review tone by eye.

    python scripts/try_ai.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from brightsmile import flow  # noqa: E402
from brightsmile.booking import MemoryScheduler  # noqa: E402
from brightsmile.llm import GroqLLM  # noqa: E402
from brightsmile.state import new_state  # noqa: E402

CONVOS = [
    ["hi, how are you?", "I have yellow tongue, is that normal?", "it's Siddharth Rao",
     "you can reach me at 98765 43210", "sid.rao@gmail.com", "hmm maybe 2 years ago, I'm a bit scared of dentists honestly",
     "does a checkup hurt?", "can I come thursday evening?", "the first one", "yes"],
    ["hey my son broke his front tooth playing cricket, he's 9, can we come today?", "Ananya Shah",
     "9819012345", "ananya.shah@outlook.com", "how much will it cost?", "ok the earliest please", "yes book it"],
    ["I want to get my teeth whitened before my wedding next month", "Priya, Priya Menon", "+91 99000 11223",
     "priya.menon@gmail.com", "how long does whitening last?", "Teeth whitening", "saturday morning", "second", "yes"],
]


async def main():
    llm = GroqLLM(os.environ["GROQ_API_KEY"])
    pick = [int(a) for a in sys.argv[1:] if a.isdigit()]  # e.g. `try_ai.py 2 3` runs conversations 2 and 3
    for i, convo in enumerate(CONVOS, 1):
        if pick and i not in pick:
            continue
        d = flow.Deps(scheduler=MemoryScheduler(), llm=llm)
        st = new_state()
        r = await flow.turn(st, "", d)
        print(f"\nARIA: {r.text}")
        for m in convo:
            t = time.perf_counter()
            r = await flow.turn(st, m, d)
            print(f"\n  YOU: {m}\nARIA: {r.text}   [{'AI' if r.used_ai else 'rules'} {time.perf_counter() - t:.1f}s]"
                  + (f"  chips={r.chips}" if r.chips else ""))
        print("\nFINAL:", st["stage"], st["slots"], st["details"], "urgent" if st["urgent"] else "")
        print("=" * 100)


asyncio.run(main())
