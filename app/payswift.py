"""PaySwift client.

Behaviour we designed against (sandbox "behaves like production"):
- 503 / connection errors: not processed, safe to retry.
- 504 or our own timeout: the payout MAY have been processed. Retry with the SAME
  Idempotency-Key and the SAME body; PaySwift then returns the stored payout.
- 409 request_in_progress: an earlier attempt with this key is still running; retry later.
- 409 idempotency_key_reused: a bug on our side (body changed). Never retry; page ops.
- 429 account_blocked: global account block (~60s). Stop ALL posting until it lifts.
- 400: invalid request. Never retry; page ops.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass
class PayResult:
    outcome: str              # paid | retry | blocked | fatal
    payout: dict | None = None
    status_code: int | None = None
    error: str | None = None
    retry_after: float = 0.0


class PostRateLimiter:
    """Sliding-window limiter shared by every payout POST in this process."""

    def __init__(self, max_posts: int, window: float = 10.0) -> None:
        self.max_posts, self.window = max_posts, window
        self._times: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()
        self.blocked_until = 0.0

    def try_acquire(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now < self.blocked_until:
                return False
            while self._times and self._times[0] <= now - self.window:
                self._times.popleft()
            if len(self._times) >= self.max_posts:
                return False
            self._times.append(now)
            return True

    def block(self, seconds: float) -> None:
        with self._lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)

    def seconds_until_free(self) -> float:
        now = time.monotonic()
        with self._lock:
            wait = max(0.0, self.blocked_until - now)
            if len(self._times) >= self.max_posts:
                wait = max(wait, self._times[0] + self.window - now)
            return wait


class PaySwiftClient:
    def __init__(self, base_url: str, limiter: PostRateLimiter) -> None:
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter
        self._http = httpx.Client(base_url=self.base_url, timeout=10.0)

    def close(self) -> None:
        self._http.close()

    def create_payout(self, *, rider_id: str, amount: int, reference: str, idempotency_key: str,
                      timeout: float) -> PayResult:
        if not self.limiter.try_acquire():
            return PayResult("retry", error="local_rate_limit", retry_after=max(1.0, self.limiter.seconds_until_free()))
        try:
            resp = self._http.post(
                "/v1/payouts",
                json={"rider_id": rider_id, "amount": amount, "reference": reference},
                headers={"Idempotency-Key": idempotency_key},
                timeout=timeout,
            )
        except httpx.TimeoutException:
            return PayResult("retry", error="timeout_outcome_unknown", retry_after=2.0)
        except httpx.HTTPError as exc:
            return PayResult("retry", error=f"connection_error: {type(exc).__name__}", retry_after=2.0)

        code = resp.status_code
        body = _json(resp)
        err = (body or {}).get("error") if isinstance(body, dict) else None
        if code in (200, 201) and isinstance(body, dict) and body.get("payout_id"):
            return PayResult("paid", payout=body, status_code=code)
        if code == 429:
            self.limiter.block(60.0)
            return PayResult("blocked", status_code=code, error=err or "rate_limited", retry_after=61.0)
        if code == 409 and err == "request_in_progress":
            return PayResult("retry", status_code=code, error=err, retry_after=3.0)
        if code in (400, 404, 409, 422):
            return PayResult("fatal", status_code=code, error=err or f"http_{code}")
        if code >= 500:
            return PayResult("retry", status_code=code, error=err or f"http_{code}", retry_after=2.0)
        return PayResult("fatal", status_code=code, error=err or f"unexpected_http_{code}")

    def list_payouts(self, rider_id: str | None = None, timeout: float = 5.0) -> list[dict]:
        params = {"rider_id": rider_id} if rider_id else None
        resp = self._http.get("/v1/payouts", params=params, timeout=timeout)
        resp.raise_for_status()
        return list(resp.json().get("data", []))

    def healthy(self) -> bool:
        try:
            return self._http.get("/health", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False


def _json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return None
