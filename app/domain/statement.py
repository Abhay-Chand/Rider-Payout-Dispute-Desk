"""Recomputes what a rider should have been paid for one IST day and compares it with what was paid.

This is the "executive with the trips sheet" step, done deterministically. The LLM never does
arithmetic: it only ever sees the result of this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

import psycopg

from app.domain import policy


@dataclass
class Item:
    key: str                 # stable identity of the money component, e.g. "trip:T926334"
    kind: str                # trip_fare | penalty | customer_cancel | incentive | unmatched_line
    issue: str               # ok | missing_payment | surge_not_applied | fare_mismatch | duplicate_penalty | ...
    expected: int
    paid: int
    trip_id: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def diff(self) -> int:
        """Positive => rider was underpaid by this much."""
        return self.expected - self.paid


@dataclass
class DayStatement:
    rider_id: str
    day: date
    completed_trips: int
    rider_cancellations: int
    customer_cancellations: int
    items: list[Item]
    has_data: bool

    @property
    def expected_total(self) -> int:
        return sum(i.expected for i in self.items)

    @property
    def paid_total(self) -> int:
        return sum(i.paid for i in self.items)

    @property
    def net_diff(self) -> int:
        return self.expected_total - self.paid_total

    @property
    def underpaid(self) -> list[Item]:
        return [i for i in self.items if i.diff > 0]

    @property
    def overpaid(self) -> list[Item]:
        return [i for i in self.items if i.diff < 0]

    def item_for_trip(self, trip_id: str) -> Item | None:
        return next((i for i in self.items if i.trip_id == trip_id), None)

    def summary(self) -> dict:
        """Compact, model-safe view (no other riders' data, no internals)."""
        return {
            "date": self.day.isoformat(),
            "has_data": self.has_data,
            "completed_trips": self.completed_trips,
            "rider_cancellations": self.rider_cancellations,
            "customer_cancellations": self.customer_cancellations,
            "incentive_rule": f"₹{policy.DAILY_INCENTIVE} for {policy.INCENTIVE_MIN_TRIPS}+ completed trips",
            "expected_total": self.expected_total,
            "paid_total": self.paid_total,
            "net_difference": self.net_diff,
            "discrepancies": [
                {k: v for k, v in asdict(i).items() if v not in (None, {})} | {"difference": i.diff}
                for i in self.items if i.diff != 0
            ],
        }


def _trip_issue(status: str, km: Decimal, surge: Decimal, expected: int, paid: int, n_lines: int) -> str:
    if expected == paid:
        return "ok"
    if status == "completed":
        if n_lines == 0:
            return "missing_payment"
        if surge != 1 and paid == policy.trip_fare(km, Decimal(1)):
            return "surge_not_applied"
        return "fare_mismatch"
    if status == "cancelled_by_rider":
        return "duplicate_penalty" if paid < expected else "penalty_mismatch"
    return "paid_for_customer_cancellation"


def day_statement(conn: psycopg.Connection, rider_id: str, day: date) -> DayStatement:
    trips = conn.execute(
        "SELECT trip_id, status, distance_km, distance_raw, surge_multiplier, started_at"
        " FROM trips WHERE rider_id = %s AND ist_date = %s ORDER BY started_at, trip_id",
        (rider_id, day),
    ).fetchall()
    trip_ids = [t["trip_id"] for t in trips]
    lines = conn.execute(
        "SELECT line_id, payout_date, line_type, trip_id, amount FROM payout_lines"
        " WHERE rider_id = %s AND (trip_id = ANY(%s) OR payout_date = %s) ORDER BY line_id",
        (rider_id, trip_ids, day),
    ).fetchall()

    by_trip: dict[str, list[dict]] = {}
    other_lines: list[dict] = []
    for ln in lines:
        if ln["trip_id"] and ln["trip_id"] in trip_ids:
            by_trip.setdefault(ln["trip_id"], []).append(ln)
        else:
            other_lines.append(ln)

    items: list[Item] = []
    completed = rider_cancels = customer_cancels = 0
    for t in trips:
        km, surge, status = Decimal(t["distance_km"]), Decimal(t["surge_multiplier"]), t["status"]
        expected = policy.expected_trip_amount(status, km, surge)
        tl = by_trip.get(t["trip_id"], [])
        paid = sum(ln["amount"] for ln in tl)
        completed += status == "completed"
        rider_cancels += status == "cancelled_by_rider"
        customer_cancels += status == "cancelled_by_customer"
        kind = {"completed": "trip_fare", "cancelled_by_rider": "penalty"}.get(status, "customer_cancel")
        prefix = "trip" if kind == "trip_fare" else "penalty" if kind == "penalty" else "custcancel"
        items.append(Item(
            key=f"{prefix}:{t['trip_id']}", kind=kind, trip_id=t["trip_id"],
            issue=_trip_issue(status, km, surge, expected, paid, len(tl)),
            expected=expected, paid=paid,
            detail={"status": status, "distance_km": float(km), "surge": float(surge),
                    "started_at": t["started_at"].isoformat(), "paid_lines": len(tl)},
        ))

    incentive_paid = 0
    unmatched: list[dict] = []
    # Lines dated this day that point at a trip of this rider on a *different* day belong to that
    # day's statement; anything else (unknown trip / another rider's trip) is unmatched.
    stray_ids = [ln["trip_id"] for ln in other_lines if ln["trip_id"]]
    own_elsewhere = {
        r["trip_id"] for r in conn.execute(
            "SELECT trip_id FROM trips WHERE rider_id = %s AND trip_id = ANY(%s)", (rider_id, stray_ids)
        ).fetchall()
    } if stray_ids else set()
    for ln in other_lines:
        if ln["payout_date"] != day:
            continue
        if ln["line_type"] == "daily_incentive" and not ln["trip_id"]:
            incentive_paid += ln["amount"]
        elif ln["trip_id"] not in own_elsewhere:
            unmatched.append(ln)

    incentive_expected = policy.daily_incentive(completed)
    if trips or incentive_paid:
        items.append(Item(
            key=f"incentive:{rider_id}:{day.isoformat()}", kind="incentive",
            issue="ok" if incentive_expected == incentive_paid else ("missing_incentive" if incentive_paid < incentive_expected else "incentive_overpaid"),
            expected=incentive_expected, paid=incentive_paid,
            detail={"completed_trips": completed, "threshold": policy.INCENTIVE_MIN_TRIPS},
        ))
    for ln in unmatched:
        items.append(Item(key=f"line:{ln['line_id']}", kind="unmatched_line", issue="unmatched_line",
                          expected=0, paid=ln["amount"], trip_id=ln["trip_id"], detail={"line_type": ln["line_type"]}))

    return DayStatement(rider_id=rider_id, day=day, completed_trips=completed, rider_cancellations=rider_cancels,
                        customer_cancellations=customer_cancels, items=items, has_data=bool(trips or lines))


def find_trip(conn: psycopg.Connection, trip_id: str) -> dict | None:
    return conn.execute(
        "SELECT trip_id, rider_id, ist_date, status, distance_km, surge_multiplier, started_at FROM trips WHERE trip_id = %s",
        (trip_id,),
    ).fetchone()
