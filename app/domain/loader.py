"""Loads the ops exports into Postgres, normalising known data-quality issues.

Every correction is counted and stored in `data_load_report`, so ops can see what we changed.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from app.db import Database
from app.domain.policy import ist_date, normalize_distance_km, normalize_rider_id

log = logging.getLogger(__name__)


def _rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def load_exports(db: Database, data_dir: str) -> dict:
    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(774002)")
        if conn.execute("SELECT 1 FROM trips LIMIT 1").fetchone():
            return {"skipped": "already loaded"}

        report: dict = {"rider_ids_normalized": 0, "distance_metres_converted": 0,
                        "duplicate_trip_rows_dropped": 0, "conflicting_duplicate_trips": [],
                        "rejected_rows": []}

        for r in _rows(os.path.join(data_dir, "riders.csv")):
            rid = normalize_rider_id(r["rider_id"])
            if not rid:
                report["rejected_rows"].append({"file": "riders.csv", "row": r})
                continue
            conn.execute(
                "INSERT INTO riders (rider_id, name, city, joined_on) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (rid, r["name"], r.get("city"), r.get("joined_on") or None),
            )

        seen: dict[str, tuple] = {}
        trip_rows = []
        for r in _rows(os.path.join(data_dir, "trips.csv")):
            rid = normalize_rider_id(r["rider_id"])
            try:
                started = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
                km, converted = normalize_distance_km(r["distance_km"])
                surge = Decimal(r["surge_multiplier"] or "1")
            except Exception:  # noqa: BLE001 - a bad row is reported, never silently used
                rid = None
            if not rid or r["status"] not in {"completed", "cancelled_by_customer", "cancelled_by_rider"}:
                report["rejected_rows"].append({"file": "trips.csv", "row": r})
                continue
            if rid != r["rider_id"]:
                report["rider_ids_normalized"] += 1
            if converted:
                report["distance_metres_converted"] += 1
            key = (rid, started, r["status"], km, surge)
            if r["trip_id"] in seen:
                report["duplicate_trip_rows_dropped"] += 1
                if seen[r["trip_id"]] != key:
                    report["conflicting_duplicate_trips"].append(r["trip_id"])
                continue
            seen[r["trip_id"]] = key
            trip_rows.append((r["trip_id"], rid, started, ist_date(started), r["status"], km, r["distance_km"], surge))

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO trips (trip_id, rider_id, started_at, ist_date, status, distance_km, distance_raw, surge_multiplier)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                trip_rows,
            )
            line_rows = []
            for r in _rows(os.path.join(data_dir, "payout_lines.csv")):
                rid = normalize_rider_id(r["rider_id"])
                if not rid:
                    report["rejected_rows"].append({"file": "payout_lines.csv", "row": r})
                    continue
                if rid != r["rider_id"]:
                    report["rider_ids_normalized"] += 1
                line_rows.append((r["line_id"], r["payout_date"], rid, r["line_type"], r["trip_id"] or None, int(r["amount"])))
            cur.executemany(
                "INSERT INTO payout_lines (line_id, payout_date, rider_id, line_type, trip_id, amount)"
                " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                line_rows,
            )
        report["trips"] = len(trip_rows)
        report["payout_lines"] = len(line_rows)
        conn.execute("INSERT INTO data_load_report (report) VALUES (%s)", (Jsonb(report),))
        log.info("exports loaded", extra={"report": report})
        return report
