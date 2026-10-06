"""Answers "is the agent doing the job?" for ops, from data the system already records.

A *dispute* is one rider asking about one IST day. A rider who re-asks about the same day (pushback,
status) is still one dispute; its outcome is the first decision the engine made for it.
"""
from __future__ import annotations

from app.db import Database

OUTCOMES = ["auto_paid", "approval_pending", "nothing_owed", "already_handled", "out_of_window", "no_data"]


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def compute(db: Database) -> dict:
    with db.tx() as conn:
        # First engine decision per (rider, day).
        conn.execute("""
            CREATE TEMP TABLE disputes ON COMMIT DROP AS
            SELECT DISTINCT ON (rider_id, input->>'date')
                   rider_id, input->>'date' AS day, input->>'issue' AS claimed,
                   output->>'status' AS status, coalesce((output->>'amount')::int, 0) AS amount, output->'items' AS items, at
            FROM trace_steps WHERE type = 'tool_call' AND name = 'resolve_dispute'
            ORDER BY rider_id, input->>'date', id""")

        outcomes = {r["status"]: {"count": r["n"], "amount": r["amount"]} for r in conn.execute(
            "SELECT status, count(*) AS n, coalesce(sum(amount), 0) AS amount FROM disputes GROUP BY status").fetchall()}
        total_disputes = sum(v["count"] for v in outcomes.values())

        found = conn.execute("""
            SELECT it->>'issue' AS issue, count(*) AS n, coalesce(sum((it->>'difference')::int), 0) AS amount
            FROM disputes, jsonb_array_elements(coalesce(items, '[]'::jsonb)) it
            WHERE status IN ('auto_paid', 'approval_pending')
            GROUP BY 1 ORDER BY amount DESC""").fetchall()

        claims = conn.execute("""
            SELECT claimed, count(*) AS n,
                   count(*) FILTER (WHERE status IN ('auto_paid', 'approval_pending')) AS owed,
                   count(*) FILTER (WHERE status = 'nothing_owed') AS not_owed
            FROM disputes GROUP BY claimed ORDER BY n DESC""").fetchall()

        riders = conn.execute("""
            SELECT count(DISTINCT m.rider_id) AS riders,
                   count(DISTINCT m.rider_id) FILTER (WHERE o.rider_id IS NOT NULL) AS needed_human
            FROM messages m LEFT JOIN (SELECT DISTINCT rider_id FROM ops_items) o ON o.rider_id = m.rider_id""").fetchone()

        money = {f"{r['kind']}:{r['status']}": {"count": r["n"], "amount": r["amount"]} for r in conn.execute(
            "SELECT kind, status, count(*) AS n, coalesce(sum(amount), 0) AS amount FROM payout_intents GROUP BY kind, status").fetchall()}
        approvals = {r["status"]: {"count": r["n"], "amount": r["amount"]} for r in conn.execute(
            "SELECT status, count(*) AS n, coalesce(sum(amount), 0) AS amount FROM ops_items WHERE type = 'approval' GROUP BY status").fetchall()}

        escalations = conn.execute("""
            SELECT category, count(*) AS n, count(*) FILTER (WHERE status = 'pending') AS open
            FROM ops_items WHERE type = 'escalation' GROUP BY category ORDER BY n DESC""").fetchall()

        speed = conn.execute("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at)) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at)) AS p95,
                   count(*) AS n
            FROM messages WHERE status = 'done' AND completed_at IS NOT NULL""").fetchone()
        decide = conn.execute("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM decided_at - created_at)) AS p50, count(*) AS n
            FROM ops_items WHERE type = 'approval' AND decided_at IS NOT NULL""").fetchone()
        oldest = conn.execute(
            "SELECT min(created_at) AS t FROM ops_items WHERE status = 'pending'").fetchone()["t"]

        planners = {r["planner"] or "unknown": r["n"] for r in conn.execute(
            "SELECT planner, count(*) AS n FROM messages WHERE status = 'done' GROUP BY planner").fetchall()}
        agent_events = {r["name"]: r["n"] for r in conn.execute(
            "SELECT name, count(*) AS n FROM trace_steps WHERE name IN ('llm_failed', 'llm_reply_rejected', 'duplicate_delivery',"
            " 'rider_rate_limited') GROUP BY name").fetchall()}
        flagged = conn.execute(
            "SELECT count(*) AS n FROM trace_steps WHERE name = 'message_understood' AND output->>'security_flag' IS NOT NULL"
        ).fetchone()["n"]

        by_day = conn.execute("""
            SELECT d.day, d.messages, coalesce(p.paid, 0) AS paid, coalesce(a.queued, 0) AS queued
            FROM (SELECT (received_at AT TIME ZONE 'Asia/Kolkata')::date AS day, count(*) AS messages
                  FROM messages GROUP BY 1) d
            LEFT JOIN (SELECT business_date AS day, sum(amount) AS paid FROM payout_intents WHERE status = 'paid' GROUP BY 1) p
                   ON p.day = d.day
            LEFT JOIN (SELECT business_date AS day, sum(amount) AS queued FROM ops_items WHERE type = 'approval' GROUP BY 1) a
                   ON a.day = d.day
            ORDER BY d.day""").fetchall()

    paid_auto = money.get("auto:paid", {"count": 0, "amount": 0})
    paid_approved = money.get("approved:paid", {"count": 0, "amount": 0})
    sending = {"count": sum(money.get(f"{k}:{s}", {}).get("count", 0) for k in ("auto", "approved") for s in ("pending", "retrying")),
               "amount": sum(money.get(f"{k}:{s}", {}).get("amount", 0) for k in ("auto", "approved") for s in ("pending", "retrying"))}
    stuck = {"count": sum(money.get(f"{k}:{s}", {}).get("count", 0) for k in ("auto", "approved") for s in ("failed", "stuck")),
             "amount": sum(money.get(f"{k}:{s}", {}).get("amount", 0) for k in ("auto", "approved") for s in ("failed", "stuck"))}
    waiting = approvals.get("pending", {"count": 0, "amount": 0})
    rejected = approvals.get("rejected", {"count": 0, "amount": 0})
    rider_n, human_n = riders["riders"], riders["needed_human"]
    resolved_by_agent = sum(outcomes.get(k, {}).get("count", 0) for k in ("auto_paid", "nothing_owed", "already_handled"))

    return {
        "riders": {"total": rider_n, "needed_human": human_n, "agent_only": rider_n - human_n,
                   "agent_only_pct": _pct(rider_n - human_n, rider_n)},
        "disputes": {
            "total": total_disputes,
            "settled_by_agent": resolved_by_agent, "settled_by_agent_pct": _pct(resolved_by_agent, total_disputes),
            "rider_right_pct": _pct(sum(outcomes.get(k, {}).get("count", 0) for k in ("auto_paid", "approval_pending")), total_disputes),
            "outcomes": [{"status": k, **outcomes.get(k, {"count": 0, "amount": 0})} for k in OUTCOMES],
        },
        "money": {"paid_automatically": paid_auto, "paid_after_approval": paid_approved, "sending": sending,
                  "waiting_for_approval": waiting, "rejected_by_ops": rejected, "failed_or_stuck": stuck},
        "underpayments_found": [dict(r) for r in found],
        "claims": [dict(r) | {"right_pct": _pct(r["owed"], r["n"])} for r in claims],
        "escalations": [dict(r) for r in escalations],
        "speed": {"reply_p50_s": _round(speed["p50"]), "reply_p95_s": _round(speed["p95"]), "messages": speed["n"],
                  "approval_decision_p50_s": _round(decide["p50"]), "approvals_decided": decide["n"],
                  "oldest_pending_at": oldest.isoformat() if oldest else None},
        "agent": {"planners": planners, "llm_failures": agent_events.get("llm_failed", 0),
                  "llm_replies_blocked": agent_events.get("llm_reply_rejected", 0),
                  "duplicate_deliveries": agent_events.get("duplicate_delivery", 0),
                  "riders_paused_for_flooding": agent_events.get("rider_rate_limited", 0), "security_flags": flagged},
        "by_day": [{"day": r["day"].isoformat(), "messages": r["messages"], "paid": r["paid"], "queued": r["queued"]} for r in by_day],
    }


def _round(v) -> float | None:
    return round(float(v), 2) if v is not None else None
