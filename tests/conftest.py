"""Fixtures. Integration tests need Postgres (TEST_DATABASE_URL, default local); PaySwift is faked
in-process with scriptable failures, so money tests are deterministic. The real sandbox is
exercised by the evals."""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime

import psycopg
import pytest

from app.agent.orchestrator import Agent, Inbound
from app.config import Settings
from app.db import Database
from app.domain.loader import load_exports
from app.payouts import PayoutService
from app.payswift import PayResult, PostRateLimiter

ADMIN_URL = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
TEST_DB = "qd_test"


class FakePaySwift:
    """Behaves like the sandbox: idempotency keys stored on success, body fingerprint checked.
    `script` is a list of outcomes consumed by successive POSTs:
      'ok' | '503' | 'lost' (processed, but caller sees a timeout) | '429' | 'inprogress'"""

    def __init__(self) -> None:
        self.ledger: list[dict] = []
        self.keys: dict[str, tuple] = {}
        self.script: list[str] = []
        self.posts = 0
        self.ledger_down = False
        self.limiter = PostRateLimiter(1000)
        self._lock = threading.Lock()

    def create_payout(self, *, rider_id, amount, reference, idempotency_key, timeout):
        with self._lock:
            self.posts += 1
            mode = self.script.pop(0) if self.script else "ok"
            fp = (rider_id, amount, reference)
            if idempotency_key in self.keys:
                stored_fp, payout = self.keys[idempotency_key]
                if stored_fp != fp:
                    return PayResult("fatal", status_code=409, error="idempotency_key_reused")
                return PayResult("paid", payout=payout, status_code=200)
            if mode == "503":
                return PayResult("retry", status_code=503, error="service_unavailable", retry_after=0.01)
            if mode == "429":
                return PayResult("blocked", status_code=429, error="account_blocked", retry_after=0.01)
            if mode == "inprogress":
                return PayResult("retry", status_code=409, error="request_in_progress", retry_after=0.01)
            payout = {"payout_id": f"pout_{uuid.uuid4().hex[:8]}", "rider_id": rider_id, "amount": amount,
                      "reference": reference, "status": "processed"}
            self.ledger.append(payout)
            self.keys[idempotency_key] = (fp, payout)
            if mode == "lost":
                return PayResult("retry", error="timeout_outcome_unknown", retry_after=0.01)
            return PayResult("paid", payout=payout, status_code=201)

    def list_payouts(self, rider_id=None, timeout=5.0):
        if self.ledger_down:
            raise RuntimeError("ledger down")
        return [p for p in self.ledger if rider_id in (None, p["rider_id"])]

    def healthy(self):
        return True

    def close(self):
        pass

    def paid(self, rider_id: str) -> int:
        return sum(p["amount"] for p in self.ledger if p["rider_id"] == rider_id)


def _db_url() -> str:
    base = ADMIN_URL.rsplit("/", 1)[0]
    return f"{base}/{TEST_DB}"


@pytest.fixture(scope="session")
def database():
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as c:
            c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError as exc:
        if os.getenv("CI"):  # in CI a missing database is a failure, never a silent skip
            raise
        pytest.skip(f"Postgres not available: {exc}")
    db = Database(_db_url(), max_size=30)
    db.open()
    db.migrate()
    load_exports(db, os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
    yield db
    db.close()


@pytest.fixture
def settings(database):
    return Settings(database_url=database.url, worker_enabled=False, reply_budget_seconds=7.0)


@pytest.fixture
def system(database, settings):
    with database.tx() as conn:
        conn.execute("TRUNCATE messages, conversations, trace_steps, settled_items, payout_intents, ops_items")
    ps = FakePaySwift()
    payouts = PayoutService(database, settings, ps)
    agent = Agent(database, settings, ps, payouts)

    class System:
        pass
    s = System()
    s.db, s.ps, s.payouts, s.agent, s.settings = database, ps, payouts, agent, settings

    def say(rider, text, at="2026-09-22T10:00:00+05:30", mid=None):
        return agent.handle(Inbound(mid or f"wamid.{uuid.uuid4().hex[:10]}", rider, text, datetime.fromisoformat(at)))["reply"]

    def drain(seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with database.tx() as conn:
                n = conn.execute("UPDATE payout_intents SET next_attempt_at = now() WHERE status IN ('pending','retrying')"
                                 " RETURNING id").fetchall()
            if not n:
                return
            for r in n:
                payouts.attempt(r["id"], 1.0)

    def pending(type_=None):
        with database.tx() as conn:
            rows = conn.execute("SELECT * FROM ops_items WHERE status='pending' ORDER BY created_at").fetchall()
        return [r for r in rows if type_ in (None, r["type"])]

    s.say, s.drain, s.pending = say, drain, pending
    return s
