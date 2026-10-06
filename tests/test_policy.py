from datetime import date
from decimal import Decimal

import pytest

from app.domain import policy


@pytest.mark.parametrize("km,surge,expected", [
    ("0.8", "1.0", 25),      # under 2 km: base only
    ("2.0", "1.0", 25),
    ("6.0", "1.5", 74),      # (25 + 24) * 1.5 = 73.5 -> rounds half up
    ("7.4", "1.0", 57),      # 25 + 32.4 = 57.4
    ("10.0", "1.2", 88),     # (25 + 48) * 1.2 = 87.6
    ("2.25", "1.0", 27),     # 25 + 1.5 = 26.5 -> 27 (half up, not banker's)
    ("9.0", "1.5", 101),     # (25 + 42) * 1.5 = 100.5 -> 101
])
def test_trip_fare(km, surge, expected):
    assert policy.trip_fare(Decimal(km), Decimal(surge)) == expected


def test_cancellations():
    assert policy.expected_trip_amount("cancelled_by_rider", Decimal("5"), Decimal("1.5")) == -10
    assert policy.expected_trip_amount("cancelled_by_customer", Decimal("5"), Decimal("1.5")) == 0


def test_incentive_threshold():
    assert policy.daily_incentive(11) == 0
    assert policy.daily_incentive(12) == 150


@pytest.mark.parametrize("raw,norm", [("R7", "R007"), ("r19", "R019"), ("R003", "R003"), (" r 5 ", "R005"), ("X1", None), ("", None)])
def test_rider_ids(raw, norm):
    assert policy.normalize_rider_id(raw) == norm


@pytest.mark.parametrize("raw,km,converted", [("5000", Decimal("5"), True), ("900", Decimal("0.9"), True),
                                              ("5.0", Decimal("5.0"), False), ("12", Decimal("12"), False)])
def test_distance_units(raw, km, converted):
    assert policy.normalize_distance_km(raw) == (km, converted)


def test_window_is_seven_days_inclusive():
    today = date(2026, 9, 23)
    assert policy.in_dispute_window(date(2026, 9, 16), today, 7)
    assert not policy.in_dispute_window(date(2026, 9, 15), today, 7)
    assert not policy.in_dispute_window(date(2026, 9, 24), today, 7)
