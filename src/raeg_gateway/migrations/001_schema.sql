-- RAEG gateway store (DESIGN §5.2). Applied by the migration role (postgres).
-- The application connects as raeg_app, which may only INSERT/SELECT the append-only tables.

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'raeg_app') THEN
        CREATE ROLE raeg_app LOGIN PASSWORD 'raeg_app';
    END IF;
END $$;

CREATE TYPE exec_state AS ENUM (
    'PREPARED', 'DISPATCHING', 'UNKNOWN', 'VERIFYING', 'RETRY_WAIT',
    'SUCCEEDED', 'DECLINED', 'NOT_EXECUTED', 'ESCALATED'
);

-- Mutable projection. Every change to it is described exactly by one execution_events.patch.
CREATE TABLE executions (
    action_key             TEXT PRIMARY KEY,
    fingerprint            TEXT NOT NULL,
    payload                JSONB NOT NULL,
    profile_id             TEXT NOT NULL,
    capability             JSONB NOT NULL,
    policy                 JSONB NOT NULL,
    state                  exec_state NOT NULL,
    version                INT NOT NULL CHECK (version >= 1),
    attempt_count          INT NOT NULL DEFAULT 0,
    verify_attempts        INT NOT NULL DEFAULT 0,
    segment_no             INT NOT NULL DEFAULT 0,
    anchor                 TIMESTAMPTZ,
    max_deadline           TIMESTAMPTZ,
    absence_proven_through TIMESTAMPTZ,
    next_action_at         TIMESTAMPTZ,
    lease_owner            TEXT,
    lease_expires_at       TIMESTAMPTZ,
    retry_basis            TEXT,
    provider_txn_id        TEXT,
    outcome_source         TEXT,
    ambiguity_cause        TEXT,
    escalation_reason      TEXT,
    duplicate_detected     BOOLEAN NOT NULL DEFAULT FALSE,
    pending_late_success   BIGINT,
    created_at             TIMESTAMPTZ NOT NULL,
    updated_at             TIMESTAMPTZ NOT NULL,
    CHECK ((state IN ('DISPATCHING', 'VERIFYING')) = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX executions_due ON executions (state, next_action_at);
CREATE INDEX executions_lease ON executions (state, lease_expires_at) WHERE lease_owner IS NOT NULL;

-- Every submission, including duplicates and fingerprint conflicts (T1, T1a, T1b).
CREATE TABLE submissions (
    id           BIGSERIAL PRIMARY KEY,
    action_key   TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    payload      JSONB NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('created', 'duplicate', 'fingerprint_conflict')),
    at_db        TIMESTAMPTZ NOT NULL
);

CREATE TABLE attempts (
    action_key   TEXT NOT NULL REFERENCES executions,
    attempt_no   INT NOT NULL,
    deadline     TIMESTAMPTZ NOT NULL,
    segment_no   INT NOT NULL,
    basis        TEXT NOT NULL,
    worker_id    TEXT NOT NULL,
    claimed_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (action_key, attempt_no)
);

CREATE TABLE evidence (
    id               BIGSERIAL PRIMARY KEY,
    action_key       TEXT NOT NULL REFERENCES executions,
    attempt_no       INT,
    kind             TEXT NOT NULL CHECK (kind IN ('dispatch_response', 'transport_error', 'lookup_response',
                                                   'late_response', 'duplicate_audit', 'operator_note', 'hook')),
    sent_at          TIMESTAMPTZ,
    received_at_db   TIMESTAMPTZ NOT NULL,
    provider_as_of   TIMESTAMPTZ,
    http_status      INT,
    request          JSONB,
    response_headers JSONB,
    raw_body         BYTEA,
    classification   TEXT,
    worker_id        TEXT NOT NULL,
    fenced           BOOLEAN NOT NULL DEFAULT FALSE,
    sha256           TEXT NOT NULL
);
CREATE INDEX evidence_action ON evidence (action_key, id);

-- Every controller/store decision, applied or not. Applied decisions map 1:1 to events (I6).
CREATE TABLE decisions (
    id             BIGSERIAL PRIMARY KEY,
    action_key     TEXT NOT NULL,
    at_db          TIMESTAMPTZ NOT NULL,
    rule_id        TEXT NOT NULL,
    transition     TEXT,
    policy_version TEXT NOT NULL,
    code_sha       TEXT NOT NULL,
    inputs         JSONB NOT NULL,
    evidence_ids   BIGINT[] NOT NULL DEFAULT '{}',
    output         JSONB NOT NULL,
    applied        BOOLEAN NOT NULL,
    worker_id      TEXT NOT NULL
);
CREATE INDEX decisions_action ON decisions (action_key, id);

-- Hash-chained per action: hash = SHA-256(prev_hash || JCS(event body)). seq = executions.version.
CREATE TABLE execution_events (
    id           BIGSERIAL PRIMARY KEY,
    action_key   TEXT NOT NULL REFERENCES executions,
    seq          INT NOT NULL,
    from_state   exec_state,
    to_state     exec_state NOT NULL,
    transition   TEXT NOT NULL,
    decision_id  BIGINT NOT NULL UNIQUE REFERENCES decisions,
    evidence_ids BIGINT[] NOT NULL DEFAULT '{}',
    patch        JSONB NOT NULL,
    worker_id    TEXT NOT NULL,
    at_db        TIMESTAMPTZ NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    UNIQUE (action_key, seq)
);

CREATE TABLE anomalies (
    id           BIGSERIAL PRIMARY KEY,
    action_key   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    evidence_ids BIGINT[] NOT NULL DEFAULT '{}',
    detail       JSONB,
    at_db        TIMESTAMPTZ NOT NULL
);

-- Layer 1 of append-only enforcement: triggers (also bind superusers).
CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = 'RA002';
END $$;

CREATE TRIGGER submissions_append_only      BEFORE UPDATE OR DELETE ON submissions      FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER attempts_append_only         BEFORE UPDATE OR DELETE ON attempts         FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER evidence_append_only         BEFORE UPDATE OR DELETE ON evidence         FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER decisions_append_only        BEFORE UPDATE OR DELETE ON decisions        FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER execution_events_append_only BEFORE UPDATE OR DELETE ON execution_events FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER anomalies_append_only        BEFORE UPDATE OR DELETE ON anomalies        FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- executions rows are never deleted.
CREATE TRIGGER executions_no_delete BEFORE DELETE ON executions FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- Layer 2: privileges. raeg_app cannot UPDATE/DELETE/TRUNCATE evidence-bearing tables.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT SELECT, INSERT ON submissions, attempts, evidence, decisions, execution_events, anomalies TO raeg_app;
GRANT SELECT, INSERT ON executions TO raeg_app;
-- Identity columns (action_key, fingerprint, payload, profile_id, capability, policy, created_at) are immutable.
GRANT UPDATE (state, version, attempt_count, verify_attempts, segment_no, anchor, max_deadline,
              absence_proven_through, next_action_at, lease_owner, lease_expires_at, retry_basis,
              provider_txn_id, outcome_source, ambiguity_cause, escalation_reason, duplicate_detected,
              pending_late_success, updated_at) ON executions TO raeg_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO raeg_app;
