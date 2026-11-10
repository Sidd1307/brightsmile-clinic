import asyncio
from datetime import datetime

import pytest

from smilebot import clinic as C
from smilebot import flow
from smilebot.booking import MemoryScheduler
from smilebot.state import BadState, new_state, sign, verify

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=C.TZ)  # Monday 10:00


def deps(sched=None):
    return flow.Deps(scheduler=sched or MemoryScheduler(), now=lambda: NOW)


async def chat(msgs, d=None, st=None):
    d = d or deps()
    st = st or new_state()
    replies = [await flow.turn(st, "", d)] if st["stage"] == "greet" else []
    for m in msgs:
        replies.append(await flow.turn(st, m, d))
    return st, replies


async def test_greeting():
    st, [r] = await chat([])
    assert "Good morning" in r.text and C.CLINIC_NAME in r.text and "name" in r.text
    assert r.chips and st["expecting"] == "name"


async def test_happy_path_one_field_at_a_time():
    st, rs = await chat(["Priya Sharma", "98765 43210", "priya@example.com", "Check-up",
                         "Over a year ago", "tomorrow at 4pm", "yes"])
    assert "Nice to meet you, Priya" in rs[1].text
    assert rs[-2].card["slot"] == "Tue 10 Nov, 4:00 PM" and rs[-2].card["phone"] == "+919876543210"
    assert st["stage"] == "done" and st["booking"]["ref"].startswith("BSD-")
    assert "confirmed" in rs[-1].text


async def test_many_fields_in_one_message():
    st, rs = await chat(["Hi, I'm Rahul Sharma, my number is 9876543210 and email rahul@example.com. I need a cleaning"])
    assert st["slots"] == {"name": "Rahul Sharma", "phone": "+919876543210", "email": "rahul@example.com", "purpose": "cleaning"}
    assert st["expecting"] == "last_visit"


async def test_any_order_purpose_first():
    st, rs = await chat(["I have tooth pain"])
    assert st["slots"]["purpose"] == "toothache" and st["expecting"] == "name"


async def test_invalid_phone_escalates_hint():
    st, rs = await chat(["Anita", "12345", "999"])
    assert "doesn't look quite right" in rs[2].text
    assert "98765 43210" in rs[3].text and st["fails"]["phone"] == 2
    st2, rs2 = await chat(["9876543210"], st=st)
    assert st["slots"]["phone"] == "+919876543210" and "phone" not in st["fails"]


async def test_unclear_name():
    st, rs = await chat(["?!"])
    assert "didn't quite catch your name" in rs[1].text and st["expecting"] == "name"


async def test_email_typo_suggestion():
    st, rs = await chat(["Anita", "9876543210", "anita@gmial.com"])
    assert "did you mean anita@gmail.com" in rs[-1].text
    await chat(["yes"], st=st)
    assert st["slots"]["email"] == "anita@gmail.com"


async def test_purpose_first_is_acknowledged():
    st, rs = await chat(["I have really bad tooth pain"])
    assert "sorry to hear" in rs[-1].text and "name" in rs[-1].text


async def test_typo_confirm_does_not_raise_email_error():
    st, rs = await chat(["Anita", "9876543210", "anita@gmial.com", "yes"])
    assert "incomplete" not in rs[-1].text and st["expecting"] == "purpose"


async def test_question_during_slot_answers_first_without_repeating_urgent():
    st, rs = await chat(["Ravi Kumar", "9876543210", "ravi@example.com", "toothache", "9", "no", "where are you?"])
    assert rs[-1].text.startswith("We're at") and "urgent" not in rs[-1].text and len(rs[-1].chips) == 3


async def test_symptom_question_is_not_misread_as_cosmetic():
    st, rs = await chat(["I have yellow tongue ?"])
    r = rs[-1].text
    assert st["slots"]["purpose"] == "checkup" and "smile" not in r
    assert "can't diagnose" in r and "name" in r and "didn't quite catch" not in r
    assert st["details"]["concern"] == "I have yellow tongue ?"


async def test_unknown_question_is_not_a_name_error():
    st, rs = await chat(["do you sell toothpaste?"])
    assert "Good question" in rs[-1].text and "didn't quite catch" not in rs[-1].text and st["expecting"] == "name"


async def test_yellow_teeth_is_still_cosmetic():
    st, _ = await chat(["my teeth look yellow"])
    assert st["slots"]["purpose"] == "cosmetic"


