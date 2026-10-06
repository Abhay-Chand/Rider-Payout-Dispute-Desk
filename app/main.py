"""HTTP surface: the vendor webhook, trace, ops queue/actions and the ops page."""
from __future__ import annotations

import hmac
import logging
import pathlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app import insights, logging_setup
from app.agent.orchestrator import Agent, Busy, Inbound
from app.config import Settings, get_settings
from app.db import Database
from app.domain.loader import load_exports
from app.domain.policy import IST, normalize_rider_id
from app.payouts import PayoutService
from app.payswift import PaySwiftClient, PostRateLimiter
from app.trace import read_trace

log = logging.getLogger("app")
STATIC = pathlib.Path(__file__).parent / "static"


class MessageIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)
    rider_id: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=0, max_length=4000)
    received_at: datetime | None = None

    @field_validator("rider_id")
    @classmethod
    def _rider(cls, v: str) -> str:
        rid = normalize_rider_id(v)
        if not rid:
            raise ValueError("invalid rider_id")
        return rid


class Decision(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    actor: str = Field(default="ops", max_length=80)


class Services:
    def __init__(self, settings: Settings, payswift: PaySwiftClient | None = None) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.payswift = payswift or PaySwiftClient(settings.payswift_base_url, PostRateLimiter(settings.payswift_max_posts_per_10s))
        self.payouts = PayoutService(self.db, settings, self.payswift)
        self.agent: Agent | None = None
        self.ready = False

    def start(self) -> None:
        self.db.open()
        self.db.migrate()
        report = load_exports(self.db, self.settings.data_dir)
        log.info("data load", extra={"report": report})
        self.agent = Agent(self.db, self.settings, self.payswift, self.payouts)
        if self.settings.worker_enabled:
            self.payouts.start()
        self.ready = True
        log.info("service ready", extra={"planner": self.agent.planner_name})

    def stop(self) -> None:
        self.payouts.stop()
        self.payswift.close()
        self.db.close()


def create_app(settings: Settings | None = None, payswift: PaySwiftClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging_setup.setup(settings.log_level)
    services = Services(settings, payswift)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await run_in_threadpool(services.start)
        yield
        await run_in_threadpool(services.stop)

    app = FastAPI(title="QuickDrop payout dispute desk", lifespan=lifespan)
    app.state.services = services

    @app.exception_handler(psycopg.OperationalError)
    async def db_down(_: Request, exc: psycopg.OperationalError):
        log.error("database unavailable", extra={"error": str(exc)})
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503, headers={"Retry-After": "5"})

    def require_ops(authorization: str | None = Header(default=None)) -> str:
        token = settings.ops_token
        if not token:
            raise HTTPException(403, "Ops sign-in is switched off on this server. Set OPS_TOKEN in .env and restart.")
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not given:
            raise HTTPException(401, "Sign in with the ops token first.")
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "That token doesn't match OPS_TOKEN on the server.")
        return "ops"

    def check_webhook(x_webhook_secret: str | None = Header(default=None)) -> None:
        if settings.webhook_secret and not hmac.compare_digest(x_webhook_secret or "", settings.webhook_secret):
            raise HTTPException(401, "invalid webhook secret")

    # ---- health ------------------------------------------------------------------------------
    @app.get("/health")
    def health():
        if not services.ready:
            raise HTTPException(503, "starting")
        with services.db.tx() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "planner": services.agent.planner_name, "payswift": services.payswift.healthy()}

    # ---- vendor webhook ----------------------------------------------------------------------
    @app.post("/messages", dependencies=[Depends(check_webhook)])
    def post_message(body: MessageIn):
        if not services.ready:
            raise HTTPException(503, "starting")
        received = body.received_at or datetime.now(UTC)
        if received.tzinfo is None:
            received = received.replace(tzinfo=IST)
        text = body.text.strip() or "(empty message)"
        try:
            result = services.agent.handle(Inbound(body.message_id, body.rider_id, text, received))
        except Busy:
            return JSONResponse({"error": "busy", "reply": None}, status_code=503, headers={"Retry-After": "2"})
        return result

    # ---- trace & ops -------------------------------------------------------------------------
    @app.get("/trace/{rider_id}")
    def trace(rider_id: str):
        rid = normalize_rider_id(rider_id)
        if not rid:
            raise HTTPException(404, "unknown rider id")
        return read_trace(services.db, rid)

    def _item(r: dict) -> dict:
        return {"id": r["id"], "rider_id": r["rider_id"], "type": r["type"], "amount": r["amount"], "reason": r["reason"],
                "created_at": r["created_at"].isoformat(), "category": r["category"], "status": r["status"],
                "rider_name": r.get("rider_name"),
                "dispute_date": r["dispute_date"].isoformat() if r["dispute_date"] else None,
                "items": r["items"], "details": r["details"], "decided_by": r["decided_by"],
                "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None, "decision_note": r["decision_note"]}

    # Contract endpoint (SUBMISSION.md): read-only, unauthenticated.
    @app.get("/ops/pending")
    def ops_pending():
        with services.db.tx() as conn:
            rows = conn.execute("SELECT * FROM ops_items WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [_item(r) for r in rows]

    # Everything under /ops/api and /ops/items is the ops page's API: rider conversations are personal
    # data, so reads need the ops token too.
    ops = [Depends(require_ops)]

    @app.get("/ops/api/session", dependencies=ops)
    def ops_session():
        return {"ok": True}

    @app.get("/ops/api/items", dependencies=ops)
    def ops_items(status: str | None = None):
        sql = ("SELECT o.*, r.name AS rider_name FROM ops_items o LEFT JOIN riders r ON r.rider_id = o.rider_id"
               + (" WHERE o.status = %s" if status else "") + " ORDER BY o.created_at DESC LIMIT 500")
        with services.db.tx() as conn:
            rows = conn.execute(sql, (status,) if status else ()).fetchall()
        return [_item(r) for r in rows]

    @app.post("/ops/items/{item_id}/{action}", dependencies=ops)
    def ops_decide(item_id: str, action: str, body: Decision | None = None):
        if action not in ("approve", "reject", "resolve"):
            raise HTTPException(404, "Unknown action.")
        body = body or Decision()
        if action == "reject" and not (body.note or "").strip():
            raise HTTPException(400, "Add a reason when rejecting, so the case history explains it.")
        try:
            return services.payouts.decide(item_id, action, body.actor, body.note)
        except LookupError:
            raise HTTPException(404, "This item no longer exists.") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.get("/ops/api/summary", dependencies=ops)
    def ops_summary():
        """One call for the page header: workload, money, and whether the agent is healthy."""
        with services.db.tx() as conn:
            pend = conn.execute(
                "SELECT type, category, count(*) AS n, coalesce(sum(amount),0) AS amount, min(created_at) AS oldest"
                " FROM ops_items WHERE status='pending' GROUP BY type, category").fetchall()
            money = conn.execute(
                "SELECT status, count(*) AS n, coalesce(sum(amount),0) AS amount FROM payout_intents GROUP BY status").fetchall()
            recent = conn.execute(
                "SELECT message_id, planner FROM messages WHERE status='done' ORDER BY completed_at DESC LIMIT 50").fetchall()
            ids = [r["message_id"] for r in recent]
            llm = conn.execute(
                "SELECT count(DISTINCT message_id) AS n, (array_agg(output->>'error' ORDER BY id DESC))[1] AS last"
                " FROM trace_steps WHERE name = 'llm_failed' AND message_id = ANY(%s)", (ids,)).fetchone()
            rejected = conn.execute(
                "SELECT count(DISTINCT message_id) AS n FROM trace_steps WHERE name = 'llm_reply_rejected' AND message_id = ANY(%s)",
                (ids,)).fetchone()["n"]
            msgs = conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
            shadow = conn.execute(
                "SELECT count(*) FILTER (WHERE status='approved') AS agreed, count(*) FILTER (WHERE status='rejected') AS disagreed,"
                " count(*) FILTER (WHERE status='pending') AS pending FROM ops_items WHERE category = 'shadow_auto_pay'").fetchone()
            spend = conn.execute(
                "SELECT business_date AS day, sum(amount) AS spent FROM ("
                " SELECT business_date, amount FROM payout_intents WHERE kind = 'auto'"
                " UNION ALL SELECT business_date, amount FROM ops_items WHERE category = 'shadow_auto_pay' AND status <> 'rejected') x"
                " GROUP BY business_date ORDER BY business_date DESC LIMIT 1").fetchone()
        by_status = {r["status"]: {"count": r["n"], "amount": r["amount"]} for r in money}

        def total(*keys):
            return {"count": sum(by_status.get(k, {}).get("count", 0) for k in keys),
                    "amount": sum(by_status.get(k, {}).get("amount", 0) for k in keys)}
        approvals = [r for r in pend if r["type"] == "approval"]
        escalations = [r for r in pend if r["type"] == "escalation"]
        planners: dict[str, int] = {}
        for r in recent:
            planners[r["planner"] or "unknown"] = planners.get(r["planner"] or "unknown", 0) + 1
        return {
            "pending_approvals": {"count": sum(r["n"] for r in approvals), "amount": sum(r["amount"] for r in approvals),
                                  "oldest": min((r["oldest"] for r in approvals), default=None)},
            "open_escalations": {"count": sum(r["n"] for r in escalations),
                                 "by_category": {r["category"]: r["n"] for r in escalations}},
            "payouts": {"paid": total("paid"), "in_flight": total("pending", "retrying"), "needs_attention": total("failed", "stuck")},
            "messages": msgs,
            "auto_pay": {
                "mode": settings.auto_pay_mode, "enabled": settings.auto_pay_enabled, "limit_per_dispute": settings.auto_pay_limit,
                "daily_budget": settings.auto_pay_daily_budget,
                "latest_day": spend["day"].isoformat() if spend else None, "spent_latest_day": spend["spent"] if spend else 0,
                "shadow": {"agreed": shadow["agreed"], "disagreed": shadow["disagreed"], "pending": shadow["pending"]},
            },
            "agent": {"configured": services.agent.planner_name, "recent_messages": len(recent), "planners": planners,
                      "llm_failures": llm["n"], "last_llm_error": llm["last"], "llm_replies_rejected": rejected},
        }

    @app.get("/ops/api/insights", dependencies=ops)
    def ops_insights():
        return insights.compute(services.db)

    @app.get("/ops/api/riders", dependencies=ops)
    def ops_riders():
        with services.db.tx() as conn:
            rows = conn.execute(
                """SELECT r.rider_id, r.name, r.city, m.last_at, m.messages,
                          (SELECT count(*) FROM ops_items o WHERE o.rider_id = r.rider_id AND o.status='pending') AS pending,
                          (SELECT coalesce(sum(amount),0) FROM payout_intents p WHERE p.rider_id = r.rider_id AND p.status='paid') AS paid,
                          (SELECT count(*) FROM payout_intents p WHERE p.rider_id = r.rider_id AND p.status IN ('pending','retrying')) AS in_flight
                   FROM riders r JOIN (SELECT rider_id, max(created_at) AS last_at, count(*) AS messages FROM messages GROUP BY rider_id) m
                   ON m.rider_id = r.rider_id ORDER BY m.last_at DESC""").fetchall()
        return [dict(r) | {"last_at": r["last_at"].isoformat()} for r in rows]

    @app.get("/ops/api/riders/{rider_id}", dependencies=ops)
    def ops_rider(rider_id: str):
        rid = normalize_rider_id(rider_id) or rider_id
        with services.db.tx() as conn:
            rider = conn.execute("SELECT * FROM riders WHERE rider_id = %s", (rid,)).fetchone()
            msgs = conn.execute("SELECT message_id, text, reply, received_at, status, planner, deliveries FROM messages"
                                " WHERE rider_id = %s ORDER BY created_at", (rid,)).fetchall()
            intents = conn.execute("SELECT id, amount, kind, status, dispute_date, payswift_payout_id, attempts, last_error, created_at"
                                   " FROM payout_intents WHERE rider_id = %s ORDER BY created_at", (rid,)).fetchall()
            items = conn.execute("SELECT o.*, r.name AS rider_name FROM ops_items o LEFT JOIN riders r ON r.rider_id = o.rider_id"
                                 " WHERE o.rider_id = %s ORDER BY o.created_at", (rid,)).fetchall()
        if not rider:
            raise HTTPException(404, "Unknown rider.")
        return {"rider": rider, "messages": msgs, "payouts": intents, "ops_items": [_item(i) for i in items],
                "trace": read_trace(services.db, rid)}

    @app.get("/ops/api/reconciliation", dependencies=ops)
    def ops_reconciliation():
        try:
            return services.payouts.reconcile()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"PaySwift unavailable: {exc}") from None

    @app.get("/ops/api/data-quality", dependencies=ops)
    def data_quality():
        with services.db.tx() as conn:
            row = conn.execute("SELECT report, loaded_at FROM data_load_report ORDER BY id DESC LIMIT 1").fetchone()
        return row or {}

    # Fonts ship with the app: no third-party requests from the ops page, and it works on closed networks.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/ops")
    def ops_page():
        # The page itself holds no data; it signs in with the ops token and then calls /ops/api/*.
        return FileResponse(STATIC / "ops.html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                       "font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    @app.get("/")
    def root():
        return {"service": "quickdrop-payout-desk", "ops_page": "/ops", "health": "/health"}

    return app


app = create_app()
