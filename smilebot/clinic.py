"""Clinic opening rules and slot generation (pure functions, no I/O)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")
CLINIC_NAME = "BrightSmile Dental Clinic"
OPEN, CLOSE = time(9, 0), time(18, 0)
LUNCH = (time(13, 0), time(14, 0))
SLOT_MINUTES = 30
MAX_DAYS_AHEAD = 60
MIN_LEAD_MINUTES = 60  # can't book a slot starting within the next hour


ASSISTANT_NAME = "Aria"
FACTS = {
    "hours": "We're open Monday to Saturday, 9 am to 6 pm (closed 1–2 pm for lunch), and closed on Sundays.",
    "location": "We're at 12 Lakeview Road, Indiranagar, Bengaluru, with free parking right outside.",
    "fees": "A consultation is ₹500 including the exam. Cleaning starts at ₹1,500 and whitening at ₹6,000; "
            "exact treatment costs are confirmed after your check-up.",
    "insurance": "We accept most major health insurance plans and offer 0% EMI on treatments above ₹10,000.",
    "doctors": "Dr. Meera Rao handles general and cosmetic dentistry, and Dr. Arjun Menon specialises in braces and implants.",
    "emergency": "For severe pain, swelling or a broken tooth we keep same-day emergency slots.",
    "payment": "We accept cash, cards and UPI.",
}
_FAQ_KEYS = {
    "hours": ["hour", "open", "close", "timing", "sunday", "weekend"],
    "location": ["where", "address", "location", "located", "parking", "directions", "reach you"],
    "fees": ["cost", "fee", "price", "charge", "how much", "expensive", "rate"],
    "insurance": ["insurance", "emi", "claim"],
    "doctors": ["doctor", "dentist", "dr.", "dr ", "who will"],
    "emergency": ["emergency", "urgent", "same day", "same-day"],
    "payment": ["pay", "upi", "card", "cash"],
}


def faq_answer(text: str) -> str | None:
    t = text.lower()
    hits = [FACTS[k] for k, kws in _FAQ_KEYS.items() if any(w in t for w in kws)]
    return " ".join(hits[:2]) or None


def now() -> datetime:
    return datetime.now(TZ)


def day_slots(d: date) -> list[datetime]:
    if d.weekday() == 6:
        return []
    out, t = [], datetime.combine(d, OPEN, TZ)
    end = datetime.combine(d, CLOSE, TZ)
    while t + timedelta(minutes=SLOT_MINUTES) <= end:
        if not (LUNCH[0] <= t.time() < LUNCH[1]):
            out.append(t)
        t += timedelta(minutes=SLOT_MINUTES)
    return out


def check_slot(dt: datetime, current: datetime) -> str | None:
    """Return a human reason why `dt` can't be booked, or None if it's a valid clinic slot."""
    dt = dt.astimezone(TZ)
    if dt < current + timedelta(minutes=MIN_LEAD_MINUTES):
        return "that time has already passed (or is less than an hour away)"
    if dt.date() > (current + timedelta(days=MAX_DAYS_AHEAD)).date():
        return f"we only take bookings up to {MAX_DAYS_AHEAD} days ahead"
    if dt.weekday() == 6:
        return "we're closed on Sundays"
    if dt.time() < OPEN or dt.time() > (datetime.combine(dt.date(), CLOSE) - timedelta(minutes=SLOT_MINUTES)).time():
        return "that's outside our hours (Mon–Sat, 9 am – 6 pm)"
    if LUNCH[0] <= dt.time() < LUNCH[1]:
        return "the clinic is closed for lunch between 1 and 2 pm"
    if dt.minute % SLOT_MINUTES:
        return "appointments start on the hour or half-hour"
    return None


def suggest_slots(after: datetime, current: datetime, busy: set[datetime], n: int = 3,
                  only_day: date | None = None, prefer: str | None = None) -> list[datetime]:
    """Next `n` free slots at/after `after`. `prefer` = morning|afternoon|evening narrows the window."""
    windows = {"morning": (time(9), time(13)), "afternoon": (time(14), time(16)), "evening": (time(16), time(18))}
    lo, hi = windows.get(prefer, (time(0), time(23, 59)))
    busy_utc = {b.astimezone(TZ) for b in busy}
    out: list[datetime] = []
    d = (only_day or after.astimezone(TZ).date())
    last = only_day or (current + timedelta(days=MAX_DAYS_AHEAD)).date()
    while d <= last and len(out) < n:
        for s in day_slots(d):
            if s >= after and lo <= s.time() < hi and s not in busy_utc and check_slot(s, current) is None:
                out.append(s)
                if len(out) == n:
                    break
        d += timedelta(days=1)
    return out


def fmt_slot(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    return dt.strftime("%a %d %b, ") + dt.strftime("%I:%M %p").lstrip("0")
