"""docs/policy.md, as code. Pure functions, no I/O: these are what money decisions rest on."""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))

BASE_FARE = Decimal(25)
PER_KM = Decimal(6)
FREE_KM = Decimal(2)
DAILY_INCENTIVE = 150
INCENTIVE_MIN_TRIPS = 12
RIDER_CANCEL_PENALTY = -10


def trip_fare(distance_km: Decimal, surge: Decimal) -> int:
    """₹25 base + ₹6/km after the first 2 km; surge multiplies the whole fare; round half up."""
    distance = max(Decimal(0), Decimal(distance_km) - FREE_KM)
    fare = (BASE_FARE + PER_KM * distance) * Decimal(surge)
    return int(fare.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def expected_trip_amount(status: str, distance_km: Decimal, surge: Decimal) -> int:
    if status == "completed":
        return trip_fare(distance_km, surge)
    if status == "cancelled_by_rider":
        return RIDER_CANCEL_PENALTY
    return 0  # cancelled_by_customer earns nothing


def daily_incentive(completed_trips: int) -> int:
    return DAILY_INCENTIVE if completed_trips >= INCENTIVE_MIN_TRIPS else 0


def ist_date(ts: datetime) -> date:
    """'Day' means the calendar day in IST."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(IST).date()


def in_dispute_window(dispute_day: date, today: date, window_days: int) -> bool:
    """We only look at disputes for the last N days (inclusive of today)."""
    return today - timedelta(days=window_days) <= dispute_day <= today


# ---- export normalisation (the data arrives "as it came") -----------------------------------

_RIDER_RE = re.compile(r"^\s*[Rr]\s*0*(\d{1,4})\s*$")


def normalize_rider_id(raw: str) -> str | None:
    """'R7', 'r19', 'R007' -> 'R007' / 'R019'. Returns None for anything else."""
    m = _RIDER_RE.match(raw or "")
    if not m:
        return None
    return f"R{int(m.group(1)):03d}"


def normalize_distance_km(raw: str) -> tuple[Decimal, bool]:
    """Some exports carry metres ('5000') instead of km ('5.0').

    Rule: an integer with no decimal point and value >= 100 is metres. No delivery trip is
    100 km, and the rule reproduces what the payout system actually paid for these riders.
    Returns (km, was_converted).
    """
    s = (raw or "").strip()
    value = Decimal(s)
    if "." not in s and value >= 100:
        return value / Decimal(1000), True
    return value, False
