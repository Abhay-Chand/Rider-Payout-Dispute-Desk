"""Runtime configuration, read once from the environment.

Every variable is documented in `.env.example`. Money limits live here so they are
visible and auditable, but the *rules* that use them live in `app/domain/guardrails.py`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/qd"))
    payswift_base_url: str = field(default_factory=lambda: os.getenv("PAYSWIFT_BASE_URL", "http://localhost:8081").rstrip("/"))
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")))

    # Finance rules (from the brief). Changing these is a finance decision, not an engineering one.
    auto_pay_limit: int = field(default_factory=lambda: _int("AUTO_PAY_LIMIT_INR", 200))
    auto_pay_enabled: bool = field(default_factory=lambda: _bool("AUTO_PAY_ENABLED", True))
    dispute_window_days: int = field(default_factory=lambda: _int("DISPUTE_WINDOW_DAYS", 7))
    # "live": the agent pays small disputes itself. "shadow": it decides, but every payout waits for ops,
    # so ops can measure how often they agree with the agent before letting it move money.
    auto_pay_mode: str = field(default_factory=lambda: (os.getenv("AUTO_PAY_MODE", "live").strip().lower() or "live"))
    # Ceiling on all automatic payouts on one business day, across every rider (0 = no ceiling).
    auto_pay_daily_budget: int = field(default_factory=lambda: _int("AUTO_PAY_DAILY_BUDGET_INR", 20000))
    # Messages one rider may send per hour before the agent stops acting on them (0 = no limit).
    rider_max_messages_per_hour: int = field(default_factory=lambda: _int("RIDER_MAX_MESSAGES_PER_HOUR", 30))

    # Latency budget. The messaging vendor retries after ~10s, so we must reply well before that.
    reply_budget_seconds: float = field(default_factory=lambda: _float("REPLY_BUDGET_SECONDS", 7.0))
    payswift_inline_timeout: float = field(default_factory=lambda: _float("PAYSWIFT_INLINE_TIMEOUT_SECONDS", 3.0))
    payswift_worker_timeout: float = field(default_factory=lambda: _float("PAYSWIFT_WORKER_TIMEOUT_SECONDS", 12.0))
    # PaySwift blocks the account for 60s above 30 POSTs / 10s. Stay well under it.
    payswift_max_posts_per_10s: int = field(default_factory=lambda: _int("PAYSWIFT_MAX_POSTS_PER_10S", 20))
    payout_max_attempts: int = field(default_factory=lambda: _int("PAYOUT_MAX_ATTEMPTS", 25))
    max_concurrent_turns: int = field(default_factory=lambda: _int("MAX_CONCURRENT_TURNS", 12))
    worker_enabled: bool = field(default_factory=lambda: _bool("WORKER_ENABLED", True))
    worker_poll_seconds: float = field(default_factory=lambda: _float("WORKER_POLL_SECONDS", 0.5))

    # LLM. Empty provider or missing key => deterministic rules planner (see README).
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "").strip().lower())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "").strip())
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", "").strip())
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "").strip())
    llm_timeout_seconds: float = field(default_factory=lambda: _float("LLM_TIMEOUT_SECONDS", 4.0))
    llm_max_steps: int = field(default_factory=lambda: _int("LLM_MAX_STEPS", 4))

    # Security.
    ops_token: str = field(default_factory=lambda: os.getenv("OPS_TOKEN", "").strip())
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", "").strip())

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        if self.auto_pay_mode not in ("live", "shadow"):
            raise ValueError(f"AUTO_PAY_MODE must be 'live' or 'shadow', got {self.auto_pay_mode!r}")

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider in {"anthropic", "openai"} and bool(self.llm_api_key)


def get_settings() -> Settings:
    return Settings()
