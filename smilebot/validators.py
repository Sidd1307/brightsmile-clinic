"""Deterministic parsing + validation. The LLM may propose values; these functions decide."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import dateparser
from dateparser.search import search_dates

# ---------- name ----------
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,49}$")
_NAME_INTRO = re.compile(
    r"\b(?:my name is|my name's|name is|i am|i'm|im|this is|it's|call me)\s+([A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*){0,2})",
    re.I,
)
_NOT_NAMES = {
    "fine", "good", "ok", "okay", "yes", "no", "hello", "hi", "hey", "here", "looking", "having", "calling",
    "interested", "not", "sure", "in", "a", "the", "going", "trying", "just", "new", "booking", "want",
}


def validate_name(raw: str | None) -> str | None:
    if not raw:
        return None
    name = " ".join(raw.strip().split())
    if not _NAME_RE.match(name) or sum(c.isalpha() for c in name) < 2:
        return None
    if name.split()[0].lower() in _NOT_NAMES:
        return None
    return " ".join(w[:1].upper() + w[1:] for w in name.split())


def extract_name(text: str, expecting: bool = False) -> str | None:
    m = _NAME_INTRO.search(text)
    if m:
        cand = re.split(r"\s+(?:and|my|i|phone|email|from|here|calling|,)\b", m.group(1), flags=re.I)[0]
        return validate_name(cand)
    if expecting:  # bare answer to "what's your name?"
        if re.search(r"[?!@#\d]", text):  # names don't come with ???, digits or symbols
            return None
        cleaned = re.sub(r"[^\w .'\-]", "", text).strip()
        if 1 <= len(cleaned.split()) <= 3 and not _looks_mashed(cleaned):
            return validate_name(cleaned)
    return None


_MASH = {"asdf", "qwerty", "qwer", "zxcv", "hjkl", "test", "abc", "xyz", "aaa", "idk", "na", "nothing"}


def _looks_mashed(s: str) -> bool:
    words = s.lower().split()
    return any(w in _MASH or re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", w) or not re.search(r"[aeiouy]", w) for w in words)


# ---------- phone ----------
_PHONE_CANDIDATE = re.compile(r"(?:\+?\d[\d\s\-().]{7,18}\d)")


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not plus and len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not plus and len(digits) == 10:
        return "+91" + digits if digits[0] in "6789" else None
    if not plus and len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return "+" + digits
    if plus and 10 <= len(digits) <= 15:
        return "+" + digits
    return None


def extract_phone(text: str) -> str | None:
    for m in _PHONE_CANDIDATE.finditer(text):
        p = normalize_phone(m.group())
        if p:
            return p
    return None


def looks_like_phone_attempt(text: str) -> bool:
    return len(re.sub(r"\D", "", text)) >= 5


# ---------- email ----------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DOMAIN_TYPOS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.con": "gmail.com", "yaho.com": "yahoo.com", "yahoo.con": "yahoo.com", "hotmial.com": "hotmail.com",
    "outlok.com": "outlook.com",
}


def validate_email(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip(".,;").lower()
    return raw if _EMAIL_RE.fullmatch(raw) else None


def extract_email(text: str) -> str | None:
    # tolerate spoken form: "john at gmail dot com"
    spoken = re.sub(r"\s+at\s+", "@", text, flags=re.I) if "@" not in text else text
    spoken = re.sub(r"\s+dot\s+", ".", spoken, flags=re.I)
    m = _EMAIL_RE.search(spoken)
    return validate_email(m.group()) if m else None


def email_typo_suggestion(email: str) -> str | None:
    local, _, domain = email.partition("@")
    fix = _DOMAIN_TYPOS.get(domain)
    return f"{local}@{fix}" if fix else None


# ---------- purpose ----------
PURPOSES = {
    "toothache": ["pain", "ache", "hurt", "toothache", "swelling", "swollen", "bleeding", "broken", "cracked", "chipped",
                  "emergency", "sensitive", "sensitivity", "infection", "abscess", "wisdom", "root canal"],
    "cleaning": ["clean", "scaling", "polish", "stain", "plaque", "tartar"],
    "checkup": ["check", "checkup", "check-up", "routine", "exam", "consult", "general", "x-ray", "xray"],
    "cosmetic": ["whiten", "whitening", "veneer", "smile", "cosmetic", "bonding", "yellow"],
    "braces": ["brace", "aligner", "invisalign", "crooked", "straighten", "orthodont"],
    "implant": ["implant", "missing tooth", "missing teeth", "denture", "crown", "bridge", "replace"],
    "kids": ["kid", "child", "son", "daughter", "baby", "toddler", "children", "pediatric", "paediatric"],
}
PURPOSE_LABELS = {
    "toothache": "Tooth pain / emergency", "cleaning": "Cleaning & scaling", "checkup": "Routine check-up",
    "cosmetic": "Cosmetic / whitening", "braces": "Braces & aligners", "implant": "Implants, crowns & dentures",
    "kids": "Children's dentistry", "other": "Other enquiry",
}


PURPOSES["toothache"] += ["killing me", "tooth is", "teeth are", "throbbing"]
PURPOSES["kids"] += ["year old", "yr old", "years old", "my little"]
_PURPOSE_RE = {p: [re.compile(r"\b" + re.escape(k) + (r"\b" if k in ("exam", "son", "general") else "")) for k in kws]
               for p, kws in PURPOSES.items()}


def classify_purpose(text: str) -> str | None:
    # strip emails/URLs first: "karan@example.com" must not read as an "exam"
    t = _EMAIL_RE.sub(" ", text.lower())
    t = re.sub(r"https?://\S+", " ", t)
    scores = {p: sum(bool(r.search(t)) for r in rs) for p, rs in _PURPOSE_RE.items()}
    if scores["kids"]:  # "my 6 year old needs a checkup" is a children's visit
        return "kids"
    best = max(scores, key=scores.get)
    return best if scores[best] else None


# ---------- yes / no ----------
def yes_no(text: str) -> bool | None:
    t = text.lower().strip(" .!")
    if re.search(r"\b(yes|yeah|yep|yup|correct|confirm|sure|ok|okay|right|perfect|sounds good|go ahead|book it|haan|ha)\b", t):
        if not re.search(r"\b(not|n't|no)\b", t):
            return True
    if re.search(r"\b(no|nope|nah|wrong|incorrect|change|not)\b", t):
        return False
    return None


# ---------- date / time ----------
_TIME_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])|\b(?:at\s+)?(\d{1,2})[:.](\d{2})\b|\bat\s+(\d{1,2})\b|\b(noon|midday)\b",
    re.I,
)
_PART_OF_DAY = {"morning": time(10, 0), "afternoon": time(14, 30), "evening": time(16, 30)}


def parse_time(text: str) -> time | None:
    m = _TIME_RE.search(text)
    if not m:
        return None
    if m.group(7):
        return time(12, 0)
    if m.group(1):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if h > 12 or mi > 59:
            return None
        h = h % 12 + (12 if ap.startswith("p") else 0)
        return time(h, mi)
    if m.group(4):
        h, mi = int(m.group(4)), int(m.group(5))
    else:
        h, mi = int(m.group(6)), 0
    if h > 23 or mi > 59:
        return None
    if 1 <= h <= 7:  # "at 4" during clinic hours means 4 pm
        h += 12
    return time(h, mi)


def part_of_day(text: str) -> str | None:
    for p in _PART_OF_DAY:
        if p in text.lower():
            return p
    return None


_DATE_WORDS = re.compile(
    r"\b(today|tomorrow|tmrw|tmr|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|week|next|in \d+ days?|\d{1,2}(?:st|nd|rd|th)|\d{1,2}[/\-]\d{1,2})\b",
    re.I,
)


def parse_date(text: str, now: datetime) -> date | None:
    t = text.lower()
    if "day after tomorrow" in t:
        return (now + timedelta(days=2)).date()
    stripped = _TIME_RE.sub(" ", text)
    if not _DATE_WORDS.search(stripped):
        return None
    stripped = re.sub(r"\b(tmrw|tmr)\b", "tomorrow", stripped, flags=re.I)
    settings = {"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now.replace(tzinfo=None), "DATE_ORDER": "DMY"}
    dt = dateparser.parse(stripped, settings=settings)
    if dt is None:
        found = search_dates(stripped, settings=settings, languages=["en"]) or []
        found = [d for s, d in found if _DATE_WORDS.search(s)]
        dt = found[0] if found else None
    return dt.date() if dt else None
