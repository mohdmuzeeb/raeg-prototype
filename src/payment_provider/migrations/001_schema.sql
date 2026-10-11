-- Synthetic payment provider schema (DESIGN §5.3).
-- Ground truth for the evaluation lives here and is never readable by systems under test.

CREATE TABLE provider_settings (
    id               INT PRIMARY KEY CHECK (id = 1),
    clock_offset_ms  INT     NOT NULL DEFAULT 0,      -- V4
    enforce_deadline BOOLEAN NOT NULL DEFAULT FALSE   -- D ∧ ¬V2
);
INSERT INTO provider_settings (id) VALUES (1);

-- The provider clock. Every provider-side time (arrival, created_at, commit check, as_of) uses it.
CREATE FUNCTION provider_now() RETURNS timestamptz LANGUAGE sql VOLATILE AS $$
    SELECT clock_timestamp()
         + make_interval(secs => (SELECT clock_offset_ms FROM provider_settings WHERE id = 1) / 1000.0)
$$;

-- Every request received, inserted at arrival (after any injected ingress hold).
CREATE TABLE receipts (
    receipt_id       BIGSERIAL PRIMARY KEY,
    arrived_at       TIMESTAMPTZ NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('dispatch', 'lookup_ref', 'lookup_id')),
    ingress_index    INT,           -- nth arrival at ingress for this client_reference and kind
    method           TEXT NOT NULL,
    path             TEXT NOT NULL,
    query            TEXT,
    headers          JSONB NOT NULL,
    body             JSONB,
    body_sha256      TEXT,
    client_reference TEXT,
    idempotency_key  TEXT,
    deadline         TIMESTAMPTZ,
    attempt_hdr      TEXT,
    system_hdr       TEXT,
    held_ms          INT NOT NULL DEFAULT 0
);
CREATE INDEX receipts_ref ON receipts (client_reference);

-- Final disposition of each receipt (append-only; one row per finished receipt).
CREATE TABLE receipt_outcomes (
    receipt_id   BIGINT PRIMARY KEY REFERENCES receipts,
    finished_at  TIMESTAMPTZ NOT NULL,
    disposition  TEXT NOT NULL,
    http_status  INT,
    payment_id   BIGINT,
    detail       JSONB
);

-- The ledger. E(a_i) = count(status = 'succeeded' AND client_reference = k_i).
CREATE TABLE payments (
    payment_id       BIGSERIAL PRIMARY KEY,
    provider_txn_id  TEXT UNIQUE NOT NULL,
    client_reference TEXT NOT NULL,
    idempotency_key  TEXT,
    status           TEXT NOT NULL CHECK (status IN ('succeeded', 'declined')),
    decline_code     TEXT,
    amount_minor     BIGINT NOT NULL,
    currency         TEXT NOT NULL,
    payer_id         TEXT NOT NULL,
    payee_id         TEXT NOT NULL,
    memo             TEXT NOT NULL,
    body_sha256      TEXT NOT NULL,
    deadline         TIMESTAMPTZ,
    committed_at     TIMESTAMPTZ NOT NULL,
    receipt_id       BIGINT NOT NULL REFERENCES receipts
);
CREATE INDEX payments_ref ON payments (client_reference);

-- D: enforced at COMMIT time by a deferred constraint trigger (DESIGN §2.7).
CREATE FUNCTION check_commit_deadline() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.deadline IS NOT NULL
       AND (SELECT enforce_deadline FROM provider_settings WHERE id = 1)
       AND provider_now() > NEW.deadline THEN
        RAISE EXCEPTION 'DEADLINE_EXCEEDED at commit for %', NEW.provider_txn_id USING ERRCODE = 'RA001';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER payments_commit_deadline
    AFTER INSERT ON payments DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_commit_deadline();

-- Idempotency records (mutable provider state; history is in key_events).
CREATE TABLE idempotency_keys (
    key              TEXT PRIMARY KEY,
    body_sha256      TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
    created_at       TIMESTAMPTZ NOT NULL,
    completed_at     TIMESTAMPTZ,
    response_status  INT,
    response_body    JSONB,
    owner_receipt_id BIGINT NOT NULL
);

CREATE TABLE key_events (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT NOT NULL,
    event      TEXT NOT NULL,   -- reserved | completed | released | expired | replayed | in_progress | mismatch | released_on_recovery
    receipt_id BIGINT,
    at         TIMESTAMPTZ NOT NULL,
    detail     JSONB
);

CREATE TABLE accounts (
    payer_id      TEXT PRIMARY KEY,
    balance_minor BIGINT NOT NULL CHECK (balance_minor >= 0)
);

CREATE TABLE fault_log (
    id               BIGSERIAL PRIMARY KEY,
    receipt_id       BIGINT,
    client_reference TEXT,
    kind             TEXT NOT NULL,
    ingress_index    INT,
    fault            TEXT NOT NULL,
    params           JSONB,
    applied_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE config_log (
    id     BIGSERIAL PRIMARY KEY,
    at     TIMESTAMPTZ NOT NULL,
    config JSONB NOT NULL
);

-- Evidence tables are append-only (DESIGN §5.2/§7). TRUNCATE (test reset only) is statement-level
-- and is additionally gated by PROVIDER_ALLOW_RESET in the application.
CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = 'RA002';
END $$;

CREATE TRIGGER receipts_append_only         BEFORE UPDATE OR DELETE ON receipts         FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER receipt_outcomes_append_only BEFORE UPDATE OR DELETE ON receipt_outcomes FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER payments_append_only         BEFORE UPDATE OR DELETE ON payments         FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER key_events_append_only       BEFORE UPDATE OR DELETE ON key_events       FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER fault_log_append_only        BEFORE UPDATE OR DELETE ON fault_log        FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER config_log_append_only       BEFORE UPDATE OR DELETE ON config_log       FOR EACH ROW EXECUTE FUNCTION reject_mutation();
