"""The deterministic money engine. The model can ask for a day to be resolved; only this code decides
whether anything is owed, how much, and whether it is auto-paid, sent for approval, or refused.

Finance rules enforced here:
  * pay only the computed shortfall for specific items, capped by the day's net shortfall
    ("never pay more than the rider is owed"), minus anything already settled;
  * "already settled" is read from PaySwift (our reconciliation source of truth) AND our own
    in-flight records, so a crash between deciding and paying can never double-pay;
  * auto-pay only <= AUTO_PAY_LIMIT per dispute and once per rider per IST day (DB-enforced);
  * everything else becomes an ops approval; disputes outside the window are refused.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from app.config import Settings
from app.db import Database
from app.domain import policy
from app.domain.statement import DayStatement, day_statement
from app.payswift import PaySwiftClient
from app.trace import Tracer, to_json

log = logging.getLogger(__name__)
REF_PREFIX = "qd"


def make_reference(intent_id: str, dispute_date: date, item_keys: list[str]) -> str:
    return f"{REF_PREFIX}|{intent_id}|{dispute_date.isoformat()}|{','.join(sorted(item_keys))}"


def parse_reference(ref: str | None) -> dict | None:
    parts = (ref or "").split("|")
    if len(parts) != 4 or parts[0] != REF_PREFIX:
        return None
    return {"intent_id": parts[1], "dispute_date": parts[2], "items": [k for k in parts[3].split(",") if k]}


@dataclass
class Context:
    """Everything a decision may depend on. rider_id comes from the vendor, never from the model."""
    db: Database
    settings: Settings
    payswift: PaySwiftClient
    tracer: Tracer
    rider_id: str
    today: date                  # business date (IST) of the message being handled
    message_id: str | None
    new_intents: list[str] = field(default_factory=list)
    _ledger: list[dict] | None = None
    _ledger_ok: bool | None = None

    def ledger(self) -> tuple[list[dict], bool]:
        """PaySwift payouts for this rider (cached per message)."""
        if self._ledger_ok is None:
            try:
                self._ledger = self.payswift.list_payouts(self.rider_id)
                self._ledger_ok = True
            except Exception as exc:  # noqa: BLE001
                self._ledger, self._ledger_ok = [], False
                self.tracer.step("error", "payswift_ledger_unavailable", {"rider_id": self.rider_id}, {"error": str(exc)})
            else:
                self.tracer.step("tool_call", "payswift_list_payouts", {"rider_id": self.rider_id},
                                 {"count": len(self._ledger), "total": sum(p.get("amount", 0) for p in self._ledger)})
        return self._ledger or [], bool(self._ledger_ok)


def _settled(conn: psycopg.Connection, ctx: Context, day: date) -> tuple[dict[str, dict], bool]:
    """Item keys already handled, from PaySwift (authoritative) plus our own pending/rejected records."""
    settled: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT item_key, amount, state FROM settled_items WHERE rider_id = %s", (ctx.rider_id,)
    ).fetchall():
        settled[row["item_key"]] = {"amount": row["amount"], "state": row["state"]}
    ledger, ok = ctx.ledger()
    for p in ledger:
        ref = parse_reference(p.get("reference"))
        if ref:
            for k in ref["items"]:
                settled.setdefault(k, {"amount": None, "state": "paid_in_payswift"})
                settled[k]["in_payswift"] = True
    return settled, ok


@dataclass
class DayOutcome:
    date: str
    status: str            # auto_paid | approval_pending | nothing_owed | out_of_window | no_data | already_handled
    amount: int = 0
    reason: str = ""
    items: list[dict] = field(default_factory=list)
    statement: dict = field(default_factory=dict)
    ops_item_id: str | None = None
    intent_id: str | None = None
    approval_reason: str | None = None
    previously_handled: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return to_json(self.__dict__)


def resolve_day(ctx: Context, day: date, claim: dict | None = None) -> DayOutcome:
    claim = claim or {}
    window_ok = policy.in_dispute_window(day, ctx.today, ctx.settings.dispute_window_days)
    with ctx.db.tx() as conn:
        stmt = day_statement(conn, ctx.rider_id, day)
        if not window_ok:
            out = DayOutcome(date=day.isoformat(), status="out_of_window",
                             reason=f"Disputes are only reviewed for the last {ctx.settings.dispute_window_days} days",
                             statement=stmt.summary())
            ctx.tracer.step("decision", "dispute_out_of_window", {"date": day, "today": ctx.today, "claim": claim},
                            {"status": out.status, "shortfall_found": sum(i.diff for i in stmt.underpaid)})
            return out
        if not stmt.has_data:
            out = DayOutcome(date=day.isoformat(), status="no_data", reason="No trips or payouts on record for this day")
            ctx.tracer.step("decision", "dispute_no_data", {"date": day, "claim": claim}, {"status": out.status})
            return out
        return _decide(conn, ctx, stmt, claim)


def _decide(conn: psycopg.Connection, ctx: Context, stmt: DayStatement, claim: dict) -> DayOutcome:
    settled, ledger_ok = _settled(conn, ctx, stmt.day)
    owed = [i for i in stmt.underpaid if i.key not in settled]
    handled = [i for i in stmt.underpaid if i.key in settled]
    already_for_day = sum(i.diff for i in handled if settled[i.key].get("state") != "rejected")
    day_cap = max(0, stmt.net_diff - already_for_day)
    gross = sum(i.diff for i in owed)
    amount = min(gross, day_cap)

    decision_input = {
        "date": stmt.day, "today": ctx.today, "claim": claim,
        "statement": {"expected_total": stmt.expected_total, "paid_total": stmt.paid_total, "net_difference": stmt.net_diff},
        "owed_items": [{"key": i.key, "issue": i.issue, "difference": i.diff} for i in owed],
        "already_handled": [{"key": i.key, **settled[i.key]} for i in handled],
        "overpaid_items": [{"key": i.key, "issue": i.issue, "difference": i.diff} for i in stmt.overpaid],
        "day_cap": day_cap, "payswift_ledger_verified": ledger_ok,
        "auto_pay_limit": ctx.settings.auto_pay_limit,
    }
    base = DayOutcome(
        date=stmt.day.isoformat(), status="nothing_owed", statement=stmt.summary(),
        items=[{"key": i.key, "trip_id": i.trip_id, "issue": i.issue, "expected": i.expected, "paid": i.paid,
                "difference": i.diff, "detail": i.detail} for i in owed],
        previously_handled=[{"key": i.key, "trip_id": i.trip_id, "issue": i.issue, "difference": i.diff,
                             "state": settled[i.key].get("state")} for i in handled],
    )

    if amount <= 0:
        if handled and not owed:
            base.status, base.reason = "already_handled", "These differences were already paid or sent to ops"
        elif owed:
            base.reason = "Shortfall is offset by overpayments on the same day"
        else:
            base.reason = "Paid amount matches policy"
        ctx.tracer.step("decision", "dispute_nothing_owed", decision_input, {"status": base.status, "reason": base.reason})
        return base

    # Never pay more than owed: if the day cap trims the shortfall, keep the item list but cap the amount.
    keys = [i.key for i in owed]
    base.amount = amount

    auto_blockers = []
    if not ctx.settings.auto_pay_enabled:
        auto_blockers.append("auto-pay is switched off")
    if amount > ctx.settings.auto_pay_limit:
        auto_blockers.append(f"₹{amount} is above the ₹{ctx.settings.auto_pay_limit} auto-pay limit")
    if not ledger_ok:
        auto_blockers.append("could not verify past payouts in PaySwift")
    shadow = ctx.settings.auto_pay_mode == "shadow"
    # In shadow mode, "would have auto-paid" decisions count as auto-payouts, so the simulation applies the
    # same once-per-day and budget rules that live mode would.
    already_auto = conn.execute(
        "SELECT 1 FROM payout_intents WHERE rider_id = %s AND business_date = %s AND kind = 'auto'"
        " UNION ALL SELECT 1 FROM ops_items WHERE rider_id = %s AND business_date = %s AND category = 'shadow_auto_pay'"
        " AND status <> 'rejected'",
        (ctx.rider_id, ctx.today, ctx.rider_id, ctx.today),
    ).fetchone()
    if already_auto:
        auto_blockers.append("rider already received an automatic payout today (limit: once per day)")

    budget = ctx.settings.auto_pay_daily_budget
    if not auto_blockers and budget > 0:
        # Serialise budget checks for this business day so two riders can't both squeeze under the ceiling.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"autopay-budget:{ctx.today.isoformat()}",))
        spent = conn.execute(
            "SELECT coalesce(sum(amount), 0) AS s FROM ("
            " SELECT amount FROM payout_intents WHERE business_date = %s AND kind = 'auto'"
            " UNION ALL SELECT amount FROM ops_items WHERE business_date = %s AND category = 'shadow_auto_pay'"
            " AND status <> 'rejected') x",
            (ctx.today, ctx.today),
        ).fetchone()["s"]
        decision_input["auto_pay_budget"] = {"budget": budget, "spent_today": spent}
        if spent + amount > budget:
            auto_blockers.append(f"the daily auto-pay budget of ₹{budget} is used up (₹{spent} paid automatically today)")

    if not auto_blockers and shadow:
        ops_id = f"apr_{uuid.uuid4().hex[:16]}"
        conn.execute(
            "INSERT INTO ops_items (id, rider_id, type, category, amount, reason, business_date, dispute_date, items, details, message_id)"
            " VALUES (%s,%s,'approval','shadow_auto_pay',%s,%s,%s,%s,%s,%s,%s)",
            (ops_id, ctx.rider_id, amount, f"Payout for {stmt.day.isoformat()} needs approval: shadow mode, the agent would have paid ₹{amount} automatically",
             ctx.today, stmt.day, Jsonb(keys),
             Jsonb(to_json({"owed_items": base.items, "statement": base.statement, "claim": claim, "shadow": True})), ctx.message_id),
        )
        _claim_items(conn, ctx.rider_id, stmt, owed, "awaiting_approval", ops_item_id=ops_id)
        base.status, base.ops_item_id = "approval_pending", ops_id
        base.approval_reason = base.reason = "shadow mode: every payout is confirmed by ops"
        ctx.tracer.step("decision", "shadow_auto_payout", decision_input,
                        {"status": base.status, "would_auto_pay": amount, "ops_item_id": ops_id, "items": keys})
        return base

    if not auto_blockers:
        intent_id = f"pi_{uuid.uuid4().hex[:20]}"
        ref = make_reference(intent_id, stmt.day, keys)
        try:
            with conn.transaction():  # savepoint: unique index enforces once-per-day under any race
                conn.execute(
                    "INSERT INTO payout_intents (id, rider_id, amount, kind, business_date, dispute_date, items, reference, message_id)"
                    " VALUES (%s,%s,%s,'auto',%s,%s,%s,%s,%s)",
                    (intent_id, ctx.rider_id, amount, ctx.today, stmt.day, Jsonb(keys), ref, ctx.message_id),
                )
                _claim_items(conn, ctx.rider_id, stmt, owed, "paying", intent_id=intent_id)
        except psycopg.errors.UniqueViolation:
            auto_blockers.append("rider already received an automatic payout today (limit: once per day)")
        else:
            ctx.new_intents.append(intent_id)
            base.status, base.intent_id = "auto_paid", intent_id
            base.reason = "Within auto-pay limits"
            ctx.tracer.step("decision", "auto_payout_approved", decision_input,
                            {"status": base.status, "amount": amount, "intent_id": intent_id, "items": keys})
            return base

    ops_id = f"apr_{uuid.uuid4().hex[:16]}"
    reason = "; ".join(auto_blockers)
    conn.execute(
        "INSERT INTO ops_items (id, rider_id, type, category, amount, reason, business_date, dispute_date, items, details, message_id)"
        " VALUES (%s,%s,'approval','payout',%s,%s,%s,%s,%s,%s,%s)",
        (ops_id, ctx.rider_id, amount, f"Payout for {stmt.day.isoformat()} needs approval: {reason}", ctx.today, stmt.day,
         Jsonb(keys), Jsonb(to_json({"owed_items": base.items, "statement": base.statement, "claim": claim})), ctx.message_id),
    )
    _claim_items(conn, ctx.rider_id, stmt, owed, "awaiting_approval", ops_item_id=ops_id)
    base.status, base.ops_item_id, base.approval_reason = "approval_pending", ops_id, reason
    base.reason = reason
    ctx.tracer.step("decision", "payout_sent_for_approval", decision_input,
                    {"status": base.status, "amount": amount, "ops_item_id": ops_id, "why": auto_blockers})
    return base


def _claim_items(conn, rider_id: str, stmt: DayStatement, items, state: str, *, intent_id=None, ops_item_id=None) -> None:
    for i in items:
        conn.execute(
            "INSERT INTO settled_items (item_key, rider_id, dispute_date, amount, state, intent_id, ops_item_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (i.key, rider_id, stmt.day, i.diff, state, intent_id, ops_item_id),
        )


def open_escalation(db: Database, tracer: Tracer, rider_id: str, category: str, reason: str,
                    details: dict | None = None, message_id: str | None = None) -> str:
    """Idempotent per (rider, category) while open, so a chatty rider doesn't flood ops."""
    esc_id = f"esc_{uuid.uuid4().hex[:16]}"
    with db.tx() as conn:
        row = conn.execute(
            "INSERT INTO ops_items (id, rider_id, type, category, reason, details, message_id)"
            " VALUES (%s,%s,'escalation',%s,%s,%s,%s)"
            " ON CONFLICT (rider_id, category) WHERE type = 'escalation' AND status = 'pending' DO NOTHING RETURNING id",
            (esc_id, rider_id, category, reason[:500], Jsonb(to_json(details or {})), message_id),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT id FROM ops_items WHERE rider_id = %s AND category = %s AND type = 'escalation' AND status = 'pending'",
                (rider_id, category),
            ).fetchone()
            esc_id = existing["id"]
            created = False
        else:
            created = True
    tracer.step("decision", "escalated_to_ops", {"category": category, "reason": reason, "details": details or {}},
                {"ops_item_id": esc_id, "new": created})
    return esc_id
