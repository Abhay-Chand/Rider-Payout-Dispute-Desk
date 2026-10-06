# Rider Payout Dispute Desk

An AI agent that settles QuickDrop riders' WhatsApp payout complaints end to end. It talks to the rider, recomputes what they were owed, pays small corrections through PaySwift, and routes everything else to an ops page with a full audit trail. Detailed design notes are in [`docs/DESIGN.md`](docs/DESIGN.md).

## Run it

```bash
cp .env.example .env            # set OPS_TOKEN; optional: LLM_PROVIDER and LLM_API_KEY
docker compose up --build       # app on :8000, Postgres, PaySwift sandbox on :8081
```

- **Ops page:** http://localhost:8000/ops (sign in with `OPS_TOKEN`)
- **Evals:** `python evals/run.py http://localhost:8000 --reset-cmd "docker compose down -v && docker compose up -d --wait"`
- **Tests:** `pip install -r requirements-dev.txt && pytest` (needs Postgres on :5432). CI runs lint, tests and every eval suite on each push.

## Design

**The model handles language; code holds the money.**

| The model decides | Code decides |
|---|---|
| What the rider means: Hinglish, English, Devanagari, several messages, two complaints at once | Who the rider is (from the vendor, never from the model) |
| Which tool to call, what to ask, when to escalate | What is owed: each IST day recomputed from policy against the payout lines |
| The wording of the reply | Whether money moves (auto-pay, ops approval or refusal), plus limits, retries and reconciliation |

**Tools**, all bound to the sender's `rider_id`:
- `lookup_trip` and `review_day`: read-only.
- `resolve_dispute`: the only path to money, and it takes no amount.
- `case_status`.
- `escalate_to_ops`.

**Money rules:**
- Pay only the uncovered shortfall per item, capped by the day's net total, with "already paid" read from PaySwift's ledger.
- Auto-pay applies only up to ₹200, once per rider per IST day (a database constraint), and within a daily budget across all riders.
- Everything else becomes an ops approval, re-checked before paying.
- **Shadow mode** (`AUTO_PAY_MODE=shadow`) lets ops approve every decision and see their agreement rate before the agent moves money on its own.

**Reliability:**
- Each payout is saved before PaySwift is called. Its id is the idempotency key, so retries are byte-identical and can never pay twice.
- Lost responses are found in the ledger, PaySwift's rate limit is respected, and stuck payouts escalate.
- Duplicate webhooks return the stored reply, and a per-rider lock keeps messages in order.
- Replies take about 3 seconds at most; slow payouts finish in the background.

**Safety:**
- Impersonation and prompt injection are blocked before the LLM sees the message.
- LLM replies may state only numbers the system computed, must mention any money moved, and cannot claim a payment that didn't happen. Otherwise a template reply is sent.
- A rule-based planner takes over when no LLM is configured or it fails.
- Message floods pause the agent, and secrets are redacted from the audit trail.

**Traceability:**
- Every step is recorded at `GET /trace/{rider_id}`.
- The ops page shows the approval queue with an itemised payout slip, escalations with next steps, a plain-language timeline, PaySwift reconciliation, LLM health, and an Insights view.

## Assumptions

- **Exports are normalised on load:** `R7`/`r19` become `R007`/`R019`, 10 duplicated trip rows are dropped, and integer distances ≥ 100 are treated as metres, which reproduces what was actually paid.
- **"Today"** is the IST date of `received_at`, and the 7-day window is inclusive. A dispute is one rider plus one IST day.
- **"Once per rider per day"** limits automatic payouts; ops-approved payouts don't use it up.
- **Escalated:**
  - records still disputed after a re-check;
  - distance disputes;
  - penalty-waiver requests;
  - questions about another rider's order;
  - shortfalls older than 7 days;
  - suspicious messages;
  - requests for a person.
- **Vague complaints** get a clarifying question before anything is paid.
- **One app instance** is assumed.

## Eval results

`evals/run.py` uses only the public API and PaySwift. It measures money in PaySwift's ledger and gives each conversation a fresh system; without `--reset-cmd`, riders with history are reported as skipped, never as passed. All runs below use the rules planner with PaySwift's fault injection on.

| Suite | Cases | Correct | Never overpaid |
|---|---|---|---|
| Main: 24 samples + 16 of mine | 40 | **40/40** | 40/40 |
| Hard: Devanagari, typos, number words, story changes | 14 | **14/14** (9/14 before parser fixes, so no longer held out) | 14/14 |
| Held-out: written after those fixes, run once, untuned | 7 | **5/7** | 7/7 |

Every miss was safe: the agent asked for the date instead of guessing.

Also verified:
- **101 automated tests:** lost responses, 429s, concurrency races, the daily budget, shadow mode and LLM guard bypass attempts.
- **19 concurrent riders** each paid exactly once.
- **Recovery from a 20-second PaySwift outage.**
- **The ops page,** driven end to end in a headless browser (`tests/ui`).

## What I skipped

- **Live-LLM eval numbers.** The LLM path is covered by scripted-model tests; run the suites with a key to compare it with the rules planner.
- **Notifying riders after an ops decision.** The vendor interface is reply-only.
- **Production extras:** multi-instance coordination, ops SSO and roles, clawback of overpayments, and data retention.