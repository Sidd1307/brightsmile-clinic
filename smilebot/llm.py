"""Groq (free tier, OpenAI-compatible) client. Three jobs: extract, phrase, summarize.

Every call has a short timeout. Callers catch any exception and fall back to rules/templates,
so an outage or rate limit degrades the bot's wording — never its correctness.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import httpx

from . import clinic as C

URL = "https://api.groq.com/openai/v1/chat/completions"

EXTRACT_SYS = """You extract structured data from a patient's message to a dental clinic receptionist.
Return ONLY a JSON object with these keys (use null when not clearly stated — never guess):
name (string, the patient's own name), phone (string as written), email (string),
purpose (one of: checkup, cleaning, toothache, cosmetic, braces, implant, kids, other),
pain_level (integer 0-10), swelling (boolean), treatment (short string), child_age (integer),
last_visit (short string), notes (short string),
date (YYYY-MM-DD, resolve relative words like "tomorrow"/"next Friday" using TODAY), time (HH:MM 24h),
part_of_day (morning|afternoon|evening), slot_choice (1-based index into OFFERED_SLOTS if they picked one),
yes_no (true/false if the message is a yes/no answer), question (string if they ask the clinic something),
intent ("cancel" | "reschedule" if they want to change an existing booking, else null).
Toothache covers pain, swelling, sensitivity, broken/chipped teeth, bleeding gums. kids = appointment for a child.
Bare numbers 1-10 answer pain_level only when EXPECTING is pain_level. "at 4" in clinic context means 16:00."""

PHRASE_SYS = f"""You are {C.ASSISTANT_NAME}, the warm, professional virtual receptionist at {C.CLINIC_NAME}.
Rewrite the receptionist's planned turn as ONE natural message. Rules:
- Keep every fact, number, date, time and email exactly as given. Never invent times, prices, doctors or policies.
- If PATIENT_QUESTION is present and ANSWER is empty, answer it ONLY from CLINIC_FACTS; if the facts don't cover it,
  say the team will confirm when they call.
- Keep it under 3 short sentences (it may be spoken aloud). No lists, no markdown, at most one emoji.
- End with the NEXT_QUESTION (same meaning) if one is given. Never say the appointment is booked/confirmed.
Output only the message text."""

SUMMARY_SYS = """You write CRM notes for a dental clinic from a receptionist chat transcript.
Return ONLY JSON: {"summary": "2 sentences, factual", "sentiment": "positive|neutral|anxious|frustrated",
"lead_score": "hot|warm|cold", "urgency": "low|medium|high", "next_action": "one short instruction for staff"}.
hot = high-value treatment (implants, braces, cosmetic) or urgent pain; cold = just browsing."""


class GroqLLM:
    def __init__(self, api_key: str, model: str | None = None, client: httpx.AsyncClient | None = None):
        self.key = api_key
        self.model = model or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.fast_model = os.environ.get("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))

    async def _chat(self, model: str, system: str, user: str, json_mode: bool, max_tokens: int = 300, temp: float = 0.0) -> str:
        body = {"model": model, "temperature": temp, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = await self.client.post(URL, json=body, headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    async def extract(self, msg: str, st: dict, now: datetime) -> dict:
        offered = [C.fmt_slot(datetime.fromisoformat(o)) for o in st["offered"]]
        ctx = (f"TODAY: {now.strftime('%A %Y-%m-%d %H:%M')} (Asia/Kolkata)\nEXPECTING: {st['expecting']}\n"
               f"KNOWN: {json.dumps(st['slots'])}\nOFFERED_SLOTS: {json.dumps(offered)}\n"
               f"LAST_BOT_MESSAGE: {next((t['text'] for t in reversed(st['transcript'][:-1]) if t['role'] == 'bot'), '')}\n"
               f"PATIENT_MESSAGE: {msg}")
        return json.loads(await self._chat(self.model, EXTRACT_SYS, ctx, json_mode=True))

    async def phrase(self, st: dict, notes: list[str], answer: str | None, question: str | None,
                     user_q: str | None = None) -> str:
        history = "\n".join(f"{t['role']}: {t['text']}" for t in st["transcript"][-6:])
        ctx = (f"CLINIC_FACTS: {json.dumps(C.FACTS)}\nRECENT_CHAT:\n{history}\n\nPLANNED_NOTES: {json.dumps(notes)}\n"
               f"ANSWER: {answer or ''}\nPATIENT_QUESTION: {user_q or ''}\nNEXT_QUESTION: {question or ''}\n"
               f"PATIENT_FIRST_NAME: {st['slots'].get('name', '').split(' ')[0]}")
        out = await self._chat(self.fast_model, PHRASE_SYS, ctx, json_mode=False, max_tokens=160, temp=0.4)
        return out.strip('"')

    async def summarize(self, transcript: list[dict], lead: dict) -> dict:
        text = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
        out = json.loads(await self._chat(self.model, SUMMARY_SYS, f"LEAD: {json.dumps(lead)}\nTRANSCRIPT:\n{text}",
                                          json_mode=True, max_tokens=250))
        allowed = {"sentiment": {"positive", "neutral", "anxious", "frustrated"}, "lead_score": {"hot", "warm", "cold"},
                   "urgency": {"low", "medium", "high"}}
        for k, ok in allowed.items():
            if out.get(k) not in ok:
                out[k] = None
        return {k: out.get(k) for k in ("summary", "sentiment", "lead_score", "urgency", "next_action")}
