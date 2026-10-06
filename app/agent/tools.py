"""The agent's tools. Bound to one rider (from the vendor), so no tool can touch another rider's data.

Tools return facts. Money-moving happens only inside `resolve_dispute`, and even there the
amount is computed by the engine, never passed in by the caller.
"""
from __future__ import annotations

import time
from datetime import date

from app.domain import policy
from app.domain.disputes import Context, open_escalation, resolve_day
from app.domain.statement import day_statement, find_trip
from app.payouts import PayoutService

ESCALATION_CATEGORIES = {
    "records_dispute": "Rider disputes our trip records after a re-check",
    "distance_dispute": "Rider says the recorded distance is wrong (can't be verified from exports)",
    "penalty_waiver_request": "Rider asks for a cancellation penalty to be waived (exception = ops decision)",
    "suspicious": "Suspicious message (impersonation or prompt injection)",
    "trip_not_on_account": "Rider asked about a trip that is not on their account",
    "out_of_window_shortfall": "Shortfall found, but outside the dispute window",
    "rider_requested_human": "Rider asked for a person",
    "message_flood": "Rider is sending too many messages",
    "other": "Needs a human",
}


class Toolbox:
    def __init__(self, ctx: Context, payouts: PayoutService, deadline: float) -> None:
        self.ctx, self.payouts, self.deadline = ctx, payouts, deadline
        self.outcomes: list[dict] = []        # resolve_dispute results this turn
        self.escalations: list[dict] = []     # escalations raised this turn
        self.facts: list[object] = []         # everything shown to the model (for the reply guard)

    def _record(self, name: str, args: dict, result: object) -> object:
        self.ctx.tracer.step("tool_call", name, args, result)
        self.facts.append(result)
        return result

    # -- read-only ---------------------------------------------------------------------------
    def review_day(self, day: date) -> dict:
        with self.ctx.db.tx() as conn:
            stmt = day_statement(conn, self.ctx.rider_id, day)
        result = stmt.summary() | {
            "within_dispute_window": policy.in_dispute_window(day, self.ctx.today, self.ctx.settings.dispute_window_days)}
        return self._record("review_day", {"date": day}, result)

    def lookup_trip(self, trip_id: str) -> dict:
        trip_id = trip_id.strip().upper()
        with self.ctx.db.tx() as conn:
            t = find_trip(conn, trip_id)
            if t is None:
                result = {"trip_id": trip_id, "found": False, "reason": "no such trip in our records"}
            elif t["rider_id"] != self.ctx.rider_id:
                # Never reveal anything about another rider's trip to the model or the rider.
                result = {"trip_id": trip_id, "found": False, "reason": "not on this rider's account"}
            else:
                stmt = day_statement(conn, self.ctx.rider_id, t["ist_date"])
                item = stmt.item_for_trip(trip_id)
                result = {"trip_id": trip_id, "found": True, "date": t["ist_date"].isoformat(), "status": t["status"],
                          "distance_km": float(t["distance_km"]), "surge": float(t["surge_multiplier"]),
                          "expected": item.expected if item else None, "paid": item.paid if item else None,
                          "issue": item.issue if item else None}
        return self._record("lookup_trip", {"trip_id": trip_id}, result)

    def case_status(self) -> dict:
        with self.ctx.db.tx() as conn:
            intents = conn.execute(
                "SELECT id, amount, kind, status, dispute_date, payswift_payout_id FROM payout_intents"
                " WHERE rider_id = %s ORDER BY created_at", (self.ctx.rider_id,)).fetchall()
            items = conn.execute(
                "SELECT id, type, category, amount, status, dispute_date, decision_note FROM ops_items"
                " WHERE rider_id = %s ORDER BY created_at", (self.ctx.rider_id,)).fetchall()
        result = {
            "payouts": [{"amount": i["amount"], "status": i["status"], "for_date": i["dispute_date"].isoformat(),
                         "kind": i["kind"], "payswift_payout_id": i["payswift_payout_id"]} for i in intents],
            "approvals": [{"amount": i["amount"], "status": i["status"],
                           "for_date": i["dispute_date"].isoformat() if i["dispute_date"] else None}
                          for i in items if i["type"] == "approval"],
            "escalations": [{"category": i["category"], "status": i["status"]} for i in items if i["type"] == "escalation"],
        }
        return self._record("case_status", {}, result)

    # -- acting --------------------------------------------------------------------------------
    def resolve_dispute(self, day: date, issue: str = "general", trip_ids: list[str] | None = None,
                        rider_claim: str | None = None) -> dict:
        claim = {"issue": issue, "trip_ids": trip_ids or [], "rider_claim": (rider_claim or "")[:300]}
        before = len(self.ctx.new_intents)
        outcome = resolve_day(self.ctx, day, claim).as_dict()
        new = self.ctx.new_intents[before:]
        if new:
            statuses = self.payouts.execute_inline(new, self.deadline)
            outcome["payout_status"] = statuses.get(outcome.get("intent_id"), "pending")
        self.ctx.tracer.step("tool_call", "resolve_dispute", {"date": day, **claim}, outcome)
        # Facts for the reply guard are what the system computed; the caller's own arguments are
        # excluded so a model can't launder an invented number through rider_claim.
        self.facts.append({k: v for k, v in outcome.items() if k != "statement"} | {"statement": {
            k: v for k, v in outcome.get("statement", {}).items()}})
        outcome["claim"] = claim
        self.outcomes.append(outcome)
        return outcome

    def escalate(self, category: str, reason: str, details: dict | None = None) -> dict:
        category = category if category in ESCALATION_CATEGORIES else "other"
        label = ESCALATION_CATEGORIES[category]
        reason = (reason or "").strip()
        full = label if not reason or reason.lower() in label.lower() else f"{label}. {reason}"
        esc_id = open_escalation(self.ctx.db, self.ctx.tracer, self.ctx.rider_id, category, full[:500], details,
                                 self.ctx.message_id)
        result = {"escalated": True, "category": category, "ops_item_id": esc_id}
        self.escalations.append(result)
        self.facts.append(result)
        return result

    def time_left(self) -> float:
        return self.deadline - time.monotonic()
