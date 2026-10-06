"""Append-only audit trail. Each step is committed on its own so errors never erase history."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from app.db import Database

log = logging.getLogger(__name__)
STEP_TYPES = {"message_in", "tool_call", "decision", "reply", "error"}


def _default(o: Any):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


# Credentials must never reach the audit trail, not even the masked fragments that provider errors echo back.
_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|rk|pk)-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-*]{6,}"),  # OpenAI / Anthropic keys
    re.compile(r"\bgsk_[A-Za-z0-9]{10,}"),                                       # Groq
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),                                    # Google
]
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}")


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return _BEARER.sub(r"\1[redacted]", text)


def to_json(value: Any) -> Any:
    """Round-trip through JSON so everything stored is plain JSON, with credentials redacted."""
    return json.loads(redact(json.dumps(value, default=_default, ensure_ascii=False)))


class Tracer:
    def __init__(self, db: Database, rider_id: str, message_id: str | None) -> None:
        self.db, self.rider_id, self.message_id = db, rider_id, message_id

    def step(self, type_: str, name: str, input_: Any = None, output: Any = None) -> None:
        assert type_ in STEP_TYPES, type_
        payload_in = None if input_ is None else Jsonb(to_json(input_))
        payload_out = None if output is None else Jsonb(to_json(output))
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO trace_steps (rider_id, message_id, type, name, input, output) VALUES (%s,%s,%s,%s,%s,%s)",
                    (self.rider_id, self.message_id, type_, name, payload_in, payload_out),
                )
        except Exception:  # noqa: BLE001 - tracing must never break the rider's reply
            log.exception("failed to write trace step", extra={"rider_id": self.rider_id, "step": name})
        log.info("trace", extra={"rider_id": self.rider_id, "message_id": self.message_id, "step_type": type_, "step": name})


def read_trace(db: Database, rider_id: str) -> list[dict]:
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT at, type, name, input, output, message_id FROM trace_steps WHERE rider_id = %s ORDER BY id",
            (rider_id,),
        ).fetchall()
    # Redact on read as well, so entries written before redaction existed never show key fragments.
    return [{"at": r["at"].isoformat(), "type": r["type"], "name": r["name"], "input": to_json(r["input"]),
             "output": to_json(r["output"]), "message_id": r["message_id"]} for r in rows]
