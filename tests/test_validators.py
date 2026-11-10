from datetime import datetime, time, date

import pytest

from brightsmile import validators as v
from brightsmile import clinic as c

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=c.TZ)  # Monday


@pytest.mark.parametrize("raw,exp", [
    ("john smith", "John Smith"), ("  priya  ", "Priya"), ("O'Neil", "O'Neil"),
    ("a", None), ("12345", None), ("", None), ("john123", None), ("fine", None),
])
def test_validate_name(raw, exp):
    assert v.validate_name(raw) == exp


@pytest.mark.parametrize("text,expecting,exp", [
    ("Hi, my name is Rahul Sharma and I have a toothache", False, "Rahul Sharma"),
    ("I'm Anita", False, "Anita"),
    ("Kavya Iyer", True, "Kavya Iyer"),
    ("I have a toothache", False, None),
    ("I'm having pain", False, None),
    ("asdf ???", True, None), ("qwerty", True, None), ("bcdfgh", True, None), ("Yash", True, "Yash"),
])
def test_extract_name(text, expecting, exp):
    assert v.extract_name(text, expecting) == exp


@pytest.mark.parametrize("raw,exp", [
    ("98765 43210", "+919876543210"), ("09876543210", "+919876543210"), ("+44 20 7946 0958", "+442079460958"),
    ("919876543210", "+919876543210"), ("12345", None), ("1234567890", None), ("abc", None),
])
def test_normalize_phone(raw, exp):
    assert v.normalize_phone(raw) == exp


def test_extract_phone_from_sentence():
    assert v.extract_phone("sure, it's 98765-43210, call after 5") == "+919876543210"


@pytest.mark.parametrize("text,exp", [
    ("mail me at Raj.K@Gmail.com.", "raj.k@gmail.com"), ("raj at gmail dot com", "raj@gmail.com"),
    ("raj@gmail", None), ("no email", None),
])
def test_extract_email(text, exp):
    assert v.extract_email(text) == exp


def test_email_typo():
    assert v.email_typo_suggestion("raj@gmial.com") == "raj@gmail.com"
    assert v.email_typo_suggestion("raj@gmail.com") is None


@pytest.mark.parametrize("text,exp", [
    ("my tooth hurts badly", "toothache"), ("I want teeth whitening", "cosmetic"), ("routine checkup", "checkup"),
    ("braces for my crooked teeth", "braces"), ("my son needs a dentist", "kids"), ("hello", None),
    ("karan@example.com", None), ("my 6 year old needs a checkup", "kids"), ("my tooth is killing me", "toothache"),
    ("I want to consult about a reason", "checkup"),
])
def test_classify_purpose(text, exp):
    assert v.classify_purpose(text) == exp


@pytest.mark.parametrize("text,exp", [("yes please", True), ("that's correct", True), ("no, change it", False),
                                      ("not right", False), ("hmm", None)])
def test_yes_no(text, exp):
    assert v.yes_no(text) == exp


@pytest.mark.parametrize("text,exp", [
    ("4pm", time(16)), ("at 10:30 am", time(10, 30)), ("11.30", time(11, 30)), ("at 4", time(16)),
    ("noon", time(12)), ("tuesday", None), ("13pm", None),
])
def test_parse_time(text, exp):
    assert v.parse_time(text) == exp


@pytest.mark.parametrize("text,exp", [
    ("tomorrow", date(2026, 11, 10)), ("day after tomorrow at 5pm", date(2026, 11, 11)),
    ("can I come on Thursday at 2:30pm please", date(2026, 11, 12)), ("20/11 at 4pm", date(2026, 11, 20)),
    ("15th nov", date(2026, 11, 15)), ("4pm", None), ("whenever", None),
])
def test_parse_date(text, exp):
    assert v.parse_date(text, NOW) == exp


# ---------- clinic ----------
def at(d, h, m=0):
    return datetime(2026, 11, d, h, m, tzinfo=c.TZ)


@pytest.mark.parametrize("dt,ok", [
    (at(10, 16), True), (at(9, 10, 30), False), (at(15, 11), False), (at(10, 13, 30), False),
    (at(10, 18), False), (at(10, 8, 30), False), (at(10, 17, 30), True), (at(10, 10, 15), False),
])
def test_check_slot(dt, ok):
    assert (c.check_slot(dt, NOW) is None) == ok


def test_suggest_skips_busy_lunch_and_sunday():
    busy = {at(14, 17), at(14, 17, 30)}
    s = c.suggest_slots(at(14, 17), NOW, busy, n=2)  # Sat 5pm busy → Sunday closed → Monday 9am
    assert s == [at(16, 9), at(16, 9, 30)]
    lunch = c.suggest_slots(at(10, 12, 30), NOW, set(), n=2)
    assert lunch == [at(10, 12, 30), at(10, 14)]


def test_suggest_prefers_part_of_day():
    s = c.suggest_slots(at(10, 9), NOW, set(), n=1, prefer="evening")
    assert s == [at(10, 16)]
