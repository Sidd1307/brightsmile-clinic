# BrightSmile Clinic — AI receptionist (Aria)

## Objective
An inbound enquiry + appointment-booking assistant (chat **and** voice "call" mode) for a dental clinic.
Built for an AI Engineer assessment: reliable core flow, natural AI conversation, real scheduling.

## Core design: hybrid state machine + LLM
- **Code owns the flow.** A slot-filling state machine decides what is still missing and never confirms until every slot is valid.
- **LLM owns language.** It extracts several slots from free text and writes natural replies.
- **Code validates LLM output.** Phone/email regex, dates must be in the future and inside clinic hours.
- **Graceful degradation.** No `GROQ_API_KEY` or the API errors → deterministic regex/dateparser fallback. Same flow, same tests.
- **Stateless server.** Conversation state travels with the client, HMAC-signed so it can't be tampered with → works on Vercel serverless and scales to concurrent users with no shared memory.

## Slots
name, phone, email, purpose (checkup | cleaning | toothache | cosmetic | braces | implant | kids | other),
purpose follow-ups (e.g. toothache → pain level 1–10 + swelling; cosmetic → treatment of interest; kids → child's age),
appointment slot (date + time).

## Requirements → acceptance criteria
| # | Requirement | Done when |
|---|---|---|
| R1 | Welcome | First turn greets with time-of-day greeting + clinic name + quick-reply chips |
| R2 | Data collection | All 4 contact/purpose slots collected, any order, multiple per message |
| R3 | Follow-ups | Purpose-specific follow-up asked; severe toothache flagged URGENT and earliest slot offered |
| R4 | Scheduling | Natural-language date/time ("next Tue 4pm") parsed; past/closed/out-of-hours rejected; 3 free slots suggested |
| R5 | Confirmation | Summary card with all details; user can confirm or correct any field ("change my email to…") |
| R6 | Error handling | Invalid/empty/unclear/off-topic input re-prompted with a specific hint; after 2 failures, an example is given |
| B1 | AI API | Groq (Llama 3) for extraction + replies, fallback without key |
| B2 | Voice | Browser call-centre mode: STT, TTS, live captions, call timer, barge-in |
| B3 | Calendar | Google Calendar free/busy + event creation; race-safe re-check before booking |
| X1 | Ops dashboard | Leads table with AI summary, sentiment, lead score, urgency, transcript |
| X2 | Reschedule/cancel | By booking reference |
| X3 | Eval suite | Scripted messy conversations, pass rate printed |

## Clinic rules
Mon–Sat 09:00–18:00 (lunch 13:00–14:00 closed), Sunday closed, 30-min slots, timezone Asia/Kolkata, bookable up to 60 days ahead.

## Stack
FastAPI (async) · httpx · Groq API · Google Calendar API (service account) · SQLite locally / Postgres (`DATABASE_URL`) in prod ·
vanilla HTML/CSS/JS frontend in `web/` · pytest · Vercel.

## Concurrency
- All I/O is async (`httpx.AsyncClient`); independent calls run with `asyncio.gather` (e.g. AI summary + calendar insert at booking).
- Double-booking guard: slot re-checked (DB unique constraint on slot + calendar free/busy) at commit time; loser gets new suggestions.
- No in-process session state → any number of concurrent conversations.

## Boundaries
- Always: validate everything the LLM returns; keep secrets in env vars.
- Never: let the LLM decide a booking is confirmed; log full PII to stdout.
