"""Conversation engine: a slot-filling state machine.

Code owns every transition. The optional LLM only (a) proposes extracted values, which are then
validated here, and (b) rephrases the reply we've already decided on.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from . import clinic as C
from . import validators as V
from .booking import Scheduler, SlotTaken

log = logging.getLogger("smilebot.flow")

CONTACT = ["name", "phone", "email", "purpose"]
FOLLOWUPS = {
    "toothache": ["pain_level", "swelling"], "cosmetic": ["treatment"], "braces": ["treatment"],
    "implant": ["treatment"], "kids": ["child_age"], "checkup": ["last_visit"], "cleaning": ["last_visit"],
    "other": ["notes"],
}
PURPOSE_CHIPS = ["Check-up", "Cleaning", "Tooth pain", "Whitening", "Braces", "Implants", "Child's visit"]
QUESTIONS: dict[str, tuple[str, list[str]]] = {
    "name": ("May I have your full name, please?", []),
    "phone": ("What's the best mobile number to reach you on?", []),
    "email": ("And your email address? I'll send your confirmation there.", []),
    "purpose": ("What would you like to come in for?", PURPOSE_CHIPS),
    "pain_level": ("On a scale of 1 to 10, how bad is the pain right now?",
                   ["Mild (1–3)", "Moderate (4–6)", "Severe (7–10)"]),
    "swelling": ("Is there any swelling in your face or gums, or a fever?", ["Yes", "No"]),
    "treatment": ("Which treatment are you most interested in?", []),
    "child_age": ("How old is your child?", []),
    "last_visit": ("When did you last visit a dentist?", ["Within 6 months", "6–12 months ago", "Over a year ago", "Never / not sure"]),
    "notes": ("Could you tell me a little more about what you need?", []),
    "slot": ("When would you like to come in? You can say something like \"tomorrow at 4 pm\", or pick one of these:", []),
}
TREATMENT_Q = {
    "cosmetic": ("Which cosmetic treatment interests you?", ["Teeth whitening", "Veneers", "Smile makeover", "Not sure yet"]),
    "braces": ("Are you leaning towards clear aligners or traditional braces?", ["Clear aligners", "Metal braces", "Ceramic braces", "Not sure yet"]),
    "implant": ("What do you need help with?", ["Missing tooth", "Crown / bridge", "Dentures", "Not sure yet"]),
}
HINTS = {
    "name": ("I didn't quite catch your name.", "Please type just your name, for example \"Priya Sharma\"."),
    "phone": ("That number doesn't look quite right.", "Please share a 10-digit mobile number like 98765 43210, or include your country code (+44 …)."),
    "email": ("That email address looks incomplete.", "It should look like name@example.com."),
    "purpose": ("I'm not sure what you'd like to come in for.", "You can pick one of the options below or describe it in a few words."),
    "pain_level": ("I didn't get the pain level.", "Just a number from 1 (mild) to 10 (unbearable) is perfect."),
    "swelling": ("Sorry, was that a yes or a no?", "Please answer yes or no."),
    "child_age": ("I didn't catch the age.", "Just the number of years is fine, e.g. 7."),
    "slot": ("I couldn't work out a date and time from that.", "Try something like \"Friday at 11 am\" or \"20 Nov, 3:30 pm\", or tap a slot."),
}
PURPOSE_ACK = {
    "toothache": "I'm sorry to hear you're in pain — let's get you seen quickly.",
    "kids": "Lovely, we see lots of little patients!",
    "cosmetic": "Great, we can definitely help with your smile.",
    "braces": "Sure, our orthodontist can help with that.",
}
FIELD_LABELS = {"name": "Name", "phone": "Phone", "email": "Email", "purpose": "Reason", "slot": "Date & time"}
_REF_RE = re.compile(r"\bBSD-?([A-Z2-9]{5})\b", re.I)
_WORD_NUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen".split())}


@dataclass
class Reply:
    text: str
    chips: list[str] = field(default_factory=list)
    card: dict | None = None
    stage: str = "collect"
    urgent: bool = False
    booking: dict | None = None
    used_ai: bool = False


@dataclass
class Deps:
    scheduler: Scheduler
    llm: Any | None = None          # smilebot.llm.GroqLLM or None
    store: Any | None = None        # smilebot.store.Store or None
    now: Callable[[], datetime] = C.now


# ---------------------------------------------------------------- extraction
def _num(text: str, lo: int, hi: int) -> int | None:
    m = re.search(r"\b(\d{1,2})\b", text)
    n = int(m.group(1)) if m else next((v for w, v in _WORD_NUM.items() if re.search(rf"\b{w}\b", text.lower())), None)
    return n if n is not None and lo <= n <= hi else None


def rule_extract(msg: str, st: dict, now: datetime) -> dict:
    """Regex/dateparser extraction. Always runs; fills whatever the LLM missed."""
    exp = st["expecting"]
    ex: dict[str, Any] = {
        "name": V.extract_name(msg, expecting=exp == "name"),
        "phone": V.extract_phone(msg),
        "email": V.extract_email(msg),
        "purpose": V.classify_purpose(msg),
        "date": V.parse_date(msg, now),
        "time": V.parse_time(msg),
        "part_of_day": V.part_of_day(msg),
        "yes_no": V.yes_no(msg),
        "question": C.faq_answer(msg) and msg,
        "ref": (m := _REF_RE.search(msg)) and f"BSD-{m.group(1).upper()}",
    }
    t = msg.lower()
    ex["intent"] = ("cancel" if re.search(r"\bcancel\b", t) else
                    "reschedule" if re.search(r"\b(reschedule|move my|change my (appointment|booking))\b", t) else None)
    if exp == "pain_level":
        # words / chip ranges first ("Severe (7–10)" must not read as 7), then a bare number
        n = 8 if re.search(r"severe|unbearable|terrible|very bad|worst|7\s*[–-]\s*10", t) else \
            5 if re.search(r"moderate|medium|4\s*[–-]\s*6", t) else \
            2 if re.search(r"mild|little|slight|1\s*[–-]\s*3", t) else None
        ex["pain_level"] = n if n is not None else _num(msg, 0, 10)
    if exp == "swelling":
        ex["swelling"] = ex["yes_no"]
    if exp == "child_age":
        ex["child_age"] = _num(msg, 0, 17)
    if exp in ("treatment", "last_visit", "notes") and len(msg) >= 2:
        ex[exp] = msg
    if exp == "purpose" and not ex["purpose"] and len(msg.split()) >= 2 and not ex["question"]:
        ex["purpose"], ex["notes"] = "other", msg
    if ex["phone"] and exp == "child_age":
        ex["phone"] = None
    return ex


def _coerce_llm(raw: dict, st: dict) -> dict:
    """Validate/normalise LLM output. Anything that fails validation is dropped, never trusted."""
    out: dict[str, Any] = {}
    out["name"] = V.validate_name(raw.get("name"))
    out["phone"] = V.normalize_phone(raw.get("phone"))
    out["email"] = V.validate_email(raw.get("email"))
    out["purpose"] = raw.get("purpose") if raw.get("purpose") in V.PURPOSE_LABELS else None
    try:
        out["date"] = date.fromisoformat(raw["date"]) if raw.get("date") else None
    except (ValueError, TypeError):
        out["date"] = None
    try:
        out["time"] = time.fromisoformat(raw["time"]) if raw.get("time") else None
    except (ValueError, TypeError):
        out["time"] = None
    out["part_of_day"] = raw.get("part_of_day") if raw.get("part_of_day") in ("morning", "afternoon", "evening") else None
    pl = raw.get("pain_level")
    out["pain_level"] = pl if isinstance(pl, int) and 0 <= pl <= 10 else None
    out["swelling"] = raw.get("swelling") if isinstance(raw.get("swelling"), bool) else None
    age = raw.get("child_age")
    out["child_age"] = age if isinstance(age, int) and 0 <= age <= 17 else None
    for k in ("treatment", "last_visit", "notes"):
        val = raw.get(k)
        out[k] = val.strip()[:200] if isinstance(val, str) and val.strip() else None
    out["yes_no"] = raw.get("yes_no") if isinstance(raw.get("yes_no"), bool) else None
    choice = raw.get("slot_choice")
    out["slot_choice"] = choice if isinstance(choice, int) and 1 <= choice <= len(st["offered"]) else None
    out["question"] = raw.get("question") if isinstance(raw.get("question"), str) and raw.get("question").strip() else None
    out["intent"] = raw.get("intent") if raw.get("intent") in ("cancel", "reschedule") else None
    return out


async def extract(msg: str, st: dict, deps: Deps, now: datetime) -> tuple[dict, bool]:
    ex = rule_extract(msg, st, now)
    used_ai = False
    if deps.llm:
        try:
            ai = _coerce_llm(await deps.llm.extract(msg, st, now), st)
            used_ai = True
            # LLM wins where it found something; rules fill the gaps.
            ex = {k: (ai.get(k) if ai.get(k) is not None else ex.get(k)) for k in set(ex) | set(ai)}
        except Exception as e:  # network, quota, bad JSON → rules only
            log.warning("llm extract failed, using rules: %s", e)
    return ex, used_ai


# ---------------------------------------------------------------- helpers
def _first(st: dict) -> str:
    return st["slots"].get("name", "").split(" ")[0]


def _needed(st: dict) -> list[str]:
    need = [f for f in CONTACT if f not in st["slots"]]
    if "purpose" in st["slots"]:
        need += [f for f in FOLLOWUPS[st["slots"]["purpose"]] if f not in st["details"]]
    if "slot" not in st["slots"]:
        need.append("slot")
    return need


def _question(st: dict, f: str) -> tuple[str, list[str]]:
    if f == "treatment":
        return TREATMENT_Q[st["slots"]["purpose"]]
    return QUESTIONS[f]


def _offer(st: dict, slots: list[datetime]) -> list[str]:
    st["offered"] = [s.isoformat() for s in slots]
    return [C.fmt_slot(s) for s in slots]


def _card(st: dict) -> dict:
    s, d = st["slots"], st["details"]
    det = []
    if "pain_level" in d: det.append(f"Pain {d['pain_level']}/10")
    if "swelling" in d: det.append("Swelling" if d["swelling"] else "No swelling")
    for k in ("treatment", "last_visit", "notes"):
        if k in d: det.append(str(d[k]))
    if "child_age" in d: det.append(f"Child aged {d['child_age']}")
    return {
        "name": s.get("name"), "phone": s.get("phone"), "email": s.get("email"),
        "purpose": V.PURPOSE_LABELS.get(s.get("purpose", ""), ""), "details": det,
        "slot": C.fmt_slot(datetime.fromisoformat(s["slot"])) if s.get("slot") else None,
        "doctor": "Dr. Arjun Menon" if s.get("purpose") in ("braces", "implant") else "Dr. Meera Rao",
        "urgent": st["urgent"], "ref": (st.get("booking") or {}).get("ref"),
    }


def _fail(st: dict, f: str) -> str:
    st["fails"][f] = st["fails"].get(f, 0) + 1
    first, example = HINTS.get(f, ("Sorry, I didn't catch that.", ""))
    return f"{first} {example}" if st["fails"][f] >= 2 or f in ("phone", "email") else first


# ---------------------------------------------------------------- scheduling
async def _resolve_slot(st: dict, ex: dict, msg: str, deps: Deps, now: datetime) -> tuple[list[str], list[str]]:
    """Try to turn what the user said into a confirmed-free slot. Returns (notes, chips)."""
    notes: list[str] = []
    # 1) picked an offered chip / "the second one"
    chosen = None
    labels = {C.fmt_slot(datetime.fromisoformat(o)).lower(): o for o in st["offered"]}
    if msg.strip().lower() in labels:
        chosen = labels[msg.strip().lower()]
    elif ex.get("slot_choice"):
        chosen = st["offered"][ex["slot_choice"] - 1]
    elif st["offered"] and (m := re.search(r"\b(first|second|third|1st|2nd|3rd)\b", msg.lower())):
        idx = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}[m.group(1)]
        if idx < len(st["offered"]):
            chosen = st["offered"][idx]
    d, t, pod = ex.get("date"), ex.get("time"), ex.get("part_of_day")
    if chosen is None and d and t:
        chosen = datetime.combine(d, t, C.TZ).isoformat()

    horizon_end = now + timedelta(days=C.MAX_DAYS_AHEAD + 1)
    busy = await deps.scheduler.busy(now, horizon_end)

    if chosen:
        dt = datetime.fromisoformat(chosen)
        reason = C.check_slot(dt, now)
        if reason is None and dt in busy:
            reason = "that slot is already booked"
        if reason is None:
            st["slots"]["slot"] = dt.isoformat()
            st["offered"] = []
            return notes, []
        notes.append(f"Sorry, {reason}.")
        alts = C.suggest_slots(max(dt.replace(minute=0), now), now, busy, 3)
        notes.append("The closest times I have are:")
        return notes, _offer(st, alts)

    if d:  # date without a usable time
        day_alts = C.suggest_slots(datetime.combine(d, time(0), C.TZ), now, busy, 3, only_day=d, prefer=pod)
        if day_alts:
            notes.append(f"Here's what's free on {d.strftime('%A %d %b')}:")
            return notes, _offer(st, day_alts)
        reason = C.check_slot(datetime.combine(d, time(10), C.TZ), now) or "we're fully booked that day"
        notes.append(f"Sorry, {reason}. The next available times are:")
        return notes, _offer(st, C.suggest_slots(datetime.combine(d, time(0), C.TZ), now, busy, 3))

    if t:  # time without date → that time on the next day it works
        for i in range(0, 14):
            cand = datetime.combine((now + timedelta(days=i)).date(), t, C.TZ)
            if C.check_slot(cand, now) is None and cand not in busy:
                notes.append(f"The earliest I can do {C.fmt_slot(cand).split(', ')[1]} is:")
                return notes, _offer(st, [cand] + C.suggest_slots(cand + timedelta(minutes=30), now, busy, 2))
        notes.append(f"Sorry, {C.check_slot(datetime.combine(now.date(), t, C.TZ), now) or 'that time is not available'}.")

    alts = C.suggest_slots(now, now, busy, 3, prefer=pod)
    return notes, _offer(st, alts)


# ---------------------------------------------------------------- main turn
async def turn(st: dict, msg: str, deps: Deps) -> Reply:
    now = deps.now()
    msg = (msg or "").strip()[:500]
    if msg:
        st["transcript"].append({"role": "user", "text": msg})
    reply = await _turn(st, msg, deps, now)
    st["transcript"].append({"role": "bot", "text": reply.text})
    st["transcript"] = st["transcript"][-60:]
    reply.stage, reply.urgent = st["stage"], st["urgent"]
    return reply


def _greeting(now: datetime) -> str:
    h = now.hour
    part = "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"
    return (f"{part}, and welcome to {C.CLINIC_NAME}! 😊 I'm {C.ASSISTANT_NAME}, the clinic's virtual receptionist. "
            "I can book an appointment for you or answer any questions.")


async def _turn(st: dict, msg: str, deps: Deps, now: datetime) -> Reply:
    if st["stage"] == "greet":
        st["stage"] = "collect"
        if not msg:
            st["expecting"] = "name"
            return Reply(f"{_greeting(now)} To get started, may I have your full name?",
                         chips=["I have tooth pain", "Book a check-up", "Clinic hours & fees"])
    if not msg:
        q, chips = _question(st, st["expecting"] or "name") if st["stage"] != "done" else ("Is there anything else I can help with?", [])
        return Reply(f"I didn't receive anything — {q[0].lower()}{q[1:]}", chips=chips or _chips_for(st))

    ex, used_ai = await extract(msg, st, deps, now)
    if st["stage"] == "done" or (ex.get("intent") and st["stage"] != "manage"):
        if ex.get("intent") or st["stage"] in ("done",):
            r = await _post_booking(st, ex, msg, deps, now)
            if r:
                r.used_ai = used_ai
                return r
    if st["stage"] == "manage":
        r = await _manage(st, ex, msg, deps, now)
        r.used_ai = used_ai
        return r

    notes: list[str] = []
    answer = None
    if ex.get("question"):
        answer = C.faq_answer(ex["question"]) or C.faq_answer(msg)
    user_q = ex.get("question") if (ex.get("question") and not answer) else None

    # --- confirmation stage ---
    if st["stage"] == "confirm":
        corrections = {k for k in ("name", "phone", "email", "date", "time") if ex.get(k)}
        if ex.get("yes_no") is True and not corrections:
            return await _book(st, deps, now, used_ai)
        if not corrections:
            if st["expecting"] == "change_field":
                f = _which_field(msg)
                if f:
                    return _reopen(st, f, used_ai)
            st["expecting"] = "change_field"
            return Reply("No problem — what would you like to change?", chips=[FIELD_LABELS[f] for f in FIELD_LABELS],
                         used_ai=used_ai)
        st["stage"] = "collect"
        if ex.get("date") or ex.get("time"):
            st["slots"].pop("slot", None)

    # --- pending email typo confirmation ---
    if st.get("pending_email"):
        typo, fixed = st["pending_email"]
        if ex.get("email") and ex["email"] not in (typo, fixed):
            st["pending_email"] = None
        elif ex.get("yes_no") is not None or msg.lower() in (fixed, typo) or msg.lower().startswith("no, keep"):
            keep_original = ex.get("yes_no") is False or msg.lower() == typo or msg.lower().startswith("no, keep")
            st["slots"]["email"] = typo if keep_original else fixed
            st["pending_email"] = None
            ex["email"] = None
            ex["yes_no"] = None
            notes.append("Great, email saved.")

    captured = _apply(st, ex, notes)
    if notes and notes[0] == "Great, email saved.":
        captured.add("email")
    exp = st["expecting"]

    # --- errors on the field we asked for ---
    if exp and exp not in captured and not answer and not user_q:
        attempted = (
            (exp == "phone" and V.looks_like_phone_attempt(msg)) or (exp == "email" and "@" in msg)
            or (exp == "name" and not captured) or (exp in ("pain_level", "swelling", "child_age", "purpose") and not captured)
        )
        if exp == "email" and "@" in msg and not ex.get("email"):
            attempted = True
        if attempted and exp != "slot":
            notes.append(_fail(st, exp))
        elif not captured and exp != "slot":
            notes.append(_fail(st, exp))

    if st.get("pending_email"):
        typo, fixed = st["pending_email"]
        return await _respond(st, deps, notes + [f"Just checking — did you mean {fixed}?"], answer,
                              None, [fixed, f"No, keep {typo}"], used_ai, user_q)

    need = _needed(st)
    chips: list[str] = []
    if need and need[0] == "slot":
        slot_notes, chips = await _resolve_slot(st, ex, msg, deps, now)
        if "slot" not in st["slots"]:
            if exp == "slot" and not slot_notes and not (ex.get("date") or ex.get("time")) and not answer and not captured:
                notes.append(_fail(st, "slot"))
            if not slot_notes:
                if st["urgent"] and "urgent_noted" not in st["details"]:
                    st["details"]["urgent_noted"] = True
                    notes.append("That sounds really uncomfortable — I'm flagging this as urgent so the dentist sees "
                                 "you as soon as possible. Here are the earliest slots I have:")
                    q = None
                elif exp == "slot":
                    q = "Would one of these work for you?"
                else:
                    q = QUESTIONS["slot"][0]
            else:
                q = " ".join(slot_notes)
            st["expecting"] = "slot"
            return await _respond(st, deps, notes, answer, q, chips, used_ai, user_q)
        need = _needed(st)
    elif ex.get("date") or ex.get("time"):
        # remember the preference for later, only once
        st["details"]["pref"] = msg

    if not need:
        st["stage"], st["expecting"] = "confirm", None
        r = await _respond(st, deps, notes + ["Here's a summary of your appointment."], answer,
                           "Shall I go ahead and confirm it?", ["Yes, confirm", "Change something"], used_ai, user_q)
        r.card = _card(st)
        return r

    nxt = need[0]
    st["expecting"] = nxt
    q, chips = _question(st, nxt)
    return await _respond(st, deps, notes, answer, q, chips, used_ai, user_q)


def _apply(st: dict, ex: dict, notes: list[str]) -> set[str]:
    s, d = st["slots"], st["details"]
    captured: set[str] = set()
    for f in ("name", "phone", "email"):
        v = ex.get(f)
        if v and s.get(f) != v:
            if f == "email" and (sugg := V.email_typo_suggestion(v)):
                st["pending_email"] = (v, sugg)
                captured.add(f)
                continue
            if f in s:
                notes.append(f"Updated your {FIELD_LABELS[f].lower()} to {v}.")
            s[f] = v
            captured.add(f)
            st["fails"].pop(f, None)
    p = ex.get("purpose")
    # a vague "other" never overwrites a specific purpose unless we actually asked for it
    if p and p != s.get("purpose") and ("purpose" not in s or st["expecting"] == "purpose" or p != "other"):
        if "purpose" in s:
            for k in ("pain_level", "swelling", "treatment", "child_age", "last_visit", "notes"):
                d.pop(k, None)
        s["purpose"] = p
        captured.add("purpose")
    if "purpose" in s:
        for k in FOLLOWUPS[s["purpose"]]:
            if ex.get(k) is not None and k not in d:
                d[k] = ex[k]
                captured.add(k)
    if ex.get("notes") and s.get("purpose") == "other":
        d.setdefault("notes", ex["notes"])
    if (d.get("pain_level") or 0) >= 8 or d.get("swelling") is True:
        st["urgent"] = True
    if captured == {"purpose"} and (st["expecting"] != "purpose" or s["purpose"] in PURPOSE_ACK):
        notes.append(PURPOSE_ACK.get(s["purpose"], "Sure, I can help with that."))
    elif "name" in captured and s.get("name") and len(captured) == 1 and not notes:
        notes.append(f"Nice to meet you, {_first(st)}!")
    elif len(captured & {"name", "phone", "email", "purpose"}) >= 2:
        notes.append("Thanks, I've noted those details.")
    return captured


def _which_field(msg: str) -> str | None:
    t = msg.lower()
    for f, kws in {"name": ["name"], "phone": ["phone", "number", "mobile"], "email": ["email", "mail"],
                   "purpose": ["reason", "purpose", "treatment"], "slot": ["date", "time", "slot", "day"]}.items():
        if any(k in t for k in kws):
            return f
    return None


def _reopen(st: dict, f: str, used_ai: bool) -> Reply:
    st["stage"] = "collect"
    st["slots"].pop(f, None)
    if f == "purpose":
        for k in ("pain_level", "swelling", "treatment", "child_age", "last_visit", "notes"):
            st["details"].pop(k, None)
        st["urgent"] = False
    st["expecting"] = f
    if f == "slot":
        return Reply("Sure — when would you prefer instead?", chips=[], used_ai=used_ai)
    q, chips = _question(st, f)
    return Reply(f"Sure. {q}", chips=chips, used_ai=used_ai)


def _chips_for(st: dict) -> list[str]:
    if st["stage"] == "done":
        return ["Reschedule", "Cancel my booking", "That's all, thanks"]
    return []


async def _respond(st, deps, notes, answer, question, chips, used_ai, user_q=None) -> Reply:
    """Compose the reply. The LLM may rephrase it; if it fails we use the template."""
    template = " ".join(p for p in [*notes, answer, question] if p)
    text = template
    if deps.llm:
        try:
            text = await deps.llm.phrase(st, notes, answer, question, user_q) or template
            used_ai = True
        except Exception as e:
            log.warning("llm phrase failed, using template: %s", e)
    return Reply(text, chips=chips, used_ai=used_ai)


# ---------------------------------------------------------------- booking
async def _book(st: dict, deps: Deps, now: datetime, used_ai: bool) -> Reply:
    s = st["slots"]
    slot = datetime.fromisoformat(s["slot"])
    lead = {"name": s["name"], "phone": s["phone"], "email": s["email"], "purpose": s["purpose"],
            "details": {k: v for k, v in st["details"].items() if k not in ("urgent_noted", "pref")}, "urgent": st["urgent"]}

    async def summarize():
        if not deps.llm:
            return None
        try:
            return await deps.llm.summarize(st["transcript"], lead)
        except Exception as e:
            log.warning("summary failed: %s", e)
            return None

    # Booking and AI summary are independent → run them concurrently.
    results = await asyncio.gather(deps.scheduler.book(slot, lead), summarize(), return_exceptions=True)
    booking, summary = results
    if isinstance(booking, SlotTaken):
        s.pop("slot", None)
        st["stage"], st["expecting"] = "collect", "slot"
        busy = await deps.scheduler.busy(now, now + timedelta(days=C.MAX_DAYS_AHEAD + 1))
        chips = _offer(st, C.suggest_slots(slot, now, busy, 3))
        return Reply("Oh no — someone just booked that exact slot a moment ago. Here are the nearest free times:",
                     chips=chips, used_ai=used_ai)
    if isinstance(booking, BaseException):
        raise booking
    if isinstance(summary, BaseException):
        summary = None

    old = st.get("booking")
    if old and old.get("ref") and st.get("rescheduling"):
        await deps.scheduler.cancel(old["ref"])
        if deps.store:
            await deps.store.mark_cancelled(old["ref"])
        st["rescheduling"] = False
    st["booking"] = {"ref": booking["ref"], "slot": s["slot"]}
    st["stage"], st["expecting"] = "done", None
    if deps.store:
        try:
            await deps.store.save_lead(st, booking, summary)
        except Exception as e:
            log.error("lead save failed: %s", e)
    card = _card(st)
    card["calendar"] = bool(booking.get("event_link"))
    text = (f"You're all set, {_first(st)}! 🎉 Your appointment is confirmed for {card['slot']} with {card['doctor']}. "
            f"Your booking reference is {booking['ref']}. "
            + ("It's on the clinic calendar, and you can add it to yours with the button below. " if booking.get("event_link") else "")
            + "Please arrive 10 minutes early. Is there anything else I can help with?")
    return Reply(text, chips=["Add to my calendar", "That's all, thanks"], card=card, booking=st["booking"], used_ai=used_ai)


# ---------------------------------------------------------------- after booking / manage existing
async def _post_booking(st: dict, ex: dict, msg: str, deps: Deps, now: datetime) -> Reply | None:
    intent = ex.get("intent")
    t = msg.lower()
    if intent in ("cancel", "reschedule"):
        st["manage"] = {"action": intent, "ref": ex.get("ref") or (st.get("booking") or {}).get("ref"), "verified": False}
        st["stage"] = "manage"
        return await _manage(st, ex, msg, deps, now)
    if st["stage"] != "done":
        return None
    if re.search(r"\b(thanks|thank you|that's all|thats all|bye|no)\b", t):
        return Reply(f"Thank you for choosing {C.CLINIC_NAME}, {_first(st)}! Take care, and see you soon. 😊",
                     chips=["Reschedule", "Cancel my booking"])
    if re.search(r"\b(another|new) (appointment|booking)\b|\bbook (another|again)\b", t):
        keep = {k: st["slots"][k] for k in ("name", "phone", "email") if k in st["slots"]}
        st.update(stage="collect", slots=keep, details={}, fails={}, offered=[], urgent=False, expecting="purpose")
        return Reply("Happy to help with another appointment. What would you like to come in for?", chips=PURPOSE_CHIPS)
    if answer := C.faq_answer(msg):
        return Reply(answer + " Anything else I can help with?", chips=_chips_for(st))
    return Reply("Your appointment is all set. I can help you reschedule, cancel, or answer any questions about the clinic.",
                 chips=_chips_for(st))


async def _manage(st: dict, ex: dict, msg: str, deps: Deps, now: datetime) -> Reply:
    m = st["manage"]
    if ex.get("ref"):
        m["ref"] = ex["ref"]
    if not m.get("ref"):
        return Reply(f"Sure — what's your booking reference? It looks like BSD-AB12C.", chips=[])
    booking = await deps.scheduler.find(m["ref"])
    if not booking:
        m["ref"] = None
        return Reply("I couldn't find an active booking with that reference. Could you double-check it?")
    if not m["verified"]:
        phone = ex.get("phone")
        if not phone:
            return Reply("For security, please confirm the mobile number used for that booking.")
        if phone != booking["phone"]:
            m["tries"] = m.get("tries", 0) + 1
            if m["tries"] >= 3:
                st["stage"] = "done" if st.get("booking") else "collect"
                return Reply("Sorry, I can't verify that booking. Please call the clinic and we'll sort it out.")
            return Reply("That number doesn't match our records for this booking. Could you try again?")
        m["verified"] = True
        when = C.fmt_slot(booking["slot"])
        if m["action"] == "cancel":
            m["awaiting"] = True
            return Reply(f"Found it — {booking['name']}, {when}. Are you sure you want to cancel?", chips=["Yes, cancel it", "No, keep it"])
        # reschedule: reuse the collect flow with known details
        st.update(stage="collect", expecting="slot", rescheduling=True, offered=[], booking={"ref": booking["ref"]},
                  slots={k: booking[k] for k in ("name", "phone", "email", "purpose")},
                  details=dict(booking.get("details") or {}), urgent=bool(booking.get("urgent")))
        busy = await deps.scheduler.busy(now, now + timedelta(days=C.MAX_DAYS_AHEAD + 1))
        chips = _offer(st, C.suggest_slots(now, now, busy, 3))
        return Reply(f"Found it — currently {when}. When would you like to move it to?", chips=chips)
    if m.get("awaiting"):
        yn = ex.get("yes_no")
        if yn is True or "yes" in msg.lower():
            await deps.scheduler.cancel(m["ref"])
            if deps.store:
                await deps.store.mark_cancelled(m["ref"])
            st["stage"] = "done"
            st["manage"] = None
            return Reply(f"Your appointment {m['ref']} has been cancelled. We hope to see you another time!",
                         chips=["Book a new appointment"])
        st["stage"] = "done"
        return Reply("No problem, your appointment stays as it is.", chips=_chips_for(st))
    st["stage"] = "done"
    return Reply("Is there anything else I can help with?", chips=_chips_for(st))


def new_lead_id() -> str:
    return uuid.uuid4().hex[:12]
