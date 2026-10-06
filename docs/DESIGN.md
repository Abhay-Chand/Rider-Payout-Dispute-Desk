# Design notes (full)

The one-page summary is in the [README](../README.md). This file has the full detail.

An agent that settles QuickDrop riders' payout complaints over WhatsApp, pays small corrections through PaySwift, and sends everything else to an ops page. Start with `BRIEF.md` for the task.

## Run

```bash
cp .env.example .env                 # optional: LLM key, OPS_TOKEN
docker compose up --build            # app :8000, Postgres, PaySwift sandbox :8081
open http://localhost:8000/ops       # ops page: sign in with OPS_TOKEN from .env (default "change-me")
python evals/run.py http://localhost:8000 --reset-cmd "docker compose down -v && docker compose up -d --wait"
```

Tests: `pip install -r requirements-dev.txt && pytest` (needs Postgres; `TEST_ADMIN_DATABASE_URL`). CI runs lint, tests, then `docker compose` + evals against PaySwift with its fault injection on.

## Design

**The model decides; code holds the money.** The planner (Claude, or any OpenAI-compatible model such as Groq or Gemini) reads the conversation, works out what the rider means, picks tools, asks follow-ups and writes the reply. It never sees or passes an amount or a rider id. The tools are bound to the vendor's `rider_id`:

| Tool | What it does |
|---|---|
| `lookup_trip(trip_id)` | The rider's own trip only; another rider's trip reads as "not on your account" |
| `review_day(date)` | Read-only recomputation of one IST day |
| `resolve_dispute(date, issue, trip_ids?)` | The only path to money: the engine recomputes, decides, and pays or queues |
| `case_status()` | Payouts and approvals so far |
| `escalate_to_ops(category, reason)` | Hands the case to a human |

