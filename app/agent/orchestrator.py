"""One inbound rider message, end to end.

  dedupe by message_id -> per-rider lock -> understand -> plan (LLM or rules) with tools
  -> guard reply -> persist reply + state -> return.

Guarantees:
  * a vendor retry (same wamid) never re-runs the agent; it gets the stored reply;
  * messages of one rider are processed one at a time and in order (advisory lock);
  * we answer inside the vendor's ~10s window; slow payouts finish in the background.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from app.agent import llm_planner, nlu, replies, rules_planner
from app.agent.tools import Toolbox
from app.config import Settings
from app.db import Database
from app.domain.disputes import Context, open_escalation
from app.domain.policy import IST
from app.payouts import PayoutService
from app.payswift import PaySwiftClient
from app.trace import Tracer, redact, to_json

log = logging.getLogger(__name__)
HISTORY_TURNS = 12
HOLDING_REPLY = "Aapka message mil gaya hai, check kar rahe hain. Thodi der mein update milega."


class Busy(Exception):
    """The rider's previous message is still being processed; the vendor should retry."""


@dataclass
class Inbound:
    message_id: str
    rider_id: str
    text: str
    received_at: datetime


class Agent:
    def __init__(self, db: Database, settings: Settings, payswift: PaySwiftClient, payouts: PayoutService) -> None:
        self.db, self.settings, self.payswift, self.payouts = db, settings, payswift, payouts
        # Bounds concurrent agent turns so a burst can't exhaust the DB pool (each turn uses <= 2 connections).
        self._slots = threading.BoundedSemaphore(max(1, settings.max_concurrent_turns))
        self._provider = None
        if settings.llm_enabled:
            try:
                self._provider = llm_planner.make_provider(settings)
            except Exception:  # noqa: BLE001
                log.exception("LLM provider init failed; using rules planner")

    @property
    def planner_name(self) -> str:
        return f"llm:{self.settings.llm_provider}" if self._provider else "rules"

    # ---- entry point -------------------------------------------------------------------------
    def handle(self, msg: Inbound) -> dict:
        started = time.monotonic()
        deadline = started + self.settings.reply_budget_seconds
        tracer = Tracer(self.db, msg.rider_id, msg.message_id)

        existing = self._claim_message(msg)
        if existing is not None:
            return self._duplicate(msg, existing, tracer, deadline)

        if not self._slots.acquire(timeout=max(0.1, deadline - time.monotonic() - 3.0)):
            self._release_claim(msg)
            raise Busy()
        try:
            try:
                lock_ctx = self.db.lock_conn()
                lock_conn = lock_ctx.__enter__()
            except Exception:
                self._release_claim(msg)
                raise Busy() from None
            try:
                if not self._lock_rider(lock_conn, msg.rider_id, deadline - 2.0):
                    # Release the claim so the vendor's retry processes it properly and in order.
                    self._release_claim(msg)
                    raise Busy()
                try:
                    reply, planner = self._process(msg, tracer, deadline)
                except psycopg.OperationalError:
                    self._release_claim(msg)
                    raise
                finally:
                    lock_conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("rider:" + msg.rider_id,))
            finally:
                lock_ctx.__exit__(None, None, None)
        finally:
            self._slots.release()

        with self.db.tx() as conn:
            conn.execute("UPDATE messages SET status='done', reply=%s, planner=%s, completed_at=now() WHERE message_id=%s",
                         (reply, planner, msg.message_id))
        tracer.step("reply", "reply_sent", None, reply)
        log.info("message handled", extra={"rider_id": msg.rider_id, "message_id": msg.message_id, "planner": planner,
                                           "ms": int((time.monotonic() - started) * 1000)})
        return {"reply": reply, "message_id": msg.message_id}

    # ---- idempotency ---------------------------------------------------------------------------
    def _claim_message(self, msg: Inbound) -> dict | None:
        with self.db.tx() as conn:
            row = conn.execute(
                "INSERT INTO messages (message_id, rider_id, text, received_at) VALUES (%s,%s,%s,%s)"
                " ON CONFLICT (message_id) DO UPDATE SET deliveries = messages.deliveries + 1"
                " RETURNING (xmax = 0) AS inserted, rider_id, text, status, reply",
                (msg.message_id, msg.rider_id, msg.text, msg.received_at),
            ).fetchone()
        return None if row["inserted"] else row

    def _duplicate(self, msg: Inbound, row: dict, tracer: Tracer, deadline: float) -> dict:
        if row["rider_id"] != msg.rider_id or row["text"] != msg.text:
            tracer.step("error", "message_id_conflict", {"message_id": msg.message_id, "text": msg.text[:200]},
                        {"stored_rider_id": row["rider_id"], "action": "ignored"})
            return {"reply": replies.ASK_DETAILS[False], "message_id": msg.message_id, "duplicate": True}
        while row["status"] == "processing" and time.monotonic() < deadline:
            time.sleep(0.2)
            with self.db.tx() as conn:
                row = conn.execute("SELECT status, reply FROM messages WHERE message_id = %s", (msg.message_id,)).fetchone()
            if row is None:  # the original gave up (rider busy): vendor will retry later
                raise Busy()
        tracer.step("decision", "duplicate_delivery", {"message_id": msg.message_id},
                    {"action": "returned stored reply" if row["status"] == "done" else "original still processing"})
        return {"reply": row["reply"] or HOLDING_REPLY, "message_id": msg.message_id, "duplicate": True}

    def _release_claim(self, msg: Inbound) -> None:
        try:
            with self.db.tx() as conn:
                conn.execute("DELETE FROM messages WHERE message_id = %s AND status = 'processing'", (msg.message_id,))
        except Exception:  # noqa: BLE001
            log.exception("could not release message claim", extra={"message_id": msg.message_id})

    def _lock_rider(self, conn, rider_id: str, until: float) -> bool:
        while True:
            if conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", ("rider:" + rider_id,)).fetchone()["ok"]:
                return True
            if time.monotonic() > until:
                return False
            time.sleep(0.1)

    # ---- the agent turn ------------------------------------------------------------------------
    def _process(self, msg: Inbound, tracer: Tracer, deadline: float) -> tuple[str, str]:
        tracer.step("message_in", "rider_message",
                    {"message_id": msg.message_id, "text": msg.text, "received_at": msg.received_at.isoformat()}, None)
        today = msg.received_at.astimezone(IST).date()
        with self.db.tx() as conn:
            known = conn.execute("SELECT 1 FROM riders WHERE rider_id = %s", (msg.rider_id,)).fetchone()
            row = conn.execute("SELECT state FROM conversations WHERE rider_id = %s", (msg.rider_id,)).fetchone()
        state = dict(row["state"]) if row else {}
        if not known:
            tracer.step("decision", "unknown_rider", {"rider_id": msg.rider_id}, {"action": "no account actions"})
            return ("Is number se juda koi rider account nahi mila. Kripya registered number se message karein.", "rules")

        limit = self.settings.rider_max_messages_per_hour
        if limit > 0:
            with self.db.tx() as conn:
                recent = conn.execute(
                    "SELECT count(*) AS n FROM messages WHERE rider_id = %s AND message_id <> %s"
                    " AND created_at > now() - interval '1 hour'", (msg.rider_id, msg.message_id)).fetchone()["n"]
            if recent >= limit:
                # A flood (abuse, a stuck phone, a script) gets a holding reply and no tool calls or LLM spend.
                tracer.step("decision", "rider_rate_limited", {"messages_last_hour": recent + 1, "limit": limit},
                            {"action": "holding reply, no agent actions"})
                open_escalation(self.db, tracer, msg.rider_id, "message_flood",
                                f"Rider sent more than {limit} messages in an hour; the agent paused for this rider",
                                {"messages_last_hour": recent + 1}, msg.message_id)
                en = nlu.detect_language(msg.text) == "english"
                return ((
                    "We've received a lot of messages from you. The ops team has your case and will get back to you."
                    if en else "Aapke bahut saare messages aaye hain. Aapka case ops team ke paas hai, wo aapse sampark karenge."),
                    "rate_limited")

        interp = nlu.parse(msg.text, msg.rider_id, today, awaiting_date=state.get("awaiting") == "date")
        tracer.step("decision", "message_understood", {"text": msg.text}, {
            "language": interp.language, "security_flag": interp.security_flag,
            "claims": [{"date": c.date, "trip_ids": c.trip_ids, "issue": c.issue(), "claimed_amount": c.claimed_amount,
                        "claimed_trip_count": c.claimed_trip_count} for c in interp.claims],
            "pushback": interp.pushback, "status_query": interp.status_query, "insist": interp.insist,
            "wants_human": interp.wants_human, "awaiting": state.get("awaiting")})

        ctx = Context(db=self.db, settings=self.settings, payswift=self.payswift, tracer=tracer,
                      rider_id=msg.rider_id, today=today, message_id=msg.message_id)
        tools = Toolbox(ctx, self.payouts, deadline)
        planner = "rules"
        try:
            if self._provider and not interp.security_flag:
                reply = self._llm_turn(msg, interp, tools, state, tracer, deadline)
                planner = self.planner_name if reply else "rules(fallback)"
                if not reply:
                    reply = self._fallback_reply(msg, interp, tools, state)
            else:
                reply = rules_planner.run(msg.text, interp, tools, state)
        except psycopg.OperationalError:
            raise
        except Exception as exc:  # noqa: BLE001 - the rider still gets an answer, ops gets the case
            log.exception("agent turn failed", extra={"rider_id": msg.rider_id})
            tracer.step("error", "agent_turn_failed", {"message_id": msg.message_id}, {"error": f"{type(exc).__name__}: {exc}"})
            open_escalation(self.db, tracer, msg.rider_id, "internal_error", f"Agent failed: {type(exc).__name__}",
                            {"message_id": msg.message_id}, msg.message_id)
            reply = replies.INTERNAL_ERROR[interp.language == "english"]

        history = (state.get("history") or []) + [{"role": "user", "content": msg.text}, {"role": "assistant", "content": reply}]
        state["history"] = history[-HISTORY_TURNS:]
        state["language"] = interp.language
        state["last_message_at"] = datetime.now(UTC).isoformat()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO conversations (rider_id, state, updated_at) VALUES (%s,%s,now())"
                " ON CONFLICT (rider_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                (msg.rider_id, Jsonb(to_json(state))),
            )
        return reply, planner

    def _llm_turn(self, msg: Inbound, interp: nlu.Interpretation, tools: Toolbox, state: dict, tracer: Tracer,
                  deadline: float) -> str | None:
        hints = {"dates_found": [c.date.isoformat() for c in interp.claims if c.date],
                 "trip_ids_found": sorted({t for c in interp.claims for t in c.trip_ids}),
                 "issues_found": sorted({c.issue() for c in interp.claims})}
        system = llm_planner.SYSTEM_PROMPT.format(today=tools.ctx.today.isoformat(), rider_id=msg.rider_id) + \
            f"\nParser hints for the latest message (may be incomplete): {hints}"
        history = [h for h in (state.get("history") or [])][-HISTORY_TURNS:] + [{"role": "user", "content": msg.text}]
        try:
            reply = self._provider.run(system, history, llm_planner.make_executor(tools),
                                       self.settings.llm_max_steps, deadline - 1.0)
        except Exception as exc:  # noqa: BLE001
            err = redact(f"{type(exc).__name__}: {exc}")[:300]
            tracer.step("error", "llm_failed", {"provider": self.settings.llm_provider}, {"error": err, "fallback": "rules planner"})
            log.warning("llm failed", extra={"rider_id": msg.rider_id, "error": err})
            return None
        rider_texts = [h["content"] for h in history if h["role"] == "user"]
        problems = llm_planner.check_reply(reply, tools, rider_texts, tools.ctx.today)
        if problems:
            tracer.step("decision", "llm_reply_rejected", {"reply": reply}, {"problems": problems})
            return None
        return reply

    def _fallback_reply(self, msg: Inbound, interp: nlu.Interpretation, tools: Toolbox, state: dict) -> str:
        en = interp.language == "english"
        if tools.outcomes:  # the engine already acted this turn: describe exactly that
            return " ".join(replies.outcome_text(o, en) for o in tools.outcomes)
        return rules_planner.run(msg.text, interp, tools, state)
