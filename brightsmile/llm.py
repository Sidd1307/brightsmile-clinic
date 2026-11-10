"""Groq (free tier, OpenAI-compatible) client. Three jobs: extract, phrase, summarize.

Each job uses a different model so they draw on separate free-tier token budgets.
Every call has a short timeout and no retries: callers catch any exception (timeout, 429, bad JSON)
and fall back to rules/templates, so an outage degrades the bot's wording, never its correctness.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import httpx

from . import clinic as C

URL = "https://api.groq.com/openai/v1/chat/completions"

EXTRACT_SYS = """You extract structured data from a patient's message to a dental clinic receptionist.
Return ONLY a JSON object with these keys (null when not clearly stated, never guess):
name (the patient's own name), phone (as written), email,
purpose (checkup|cleaning|toothache|cosmetic|braces|implant|kids|other),
pain_level (int 0-10), swelling (bool), treatment (short), child_age (int), last_visit (short), notes (short),
date (YYYY-MM-DD, resolve "tomorrow", "next Friday", "day after tomorrow" from TODAY), time (HH:MM 24h),
part_of_day (morning|afternoon|evening), slot_choice (1-based index into OFFERED_SLOTS if they picked one),
yes_no (bool, only if the message answers a yes/no question), question (the patient's question, if they ask one),
smalltalk (bool: greeting/thanks/chit-chat with no booking info), intent ("cancel"|"reschedule"|null).
Rules: toothache = pain, swelling, sensitivity, broken/chipped tooth, bleeding gums. kids = visit for a child.
Tongue, breath, mouth, jaw or other non-tooth symptoms = checkup. Yellow/stained TEETH = cosmetic.
A bare number answers pain_level only if EXPECTING is pain_level, child_age only if EXPECTING is child_age.
"at 4"/"around 4" means 16:00. date/time/part_of_day ONLY describe when they want the APPOINTMENT; ignore dates of
other events ("before my wedding next month" gives no date). Use RECENT_CHAT to resolve references like "same time" or "the earlier one"."""

PHRASE_SYS = f"""You are {C.ASSISTANT_NAME}, the receptionist at {C.CLINIC_NAME}: warm, calm, quick, human.
Write your next message to the patient from the PLAN. Rules:
- Sound like a real receptionist, not a form. Briefly react to what the patient actually said (their words,
  their worry, their small talk) before moving on. Vary your wording; never repeat your previous message.
- Keep every fact, date, time, number and email from the PLAN exactly. Never invent times, prices, doctors or policies.
- If PATIENT_QUESTION is set: answer clinic questions only from CLINIC_FACTS. General dental questions
  (e.g. "does whitening hurt?") may get one sentence of general, widely accepted information. Never diagnose
  or guess causes of symptoms; say the dentist will check at the visit. If unsure, say the team will confirm.
- If URGENT is true, sound reassuring and prioritise getting them in.
- Use the patient's first name at most once every few messages. Don't open every message with "Hi".
- End with NEXT_QUESTION (exactly the same meaning, your own words) if one is given. "Your name"/"your number"
  always means the person chatting (a parent booking for a child gives THEIR OWN details). Never say the booking is confirmed.
- Length: {{length}}. Plain text, no lists or markdown, at most one emoji (none in voice mode).
Output only the message."""

REACT_SYS = f"""You are {C.ASSISTANT_NAME}, the warm receptionist at {C.CLINIC_NAME}.
Write ONE short sentence (max 18 words) reacting naturally to the patient's LAST message: empathy, a friendly
acknowledgement, or, if they asked a general question, a brief general answer (clinic answers only from CLINIC_FACTS).
Never diagnose. Do NOT mention any dates, days, times, numbers, prices, doctors or whether anything is booked:
the exact details follow your sentence automatically. No question marks. Output only the sentence."""

SUMMARY_SYS = """You write CRM notes for a dental clinic from a receptionist chat transcript.
Return ONLY JSON: {"summary": "2 factual sentences", "sentiment": "positive|neutral|anxious|frustrated",
"lead_score": "hot|warm|cold", "urgency": "low|medium|high", "next_action": "one short instruction for staff"}.
hot = high-value treatment (implants, braces, cosmetic) or urgent pain; warm = any other confirmed booking.
These are all confirmed bookings, so never use cold."""

_THINK = re.compile(r"<think>.*?</think>", re.S)


class GroqLLM:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self.key = api_key
        self.extract_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        self.phrase_model = os.environ.get("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
        self.summary_model = os.environ.get("GROQ_SUMMARY_MODEL", "qwen/qwen3.8-27b")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))

    async def _chat(self, model: str, system: str, user: str, json_mode: bool, max_tokens: int, temp: float = 0.0) -> str:
        body = {"model": model, "temperature": temp, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"
            body["max_tokens"] = max_tokens + 400  # reasoning tokens count against max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = await self.client.post(URL, json=body, headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        out = _THINK.sub("", r.json()["choices"][0]["message"]["content"] or "").strip()
        if not out:
            raise ValueError("empty completion")
        return out

    @staticmethod
    def _recent(st: dict, n: int) -> str:
        return "\n".join(f"{t['role']}: {t['text']}" for t in st["transcript"][-n:])

    async def extract(self, msg: str, st: dict, now: datetime) -> dict:
        offered = [C.fmt_slot(datetime.fromisoformat(o)) for o in st["offered"]]
        ctx = (f"TODAY: {now.strftime('%A %Y-%m-%d %H:%M')} (Asia/Kolkata)\nEXPECTING: {st['expecting']}\n"
               f"KNOWN: {json.dumps(st['slots'])}\nOFFERED_SLOTS: {json.dumps(offered)}\n"
               f"RECENT_CHAT:\n{self._recent(st, 5)}\n\nPATIENT_MESSAGE: {msg}")
        return json.loads(await self._chat_any(EXTRACT_SYS, ctx, max_tokens=300, temp=0.0, first=self.extract_model,
                                               json_mode=True))

    async def _chat_any(self, system: str, user: str, max_tokens: int, temp: float, first: str | None = None,
                        json_mode: bool = False) -> str:
        """Fall through to the next model when one is rate-limited (each model has its own free-tier budget)."""
        last: Exception | None = None
        for model in dict.fromkeys([first or self.phrase_model, self.phrase_model, self.summary_model, self.extract_model]):
            try:
                return await self._chat(model, system, user, json_mode=json_mode, max_tokens=max_tokens, temp=temp)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 429:
                    raise
                last = e
        raise last or RuntimeError("no model available")

    async def phrase(self, st: dict, notes: list[str], answer: str | None, question: str | None,
                     user_q: str | None = None) -> str:
        voice = bool(st.get("voice"))
        length = "1-2 short spoken sentences" if voice else "at most 3 short sentences"
        facts = f"CLINIC_FACTS: {json.dumps(C.FACTS)}\n" if user_q else ""  # only spend tokens when needed
        ctx = (f"{facts}RECENT_CHAT:\n{self._recent(st, 6)}\n\n"
               f"PLAN: {json.dumps(notes)}\nANSWER: {answer or ''}\nPATIENT_QUESTION: {user_q or ''}\n"
               f"NEXT_QUESTION: {question or ''}\nURGENT: {str(st.get('urgent', False)).lower()}\n"
               f"PATIENT_FIRST_NAME: {st['slots'].get('name', '').split(' ')[0]}\nVOICE_MODE: {str(voice).lower()}")
        out = await self._chat_any(PHRASE_SYS.replace("{length}", length), ctx, max_tokens=180, temp=0.6)
        return out.strip().strip('"')

    async def react(self, st: dict, user_q: str | None = None) -> str:
        facts = f"CLINIC_FACTS: {json.dumps(C.FACTS)}\n" if user_q else ""
        ctx = f"{facts}RECENT_CHAT:\n{self._recent(st, 4)}\nURGENT: {str(st.get('urgent', False)).lower()}"
        return (await self._chat_any(REACT_SYS, ctx, max_tokens=60, temp=0.6)).strip().strip('"')

    async def summarize(self, transcript: list[dict], lead: dict) -> dict:
        text = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
        out = json.loads(await self._chat_any(SUMMARY_SYS, f"LEAD: {json.dumps(lead)}\nTRANSCRIPT:\n{text}",
                                              max_tokens=300, temp=0.0, first=self.summary_model, json_mode=True))
        allowed = {"sentiment": {"positive", "neutral", "anxious", "frustrated"}, "lead_score": {"hot", "warm", "cold"},
                   "urgency": {"low", "medium", "high"}}
        for k, ok in allowed.items():
            if out.get(k) not in ok:
                out[k] = None
        return {k: out.get(k) for k in ("summary", "sentiment", "lead_score", "urgency", "next_action")}
