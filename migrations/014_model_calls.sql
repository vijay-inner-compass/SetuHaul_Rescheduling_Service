-- Governance record of model use: one row per driver message that called the model, and one per call. Numbers and codes only:
-- no message text, prompt, driver id or key is stored.
CREATE TABLE model_turns (
    turn_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    -- Seconds since 1970, for time windows and ordering.
    ts_epoch REAL NOT NULL,
    -- How long the whole message took to handle.
    duration_ms REAL NOT NULL,
    -- The reply's status, or ERROR when handling failed part-way.
    status TEXT NOT NULL,
    reason_code TEXT
);
CREATE TABLE model_calls (
    call_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES model_turns(turn_id),
    ts TEXT NOT NULL,
    ts_epoch REAL NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    -- How long this one attempt took.
    duration_ms REAL NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('OK','TIMEOUT','TRANSIENT','REFUSED','NOT_CONFIGURED','ERROR')),
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    -- NULL when no price is set (never a made-up zero) or the call failed.
    cost_usd REAL
);
CREATE INDEX ix_model_turns_when ON model_turns(ts_epoch);
CREATE INDEX ix_model_calls_when ON model_calls(ts_epoch);
CREATE INDEX ix_model_calls_turn ON model_calls(turn_id);