async def test_pain_chip_range_maps_to_band_not_first_digit():
    st, _ = await chat(["Ravi Kumar", "9876543210", "ravi@example.com", "toothache", "Severe (7–10)"])
    assert st["details"]["pain_level"] == 8 and st["urgent"] is True


async def test_severe_toothache_is_urgent_and_offers_earliest():
    st, rs = await chat(["Ravi Kumar", "9876543210", "ravi@example.com", "My tooth hurts", "9", "yes"])
    assert st["urgent"] is True
    assert "urgent" in rs[-1].text
    assert rs[-1].chips[0] == "Mon 09 Nov, 11:00 AM"  # earliest ≥1h from now


async def test_rejects_sunday_and_past_and_suggests():
    base = ["Ravi Kumar", "9876543210", "ravi@example.com", "cleaning", "6 months ago"]
    st, rs = await chat(base + ["Sunday at 11am"])
    assert "closed on Sundays" in rs[-1].text and len(rs[-1].chips) == 3
    st, rs = await chat(base + ["today at 9am"])
    assert "already passed" in rs[-1].text


async def test_date_only_offers_that_day_and_chip_pick():
    st, rs = await chat(["Ravi Kumar", "9876543210", "ravi@example.com", "cleaning", "6 months ago", "friday afternoon"])
    assert rs[-1].chips[0] == "Fri 13 Nov, 2:00 PM"
    await chat([rs[-1].chips[1]], st=st)
    assert st["stage"] == "confirm" and st["slots"]["slot"].startswith("2026-11-13T14:30")


async def test_correction_at_confirmation():
    st, rs = await chat(["Priya Sharma", "9876543210", "priya@example.com", "checkup", "never", "tomorrow 11am",
                         "actually my email is priya.s@example.com"])
    assert st["slots"]["email"] == "priya.s@example.com"
    assert rs[-1].card and rs[-1].card["email"] == "priya.s@example.com"


async def test_change_something_flow():
    st, rs = await chat(["Priya Sharma", "9876543210", "priya@example.com", "checkup", "never", "tomorrow 11am",
                         "Change something", "Date & time", "wednesday 3pm"])
    assert rs[-1].card["slot"] == "Wed 11 Nov, 3:00 PM"


async def test_faq_mid_flow_answers_and_reasks():
    st, rs = await chat(["Priya Sharma", "what are your opening hours?"])
    assert "Monday to Saturday" in rs[-1].text and "mobile number" in rs[-1].text


async def test_empty_message():
    st, rs = await chat(["Priya", ""])
    assert "didn't receive anything" in rs[-1].text


async def test_concurrent_double_booking_only_one_wins():
    sched = MemoryScheduler()
    d = deps(sched)
    script = ["checkup", "never", "tomorrow at 4pm"]
    a, _ = await chat(["Asha Rao", "9876543210", "asha@example.com", *script], d)
    b, _ = await chat(["Bina Das", "9123456780", "bina@example.com", *script], d)
    ra, rb = await asyncio.gather(flow.turn(a, "yes", d), flow.turn(b, "yes", d))
    outcomes = sorted([a["stage"], b["stage"]])
    assert outcomes == ["collect", "done"]
    loser = rb if b["stage"] == "collect" else ra
    assert "someone just booked" in loser.text and len(loser.chips) == 3
    assert len(sched.bookings) == 1


async def test_cancel_with_verification():
    sched = MemoryScheduler()
    d = deps(sched)
    st, _ = await chat(["Priya Sharma", "9876543210", "priya@example.com", "checkup", "never", "tomorrow 11am", "yes"], d)
    ref = st["booking"]["ref"]
    other, rs = await chat([f"I want to cancel {ref}", "9123456780", "9876543210", "yes"], d)
    assert "doesn't match" in rs[2].text
    assert "Are you sure" in rs[3].text
    assert "has been cancelled" in rs[4].text
    assert sched.bookings[ref]["status"] == "cancelled"


async def test_reschedule_moves_booking():
    sched = MemoryScheduler()
    d = deps(sched)
    st, _ = await chat(["Priya Sharma", "9876543210", "priya@example.com", "checkup", "never", "tomorrow 11am", "yes"], d)
    old = st["booking"]["ref"]
    _, rs = await chat(["reschedule please", "9876543210", "thursday at 3pm", "yes"], d, st)
    assert st["stage"] == "done" and st["booking"]["ref"] != old
    assert sched.bookings[old]["status"] == "cancelled"
    assert "Thu 12 Nov, 3:00 PM" in rs[-1].text