**Code decides the money** (`app/domain/`). Per IST day it recomputes every trip, penalty and incentive from policy and diffs that against the payout lines. A dispute is one day. The payable amount is the shortfall on items not already settled, capped by the day's net shortfall, so "never pay more than owed" holds even when the rider was overpaid elsewhere that day.
- "Already settled" comes from **PaySwift's ledger**: each payout reference encodes the items it covered. It's combined with our own in-flight rows.
- Auto-pay applies only if:
  - the amount is ≤ ₹200;
  - it's the rider's first auto-pay that IST day (a DB unique index enforces this);
  - the ledger was verifiable;
  - total auto-pay that day stays under `AUTO_PAY_DAILY_BUDGET_INR` (₹20,000 by default; checked under a lock, so racing riders can't both slip under).

  Anything else becomes an ops approval.
- **Shadow mode** (`AUTO_PAY_MODE=shadow`) answers "I need to trust it before it moves money". The agent decides exactly as in live mode, including the daily rules, but every payout waits for ops. The ops page shows how often ops agreed with the agent, so you go live once the agreement rate is high enough.
- Approving re-validates against the trips and the ledger first. Rejected items stay closed.

**Payout safety.** The intent row is committed before PaySwift is called (outbox pattern). The intent id is the `Idempotency-Key`, and the request body never changes between retries.
- 503s, timeouts, 504 "lost responses" and 409 in-progress replies are retried by a worker with backoff. It checks the ledger first.
- A process-wide limiter stays under PaySwift's 30 POSTs/10s account block.
- Intents that can't complete escalate to ops. `/ops/api/reconciliation` diffs our intents against PaySwift.

**Messages.** The vendor's `wamid` is deduplicated in Postgres, so retries return the stored reply and never re-run the agent. A per-rider advisory lock keeps each rider's messages in order, and turns are bounded so a burst can't exhaust the DB pool. Replies go out within about 3s; slow payouts finish in the background, and the rider is told "processing".

**Safety.**
- A deterministic screen flags impersonation (another rider id) and prompt injection. Flagged messages never reach the model, get a fixed refusal, and escalate.
- Every number in an LLM reply must come from tool results or the rider's own words, excluding the model's own tool arguments. Every amount paid or queued that turn must be mentioned. A reply saying money was sent ("bhej diya", "has been paid") is blocked unless a payment actually exists. Failing any check falls back to a templated reply.
- A rider sending more than `RIDER_MAX_MESSAGES_PER_HOUR` (30) gets a holding reply, with no tool calls and no LLM spend, and ops gets one escalation.
- If the LLM fails or times out, the rules planner takes over, and the ops page shows a banner saying why (for example, a rejected API key).
- The ops page signs in with `OPS_TOKEN`, and its API needs it for reads as well as approvals, since conversations are personal data. `/trace` and `/ops/pending` stay open, as the brief's contract requires. An optional webhook secret protects `/messages`.
- Credentials are redacted from the audit trail, including the masked key fragments that provider errors echo back.

**Audit.** Every step (message, parse, tool call, decision, PaySwift attempt, reply) goes to `trace_steps` → `GET /trace/{rider}`.

**Ops page.** A work queue (approvals by amount, escalations, conversations) next to the selected case:
- each approval is itemised like a pay slip: order, what went wrong, should be, was paid, owed, and why it needs a person;
- approving re-checks before paying, and rejecting needs a reason that goes into the case history;
- each escalation says what to do next;
- the agent's trace is shown as a plain-language timeline, with the raw data one click away;
- the header shows PaySwift reconciliation, payouts in flight and LLM health. A reconciliation mismatch opens a details dialog that names the likely cause and the affected riders.

The page loads no third-party assets: Public Sans (SIL OFL, `app/static/fonts/`) ships with the app, so it works on closed networks under a strict CSP. It is responsive down to phone width, with wide tables scrolling inside their cards.

**Insights** (`GET /ops/api/insights`, `app/insights.py`) are computed from the trace and payout tables, with no extra bookkeeping. A dispute is one rider asking about one IST day, counted once by the first decision made. The view covers:
- the share of riders settled with no human involved;
- where the money went (paid automatically, paid after approval, sending, waiting, rejected, stuck);
- underpayments by type, which point at upstream payout-system bugs;
- how often each kind of rider claim was right;
- how disputes ended, and why cases reached ops;
- reply and decision times, and the oldest waiting item;
- agent health: LLM versus template replies, guard blocks, duplicates absorbed, security flags.

## Assumptions

- **Messy exports normalized on load** (counts in `/ops/api/data-quality`): `R7`/`r19` become `R007`/`R019`; 10 duplicated trip rows are dropped; integer distances ≥ 100 are meters. That last rule reproduces what was actually paid.
- **"Today"** is the IST date of `received_at`. The 7-day window is inclusive. "20" said on the 5th means last month.
- **Auto-pay day:** "once per rider per day" uses the business date of the message. Approved payouts don't use up the daily auto-pay.
- **When we escalate:**
  - a rider disputes our records after a re-check;
  - a rider disputes the recorded distance (it can't be verified);
  - a rider asks for a penalty waiver;
  - a trip isn't on the rider's account;
  - a shortfall is found outside the window;
  - a message is suspicious;
  - a rider asks for a human.
- **Vague complaints** get a question for the date or order before anything is paid.
- **Single instance:** the rate limiter and worker assume one app process.

## Eval results

`evals/run.py` uses only the public interface plus PaySwift, and measures money in PaySwift. Each conversation runs on a fresh system: with `--reset-cmd` the system is reset, and without it a rider with history is reported as SKIP rather than a false pass. It covers the 24 sample conversations plus 16 of mine: IST-vs-UTC incentive, meters, `r19`, once-per-day, concurrent duplicates, English, injection, customer cancellation, window, and story change.

| Suite | Cases | Correct | Never overpaid | Notes |
|---|---|---|---|---|
| Main (24 samples + 16 of mine) | 40 | **40/40** | 40/40 | All checks pass, including reply facts, < 10s replies and trace/pending shape. p95 latency is about 1–3s with PaySwift chaos on. |
| Hard (`cases_hard.json`) | 14 | 9/14 → **14/14** | 14/14 | Devanagari, typos, number words, story changes, two days in one phrase. The 5 misses (all safe: the agent asked for the date) led to parser fixes, so this set is no longer held out for the rules planner. |
| Held-out (`cases_heldout.json`) | 7 | **5/7** | 7/7 | Written after those fixes and run once, untuned. The misses are a Devanagari number word ("सोलह") and two days as words; both are safe, with the agent asking for the date. This is the gap the LLM planner is for. |

All runs use the rules planner with PaySwift chaos on. CI fails if the main suite has any failure, or if the hard or held-out suites ever overpay. Results appear on the GitHub Actions run page.

Also verified:
- 19 riders messaging concurrently under chaos all settled exactly once.
- A 20s PaySwift outage recovered with one payout.
- 6 simultaneous deliveries of one wamid produced one reply and one payout.
- 98 pytest tests pass. They cover:
  - lost responses, 429, in-progress replies and a stale ledger;
  - approve/reject;
  - shadow mode, and the daily budget under a race;
  - the message flood limit;
  - LLM guard bypass attempts and false payment claims.
- The ops page was driven end to end in a headless DOM: sign-in, approve, reject, resolve, timeline, and shadow-mode agreement.

## Skipped / limits

- **No live LLM numbers yet.** The planner, guard and fallback are tested with a scripted model. To measure it, set `LLM_PROVIDER`/`LLM_API_KEY` and run all three suites; the held-out set shows the difference most.
- **Not built:**
  - outbound rider notifications when ops approves (the vendor interface is reply-only);
  - multi-instance coordination;
  - ops SSO / roles;
  - clawback of overpayments (they are only reported).
