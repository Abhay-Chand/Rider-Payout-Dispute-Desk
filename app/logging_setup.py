"""Structured (JSON-lines) logging to stdout."""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "psycopg.pool", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
