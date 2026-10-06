-- Reference data (loaded from the ops exports, normalised on load).
CREATE TABLE IF NOT EXISTS riders (
    rider_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    city       TEXT,
    joined_on  DATE
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id          TEXT PRIMARY KEY,
    rider_id         TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL,
    ist_date         DATE NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('completed','cancelled_by_customer','cancelled_by_rider')),
    distance_km      NUMERIC(8,3) NOT NULL,
    distance_raw     TEXT NOT NULL,
    surge_multiplier NUMERIC(4,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS trips_rider_day ON trips (rider_id, ist_date);

CREATE TABLE IF NOT EXISTS payout_lines (
    line_id     TEXT PRIMARY KEY,
    payout_date DATE NOT NULL,
    rider_id    TEXT NOT NULL,
    line_type   TEXT NOT NULL CHECK (line_type IN ('trip','daily_incentive','cancellation_penalty')),
    trip_id     TEXT,
    amount      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lines_rider_day ON payout_lines (rider_id, payout_date);
CREATE INDEX IF NOT EXISTS lines_trip ON payout_lines (trip_id);

CREATE TABLE IF NOT EXISTS data_load_report (
    id         SERIAL PRIMARY KEY,
    loaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    report     JSONB NOT NULL
);

-- Inbound messages. message_id (the vendor's wamid) is the idempotency key for the webhook.
CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    rider_id     TEXT NOT NULL,
    text         TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing','done','failed')),
    reply        TEXT,
    planner      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    deliveries   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS messages_rider ON messages (rider_id, created_at);

-- One row per rider: the conversation memory the planner works from.
CREATE TABLE IF NOT EXISTS conversations (
    rider_id    TEXT PRIMARY KEY,
    state       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only audit trail. Served by GET /trace/{rider_id}.
CREATE TABLE IF NOT EXISTS trace_steps (
    id          BIGSERIAL PRIMARY KEY,
    rider_id    TEXT NOT NULL,
    message_id  TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    type        TEXT NOT NULL CHECK (type IN ('message_in','tool_call','decision','reply','error')),
    name        TEXT NOT NULL,
    input       JSONB,
    output      JSONB
);
CREATE INDEX IF NOT EXISTS trace_rider ON trace_steps (rider_id, id);

-- Everything waiting for (or decided by) a human.
CREATE TABLE IF NOT EXISTS ops_items (
    id            TEXT PRIMARY KEY,
    rider_id      TEXT NOT NULL,
    type          TEXT NOT NULL CHECK (type IN ('approval','escalation')),
    category      TEXT NOT NULL,
    amount        INTEGER,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','resolved')),
    business_date DATE,
    dispute_date  DATE,
    items         JSONB NOT NULL DEFAULT '[]'::jsonb,
    details       JSONB NOT NULL DEFAULT '{}'::jsonb,
    message_id    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ,
    decided_by    TEXT,
    decision_note TEXT,
    CHECK (type <> 'approval' OR (amount IS NOT NULL AND amount > 0))
);
CREATE INDEX IF NOT EXISTS ops_items_pending ON ops_items (status, created_at);
-- At most one open escalation per rider and category (no spam for ops).
CREATE UNIQUE INDEX IF NOT EXISTS ops_items_one_open_escalation
    ON ops_items (rider_id, category) WHERE type = 'escalation' AND status = 'pending';

-- Money we have decided to send. id doubles as the PaySwift Idempotency-Key.
CREATE TABLE IF NOT EXISTS payout_intents (
    id                TEXT PRIMARY KEY,
    rider_id          TEXT NOT NULL,
    amount            INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 10000),
    kind              TEXT NOT NULL CHECK (kind IN ('auto','approved')),
    business_date     DATE NOT NULL,
    dispute_date      DATE NOT NULL,
    items             JSONB NOT NULL,
    reference         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','retrying','paid','failed','stuck')),
    payswift_payout_id TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ops_item_id       TEXT REFERENCES ops_items(id),
    message_id        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payout_intents_due ON payout_intents (status, next_attempt_at);
-- Finance: auto-pay at most once per rider per day. Enforced by the database, not just by code.
CREATE UNIQUE INDEX IF NOT EXISTS payout_intents_one_auto_per_day
    ON payout_intents (rider_id, business_date) WHERE kind = 'auto';

-- Each discrepancy (a trip fare, a penalty, a day's incentive) can be settled once.
-- This is what makes "never pay more than owed" hold across messages, retries and ops actions.
CREATE TABLE IF NOT EXISTS settled_items (
    item_key     TEXT PRIMARY KEY,
    rider_id     TEXT NOT NULL,
    dispute_date DATE NOT NULL,
    amount       INTEGER NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('paying','awaiting_approval','rejected')),
    intent_id    TEXT REFERENCES payout_intents(id),
    ops_item_id  TEXT REFERENCES ops_items(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS settled_items_rider ON settled_items (rider_id, dispute_date);
