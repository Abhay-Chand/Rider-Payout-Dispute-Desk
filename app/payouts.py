"""Moves money that the engine has already decided on, safely.

An intent row is committed BEFORE PaySwift is called (outbox pattern). Its id is the
Idempotency-Key and its reference is fixed, so every retry sends a byte-identical request and
PaySwift can never pay the same intent twice. A background worker drives anything that did not
settle inside the rider's reply budget, and escalates intents that cannot be completed.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import date

from psycopg.types.json import Jsonb

from app.config import Settings
from app.db import Database
from app.domain.disputes import open_escalation, parse_reference
from app.domain.statement import day_statement
from app.payswift import PaySwiftClient
from app.trace import Tracer

log = logging.getLogger(__name__)


class PayoutService:
    def __init__(self, db: Database, settings: Settings, client: PaySwiftClient) -> None:
        self.db, self.settings, self.client = db, settings, client
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- single attempt ------------------------------------------------------------------
    def attempt(self, intent_id: str, timeout: float) -> str:
        """One guarded attempt. Returns the intent's status afterwards."""
        with self.db.tx() as conn:
            it = conn.execute(
                "SELECT * FROM payout_intents WHERE id = %s AND status IN ('pending','retrying') FOR UPDATE SKIP LOCKED",
                (intent_id,),
            ).fetchone()
            if it is None:
                row = conn.execute("SELECT status FROM payout_intents WHERE id = %s", (intent_id,)).fetchone()
                return row["status"] if row else "missing"

            tracer = Tracer(self.db, it["rider_id"], it["message_id"])
            # After an ambiguous failure, look in the ledger first: cheaper than a POST and keeps us under rate limits.
            if it["attempts"] > 0:
                found = self._find_in_ledger(it)
                if found:
                    self._mark_paid(conn, it, found, tracer, via="ledger_reconciliation")
                    return "paid"

            res = self.client.create_payout(rider_id=it["rider_id"], amount=it["amount"], reference=it["reference"],
                                            idempotency_key=it["id"], timeout=timeout)
            attempts = it["attempts"] + (0 if res.error == "local_rate_limit" else 1)
            tracer.step("tool_call", "payswift_create_payout",
                        {"intent_id": it["id"], "amount": it["amount"], "attempt": attempts, "idempotency_key": it["id"]},
                        {"outcome": res.outcome, "status_code": res.status_code, "error": res.error,
                         "payout_id": (res.payout or {}).get("payout_id")})
            if res.outcome == "paid":
                self._mark_paid(conn, it, res.payout, tracer, via="payswift")
                return "paid"
            if res.outcome == "fatal" or attempts >= self.settings.payout_max_attempts:
                status = "failed" if res.outcome == "fatal" else "stuck"
                conn.execute(
                    "UPDATE payout_intents SET status=%s, attempts=%s, last_error=%s, updated_at=now() WHERE id=%s",
                    (status, attempts, res.error, it["id"]),
                )
                tracer.step("error", f"payout_{status}", {"intent_id": it["id"]}, {"error": res.error, "attempts": attempts})
                open_escalation(self.db, tracer, it["rider_id"], f"payout_{status}",
                                f"Payout {it['id']} of ₹{it['amount']} is {status} after {attempts} attempts: {res.error}",
                                {"intent_id": it["id"], "amount": it["amount"]}, it["message_id"])
                return status
            backoff = max(res.retry_after, min(10.0, 2.0 ** min(attempts, 4)))
            conn.execute(
                "UPDATE payout_intents SET status='retrying', attempts=%s, last_error=%s,"
                " next_attempt_at = now() + make_interval(secs => %s), updated_at=now() WHERE id=%s",
                (attempts, res.error, backoff, it["id"]),
            )
            return "retrying"

    def _find_in_ledger(self, it: dict) -> dict | None:
        try:
            for p in self.client.list_payouts(it["rider_id"]):
                ref = parse_reference(p.get("reference"))
                if ref and ref["intent_id"] == it["id"]:
                    return p
        except Exception:  # noqa: BLE001 - ledger outage just means "POST again with the same key"
            log.warning("ledger lookup failed", extra={"intent_id": it["id"]})
        return None

    def _mark_paid(self, conn, it: dict, payout: dict, tracer: Tracer, via: str) -> None:
        conn.execute(
            "UPDATE payout_intents SET status='paid', payswift_payout_id=%s, attempts=attempts+1, last_error=NULL,"
            " updated_at=now() WHERE id=%s",
            (payout.get("payout_id"), it["id"]),
        )
        tracer.step("decision", "payout_confirmed",
                    {"intent_id": it["id"], "via": via},
                    {"payout_id": payout.get("payout_id"), "amount": payout.get("amount"), "status": payout.get("status")})

    def execute_inline(self, intent_ids: list[str], deadline: float) -> dict[str, str]:
        """Try to settle within the reply budget; whatever is left is the worker's job."""
        results = {}
        for iid in intent_ids:
            status = "pending"
            while time.monotonic() < deadline - 0.5:
                timeout = min(self.settings.payswift_inline_timeout, deadline - time.monotonic())
                if timeout < 0.75:
                    break
                status = self.attempt(iid, timeout)
                if status != "retrying":
                    break
                with self.db.tx() as conn:
                    err = conn.execute("SELECT last_error FROM payout_intents WHERE id=%s", (iid,)).fetchone()["last_error"]
                # Only retry inline on errors known to be fast (plain 503). Anything slow goes to the worker.
                if err not in ("service_unavailable", "http_503"):
                    break
            results[iid] = status
        return results

    # ---- background worker ---------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="payout-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)

    def _run(self) -> None:
        log.info("payout worker started")
        while not self._stop.is_set():
            try:
                if self.run_due_once() == 0:
                    self._stop.wait(self.settings.worker_poll_seconds)
            except Exception:  # noqa: BLE001
                log.exception("payout worker iteration failed")
                self._stop.wait(2.0)

    def run_due_once(self) -> int:
        with self.db.tx() as conn:
            due = [r["id"] for r in conn.execute(
                "SELECT id FROM payout_intents WHERE status IN ('pending','retrying') AND next_attempt_at <= now()"
                " AND created_at < now() - interval '1 second' ORDER BY next_attempt_at LIMIT 10"
            ).fetchall()]
        for iid in due:
            if self.client.limiter.seconds_until_free() > 0:
                break
            self.attempt(iid, self.settings.payswift_worker_timeout)
        return len(due)

    # ---- ops decisions -------------------------------------------------------------------
    def decide(self, ops_item_id: str, action: str, actor: str, note: str | None = None) -> dict:
        """approve / reject an approval, or resolve an escalation. Re-validates money before paying."""
        with self.db.tx() as conn:
            item = conn.execute("SELECT * FROM ops_items WHERE id = %s FOR UPDATE", (ops_item_id,)).fetchone()
            if item is None:
                raise LookupError("not found")
            if item["status"] != "pending":
                return {"id": item["id"], "status": item["status"], "changed": False}
            tracer = Tracer(self.db, item["rider_id"], item["message_id"])

            if item["type"] == "escalation":
                if action not in ("resolve", "reject", "approve"):
                    raise ValueError("escalations can only be resolved")
                conn.execute("UPDATE ops_items SET status='resolved', decided_at=now(), decided_by=%s, decision_note=%s WHERE id=%s",
                             (actor, note, item["id"]))
                tracer.step("decision", "ops_resolved_escalation", {"ops_item_id": item["id"], "actor": actor, "note": note},
                            {"status": "resolved"})
                return {"id": item["id"], "status": "resolved", "changed": True}

            if action == "reject":
                conn.execute("UPDATE ops_items SET status='rejected', decided_at=now(), decided_by=%s, decision_note=%s WHERE id=%s",
                             (actor, note, item["id"]))
                conn.execute("UPDATE settled_items SET state='rejected' WHERE ops_item_id=%s", (item["id"],))
                tracer.step("decision", "ops_rejected_payout", {"ops_item_id": item["id"], "actor": actor, "note": note},
                            {"status": "rejected", "amount": item["amount"]})
                return {"id": item["id"], "status": "rejected", "changed": True}
            if action != "approve":
                raise ValueError("unknown action")

            # Re-validate: data or the ledger may have changed since the request was raised.
            stmt = day_statement(conn, item["rider_id"], item["dispute_date"])
            keys = set(item["items"])
            still_owed = sum(i.diff for i in stmt.underpaid if i.key in keys)
            ledger_keys = set()
            ledger_ok = True
            try:
                for p in self.client.list_payouts(item["rider_id"]):
                    ref = parse_reference(p.get("reference"))
                    if ref:
                        ledger_keys.update(ref["items"])
            except Exception:  # noqa: BLE001
                ledger_ok = False
            if not ledger_ok:
                raise RuntimeError("PaySwift ledger unavailable; cannot verify before paying. Try again shortly.")
            already = sum(i.diff for i in stmt.underpaid if i.key in keys and i.key in ledger_keys)
            amount = min(item["amount"], max(0, still_owed - already), max(0, stmt.net_diff))
            validation = {"requested": item["amount"], "still_owed": still_owed, "already_in_payswift": already,
                          "day_net_difference": stmt.net_diff, "approved_amount": amount}
            if amount <= 0:
                conn.execute("UPDATE ops_items SET status='rejected', decided_at=now(), decided_by=%s, decision_note=%s WHERE id=%s",
                             (actor, "auto: nothing owed on re-validation", item["id"]))
                conn.execute("UPDATE settled_items SET state='rejected' WHERE ops_item_id=%s", (item["id"],))
                tracer.step("decision", "ops_approval_voided", {"ops_item_id": item["id"], "actor": actor, **validation},
                            {"status": "rejected", "why": "nothing owed on re-validation"})
                return {"id": item["id"], "status": "rejected", "changed": True, "validation": validation}

            intent_id = f"pi_{uuid.uuid4().hex[:20]}"
            from app.domain.disputes import make_reference
            ref = make_reference(intent_id, item["dispute_date"], sorted(keys))
            conn.execute(
                "INSERT INTO payout_intents (id, rider_id, amount, kind, business_date, dispute_date, items, reference, ops_item_id, message_id)"
                " VALUES (%s,%s,%s,'approved',%s,%s,%s,%s,%s,%s)",
                (intent_id, item["rider_id"], amount, item["business_date"] or date.today(), item["dispute_date"],
                 Jsonb(sorted(keys)), ref, item["id"], item["message_id"]),
            )
            conn.execute("UPDATE settled_items SET state='paying', intent_id=%s WHERE ops_item_id=%s", (intent_id, item["id"]))
            conn.execute("UPDATE ops_items SET status='approved', decided_at=now(), decided_by=%s, decision_note=%s WHERE id=%s",
                         (actor, note, item["id"]))
            tracer.step("decision", "ops_approved_payout", {"ops_item_id": item["id"], "actor": actor, "note": note, **validation},
                        {"status": "approved", "intent_id": intent_id, "amount": amount})
        status = self.execute_inline([intent_id], time.monotonic() + 6.0)[intent_id]
        return {"id": ops_item_id, "status": "approved", "changed": True, "intent_id": intent_id,
                "payout_status": status, "validation": validation}

    # ---- reconciliation ------------------------------------------------------------------
    def reconcile(self) -> dict:
        """Compare our intents with PaySwift's ledger (the source of truth for money)."""
        ledger = self.client.list_payouts()
        by_intent = {}
        foreign = []
        for p in ledger:
            ref = parse_reference(p.get("reference"))
            if ref:
                by_intent.setdefault(ref["intent_id"], []).append(p)
            else:
                foreign.append(p)
        with self.db.tx() as conn:
            intents = conn.execute("SELECT id, rider_id, amount, status, payswift_payout_id FROM payout_intents").fetchall()
        report = {"intents": len(intents), "ledger_payouts": len(ledger), "matched": 0,
                  "paid_but_missing_in_payswift": [], "in_payswift_but_not_marked_paid": [],
                  "amount_mismatch": [], "duplicate_payouts_for_intent": [], "unknown_payswift_payouts": foreign}
        known = set()
        for it in intents:
            known.add(it["id"])
            found = by_intent.get(it["id"], [])
            if len(found) > 1:
                report["duplicate_payouts_for_intent"].append({"intent_id": it["id"], "payouts": found})
            if found and found[0].get("amount") != it["amount"]:
                report["amount_mismatch"].append({"intent_id": it["id"], "ours": it["amount"], "payswift": found[0].get("amount")})
            if it["status"] == "paid" and not found:
                report["paid_but_missing_in_payswift"].append(dict(it))
            elif found and it["status"] != "paid":
                report["in_payswift_but_not_marked_paid"].append(dict(it))
            elif found:
                report["matched"] += 1
        report["payswift_refs_without_intent"] = [p for k, ps in by_intent.items() if k not in known for p in ps]
        report["ok"] = not any(report[k] for k in ("paid_but_missing_in_payswift", "amount_mismatch",
                                                   "duplicate_payouts_for_intent", "payswift_refs_without_intent"))
        return report