def test_state_signing_rejects_tampering():
    st = new_state()
    st["slots"]["name"] = "X"
    token = sign(st)
    assert verify(token)["slots"]["name"] == "X"
    body, mac = token.rsplit(".", 1)
    with pytest.raises(BadState):
        verify(body[:-2] + "AA." + mac)


async def test_gum_bleeding_question_gets_no_diagnosis_and_purpose():
    st, rs = await chat(["why do my gums bleed?"])
    assert st["slots"]["purpose"] == "toothache" and "can't diagnose" in rs[-1].text


async def test_location_faq_near_metro():
    st, rs = await chat(["is the clinic near the metro?"])
    assert "Lakeview Road" in rs[-1].text


async def test_smalltalk_is_not_a_name_error():
    st, rs = await chat(["hi, how are you?"])
    assert "didn't quite catch" not in rs[-1].text and "name" in rs[-1].text


def test_fast_path_skips_llm_for_button_taps_and_bare_values():
    st = new_state()
    st["expecting"] = "phone"
    ex = flow.rule_extract("98765 43210", st, NOW)
    assert flow._rules_enough("98765 43210", st, ex)
    assert flow._rules_enough("Yes, confirm", st, ex)
    st["expecting"] = "name"
    assert not flow._rules_enough("hey it's rahul and my tooth hurts", st, flow.rule_extract("hey it's rahul and my tooth hurts", st, NOW))


def test_symptom_vs_general_question():
    from smilebot.validators import is_symptom_question as s
    assert s("I have yellow tongue, is that normal?") and s("why do my gums bleed?")
    assert not s("does a checkup hurt?") and not s("how long does whitening last?")


def test_fact_guard_rejects_invented_numbers_and_promises():
    st = new_state()
    st["transcript"] = [{"role": "user", "text": "the first one"}]
    plan = "Here's a summary of your appointment. Thu 12 Nov, 4:00 PM. Shall I go ahead and confirm it?"
    assert flow._safe("Lovely! Thursday 12 Nov at 4:00 PM it is. Shall I confirm?", plan, st)
    assert not flow._safe("Thursday at 5 pm then. Shall I confirm?", plan, st)
    assert not flow._safe("Great, we'll see you then!", plan, st)


class _LyingLLM:
    """An LLM that ignores instructions: invents a time and claims the booking."""
    async def extract(self, msg, st, now): return {}
    async def phrase(self, st, notes, answer, question, user_q=None): return "Booked for 5 pm, see you then! Anything else?"
    async def react(self, st, user_q=None): return "Great, you're booked for 5 pm!"
    async def summarize(self, transcript, lead): return None


async def test_lying_llm_cannot_change_facts():
    d = flow.Deps(scheduler=MemoryScheduler(), llm=_LyingLLM(), now=lambda: NOW)
    st, rs = await chat(["Priya Sharma", "9876543210", "priya@example.com", "checkup", "never", "tomorrow 4pm"], d)
    assert "5 pm" not in rs[-1].text and "summary" in rs[-1].text and rs[-1].card["slot"] == "Tue 10 Nov, 4:00 PM"
    assert all("booked" not in r.text.lower() for r in rs)


async def test_ignored_followup_is_skipped_and_early_date_preference_used():
    st, rs = await chat(["my son broke his tooth, can we come tomorrow?", "Ananya Shah", "9819012345",
                         "ananya@example.com", "ok the earliest please"])
    assert "not given" in st["details"].values() and st["expecting"] == "slot"
    assert all(c.startswith("Tue 10 Nov") for c in rs[-1].chips)  # "tomorrow" from the first message
    assert "Good question" not in rs[1].text


async def test_comma_name():
    st, _ = await chat(["Priya, Priya Menon"])
    assert st["slots"]["name"] == "Priya Menon"


async def test_skipped_pain_then_swelling_does_not_crash():
    st, rs = await chat(["Ravi Kumar", "9876543210", "ravi@example.com", "toothache", "just book it", "yes"])
    assert st["details"]["pain_level"] == "not given" and st["urgent"] is True and st["expecting"] == "slot"


async def test_question_during_scheduling_keeps_same_offered_slots():
    st, rs = await chat(["Ravi Kumar", "9876543210", "ravi@example.com", "cleaning", "never", "friday afternoon",
                         "how much is a cleaning?"])
    assert rs[-1].chips == rs[-2].chips and rs[-1].chips[0].startswith("Fri 13 Nov")
